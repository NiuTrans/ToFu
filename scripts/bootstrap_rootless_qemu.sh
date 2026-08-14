#!/usr/bin/env bash
# Build a minimal seccomp/libslirp-enabled QEMU toolchain without root privileges.
# Everything, including package caches and build logs, stays below --prefix.

set -euo pipefail

QEMU_VERSION=11.1.0
QEMU_SHA256=6ee1d1a61f68212476b27108c26da5f449dc09b626d42f8279ba0dc2e08fa858
LIBSECCOMP_VERSION=2.6.0
LIBSECCOMP_SHA256=83b6085232d1588c379dc9b9cae47bb37407cf262e6e74993c61ba72d2a784dc
GPERF_VERSION=3.3
GPERF_SHA256=fd87e0aba7e43ae054837afd6cd4db03a3f2693deb3619085e6ed9d8d9604ad8
MICROMAMBA_VERSION=2.3.3
MICROMAMBA_SHA256=e7274528ceb9c20d048a428d6c22d7e02e268f8ffb762c4c365422347c8b8ba2
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(cd -- "$script_dir/.." && pwd -P)
conda_lock="$script_dir/rootless_qemu-conda-linux-64.lock"
bridge_source="$script_dir/rootless_egress_bridge.c"
qemu_config_id="qemu-$QEMU_VERSION-x86_64-tcg-seccomp-slirp-no-download-v2"
previous_runtime_id="rootless-qemu-$QEMU_VERSION-x86_64-exact-lock-network-v2"
runtime_id="rootless-qemu-$QEMU_VERSION-x86_64-exact-lock-network-chroot-v3"

usage() {
  echo "Usage: $0 --prefix /absolute/private/path [--jobs N] [--source-cache PATH]"
  echo ""
  echo "Builds x86_64 QEMU TCG, qemu-img, libseccomp, and user networking below PREFIX."
  echo "PREFIX must be absent/empty or contain this script's bootstrap marker."
}

fail() {
  echo "bootstrap_rootless_qemu: $*" >&2
  exit 2
}

prefix=""
source_cache=""
jobs="${ROOTLESS_VM_BUILD_JOBS:-}"
while (($#)); do
  case "$1" in
    --prefix)
      (($# >= 2)) || fail "--prefix needs a value"
      prefix=$2
      shift 2
      ;;
    --jobs)
      (($# >= 2)) || fail "--jobs needs a value"
      jobs=$2
      shift 2
      ;;
    --source-cache)
      (($# >= 2)) || fail "--source-cache needs a value"
      source_cache=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

[[ -n "$prefix" ]] || fail "--prefix is required"
[[ "$prefix" == /* ]] || fail "--prefix must be absolute"
[[ "$prefix" != / ]] || fail "refusing filesystem root as --prefix"
if [[ -n "$jobs" && ! "$jobs" =~ ^[1-9][0-9]*$ ]]; then
  fail "--jobs must be a positive integer"
fi
if [[ -z "$jobs" ]]; then
  jobs=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)
  ((jobs > 8)) && jobs=8
fi

for command in bash curl sha256sum tar chmod mkdir find cp mv awk uname dirname; do
  command -v "$command" >/dev/null || fail "required host command not found: $command"
done
[[ $(uname -s) == Linux ]] || fail "only Linux is supported"
[[ $(uname -m) == x86_64 ]] || fail "this bootstrap currently supports x86_64 only"
[[ -f "$conda_lock" ]] || fail "missing exact conda lock: $conda_lock"
[[ -f "$bridge_source" ]] || fail "missing native egress bridge: $bridge_source"

marker="$prefix/.rootless-qemu-bootstrap"
if [[ -e "$prefix" && ! -d "$prefix" ]]; then
  fail "prefix exists and is not a directory: $prefix"
fi
[[ ! -L "$prefix" ]] || fail "prefix must not be a symbolic link"
if [[ -d "$prefix" && ! -f "$marker" ]]; then
  first_entry=$(find "$prefix" -mindepth 1 -maxdepth 1 -print -quit)
  [[ -z "$first_entry" ]] || fail "refusing non-empty prefix without bootstrap marker"
fi
mkdir -p "$prefix"
chmod 700 "$prefix"
if [[ ! -f "$marker" ]]; then
  printf 'rootless-qemu-bootstrap-v1\n' >"$marker"
  chmod 600 "$marker"
elif [[ $(<"$marker") != rootless-qemu-bootstrap-v1 ]]; then
  fail "prefix has an unrecognized bootstrap marker"
fi

downloads="$prefix/downloads"
sources="$prefix/sources"
runtime="$prefix/runtime"
build_env="$prefix/build-env"
mamba_root="$prefix/mamba-root"
qemu_build="$prefix/build-qemu"
qemu_config_stamp="$qemu_build/.rootless-qemu-config"
runtime_marker="$runtime/.rootless-qemu-runtime"
mkdir -p "$downloads" "$sources" "$runtime" "$mamba_root"
chmod 700 "$downloads" "$sources" "$runtime" "$mamba_root"

verify_runtime() {
  PYTHONPATH="$repo_root" \
  ROOTLESS_VM_QEMU="$runtime/bin/qemu-system-x86_64" \
  ROOTLESS_VM_QEMU_IMG="$runtime/bin/qemu-img" \
    "$build_env/bin/python" -m rootless_vm doctor --json
}

compile_egress_bridge() {
  local compiler bridge bridge_partial
  compiler="$build_env/bin/x86_64-conda-linux-gnu-cc"
  bridge="$runtime/bin/rootless-egress-bridge"
  [[ -x "$compiler" ]] || return 1
  if [[ ! -x "$bridge" || "$bridge_source" -nt "$bridge" ]]; then
    mkdir -p "$runtime/bin"
    bridge_partial="$runtime/bin/.rootless-egress-bridge.$$"
    "$compiler" -std=c11 -O2 -Wall -Wextra -Werror \
      -D_FORTIFY_SOURCE=2 -fPIE -pie -Wl,-z,relro,-z,now \
      "$bridge_source" -o "$bridge_partial"
    chmod 555 "$bridge_partial"
    mv "$bridge_partial" "$bridge"
  fi
}

show_exports() {
  echo "Rootless QEMU installed and verified."
  echo "export ROOTLESS_VM_QEMU='$runtime/bin/qemu-system-x86_64'"
  echo "export ROOTLESS_VM_QEMU_IMG='$runtime/bin/qemu-img'"
  echo "export ROOTLESS_VM_EGRESS_BRIDGE='$runtime/bin/rootless-egress-bridge'"
}

runtime_files_ready() {
  [[ -x "$runtime/bin/qemu-system-x86_64" ]] &&
    [[ -x "$runtime/bin/qemu-img" ]] &&
    [[ -x "$runtime/bin/rootless-egress-bridge" ]] &&
    [[ -x "$build_env/bin/python" ]]
}

previous_runtime_files_ready() {
  [[ -x "$runtime/bin/qemu-system-x86_64" ]] &&
    [[ -x "$runtime/bin/qemu-img" ]] &&
    [[ -x "$build_env/bin/python" ]] &&
    [[ -x "$build_env/bin/x86_64-conda-linux-gnu-cc" ]]
}

# A completed prefix is immutable for a given runtime ID. Reruns retain the
# real QMP/seccomp probe but skip package relinking and compilation. A build
# that reached the trusted QEMU config stamp before an older bootstrap exited
# can be adopted only after the same probe succeeds.
if [[ -f "$runtime_marker" ]]; then
  current_runtime_id=$(<"$runtime_marker")
  if [[ "$current_runtime_id" == "$runtime_id" ]]; then
    runtime_files_ready || fail "marked runtime is incomplete: $runtime"
    verify_runtime
    show_exports
    exit 0
  fi
  [[ "$current_runtime_id" == "$previous_runtime_id" ]] || fail \
    "runtime marker differs; use a new prefix for this bootstrap version"
  previous_runtime_files_ready || fail "previous runtime is incomplete: $runtime"
  compile_egress_bridge || fail "failed to compile the native egress bridge"
  verify_runtime
  printf '%s\n' "$runtime_id" >"$runtime_marker"
  chmod 600 "$runtime_marker"
  show_exports
  exit 0
fi
if runtime_files_ready && [[ -f "$qemu_config_stamp" ]] &&
  [[ $(<"$qemu_config_stamp") == "$qemu_config_id" ]]; then
  verify_runtime
  printf '%s\n' "$runtime_id" >"$runtime_marker"
  chmod 600 "$runtime_marker"
  show_exports
  exit 0
fi

fetch() {
  local name=$1 url=$2 expected=$3 target cached actual partial
  target="$downloads/$name"
  if [[ ! -f "$target" && -n "$source_cache" ]]; then
    cached="$source_cache/$name"
    if [[ -f "$cached" ]]; then
      cp "$cached" "$target"
    fi
  fi
  if [[ ! -f "$target" ]]; then
    partial="$target.partial.$$"
    curl --fail --location --retry 3 --connect-timeout 20 --output "$partial" "$url"
    chmod 600 "$partial"
    mv "$partial" "$target"
  fi
  actual=$(sha256sum "$target" | awk '{print $1}')
  [[ "$actual" == "$expected" ]] || fail "SHA-256 mismatch for $target"
}

fetch "micromamba-$MICROMAMBA_VERSION.tar.bz2" \
  "https://micro.mamba.pm/api/micromamba/linux-64/$MICROMAMBA_VERSION" \
  "$MICROMAMBA_SHA256"
fetch "gperf-$GPERF_VERSION.tar.gz" \
  "https://ftp.gnu.org/gnu/gperf/gperf-$GPERF_VERSION.tar.gz" \
  "$GPERF_SHA256"
fetch "libseccomp-$LIBSECCOMP_VERSION.tar.gz" \
  "https://github.com/seccomp/libseccomp/releases/download/v$LIBSECCOMP_VERSION/libseccomp-$LIBSECCOMP_VERSION.tar.gz" \
  "$LIBSECCOMP_SHA256"
fetch "qemu-$QEMU_VERSION.tar.xz" \
  "https://download.qemu.org/qemu-$QEMU_VERSION.tar.xz" \
  "$QEMU_SHA256"

tools="$prefix/tools"
mkdir -p "$tools"
if [[ ! -x "$tools/bin/micromamba" ]]; then
  tar -xjf "$downloads/micromamba-$MICROMAMBA_VERSION.tar.bz2" \
    -C "$tools" bin/micromamba
fi
micromamba="$tools/bin/micromamba"

if [[ -x "$build_env/bin/python" ]]; then
  mamba_action=install
else
  mamba_action=create
fi
"$micromamba" --no-rc --no-env --root-prefix "$mamba_root" \
  "$mamba_action" --yes --prefix "$build_env" \
  --file "$conda_lock"

# Activation is intentionally scoped to this process. MAMBA_ROOT_PREFIX keeps
# package caches and metadata below the selected prefix instead of ~/.conda.
eval "$("$micromamba" --no-rc --no-env --root-prefix "$mamba_root" \
  shell hook --shell bash)"
micromamba activate "$build_env"
export PKG_CONFIG_PATH="$runtime/lib/pkgconfig:$build_env/lib/pkgconfig"
export CPPFLAGS="-I$runtime/include ${CPPFLAGS:-}"
export LDFLAGS="-L$runtime/lib ${LDFLAGS:-}"
export PIP_NO_INDEX=1

compile_egress_bridge || fail "failed to compile the native egress bridge"

if [[ ! -d "$sources/gperf-$GPERF_VERSION" ]]; then
  tar -xzf "$downloads/gperf-$GPERF_VERSION.tar.gz" -C "$sources"
fi
if [[ ! -x "$runtime/bin/gperf" ]]; then
  mkdir -p "$prefix/build-gperf"
  (
    cd "$prefix/build-gperf"
    "$sources/gperf-$GPERF_VERSION/configure" --prefix="$runtime"
    make -j"$jobs"
    make install
  )
fi
export PATH="$runtime/bin:$PATH"

if [[ ! -d "$sources/libseccomp-$LIBSECCOMP_VERSION" ]]; then
  tar -xzf "$downloads/libseccomp-$LIBSECCOMP_VERSION.tar.gz" -C "$sources"
fi
if [[ ! -f "$runtime/lib/libseccomp.so" ]]; then
  mkdir -p "$prefix/build-libseccomp"
  (
    cd "$prefix/build-libseccomp"
    "$sources/libseccomp-$LIBSECCOMP_VERSION/configure" \
      --prefix="$runtime" --disable-python --disable-static
    make -j"$jobs"
    make install
  )
fi

if [[ ! -d "$sources/qemu-$QEMU_VERSION" ]]; then
  tar -xJf "$downloads/qemu-$QEMU_VERSION.tar.xz" -C "$sources"
fi
mkdir -p "$qemu_build"
if [[ -f "$qemu_build/build.ninja" ]]; then
  [[ -f "$qemu_config_stamp" ]] || fail \
    "existing QEMU build has no trusted config stamp; move $qemu_build aside"
  [[ $(<"$qemu_config_stamp") == "$qemu_config_id" ]] || fail \
    "existing QEMU build configuration differs; move $qemu_build aside"
fi
if [[ ! -f "$qemu_build/build.ninja" ]]; then
  (
    cd "$qemu_build"
    "$sources/qemu-$QEMU_VERSION/configure" \
      --prefix="$runtime" \
      --target-list=x86_64-softmmu \
      --without-default-features \
      --enable-system \
      --enable-tools \
      --enable-tcg \
      --enable-seccomp \
      --enable-slirp \
      --enable-fdt=disabled \
      -Dwrap_mode=nodownload \
      --disable-werror
  )
  printf '%s\n' "$qemu_config_id" >"$qemu_config_stamp"
  chmod 600 "$qemu_config_stamp"
fi
ninja -C "$qemu_build" -j"$jobs"
ninja -C "$qemu_build" install

runtime_rpath="$runtime/lib:$build_env/lib"
for binary in "$runtime/bin/qemu-system-x86_64" "$runtime/bin/qemu-img"; do
  [[ -x "$binary" ]] || fail "QEMU install did not produce $binary"
  # DT_RPATH deliberately wins over a hostile/incompatible inherited
  # LD_LIBRARY_PATH. Both directories are children of the private prefix.
  patchelf --force-rpath --set-rpath "$runtime_rpath" "$binary"
done

verify_runtime
printf '%s\n' "$runtime_id" >"$runtime_marker"
chmod 600 "$runtime_marker"
show_exports
