# proot for the on-device kernel (`android/native/`)

This directory builds proot for Android arm64-v8a as two APK native libraries:

| File | What it is |
|---|---|
| `libproot.so` | the proot executable: a PIE run by `/system/bin/linker64`, needing only `libc.so` and `libdl.so`; talloc is linked in statically |
| `libproot-loader.so` | proot's loader: a static executable at the fixed address `0x5400000000` |

They are executables named `lib*.so` so that the package installer extracts them
into the app's `nativeLibraryDir`: an app targeting SDK 29 or later may
`execve()` files there, but not files it wrote itself. proot never hands a guest
file to `execve()`: every guest `execve()` becomes an exec of the loader, which
maps the guest ELF itself (design §3, "Evidence that settles W^X"). The two files are a matched
pair: `libproot.so` embeds an offset into this exact loader build, so never
combine files from different builds.

## Build

```sh
ANDROID_HOME=/path/to/android-sdk android/native/build-proot.sh [BUILD_DIR]
```

- `BUILD_DIR` defaults to `android/native/build/` (gitignored). The outputs are
  `BUILD_DIR/jniLibs/arm64-v8a/libproot.so` and `libproot-loader.so`;
  `BUILD_DIR/downloads/` keeps the verified source archives and
  `BUILD_DIR/work/` the build tree and logs (`talloc.log`, `proot.log`).
- Needs the NDK installed side by side as `$ANDROID_HOME/ndk/27.3.13750724`
  (`sdkmanager "ndk;27.3.13750724"`), plus `curl`, `unzip`, `tar`, `patch`,
  `make`, `python3` (talloc's waf) and GNU awk (proot's
  `src/loader/loader-info.awk` uses gawk extensions; mawk fails).
- The API level is the app's `minSdk`, read from `android/app/build.gradle.kts`
  (26 today), and is checked in the ELF note of `libproot.so`.
- Every build starts from a fresh `work/` tree and ends by running
  `check_proot.py`; a failing check fails the build and removes `jniLibs/`, so
  the directory only ever holds checked files.
- The app consumes the output by adding `BUILD_DIR/jniLibs` as a `jniLibs`
  source directory through a Gradle property (owned by the app-side lane), with
  `packaging.jniLibs.useLegacyPackaging = true` so the files are extracted to
  `nativeLibraryDir`. There is no 32-bit or x86 build.

What the build does, in order: verify both archives against their SHA-256 pins;
apply `patches/proot-ashmem_memfd-string-h.patch`; check that
`licenses/proot-COPYING` and `licenses/talloc-LICENSE` are byte-identical to the
files in the archives; cross-configure talloc with waf from
`talloc-cross-answers.txt` and archive the objects waf links into
`libtalloc.so` (`talloc.c` and its built-in `lib/replace`); build proot's `proot`
and `loader/loader` targets with `PROOT_UNBUNDLE_LOADER` set and the 32-bit
loader disabled, linking both with `-Wl,-z,max-page-size=16384`; require proot's
`HAVE_SECCOMP_FILTER` and `HAVE_PROCESS_VM` probes to have passed (without them
proot still builds, only much slower); cross-check the loader offset compiled
into proot against `llvm-nm` of the loader; strip both files.

`talloc-cross-answers.txt` answers the probes waf would run on the device.
Only `C99 vsnprintf` changes `talloc.c`; `--version-script` must be `FAIL`
because lld rejects talloc's version script, and the rest describe Linux/bionic
behaviour for `lib/replace` functions talloc does not use.

## Pins

All pins live in `pins.env`.

| Input | Version | Source | SHA-256 |
|---|---|---|---|
| Android NDK | r27d, 27.3.13750724 | `sdkmanager "ndk;27.3.13750724"` | checked through `source.properties` |
| proot | 5.1.107.102, commit `52d65a4dc140ca554d241a1438ddd4f6b00c9813` | <https://github.com/termux/proot/archive/v5.1.107.102.zip> | `30d12a6e2cc4147123756ff58edea262d3083bbc238c12d0ab9b39e3d1c40852` |
| talloc | 2.5.0 | <https://www.samba.org/ftp/talloc/talloc-2.5.0.tar.gz> | `912afa237510ae542a7733998eb18a12bcda35ab6729c8e2ddb43e8d0ebab007` |
| GPL-3.0 text | 29 June 2007 | <https://www.gnu.org/licenses/gpl-3.0.txt> | `3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986` |

- termux/termux-packages pins the same proot and talloc archives with the same
  digests (`packages/proot/build.sh`, `packages/libtalloc/build.sh`). The talloc
  archive's `.tar.asc` verifies against the Samba Library Distribution Key
  `9147A339719518EE9011BCB54793916113084025`.
- proot 5.1.107.102 is the newest tag (2026-11-01). The Play build of Termux
  still ships 5.1.107.98; the later tags fix fork, tracee-bookkeeping and
  link2symlink bugs found on Android kernels. proot computes guest mappings from
  `sysconf(_SC_PAGE_SIZE)` at run time (`src/execve/enter.c`), so it carries no
  4 KB assumption.
- `patches/proot-ashmem_memfd-string-h.patch` adds the `#include <string.h>`
  that `src/extension/ashmem_memfd/ashmem_memfd.c` is missing; NDK r27 clang
  rejects the implicit declarations. The Play build of Termux carries the same
  fix.

## Run-time contract for the app

### Environment

proot passes its own environment to the first guest process unchanged, so build
the environment explicitly instead of inheriting the app's.

| Variable | Value | Why |
|---|---|---|
| `PROOT_LOADER` | `<nativeLibraryDir>/libproot-loader.so` | **Required.** proot reads it at every guest `execve()`. The compiled-in fallback `/PROOT_LOADER-unset/loader` does not exist, so without it every guest `execve()` fails with `ENOENT`. |
| `PROOT_TMP_DIR` | an existing, app-private, writable directory, e.g. `<filesDir>/proot-tmp` | **Required.** proot's default is bionic's `P_tmpdir`, `/tmp/`, which does not exist on Android. proot creates its temporary files there: the glue directories for bindings whose guest path is missing, the files it serves for `/proc/self/mountinfo` and auxv, and its f2fs-bug probe. Without it those fail ("Please set PROOT_TMP_DIR env. variable…"). Do not use `cacheDir`: the system may clear it while proot runs. |
| `PROOT_L2S_DIR` | leave unset | Optional central directory for `--link2symlink` backing files. Unset, each one is created next to the file it stands for, inside the rootfs. |
| `PROOT_NO_SECCOMP` | leave unset | Setting it disables proot's seccomp acceleration and makes every syscall stop; only a kernel bug justifies it. |
| `LD_PRELOAD`, `LD_LIBRARY_PATH` | unset | proot hands both to the glibc guest programs, which cannot load bionic libraries. |
| `PROOT_VERBOSE` | unset; `1` or higher for diagnostics only | Verbose output goes to proot's stderr. |
| `PROOT_LOADER_32` | unset | There is no 32-bit loader: a 32-bit guest ELF fails to execute with `ENOENT`. |

### Command line

```
<nativeLibraryDir>/libproot.so \
  --kill-on-exit --link2symlink --root-id \
  --rootfs=<rootfs> --bind=/dev --bind=/proc --bind=/sys \
  [--sysvipc] [--ashmem-memfd] [--bind=<app dir>:/dev/shm] \
  [--cwd=<guest dir>] <guest program> [args...]
```

| Option | Status | Why |
|---|---|---|
| `--kill-on-exit` | required | When the first guest process (the server) ends, proot sends `SIGKILL` to every remaining guest process and exits. Without it proot keeps running until every daemonised descendant (an MCP server, a background shell) exits on its own. |
| `--link2symlink` | required | Untrusted apps may not create hard links: AOSP `private/app_neverallows.te` has `neverallow all_untrusted_apps file_type:file link;`. Without the option every guest `link(2)` fails; with it proot emulates each hard link with symbolic links. Package managers and git rely on hard links. |
| `--root-id` (`-0`) | required for apt/dpkg | Makes the guest user appear as root and fakes its privileges, which dpkg and apt require. The server itself does not need it. |
| `--rootfs=<dir>` | required | The Ubuntu userland. |
| `--bind=/dev`, `--bind=/proc`, `--bind=/sys` | required | Ordinary Linux programs need `/dev/null`, `/dev/urandom`, `/dev/pts`, `/proc/self` and `/sys`. proot's `-R`/`-S` aliases also bind desktop-Linux `/etc` files and `$HOME`, so do not use them on Android. |
| `--sysvipc` | recommended | Emulates System V IPC inside proot; bionic's `<sys/shm.h>` documents those calls as "disallowed by SELinux" for apps. |
| `--ashmem-memfd` | recommended | Emulates `memfd_create(2)` through ashmem only where a probe at start finds it unavailable. |
| `--bind=<app dir>:/dev/shm` | recommended | glibc's `shm_open(3)` and `sem_open(3)` (Python multiprocessing) use `/dev/shm`, which Android's `/dev` lacks. |

S1 must confirm the recommended options and the bind list on devices. proot's
`-L` is not listed: it only rewrites `lstat`/`lstat64`, which arm64 does not
have.

### Stopping, signals and exit status

These follow from `src/tracee/event.c` and `src/tracee/tracee.c`:

- proot **ignores** `SIGTERM`, `SIGINT`, `SIGHUP` and every other signal not
  listed here, so a `SIGTERM` sent to proot does nothing. Job-control signals
  (`SIGCHLD`, `SIGCONT`, `SIGSTOP`, `SIGTSTP`, `SIGTTIN`, `SIGTTOU`) keep their
  default action and `SIGALRM` is proot's own wake-up timer.
- **Graceful stop:** send `SIGTERM` to the server process itself. Guest PIDs are
  real kernel PIDs, and the server is proot's only long-lived child (its parent
  PID is proot's). When it exits, `--kill-on-exit` kills the rest and proot
  exits.
- **Forced stop:** send `SIGQUIT` to proot. It sends `SIGKILL` to every guest
  process, then exits once they are reaped. `SIGILL`, `SIGABRT`, `SIGFPE` and
  `SIGSEGV` do the same, then proot exits immediately with status 1.
- **Never `SIGKILL` proot alone.** proot does not set `PTRACE_O_EXITKILL`, so its
  guest processes would keep running, untraced and outside path translation. If
  proot itself hangs, kill its guest processes too.
- `SIGUSR1` and `SIGUSR2` make proot print its talloc tree on stderr.
- proot exits when no guest process is left. Its exit status is that of the
  last guest process that **exited normally** (255 if none did), so it is not
  necessarily the server's. Judge the kernel by the server's own readiness and
  health protocol, not by proot's exit code.

### Other constraints

- **Keep the rootfs at a fixed host path.** link2symlink writes absolute host
  paths into the symbolic links that stand in for hard links, so moving or
  renaming the rootfs (or `PROOT_L2S_DIR`) breaks every hard link created in it.
- On a 16 KB-page device the loader maps guest ELFs with the device's page size,
  so every guest ELF needs `PT_LOAD` alignment of at least 16 KB. Ubuntu's arm64
  binaries normally use 64 KB; the runtime-image lane reports each one.
- proot is not a sandbox: every guest process runs as the app's uid (design §2,
  D15).

## Verification

`python3 android/native/check_proot.py [BUILD_DIR]` (also run by the build)
checks, with the pinned NDK's `llvm-readelf`:

- both files are ELF64 little-endian AArch64, with every `PT_LOAD` aligned to at
  least 0x4000 and its offset and address congruent modulo 0x4000, and no glibc
  symbol versions;
- `libproot.so` is a PIE for `/system/bin/linker64`, built for API level =
  `minSdk`, with `DT_NEEDED` only among `libc.so`, `libdl.so`, `libm.so` and
  `liblog.so`, no `RPATH`/`RUNPATH` and no text relocations, and contains the
  `PROOT_LOADER` lookup and the pinned fallback path (an unbundled-loader
  build);
- `libproot-loader.so` is a static executable with no `PT_INTERP` or
  `PT_DYNAMIC`;
- `licenses/GPL-3.0.txt` matches its pin.

`tests/mobile/test_android_native_proot.py` checks the checker itself against
synthetic `llvm-readelf` output and keeps the committed pins consistent.

Result on 2026-11-05 (NDK r27d on x86_64 Linux): four builds in different
directories, one of them inside a Git repository, were byte-identical. Builds on
another host need the same NDK; that was not tried.

| File | Size | SHA-256 |
|---|---|---|
| `libproot.so` | 280,272 B | `d624ead821f41a934f3ff2e52a185a9790989f2bdd58ea496ed6c9aa5e197e8b` |
| `libproot-loader.so` | 17,624 B | `ee2f686c978ef9edff446ba4b47c6fa0f6c05d363b2612453c8bfe4c07050073` |

`libproot.so` has four `LOAD` segments and `libproot-loader.so` two, all aligned
to 0x4000; `libproot.so` needs `libdl.so` and `libc.so`, and the loader needs
nothing.

**Only a device can verify** (this host cannot run bionic arm64 code), as device
spike S1:

- that targetSdk 34 permits the mechanism: exec of `libproot.so` and the loader
  from `nativeLibraryDir`, and the loader's `PROT_EXEC` mappings of rootfs files,
  with no AVC denial other than the `auditallow` lines;
- that both files load on a 16 KB-page device;
- the seccomp acceleration and the ptrace behaviour on vendor kernels;
- the recommended options and binds, and link2symlink under dpkg, apt and git;
- the signal and exit-status behaviour described above.

## Licences and source

| Component | Licence | Copyright |
|---|---|---|
| proot | GPL-2.0-or-later (`src/cli/proot.h`: "licensed under GPL v2 or later") | STMicroelectronics and the PRoot and Termux contributors |
| talloc, with its `lib/replace` objects | LGPL-3.0-or-later | Andrew Tridgell, Stefan Metzmacher, Jelmer Vernooij, Volker Lendecke and the other Samba contributors |

`libproot.so` links talloc statically, which makes it a combined work under
LGPL-3.0 section 4. proot's "or later" clause lets it be conveyed under
GPL-3.0-or-later, the terms the combination needs. The texts the app must show
are in `licenses/`:

- `proot-COPYING`: proot's `COPYING`, its authors and the GPL-2.0 text;
- `talloc-LICENSE`: talloc's `LICENSE`, the LGPL-3.0 text;
- `GPL-3.0.txt`: the GNU GPL version 3, which LGPL-3.0 section 4(b) requires
  next to it.

**Source offer.** The complete corresponding source of both files is:

- the proot and talloc archives above, identified by their SHA-256;
- this directory at the Tofu commit the APK was built from: `build-proot.sh`,
  `pins.env`, `talloc-cross-answers.txt` and `patches/`.

Whoever distributes an APK containing these files must also publish those
archives and this directory with that release, rather than rely on the upstream
URLs staying up.
