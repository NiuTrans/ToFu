#!/bin/sh
# Builds proot for Android arm64-v8a from the sources pinned in pins.env and
# packages it as APK native libraries (README.md has the run-time contract):
#   BUILD_DIR/jniLibs/arm64-v8a/libproot.so         proot, a PIE linked to bionic
#   BUILD_DIR/jniLibs/arm64-v8a/libproot-loader.so  its static loader
# talloc is linked statically, so libproot.so needs only Android system
# libraries. The two files are a matched pair: proot embeds an offset into
# this exact loader build.
#
# usage: android/native/build-proot.sh [BUILD_DIR]   (default: android/native/build)
# needs: ANDROID_HOME with ndk/$NDK_VERSION, curl, unzip, tar, make, python3,
#        GNU awk (proot's src/loader/loader-info.awk uses gawk extensions)
set -eu

here=$(cd "$(dirname "$0")" && pwd)
. "$here/pins.env"
die() { echo "build-proot.sh: $*" >&2; exit 1; }

build_dir=${1:-$here/build}
mkdir -p "$build_dir"
build_dir=$(cd "$build_dir" && pwd)
downloads=$build_dir/downloads
work=$build_dir/work
out=$build_dir/jniLibs/arm64-v8a

# The binaries target the app's minSdk; app/build.gradle.kts is its only source.
api=$(sed -n 's/^ *minSdk *= *\([0-9][0-9]*\).*/\1/p' "$here/../app/build.gradle.kts")
[ -n "$api" ] || die "no minSdk in android/app/build.gradle.kts"

[ -n "${ANDROID_HOME:-}" ] || die "set ANDROID_HOME to the Android SDK that holds ndk/$NDK_VERSION"
ndk=$ANDROID_HOME/ndk/$NDK_VERSION
grep -qx "Pkg.Revision = $NDK_VERSION" "$ndk/source.properties" 2>/dev/null ||
  die "NDK $NDK_VERSION is not installed at $ndk (sdkmanager \"ndk;$NDK_VERSION\")"
llvm=$ndk/toolchains/llvm/prebuilt/linux-x86_64/bin
cc=$llvm/aarch64-linux-android$api-clang
[ -x "$cc" ] || die "no compiler for API $api: $cc"

if command -v gawk >/dev/null 2>&1; then
  gawk=$(command -v gawk)
elif awk --version 2>/dev/null | grep -q '^GNU Awk'; then
  gawk=$(command -v awk)
else
  die "GNU awk is required: proot's src/loader/loader-info.awk uses gawk extensions"
fi

fetch() { # URL SHA256 NAME: verified download, reused while its digest matches
  file=$downloads/$3
  if [ -f "$file" ] && echo "$2  $file" | sha256sum -c --status; then
    return
  fi
  mkdir -p "$downloads"
  curl -fsSL -o "$file.part" "$1" || die "download failed: $1"
  echo "$2  $file.part" | sha256sum -c --status || die "SHA-256 mismatch: $1"
  mv "$file.part" "$file"
}
fetch "$PROOT_URL" "$PROOT_SHA256" "proot-$PROOT_VERSION.zip"
fetch "$TALLOC_URL" "$TALLOC_SHA256" "talloc-$TALLOC_VERSION.tar.gz"

rm -rf "$work" "$build_dir/jniLibs"
mkdir -p "$work/bin" "$work/talloc/include" "$work/talloc/lib" "$out"
unzip -q "$downloads/proot-$PROOT_VERSION.zip" -d "$work"
tar -xzf "$downloads/talloc-$TALLOC_VERSION.tar.gz" -C "$work"
proot_src=$work/proot-$PROOT_VERSION/src
talloc_src=$work/talloc-$TALLOC_VERSION

patch -s -p1 -d "$work/proot-$PROOT_VERSION" <"$here/patches/proot-ashmem_memfd-string-h.patch" ||
  die "patches/proot-ashmem_memfd-string-h.patch does not apply"

cmp -s "$work/proot-$PROOT_VERSION/COPYING" "$here/licenses/proot-COPYING" ||
  die "licenses/proot-COPYING differs from the COPYING of proot $PROOT_VERSION"
cmp -s "$talloc_src/LICENSE" "$here/licenses/talloc-LICENSE" ||
  die "licenses/talloc-LICENSE differs from the LICENSE of talloc $TALLOC_VERSION"

# proot's makefile runs plain `awk` and `readelf`: give it GNU awk and the
# NDK's readelf. Only the flags set below reach the compilers.
ln -s "$gawk" "$work/bin/awk"
ln -s "$llvm/llvm-readelf" "$work/bin/readelf"
PATH=$work/bin:$PATH
export PATH
unset CPPFLAGS CFLAGS LDFLAGS LOADER_CFLAGS LOADER_LDFLAGS MAKEFLAGS
page_size_flag=-Wl,-z,max-page-size=16384

# talloc: waf takes the answers to the probes it cannot run on the build host
# from talloc-cross-answers.txt (it appends UNKNOWN lines to that file, hence the
# copy). The archive holds exactly the objects waf links into libtalloc.so for
# this talloc version: talloc.c and its builtin lib/replace.
cp "$here/talloc-cross-answers.txt" "$work/talloc-cross-answers.txt"
(
  cd "$talloc_src"
  CC=$cc AR=$llvm/llvm-ar CFLAGS=-O2 PYTHONHASHSEED=1 python3 buildtools/bin/waf configure \
    --prefix=/usr --disable-python --disable-rpath \
    --cross-compile --cross-answers="$work/talloc-cross-answers.txt"
  PYTHONHASHSEED=1 python3 buildtools/bin/waf build --targets=talloc
) >"$work/talloc.log" 2>&1 || die "talloc build failed; see $work/talloc.log"
(
  cd "$talloc_src/bin/default"
  "$llvm/llvm-ar" rcsD "$work/talloc/lib/libtalloc.a" \
    talloc.c.6.o lib/replace/replace.c.2.o lib/replace/closefrom.c.2.o
) || die "talloc objects not where waf puts them for talloc $TALLOC_VERSION"
cp "$talloc_src/talloc.h" "$work/talloc/include/"

# proot with an unbundled loader: proot runs getenv("PROOT_LOADER") at each guest
# execve. No 32-bit loader: the guest is arm64 only.
CPPFLAGS="-I$work/talloc/include -DVERSION=\\\"$PROOT_VERSION\\\"" \
LDFLAGS="-L$work/talloc/lib $page_size_flag" \
LOADER_LDFLAGS=$page_size_flag \
  make -C "$proot_src" -j"$(nproc)" V=1 CC="$cc" GIT=false HAS_LOADER_32BIT= \
    PROOT_UNBUNDLE_LOADER="$PROOT_UNBUNDLE_LOADER" proot loader/loader \
  >"$work/proot.log" 2>&1 || die "proot build failed; see $work/proot.log"

# A failed probe still builds, but without the seccomp fast path (every syscall
# then stops proot) or process_vm_* (word-by-word tracee memory access).
for feature in HAVE_SECCOMP_FILTER HAVE_PROCESS_VM; do
  grep -qx "#define $feature" "$proot_src/build.h" ||
    die "proot's $feature probe failed; see $work/proot.log"
done

# proot carries the loader's pokedata_workaround offset (loader/loader-info.c,
# generated with gawk from readelf). Recompute it with llvm-nm so a pair that
# disagrees is never packaged.
loader_symbol() {
  "$llvm/llvm-nm" "$proot_src/loader/loader" | sed -n "s/^\([0-9a-f]*\) [A-Za-z] $1\$/\1/p"
}
start=$(loader_symbol _start)
stub=$(loader_symbol pokedata_workaround)
[ -n "$start" ] && [ -n "$stub" ] || die "loader symbols _start/pokedata_workaround missing"
grep -qx "const ssize_t offset_to_pokedata_workaround=$((0x$stub - 0x$start));" \
  "$proot_src/loader/loader-info.c" || die "loader/loader-info.c does not match loader/loader"

"$llvm/llvm-strip" --strip-all -o "$out/libproot.so" "$proot_src/proot"
"$llvm/llvm-strip" --strip-all -o "$out/libproot-loader.so" "$proot_src/loader/loader"
python3 "$here/check_proot.py" "$build_dir" || {
  rm -rf "$build_dir/jniLibs"
  die "static checks failed; outputs removed"
}
