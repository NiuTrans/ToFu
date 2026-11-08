# On-device kernel runtime images (RUNTIME CONTRACT v1.1)

This directory builds the two archives the Android app ships for its local
kernel: an Ubuntu 24.04 arm64 userland (the **rootfs**) and the Tofu
**payload** that runs inside it under proot. It implements lane L2 of the
Android design (Option B: proot userland, targetSdk 34). That decision is a
recommendation pending the owner's confirmation and the S1 device spike.

| Concern | Owner |
|---|---|
| Archives, manifests, `index.json`, guest entry `bin/tofu-local-kernel` | this directory (`android/runtime/`) |
| Server launch contract (stdin secret, port line, readiness, SIGTERM, exit codes, restart budget) | `docs/ANDROID_LOCAL_KERNEL.md`, `local_kernel.py` |
| Extraction, verification, proot process, foreground service | the app's local-kernel service (`runtime/` package, lane L5) |
| `libproot.so`, `libproot-loader.so` (`jniLibs/arm64-v8a`) | proot native packaging (lane L3) |

Only a device can run the arm64 artifacts under proot. Everything here is
verified on an x86_64 build host: structure, determinism, ELF machine and
alignment, sizes, and a boot of an x86_64 build of the same payload without
proot.

Contract history:
- v1: first version.
- v1.1:
  - The archives are named `.tgz`, not `.tar.gz`. The Android Gradle
    Plugin's asset merger un-gzips every `*.gz` asset during
    `merge<Variant>Assets` (sdk-common `AssetItem.shouldBeUnGzipped`), which
    would ship `rootfs-<v>.tar` and break the index's name and SHA-256. The
    bytes are unchanged.
  - The version digest also covers the manifest's `inputs` field, so equal
    versions mean byte-identical archives.
  - No hard links: Android apps cannot create them (AOSP sepolicy
    `app_neverallows.te`), so every path of a hard-link set is written as
    its own regular file.
  - The on-device layout follows the proot lane: `PROOT_TMP_DIR` is
    `files/kernel/proot-tmp`, and `/dev/shm` is bound from
    `files/kernel/shm`.

## 1. Artifacts

The build writes these files, which the app packages unchanged under
`assets/kernel/`:

| File | Content |
|---|---|
| `rootfs-<rootfsVersion>.tgz` | Ubuntu 24.04 ("noble") arm64 userland with git installed |
| `payload-<payloadVersion>.tgz` | python-build-standalone, the site-packages of Tofu's runtime closure, the Tofu source tree with its built frontend, uv, the TofuDB engine when supplied, and `bin/tofu-local-kernel` |
| `index.json` | which archives belong together, with their SHA-256 and size |

No output name ends in `.gz`. The app refuses any `*.gz` under its assets
directory and stores `*.tgz` uncompressed (`noCompress`), so the first
install does not inflate twice.

ABI: `arm64-v8a` only. (An `x86_64` payload exists solely for the host boot
test in §8 and is never shipped.)

`index.json` (keys sorted, two-space indent, trailing newline):

```json
{
  "abi": "arm64-v8a",
  "format": 1,
  "payload": {"file": "payload-<payloadVersion>.tgz", "sha256": "<64 hex>", "size": 123, "version": "<payloadVersion>"},
  "rootfs": {"file": "rootfs-<rootfsVersion>.tgz", "sha256": "<64 hex>", "size": 456, "version": "<rootfsVersion>"}
}
```

`sha256` and `size` describe the `.tgz` file as stored in the APK.

**Versions are content-derived.** A version is a readable label plus the
first 12 hex digits of the SHA-256 of the archive's manifest without its
`version` field (§2). The manifest pins every entry's metadata and content
digest, and the tar is a pure function of it, so equal versions mean
byte-identical archives and any change yields a new version.
- `rootfsVersion` = `<ubuntu-base point release>-<digest>`, e.g. `24.04.5-0123456789ab`.
- `payloadVersion` = `<Tofu VERSION>-<first 10 hex of the source commit>-<digest>`,
  e.g. `0.18.4-b2f46f7285-0123456789ab`.

Versions contain only `[0-9A-Za-z.-]`, so they are safe as file and
directory names.

## 2. Archive format

Each archive is a deterministic POSIX (pax) tar stream compressed with
`gzip -n -9`:
- **Order.** The first entry is `.manifest.json`. All other entries follow
  in byte order of their UTF-8 path, so every directory precedes its
  contents.
- **Paths** are relative and `/`-separated, without a leading `./`.
  Directory members carry tar's conventional trailing `/`; manifest paths
  never do. No entry's parent is a symlink, so extraction never writes
  through a link. pax `path`/`linkpath` records appear only for names that
  do not fit the ustar header.
- **Metadata.** uid and gid 0, empty user and group names, mtime 0. File
  modes are preserved, including setuid/setgid and sticky bits in the
  rootfs.
- **Types**: regular file, directory, symlink. Device nodes, FIFOs, sockets
  and hard links never appear: a hard-linked file in a source (three in the
  rootfs, e.g. `usr/bin/perl5.38.2`) is written once per path, as a regular
  file with the same content. `hardlink` stays a manifest type for readers,
  but the builder never emits it.
- gzip header: no file name, mtime 0.

`.manifest.json` (one line of compact JSON, keys sorted, trailing newline)
lists every other entry of its archive; it does not list itself:

```json
{"abi":"arm64-v8a","entries":[
  {"mode":"0755","path":"bin","type":"dir"},
  {"mode":"0755","path":"bin/tofu-local-kernel","sha256":"<64 hex>","size":1234,"type":"file"},
  {"mode":"0777","path":"python/bin/python3","target":"python3.12","type":"symlink"}
 ],"format":1,"inputs":{...},"kind":"payload","version":"<payloadVersion>"}
```

(Line breaks added here for reading.)
- `entries` are in archive order.
- `mode` is a JSON **string** of four octal digits (`"0755"`, `"4755"`,
  `"1777"`): permission plus setuid/setgid/sticky bits. Parse it base 8; it
  equals the tar header's mode field.
- `file` entries carry `size` and `sha256` of the content. `symlink`
  entries carry `target`, stored verbatim; it may be absolute (interpreted
  inside the guest). `dir` entries carry neither. (A `hardlink` entry, which
  this builder never writes, would carry all three, its `target` naming an
  earlier `file` entry.)
- `kind` is `rootfs` or `payload`. `abi`, `kind`, `version` and `inputs` are
  extra top-level keys next to `format` and `entries`. `inputs` is
  informative provenance (§7); consumers must not depend on its shape.

**Consumer rules** (the app's installer):
1. Before extracting, check the asset's size and SHA-256 against
   `index.json`. Refuse a mismatch.
2. Extract into a staging directory next to the destination, then rename it
   into place. Refuse any entry whose path is absolute, contains `..`, or
   whose parent is not an earlier directory entry.
3. Restore permission bits; at least the owner-execute bit must survive.
   Ownership, mtimes and setuid/setgid bits carry no meaning on the device
   and may be dropped.
4. There are no hard links to materialize (v1.1).
5. `.manifest.json` is extracted too. It stays at the root of the extracted
   tree and lets the app or a user re-verify the installation later.

## 3. On-device layout and proot

App-private paths, all owned by the app uid:

| Path | Purpose | Guest path |
|---|---|---|
| `files/kernel/rootfs/` | the extracted rootfs; persistent, user-extensible with apt | `/` |
| `files/kernel/payload/<payloadVersion>/` | one extracted payload per version | |
| `files/kernel/payload/current` | pointer (a relative symlink is suggested) to the active version | `/opt/tofu` |
| `files/kernel/data/` | Tofu's data directory (`TOFU_DATA_DIR`): database, config, logs | `/var/lib/tofu` |
| `files/kernel/home/` | the guest user's home: uv cache, uv tools and Pythons, MCP servers | `/root` |
| `files/kernel/proot-tmp/` | `PROOT_TMP_DIR`; emptied before every launch | |
| `files/kernel/shm/` | shared memory for the guest; emptied before every launch | `/dev/shm` |

proot binds `payload/current` → `/opt/tofu`, `data` → `/var/lib/tofu`,
`home` → `/root`, `shm` → `/dev/shm`, plus `/dev`, `/proc` and `/sys`
(the proot lane's README owns the exact command line). The rootfs already
contains the empty mount-point directories `/opt/tofu` and `/var/lib/tofu`.
proot runs from `nativeLibraryDir/libproot.so` with
`PROOT_LOADER=<nativeLibraryDir>/libproot-loader.so` and
`PROOT_TMP_DIR=<files>/kernel/proot-tmp`.

What the rootfs expects from the launcher (lane L5):
- **Root identity.** The rootfs is prepared for guest uid 0 (proot
  `--root-id`/`-0`). `/etc/passwd` names only system users, and apt/dpkg
  refuse to run as non-root.
- **A cleared environment.** Start proot with a cleared environment holding
  only the proot variables above, `HOME=/root` and optionally
  `LANG=C.UTF-8`. The guest entry sets `PATH` itself and calls no external
  command before it has.
- **DNS.** Android has no `/etc/resolv.conf`. The shipped
  `rootfs/etc/resolv.conf` is an empty regular file; before each launch the
  app writes the active network's DNS servers into it (`nameserver <ip>`
  lines). Without that, every name lookup in the guest fails.
- `/etc/hosts` ships with the standard localhost entries.

Guest command: `/opt/tofu/bin/tofu-local-kernel` (no arguments needed). The
app writes the secret line to its stdin and reads the listening line from
its stdout, exactly as `docs/ANDROID_LOCAL_KERNEL.md` specifies. To stop the
kernel, the app sends SIGTERM to the listening line's `pid`, the server
process, not to proot; the guest entry has exec'd, so that pid is the
launched process (§4). The local kernel's data and logs live under
`/var/lib/tofu`. The payload stays unwritten: the server's caches and other
reconstructible state follow the data directory too
(`docs/modules/infra_runtime.md`, "Writable path selection").

## 4. Guest entry: `bin/tofu-local-kernel`

A POSIX `sh` script (source: `payload/bin/tofu-local-kernel`). It:
1. finds the payload root from its own location with shell builtins only,
   so the same payload also runs unmounted (the host boot test does this);
2. sets `PATH=<payload>/bin:<payload>/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`
   (the server and its tools see the payload's `uv`, `uvx`, `python3` and
   `tofu-db`, then the userland's `git`), and `HOME=/root` when `HOME` is
   unset;
3. changes into `<payload>/tofu` and `exec`s
   `<payload>/python/bin/python3 local_kernel.py --data-dir /var/lib/tofu "$@"`.

It reads nothing from stdin and prints nothing. Because it `exec`s, stdin
(the secret), stdout (the one listening line), stderr, signals and the exit
status all belong to `local_kernel.py` directly. The listening line's `pid`
is therefore the pid the guest entry was started as: SIGTERM to that pid
reaches the server, which drains and exits 0. Arguments are appended, so
`--port N` passes through; a later `--data-dir` overrides the default (used
only by the host boot test).

## 5. Rootfs content

- `ubuntu-base-24.04.5-base-arm64.tar.gz`, as published.
- Added packages: `git` and `ca-certificates` with their complete
  `Pre-Depends`/`Depends` closure from the noble, noble-updates and
  noble-security `main` arm64 indexes, at the highest version apt would
  pick. 25 packages are added. `perl-base` is upgraded in place because
  `perl` depends on its exact version, as `apt install git` would do on the
  device.
- They are unpacked the way dpkg unpacks them: through the merged-/usr
  directory symlinks, honouring the base's own
  `/etc/dpkg/dpkg.cfg.d/excludes` (no man pages, translations or docs except
  copyright and changelog files), and registered in the dpkg database
  (`/var/lib/dpkg/status`; `info/<pkg>[:arm64].list`, `.md5sums`,
  `.conffiles`, maintainer scripts; trigger interests), so `apt` on the
  device sees a consistent system. `dpkg --root=<rootfs> --verify` reports
  only the paths the base's filters exclude.
- Maintainer scripts cannot run on the x86_64 build host. Every script of
  the added packages was reviewed (`MAINTAINER_SCRIPT_REVIEW` in
  `runtime_builder/rootfs.py`). Only ca-certificates' configure step has an
  effect, and the builder reproduces it: `/etc/ca-certificates.conf` (every
  certificate trusted, the debconf default), `/usr/local/share/ca-certificates`,
  the `/etc/ssl/certs/*.pem` links, the bundle `ca-certificates.crt`, and the
  `<subject-hash>.<n>` links. A test compares those links with `openssl
  rehash` on the same files. The other scripts only `activate-noawait
  ldconfig`; the new libraries live in `/usr/lib/aarch64-linux-gnu`, which
  the arm64 loader searches by default, so the unrefreshed
  `/etc/ld.so.cache` costs only a lookup.
- Base packages with newer versions in the indexes stay at the base's
  versions (12 at the current pins, recorded in `pins.json` as
  `base_packages_with_updates`, e.g. libc6 and libssl3t64); `apt update &&
  apt upgrade` on the device applies them with their scripts.
- `/etc/hosts` gets the localhost entries; `/opt/tofu` and `/var/lib/tofu`
  are created as mount points.
- No apt lists are shipped. Run `apt update` before the first
  `apt install` (Node.js, for example, is user-installed this way).

## 6. Payload content

```
.manifest.json
bin/tofu-local-kernel     guest entry (§4)
bin/uv, bin/uvx           uv, official release asset
bin/tofu-db               TofuDB engine (only when supplied with --tofudb, §9)
bin/libtofu_db.so         its embedded-transport library, when it sits beside the engine
python/                   python-build-standalone "install_only" CPython 3.12
python/lib/python3.12/site-packages/   Tofu's runtime closure
tofu/                     Tofu source tree at the built commit, with static/vite
```

- **Runtime closure:** `uv.lock`'s non-dev closure of the default
  dependencies plus the `app` extra, the closure the API image installs (it
  adds `scale`, the multi-replica Redis client, which a phone does not
  need). `uv export --frozen` lists it; `uv pip install --target
  --python-platform aarch64-manylinux_2_39 --no-deps --require-hashes
  --no-build` installs the wheels for noble's glibc 2.39 using the build
  host's CPython of the same version for interpreter discovery, without
  running the target interpreter. Packages that `uv.lock` knows only as an
  sdist are built on the host and refused unless the result is pure Python
  (today only `zhconv`); their build backend is pinned to `uv.lock`'s
  setuptools and wheel. Workspace members (`tofu-search`) are built into a
  wheelhouse first, so no build path leaks into the installed metadata.
  Console scripts (their shebang names the build host's interpreter; Tofu
  runs modules) and uv's install-time `uv_cache.json` are dropped together
  with their RECORD rows, so every RECORD lists exactly the shipped files.
  The `uv` wheel is left out because `bin/uv` provides the same version.
- **Python bytecode:** every `.py` file in the payload has a
  `__pycache__/*.cpython-312.pyc` compiled with `unchecked-hash`
  invalidation by the build host's CPython of the same version, recording
  the guest path `/opt/tofu/...`. Imports never stat or hash the source,
  which saves work on each cold start under ptrace, and the payload is
  immutable anyway. Files that do not compile ship without bytecode and are
  listed in the build report (today one Python 2 file in olefile).
- **Tofu source tree:** the commit's tracked files (`git archive`), minus
  trees that are never read at runtime: `tests/`, `designs/`,
  `screenshots/`, `benchmarks/`, `android/`, `ios/`, `.github/`, `.vscode/`,
  and the Rust sources `packages/tofu-db/`, `packages/tofu-runtime/` and
  `packages/vendor/`. Leaving out `packages/tofu-db/` also keeps the server
  from treating the payload as an engine source checkout. The tree has no
  `.git`; the local kernel refuses self-update anyway.
- **Frontend:** `tofu/static/vite/` comes from a prebuilt directory, checked
  with the commit's own `lib.vite_assets.validate_source_vite_artifact`.
  - *Problem.* Fresh trees lack `static/vite`; the server serves only a
    published graph (`lib/server/server_assembly.py`
    `validate_published_vite_artifact`).
  - *Decision.* Take the output of `npm run build:frontend` for the same
    commit (`--frontend-dist`). The builder refuses it unless its authoring
    and i18n digests match the commit's sources, the same check
    `scripts/install_frontend_dist.py` applies to release frontends. Build
    it from a clean export: a long-lived checkout's `static/vite` also
    retains the previous generation's assets (about 70 MB instead of 19 MB).
  - *Alternatives considered.* Running npm inside the builder: it duplicates
    the frontend build's owner, needs Node 22+ and the npm registry, and its
    output is platform-independent anyway. Shipping frontend sources only:
    the server never builds them.

## 7. Inputs and pins

All downloads are pinned in `pins.json` by version, URL and SHA-256. Each
SHA-256 is copied from the publisher's own checksum file, whose URL is
recorded next to it.
- **ubuntu-base 24.04.5 arm64:** cdimage `SHA256SUMS`, whose `.gpg`
  signature `resolve` checks with the build host's
  `ubuntu-archive-keyring.gpg` (it holds the CD image signing key).
- **Debian packages:** resolved from the `ports.ubuntu.com` InRelease and
  `Packages.xz` indexes. InRelease is verified with `gpgv` against the
  `ubuntu-archive-keyring.gpg` inside the pinned ubuntu-base, and the
  indexes are fetched by hash (`Acquire-By-Hash`). Every `.deb` is pinned by
  its pool path and the index's SHA-256.
- **python-build-standalone:** the CPython version Tofu requires (3.12, from
  `requires-python` and `.python-version`; the build checks both), aarch64
  and x86_64 `install_only` builds, release `SHA256SUMS`.
- **uv:** the same version as `uv.lock`'s `uv` package (the build checks
  it); the release's per-asset `.sha256` files, aarch64 and x86_64.
- **Wheels:** `uv.lock` already pins every wheel's SHA-256; the install
  passes them as `--require-hashes`.

Two commands, kept apart on purpose:
- `resolve` reads current publisher indexes and rewrites `pins.json`. It is
  the only step that sees "latest". Review and commit the diff.
- `build` is deterministic from `pins.json`, the source commit and the
  prebuilt frontend.

Ubuntu removes superseded `-updates`/`-security` builds from the ports pool
(ports has no snapshot service). An old `pins.json` can therefore stop
downloading; rerun `resolve`. That trade buys current security fixes for
git and its libraries.

## 8. Building and verification

Build host: Linux x86_64 with glibc 2.35 or newer, Python 3.11+, `git`,
`gzip`, `gpgv`, `dpkg-deb`, `openssl`, and network access (proxies from the
standard environment variables). `xz` and `zstd` only feed the size report.

```sh
# Frontend for the commit, from a clean export (Node 22+, see package.json):
git archive <commit> | tar -x -C /tmp/fe && (cd /tmp/fe && npm ci && npm run build:frontend)

python3 android/runtime/build_runtime.py build --rev <commit> \
    --frontend-dist /tmp/fe/static/vite [--tofudb <aarch64 tofu-db>] \
    [--out DIR] [--cache DIR]                    # arm64-v8a: rootfs + payload + index
python3 android/runtime/build_runtime.py build --abi x86_64 --rev <commit> \
    --frontend-dist /tmp/fe/static/vite --out DIR   # host boot test payload only
python3 android/runtime/build_runtime.py resolve  # refresh pins.json (network)
```

Outputs go to `<out>/<abi>/assets/kernel/` (default `android/runtime/out/`,
gitignored); downloads are cached by SHA-256 in `android/runtime/cache/`
(gitignored) unless `--cache` is given. `<out>/<abi>/report.json` records
the inputs, the added and upgraded packages, the installed distributions
with their wheel tags, every ELF with its machine and PT_LOAD alignments,
and the size of each component as content bytes, tar bytes, and gzip -9,
xz -9 and zstd -19 sizes. A build fails if any ELF is not for the target
machine.

Tests (`android/runtime/tests/`):

```sh
python -m pytest android/runtime/tests                  # unit tests, no build needed
python -m pytest android/runtime/tests \
    --runtime-out <out>/arm64-v8a --runtime-out-repeat <out2>/arm64-v8a \
    --runtime-host-payload <out3>/x86_64                 # contract, determinism, host boot
```

- Unit tests: archive determinism and manifest correctness, Debian version
  ordering (cross-checked with `dpkg --compare-versions`), dependency
  closure, dpkg path filters, unpacking and registration of real `.deb`s
  built with `dpkg-deb`, the ca-certificates emulation against `openssl
  rehash`, and the guest entry (argv, stdin, environment, exit status,
  SIGTERM).
- Artifact tests: `index.json`, no output named `*.gz`, the tar format and
  manifest of both archives (every file's SHA-256), the rootfs and payload
  layout, bytecode presence and mode, AArch64 ELFs, the size report, and
  byte identity with a second build.
- Host boot test: the x86_64 payload's own python runs
  `bin/tofu-local-kernel` with a temporary data directory and a clean
  environment, reads the secret from stdin, prints the listening line with
  its own pid, and answers `GET /api/v4/meta` with 200 for the secret (401
  for another secret, 403 for another Host). Without a credential
  `/api/v4/keys` is 401, with it 200. SIGTERM exits 0, state stays in the
  data directory, the payload stays unwritten and the secret appears in no
  log.

## 9. Known limits and deviations

- Only a device can run the arm64 artifacts under proot (W^X, 16 KB pages,
  symlinks in app storage, cold start; see the design's S1).
- **TofuDB** is an optional input (`--tofudb`, an aarch64 `tofu-db`; a
  `libtofu_db.so` beside it ships too, for the embedded transport). The
  local kernel does not need it: personal mode's storage default is the SQL
  authority sidecar on SQLite (`runtime_guards.py`
  `load_deployment_configuration`), selected without any setting; the
  engine serves only a kernel that selects the `tofudb` backend.
  - The cross-build works with the repository's own producer, unchanged:
    `rustup target add aarch64-unknown-linux-gnu`, then
    `scripts/build_tofudb_prebuilt.py --target aarch64-unknown-linux-gnu
    --no-smoke` in a `git archive` of the commit (`packages/`, `contracts/`,
    `VERSION`, the script), with
    `CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER`,
    `CC_aarch64_unknown_linux_gnu` and `AR_aarch64_unknown_linux_gnu`
    pointing at clang (`--target=aarch64-linux-gnu --sysroot=<sysroot>`,
    plus `-fuse-ld=lld` for the linker) and `llvm-ar`. The linker wrapper's
    name must not end in `ld`, or rustc passes raw `ld` flags to clang.
  - The sysroot is the noble arm64 `libc6`, `libc6-dev`, `linux-libc-dev`,
    `libgcc-13-dev`, `libgcc-s1`, `libcrypt1` and `libcrypt-dev` packages
    from the same signed indexes, unpacked with `dpkg-deb -x`, plus a
    `lib -> usr/lib` link for glibc's linker scripts.
  - The build host has no aarch64 C compiler of its own; the clang used was
    an Android NDK r27d already on the host (clang 18 can target glibc).
    Wiring this into CI (lane L9) is a follow-up.
  - Result for b2f46f728: both files are AArch64 with 64 KB-aligned LOAD
    segments, need only libc, libm and libgcc_s, and require at most
    GLIBC_2.39 (noble ships 2.39). They are unstripped (21 MB each, debug
    info). They cannot run on this host.
- Browser tools are not in v1: no Playwright or Chromium in the payload.
- `tofu-repo-kernel`, the optional native repository-ranking accelerator
  that the API image builds with cargo, is not in the payload.
  `lib/repo_map` falls back to its pure-Python ranking.
