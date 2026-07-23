#!/usr/bin/env python3
# Copyright (c) Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause-Clear

import argparse
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _find_aosp_root() -> Path:
    """Locate the AOSP root. Prefer full trees over reduced sbox copies."""
    # In Soong genrules we may execute from a sandboxed working directory.
    # Allow explicit override first for deterministic CI/repro use-cases.
    env_root = os.environ.get("AOSP_ROOT")
    if env_root:
        candidate = Path(env_root).resolve()
        if (candidate / "external/virtio/mesa3d").is_dir():
            return candidate

    cur = Path.cwd().resolve()
    # First pass: prefer directories that look like a full checkout
    # (contains build/envsetup.sh), not partial copied trees.
    for parent in [cur] + list(cur.parents):
        if not (parent / "external/virtio/mesa3d").is_dir():
            continue
        if (parent / "build/envsetup.sh").is_file():
            return parent

    # Second pass: fallback for environments where build/envsetup.sh is absent
    # but mesa source is still laid out under external/.
    for parent in [cur] + list(cur.parents):
        if (parent / "external/virtio/mesa3d").is_dir():
            return parent

    raise RuntimeError("Cannot determine AOSP root directory.")


def _pick_clang_bin(aosp_root: Path) -> Path:
    """Pick Clang bin directory from AOSP prebuilts."""
    # Keep an override for local experiments/pinned toolchains.
    env_clang = os.environ.get("AOSP_CLANG_BIN")
    if env_clang:
        clang_bin = Path(env_clang).resolve()
        if (clang_bin / "clang").is_file():
            return clang_bin

    # Otherwise use the latest clang-* directory shipped in this tree.
    candidates = sorted(
        glob.glob(str(aosp_root / "prebuilts/clang/host/linux-x86/clang-*/bin/clang")),
        key=lambda p: Path(p).as_posix(),
    )
    for c in reversed(candidates):
        clang = Path(c)
        if clang.is_file():
            return clang.resolve().parent

    raise RuntimeError(
        "Cannot find AOSP clang toolchain. Checked "
        "AOSP_CLANG_BIN and prebuilts/clang/host/linux-x86/clang-*/bin."
    )


def _pick_sysroot(aosp_root: Path) -> Path:
    """Use Soong-generated sysroot (not external NDK)."""
    # Intentional design: use sysroot produced/expected by this AOSP build,
    # so Mesa build is aligned with platform headers/libs from the same tree.
    env_sysroot = os.environ.get("AOSP_SYSROOT")
    if env_sysroot:
        sysroot = Path(env_sysroot).resolve()
        if (sysroot / "usr/include").is_dir():
            return sysroot

    out_dir = os.environ.get("OUT_DIR", "out")
    out_path = Path(out_dir)
    if not out_path.is_absolute():
        out_path = (aosp_root / out_path).resolve()

    candidates = [
        out_path / "soong/ndk/sysroot",
        aosp_root / "out/soong/ndk/sysroot",
    ]
    for c in candidates:
        if (c / "usr/include").is_dir():
            return c

    raise RuntimeError(
        "Cannot find Soong sysroot. Checked "
        "AOSP_SYSROOT and out/soong/ndk/sysroot."
    )


def _arch_info(arch: str):
    """Return (sdk, clang target triple, meson cpu_family, meson cpu)."""
    sdk = "31"
    if arch == "arm":
        return sdk, f"armv7a-linux-androideabi{sdk}", "arm", "armv7a"
    if arch == "arm64":
        return sdk, f"aarch64-linux-android{sdk}", "aarch64", "armv8"
    raise RuntimeError(f"Unsupported architecture: {arch}")


def _arch_key(arch: str) -> str:
    if arch == "arm64":
        return "_arm64_"
    if arch == "arm":
        return "_arm_"
    raise RuntimeError(f"Unsupported architecture: {arch}")


def _pick_bionic_stub_libdirs(aosp_root: Path, arch: str, sdk: str):
    """Collect Soong-produced stub shared libs needed by Meson link checks."""
    # Meson does compile+link feature tests during setup. In Soong context these
    # tests can fail if only runtime images are available but not link stubs.
    out_root = aosp_root / "out/soong/.intermediates/bionic"
    arch_key = _arch_key(arch)

    modules = ["libc", "libdl", "libm"]
    libdirs = []
    for module in modules:
        pattern = str(out_root / f"{module}/{module}.ndk/android*{arch_key}*sdk_shared_{sdk}")
        for d in sorted(glob.glob(pattern)):
            cand = Path(d)
            if (cand / f"{module}.so").is_file():
                libdirs.append(cand)
                break
        else:
            raise RuntimeError(
                f"Missing Soong stub dir for {module} (sdk {sdk}, arch {arch})"
            )
    return libdirs


def _pick_libcxx_libdir(aosp_root: Path, arch: str):
    """Locate the Soong libc++ shared prebuilt for target arch."""
    # We point Meson probes to Soong-produced libc++ so it links against the
    # exact C++ runtime variant used by this platform build.
    arch_key = _arch_key(arch)
    pattern = str(
        aosp_root
        / (
            "out/soong/.intermediates/prebuilts/clang/host/linux-x86/libc++/"
            f"android*{arch_key}*shared"
        )
    )
    for d in sorted(glob.glob(pattern)):
        cand = Path(d)
        if (cand / "libc++.so").is_file():
            return cand
    raise RuntimeError(f"Missing libc++ shared libdir for arch {arch}")


def _libcxx_include_flags(clang_bin: Path, arch: str):
    """Inject libc++ headers expected by clang++ for this cross target."""
    clang_root = clang_bin.parent
    if arch == "arm64":
        arch_dir = "aarch64"
    elif arch == "arm":
        arch_dir = "arm"
    else:
        raise RuntimeError(f"Unsupported architecture: {arch}")

    dirs = [
        clang_root / "android_libc++/platform" / arch_dir / "include/c++/v1",
        clang_root / "include/c++/v1",
    ]
    flags = []
    for d in dirs:
        if d.is_dir():
            flags += ["-isystem", str(d)]
    return flags


def _pick_crt_objects(aosp_root: Path, arch: str):
    """
    Return (crt_dirs, crtbegin_dynamic, crtend_android).

    Meson checks may miss Soong's default linker crt search directories in sbox.
    We provide -B directories and fallback object paths to keep link probes stable.
    This is about configure-time link tests, not final runtime packaging.
    """
    root = aosp_root / "out/soong/.intermediates/bionic/libc"
    arch_key = _arch_key(arch)

    crt_dirs = []
    for stem in ["crtbegin_dynamic", "crtend_android", "crtbegin_so", "crtend_so"]:
        pattern = str(root / f"{stem}/android*{arch_key}*")
        for d in sorted(glob.glob(pattern)):
            cand = Path(d)
            if (cand / f"{stem}.o").is_file():
                crt_dirs.append(cand)
                break

    crt_tag = "android_arm64" if arch == "arm64" else "android_arm"
    begin = sorted(glob.glob(str(root / f"crtbegin_dynamic/{crt_tag}*/crtbegin_dynamic.o")))
    end = sorted(glob.glob(str(root / f"crtend_android/{crt_tag}*/crtend_android.o")))
    crtbegin = Path(begin[0]) if begin else None
    crtend = Path(end[0]) if end else None

    return crt_dirs, crtbegin, crtend


def _builtins_name_for_arch(arch: str) -> str:
    if arch == "arm":
        return "libclang_rt.builtins-arm-android.a"
    if arch == "arm64":
        return "libclang_rt.builtins-aarch64-android.a"
    raise RuntimeError(f"Unsupported architecture: {arch}")


def _prepare_atomic_shim(gen_dir: Path, clang_bin: Path, arch: str) -> Path:
    """
    Create local libatomic.a shim for Meson's optional atomic lookup on Android.

    We reuse compiler-rt builtins archive, which already ships in AOSP Clang prebuilt.
    Some Mesa/Meson probes ask for -latomic on Android; providing a local shim
    avoids introducing another external dependency just for probe linking.
    """
    shim_dir = gen_dir / "shim-lib"
    shim_dir.mkdir(parents=True, exist_ok=True)

    clang_root = clang_bin.parent
    builtins_name = _builtins_name_for_arch(arch)
    candidates = sorted((clang_root / "lib/clang").glob(f"*/lib/linux/{builtins_name}"))
    builtins = candidates[-1] if candidates else None
    if builtins is None or not builtins.is_file():
        raise RuntimeError(
            "Missing clang builtins archive for "
            f"{arch} under {clang_root}/lib/clang/*/lib/linux"
        )

    atomic = shim_dir / "libatomic.a"
    shutil.copy2(builtins, atomic)
    return shim_dir


def _write_cross_file(
    path: Path,
    clang_bin: Path,
    sysroot: Path,
    target: str,
    cpu_family: str,
    cpu: str,
    extra_c_args=None,
    extra_cpp_args=None,
):
    """Emit Meson cross file for Android target."""
    # Start with pkg-config disabled; we replace it later with our shim.
    # This keeps cross-file generation simple and patching explicit.
    extra_c = "".join([f", '{a}'" for a in (extra_c_args or [])])
    extra_cpp = "".join([f", '{a}'" for a in (extra_cpp_args or [])])
    lines = [
        "[binaries]",
        f"ar = '{clang_bin}/llvm-ar'",
        f"c = ['{clang_bin}/clang', '--target={target}', '--sysroot={sysroot}'{extra_c}]",
        "cpp = ["
        f"'{clang_bin}/clang++', '--target={target}', '--sysroot={sysroot}'{extra_cpp}, "
        "'-fno-exceptions', '-fno-unwind-tables', '-fno-asynchronous-unwind-tables']",
        "c_ld = 'lld'",
        "cpp_ld = 'lld'",
        f"strip = '{clang_bin}/llvm-strip'",
        "pkg-config = '/bin/false'",
        "",
        "[built-in options]",
        "cpp_link_args = ['-static-libstdc++', '-Wl,-z,max-page-size=16384']",
        "",
        "[host_machine]",
        "system = 'android'",
        f"cpu_family = '{cpu_family}'",
        f"cpu = '{cpu}'",
        "endian = 'little'",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_native_file(path: Path, clang_bin: Path):
    """Emit Meson native file for host tools built during the same run."""
    # Native file is for any helper binaries Meson may build for the host.
    # Keep host toolchain deterministic by using AOSP-prebuilt clang as well.
    lines = [
        "[binaries]",
        f"c = '{clang_bin}/clang'",
        f"cpp = '{clang_bin}/clang++'",
        f"ar = '{clang_bin}/llvm-ar'",
        f"strip = '{clang_bin}/llvm-strip'",
        "pkg-config = '/bin/false'",
        "",
        "[host_machine]",
        "system = 'linux'",
        "cpu_family = 'x86_64'",
        "cpu = 'x86_64'",
        "endian = 'little'",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_pkg_config_shim(
    pkg_config: Path,
    aosp_root: Path,
    libdrm_dir: str,
    libz_dir: str,
    libzstd_dir: str,
):
    """
    Provide a tiny pkg-config shim for Meson.

    We only emulate what this build needs: --version/--modversion/--cflags/--libs
    for `libdrm`, `zlib`, and `libzstd`.
    This avoids depending on an external pkg-config binary/database while still
    satisfying Meson's dependency discovery in a hermetic Soong invocation.
    """
    # Keep this script intentionally minimal and deterministic.
    pkg_script = f"""#!/bin/bash
set -e
if [[ " $@ " == *" --version "* ]]; then
  echo 0.29.2
  exit 0
fi
pkg=""
for a in "$@"; do
  case "$a" in
    -* ) ;;
    libdrm|zlib|libzstd) pkg="$a" ;;
  esac
done
if [ -z "$pkg" ]; then
  exit 1
fi
if [[ " $@ " == *" --modversion "* ]]; then
  if [ "$pkg" = "libdrm" ]; then
    echo 3.0.0
  elif [ "$pkg" = "libzstd" ]; then
    echo 1.5.0
  else
    echo 2.0.0
  fi
  exit 0
fi
if [[ " $@ " == *" --cflags "* ]]; then
  if [ "$pkg" = "libdrm" ]; then
    echo "-I{aosp_root}/external/libdrm -I{aosp_root}/external/libdrm/include -I{aosp_root}/external/libdrm/include/drm"
  elif [ "$pkg" = "libzstd" ]; then
    echo "-I{aosp_root}/external/zstd/lib"
  else
    echo "-I{aosp_root}/external/zlib"
  fi
  exit 0
fi
if [[ " $@ " == *" --libs "* ]]; then
  if [ "$pkg" = "libdrm" ]; then
    echo "-L{libdrm_dir} -ldrm"
  elif [ "$pkg" = "libzstd" ]; then
    echo "-L{libzstd_dir} -lzstd"
  else
    echo "-L{libz_dir} -lz"
  fi
  exit 0
fi
exit 0
"""
    pkg_config.write_text(pkg_script, encoding="utf-8")
    pkg_config.chmod(0o755)


def _configure_env(aosp_root: Path, shim_lib_dir: Path, link_libdirs, crtbegin: Path, crtend: Path):
    """Build deterministic env for Meson setup/install in Soong sandbox."""
    # Meson spawns compiler/linker subprocesses that do not automatically inherit
    # Soong's full implicit path wiring, so we pass library/crt hints explicitly.
    env = os.environ.copy()
    env["PATH"] = (
        f"{aosp_root}/prebuilts/build-tools/path/linux-x86:"
        f"{aosp_root}/prebuilts/build-tools/linux-x86/bin:"
        f"{env.get('PATH', '')}"
    )
    # Meson+Mesa python helpers rely on these modules in AOSP tree.
    # Preserve the incoming PYTHONPATH from Soong/sbox; replacing it may hide
    # already-exported module search paths and break Mako discovery.
    py_entries = [
        f"{aosp_root}/external/python/mako",
        f"{aosp_root}/external/python/pyyaml/lib",
        f"{aosp_root}/external/python/markupsafe/src",
    ]
    # Meson probes import packaging.version first. AOSP prebuilts often vendor
    # `packaging` under pip/pkg_resources _vendor trees, so expose those.
    vendored = sorted(
        glob.glob(
            str(
                aosp_root
                / "prebuilts/clang/host/linux-x86/clang-*/python3/lib/python*/site-packages/pip/_vendor"
            )
        )
    ) + sorted(
        glob.glob(
            str(
                aosp_root
                / "prebuilts/clang/host/linux-x86/clang-*/python3/lib/python*/site-packages/pkg_resources/_vendor"
            )
        )
    )
    py_entries.extend(vendored)
    if env.get("PYTHONPATH"):
        py_entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = ":".join(py_entries)

    libpath_entries = [str(shim_lib_dir)] + [str(d) for d in link_libdirs]
    env["LIBRARY_PATH"] = ":".join(libpath_entries + [env.get("LIBRARY_PATH", "")]).rstrip(":")

    ldflags = " ".join([f"-L{d}" for d in libpath_entries])
    if crtbegin and crtend:
        ldflags += f" {crtbegin} {crtend}"
    env["LDFLAGS"] = f"{ldflags} {env.get('LDFLAGS', '')}".strip()

    return env


def _pick_host_python() -> str:
    """
    Pick a host Python interpreter compatible with Meson's dependency probes.

    Prefer the current interpreter (AOSP prebuilt when launched from Soong),
    with optional override for experiments.
    """
    override = os.environ.get("AOSP_PYTHON")
    if override:
        return override
    return sys.executable or "python3"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", required=True)
    parser.add_argument("--gen-dir", required=True)
    parser.add_argument("--libdrm", required=True)
    parser.add_argument("--libz", required=True)
    parser.add_argument("--libzstd", required=True)
    args = parser.parse_args()

    # 1) Resolve toolchain and platform metadata from current AOSP build tree.
    #    No kernel_platform NDK lookup here: this flow is AOSP-tree-centric.
    aosp_root = _find_aosp_root()
    clang_bin = _pick_clang_bin(aosp_root)
    sysroot = _pick_sysroot(aosp_root)
    sdk, target, cpu_family, cpu = _arch_info(args.arch)
    bionic_stub_libdirs = _pick_bionic_stub_libdirs(aosp_root, args.arch, sdk)
    bionic_stub_libdirs.append(_pick_libcxx_libdir(aosp_root, args.arch))
    libcxx_inc_flags = _libcxx_include_flags(clang_bin, args.arch)
    crt_dirs, crtbegin, crtend = _pick_crt_objects(aosp_root, args.arch)

    meson_py = aosp_root / "external/python/meson/meson.py"
    if not meson_py.is_file():
        raise RuntimeError(f"Missing Meson at {meson_py}")

    # 2) Prepare isolated gen/build output directories.
    #    We delete only our generated working dirs to avoid stale Meson cache.
    gen_dir = Path(args.gen_dir).resolve()
    build_dir = gen_dir / "build"
    dest_dir = gen_dir / "dest"
    meson_ini = build_dir / "meson.ini"
    native_ini = build_dir / "native.ini"
    pkg_config = gen_dir / "pkg-config"
    shutil.rmtree(build_dir, ignore_errors=True)
    shutil.rmtree(dest_dir, ignore_errors=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    dest_dir.mkdir(parents=True, exist_ok=True)

    libdrm_path = Path(args.libdrm).resolve()
    libz_path = Path(args.libz).resolve()
    libzstd_path = Path(args.libzstd).resolve()
    _write_pkg_config_shim(
        pkg_config,
        aosp_root,
        str(libdrm_path.parent),
        str(libz_path.parent),
        str(libzstd_path.parent),
    )

    # 3) Write Meson cross/native files and patch link args for Soong stubs.
    #    Patch-after-write keeps the base cross-file readable and the Soong-
    #    specific link quirks localized in one place.
    crt_b_flags = [f"-B{d}" for d in crt_dirs]
    if not crt_b_flags:
        if crtbegin:
            crt_b_flags.append(f"-B{crtbegin.parent}")
        if crtend:
            crt_b_flags.append(f"-B{crtend.parent}")

    cpp_extra = crt_b_flags + libcxx_inc_flags
    _write_cross_file(
        meson_ini,
        clang_bin,
        sysroot,
        target,
        cpu_family,
        cpu,
        extra_c_args=crt_b_flags,
        extra_cpp_args=cpp_extra,
    )
    _write_native_file(native_ini, clang_bin)

    shim_lib_dir = _prepare_atomic_shim(gen_dir, clang_bin, args.arch)
    txt = meson_ini.read_text(encoding="utf-8")
    txt = txt.replace("pkg-config = '/bin/false'", f"pkg-config = '{pkg_config}'")
    link_lib_flags = ", ".join([f"'-L{d}'" for d in bionic_stub_libdirs])
    txt = txt.replace(
        "cpp_link_args = ['-static-libstdc++', '-Wl,-z,max-page-size=16384']",
        f"c_link_args = ['-L{shim_lib_dir}', {link_lib_flags}]\n"
        f"cpp_link_args = ['-L{shim_lib_dir}', {link_lib_flags}, '-Wl,-z,max-page-size=16384']",
    )
    meson_ini.write_text(txt, encoding="utf-8")

    # 4) Run Meson setup + install and copy produced shared libs to genDir.
    #    Meson installs under dest/usr/local/lib by default; copy just the
    #    artifacts exported by Android.bp from that location.
    env = _configure_env(aosp_root, shim_lib_dir, bionic_stub_libdirs, crtbegin, crtend)
    host_python = _pick_host_python()
    subprocess.run([host_python, "--version"], check=True, env=env)
    mesa_src = aosp_root / "external/virtio/mesa3d"
    setup_cmd = [
        host_python,
        str(meson_py),
        "setup",
        str(build_dir),
        str(mesa_src),
        "--cross-file",
        str(meson_ini),
        "--native-file",
        str(native_ini),
        "-Dplatforms=android",
        f"-Dplatform-sdk-version={sdk}",
        "-Dandroid-stub=true",
        "-Dandroid-libbacktrace=disabled",
        "-Dgallium-drivers=virgl",
        "-Dvulkan-drivers=virtio",
        # Keep EGL/GLES enabled to provide Android GL stack entry points.
        "-Degl=enabled",
        "-Dgles1=enabled",
        "-Dgles2=enabled",
        "-Degl-lib-suffix=_mesa",
        "-Dgles-lib-suffix=_mesa",
        "-Dopengl=false",
        "-Dvideo-codecs=",
        "-Dzstd=enabled",
        "-Dvalgrind=disabled",
    ]
    subprocess.run(setup_cmd, check=True, env=env)
    subprocess.run(
        [host_python, str(meson_py), "install", "-C", str(build_dir), "--destdir", str(dest_dir)],
        check=True,
        env=env,
    )

    out_lib = dest_dir / "usr/local/lib"
    for so in [
        "libEGL_mesa.so",
        "libGLESv1_CM_mesa.so",
        "libGLESv2_mesa.so",
        "libgallium_dri.so",
        "libvulkan_virtio.so",
    ]:
        shutil.copy2(out_lib / so, gen_dir / so)

    return 0


if __name__ == "__main__":
    sys.exit(main())
