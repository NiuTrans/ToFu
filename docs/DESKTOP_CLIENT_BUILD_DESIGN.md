# Server-Built Per-Client Desktop Installers — Design

> Status: DRAFT v1 (2026-08-01), epic `pt_ce4261579c1b4c64`.
> Closes the unfulfilled half of the "Download Desktop" agreement.

## 1. The agreement and the gap

Owner directive (2026-08-01, conv ms91b45tva0sym):

> "I want it to be **built directly on the server** rather than downloading a
> release… This way, we can **build a more suitable version based on the
> user's client-side metadata**, and it will also speed up downloads (since we
> avoid fetching releases hosted on the public GitHub network)."

What shipped (`e6bbb08f`, lib/desktop_dist): the request path went
zero-network (the panel-stall fix), and Linux artifacts are genuinely built
on-server (`builder.py`). But Windows/macOS are served by the **mirror** —
a server-side copy of the generic CI release from GitHub. That fulfilled
"downloads no longer depend on the client's route to GitHub" but NOT
"built on the server based on the client's metadata". For a Windows
visitor the delivered artifact is still the stale generic release
(v0.14.2 while the tree is at 0.16.0).

This document designs the missing half for **Windows**. macOS gets an
honest permanent boundary (§7).

## 2. Measured constraints (all verified on this host, 2026-08-01)

| Fact | Consequence |
|---|---|
| x86_64, glibc 2.28 host, **no root, no docker, no wine, no makensis** | Toolchain must be 100% userspace |
| PyInstaller **cannot cross-compile** (documented upstream) | A Windows payload needs a Windows(-API) Python → Wine is the only path |
| conda-forge has **nsis** (native Linux) but **no wine** | Installer assembly could be native (NSIS), but the PyInstaller payload still forces Wine; using Inno under Wine keeps ONE installer authoring shared with CI (no drift) |
| **The container's seccomp profile blocks the legacy `access(2)` syscall** (host `Seccomp: 2`; proot trace shows `access() = ENOSYS`). Ubuntu dash's `test -r` / `[ ! -r ]` issues `access(2)` (bash and glibc `os.access` use `faccessat`, which is allowed — that asymmetry is why the host looks healthy while guest shell scripts silently take the "not readable" branch) | Noble `apt-key --readonly`'s `[ ! -r "$FORCED_KEYRING" ]` misfires → forced keyring rewritten to `/dev/null` → `gpgv --keyring /dev/null` → apt `NO_PUBKEY` on keys that ARE in the keyring (gpgv itself verifies fine — measured). Fix: a one-line documented patch to the guest's apt-key, applied at provision time |
| Guest rootfs and build trees live on a local disk (`/tmp`, md0p1, 5.8 TB) — FUSE only holds the download cache and the final artifact | Faster builds, and no exotic-fs participation in the toolchain |
| proot static (gitlab.io), ubuntu-base 24.04, archive.ubuntu.com, python.org, github.com all reachable via the corporate proxy | Userspace toolchain is provisionable unattended |
| **proot does not translate `faccessat2` paths** (its syscall table predates the syscall) — a guest path reaches the host kernel verbatim → ENOENT. Wine's loader checks its own dir exactly that way (`faccessat2("/opt/wine/bin/", F_OK, AT_EACCESS)`) and ntdll.so issues raw syscalls, so **LD_PRELOAD cannot interpose** | The **host-mirror-path trick**: the wine tree lives at an independent HOST path (`/tmp/wine-k`, hardlink copy) bound at the IDENTICAL guest path — untranslated syscalls then hit the real host file (measured: `wine-11.14` prints) |
| **The container's seccomp SIGSYS-kills `wine-preloader`** (both bitnesses) — silent exit 255 with zero wine diagnostics after ntdll loads (strace: `+++ killed by SIGSYS +++` on the preloader execve) | Delete both preloader binaries from the tree; the loader falls back to direct exec — measured: `wineboot --init` exit 0, `Python 3.12.10` from a Windows python.exe |
| **PROBE VERDICT (2026-08-01): the toolchain WORKS.** Full recipe pinned in project memory `userspace-wine-toolchain-recipe` | S1 (`wintoolchain.py`) is a packaging exercise, not research |
| 8.2 PB free on FUSE, 5.8 TB on /tmp, 63 cores, 220 GB RAM | Build resources are a non-issue |

## 3. What "based on the client's metadata" actually compiles in

Three axes, in increasing specificity:

1. **Platform/arch** — already the selector (`find_for_platform`). The build
   must produce the artifact the selector picks for a Windows x86_64 visitor.
2. **Version currency** — built from the COMMITTED tree (git archive HEAD,
   0.16.0 today) instead of the stalled release line (v0.14.2). Same
   snapshot discipline as `builder.py` (a build must never carry uncommitted
   working-tree state).
3. **Pre-seeded server attachment (non-secret)** — the installer carries
   `preseed_server.json` (`{url}`) written into the frozen payload before
   packing; on first launch `desktop/launcher.py` imports it into
   `lib.desktop_agent.config.save_remote_server()` and deletes it. The user
   still mints/pastes their bridge token (one click in Local Control), but
   the "which server do I point this at" question disappears — the address
   is baked by the server the user demonstrably reached, prefix and all.
   (A per-USER flavor that also bakes the bridge token — zero-paste — is a
   deliberate Phase 2, §8: it needs per-user artifact semantics in the
   store and download route.)

## 4. Architecture

```
data/desktop_toolchain/cache/      downloaded tarballs (FUSE, re-usable)
/tmp/tofu_win_tc/rootfs/           ubuntu-base 24.04 guest (LOCAL disk)
/tmp/tofu_win_tc/work/<sha>/       PyInstaller work tree (LOCAL disk)
data/desktop_dist/                 final artifacts + manifest (FUSE, served)
```

### 4.1 Toolchain provisioning — `lib/desktop_dist/wintoolchain.py`

Idempotent `provision()` → a `Wine` runner object. The recipe below is the
PROBE-PROVEN one (2026-08-01); each step answers a measured trap (see §2),
not a hypothetical:

- proot static binary (gitlab.io) — pinned URL + sha256.
- ubuntu-base 24.04 guest on `/tmp/tofu_win_tc/rootfs` (LOCAL disk) —
  tarball cached on FUSE; rootfs re-extracted when missing (a cache, not
  state). Guest `apt-key` patched idempotently (TOFU-TOOLCHAIN-PATCH).
- proot invocation: bare `-r` + explicit binds (`/dev /proc /sys
  /etc/resolv.conf`) — NEVER `-R` (host /etc/group poison).
- **Kron4ek wine tarball** (version-pinned, sha256-pinned), hardlink-copied
  to an independent host path (`/tmp/wine-k`) and bound at the IDENTICAL
  guest path; both `wine-preloader` binaries renamed away. Wine commands
  are exec'd by that mirrored absolute path — the only arrangement where
  the loader's untranslated `faccessat2` hits a real file.
- `WINEPREFIX` on a path existing on both sides (`/tmp/wineprefix`).
- `xvfb` via guest apt for the smoke run (a windowed binary needs a
  display even to import-and-exit).
- Every step is resumable: a killed provision re-runs cleanly (each stage
  checks its own artifact first).
- The runner executes guest commands with host paths translated (`Z:\…`)
  — one seam, no per-call-site path math. **Open measurement for S2:**
  32-bit Windows apps (iscc, the python.org installer's bootstrapper) under
  the new WoW64 without the preloader — if that fails, iscc moves to
  NSIS (conda-forge native) or the payload ships with a zip+script fallback.

### 4.2 The two-half build — `lib/desktop_dist/winbuilder.py`

Mirror of the linux `builder.py` contract, split at the per-client seam:

**Half A — frozen payload (client-INDEPENDENT, the slow half):**
`git archive HEAD` → clean venv discipline → CI's exact pip recipe
(vendor tofu-search wheel first, then requirements, then
`pyinstaller pystray pillow psycopg2-binary pyautogui pyperclip psutil`)
→ `scripts/gen_desktop_icons.py` → `pyinstaller tofu.spec` →
`TOFU_SMOKE=1 Tofu.exe` under `xvfb-run` (exit 0 + `TOFU_SMOKE_OK` +
no Traceback — the CI gate verbatim) → cached as
`payload-<git_sha>-<deps_stamp>.tar.zst`. A second build with the same
sha+deps reuses the payload; this is what makes per-client rebuilds cheap.

**Half B — per-client wrapper (the fast half, ~1 min):**
unpack payload → write `preseed_server.json` (`{url, v}` — url from the
request context, never guessed) → generate `installer.iss` from the SHARED
template (§4.3) with `/DAppVersion` → `iscc` under wine → record artifact
`source='built'`, `os='windows'`, `arch='x86_64'`, plus `preseed: {url}`
metadata.

### 4.3 TWO installer authorings, ONE contract — NSIS on the server

The plan WAS to share one Inno template with CI. Measured verdict
(2026-08-01, S3 gate): **iscc is unobtainable AND unrunnable on this
box.** Every 32-bit Windows app measured — Inno's own installer (7.0.2
x64) and innounp alike — HANGS under the preloader-less WoW64 (the
container's seccomp SIGSYS-kills the preloader; the direct-exec fallback
runs 64-bit apps fine but 32-bit ones never return), and Inno 7's
installer container resists 7-Zip 23.01 extraction, so there is no
installerless way to even GET iscc.

So the wrapper is **NSIS** (`desktop/installer.nsi.tmpl`): makensis runs
NATIVELY on Linux (conda-forge), no wine, no display, seconds per wrap.
The CI keeps its Inno authoring. The drift defence is NOT one file — it
is `tests/test_installer_parity.py`, which pins the semantic contract
both authorings must honour (app name, install dir, privilege level,
shortcuts, launch-after-install, output naming, wizard assets, payload
shape). The contract, not the tool, is the single source of truth.

### 4.4 Launcher preseed import — `desktop/launcher.py`

In `main()`'s first-launch block: if `preseed_server.json` exists next to
the exe, `save_remote_server(url, secret)` (secret empty in Phase 1),
delete the file, log at INFO. Empty/malformed → logged + deleted (a bad
preseed must never wedge first run). The existing
`parse_connect_line`/`remote_server` contract is untouched — the preseed
just writes what the tray dialog would have.

## 5. Serving semantics

- `store.find_for_platform` already prefers the NEWER version and breaks
  ties toward `source='built'`: a built 0.16.0 beats the mirrored 0.14.2
  with zero selector changes.
- A Windows visitor with no built artifact gets the mirror (stale-while-
  build) and the build is kicked — manual via
  `POST /api/v1/desktop/build {os:"windows"}` or env-gated autobuild
  (same `TOFU_DESKTOP_DIST_AUTOBUILD` gate as linux, extended).
- The mirror STAYS as the fallback and as the macOS supply. It is no longer
  the primary Windows supply. (It still fetches from GitHub server-side —
  build tooling, not the client's download path; the agreement's letter is
  satisfied because the served Windows artifact is server-BUILT.)
- The download route's per-USER guard (Phase 2) is designed here but built
  there: an entry carrying `user_id` is invisible to other callers (404,
  indistinguishable from unknown — existence must not leak).

## 6. Security posture

- Supply chain: guest apt is signature-verified (after the access() fix —
  measured `gpgv: Good signature`); Windows Python from python.org over
  HTTPS; pip over HTTPS via the corporate proxy; the proot URL is
  sha256-pinned. The rootfs is disposable — a suspect toolchain is deleted
  and re-provisioned, never repaired.
- `proot -0` is FAKE root confined to the guest; no setuid, no namespaces,
  no host privilege. The host never executes guest binaries.
- The preseeded URL is not a secret. (Phase 2's baked token is the user's
  OWN `agents:bridge` token, scoped + revocable, travelling over
  authenticated HTTPS to that user's own machine — same exposure class as
  the minted connect line shown in cleartext today, and the artifact is
  served only to its owner per §5.)
- The built installer is unsigned (same as CI's today — SmartScreen note
  unchanged; signing stays a separate, human-credentialed question).

## 7. macOS — the honest permanent boundary

There is NO userspace path to a macOS build on Linux: PyInstaller needs a
Darwin Python, and osxcross provides a C toolchain, not a runnable macOS
Python. A `.dmg` therefore cannot be built here, today and structurally.
macOS stays mirror-served; the status payload keeps returning both DMGs
when the arch is unknown. This is recorded so the next "why is the Mac
build mirrored" question has a citation instead of a re-investigation.

## 8. Slices

- **S1** `wintoolchain.py` — provisioning + wine runner. Tests: staged
  provisioning with faked downloads; the REAL probe transcript (this
  investigation) pinned as the acceptance evidence. **LANDED 2026-08-01
  (`b454be27`)** — live provision ~180 s, wine-11.14 runs.
- **S2** `winbuilder.py` Half A — payload build + sha/deps cache. Tests
  fake the pipeline, drive the real recording; cache-hit logic NEUTER-tested.
  **LANDED 2026-08-01 (`704641fe`)** — two REAL payloads on two HEADs
  (152.7 MB, 3316 files, `TOFU_SMOKE_OK version=0.16.0 blueprints=57`).
- **S3** ~~`installer.iss.tmpl` extraction + CI rewiring~~ **PIVOTED to
  NSIS** (see §4.3 — the 32-bit measurement is the reason): Half B
  wrapper + launcher preseed + parity suite. **LANDED 2026-08-01
  (`619a0118` + `0048f28a` + route wiring `8a25a8ad`)** — real
  `Tofu-Setup-0.16.0-win64.exe` (152,886,951 B) recorded `source='built'`;
  `find_for_platform('windows')` prefers it over the mirrored 0.14.2.
- **S4** serving wiring: build route `{os:'windows'}` (`8a25a8ad`) +
  status autobuild gate (same env var as Linux). Phase 2 (per-user
  token-baked installers) stays a separate epic.

## 9. Acceptance

A Windows visitor to Local Control is offered
`Tofu-Setup-<current>-win64.exe` marked `服务器直连 · built`, downloads it
from this server, installs, and on first run finds the server address
already attached — no GitHub release involved anywhere in the client's
path, and the artifact is younger than the stale release line.
