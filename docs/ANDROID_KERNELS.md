# Android kernels

The Android app shows one Tofu server at a time in its WebView. That server is
the app's *kernel*. A kernel is either this phone, running the complete Tofu
harness on the device, or a remote Tofu server.

**Status.** The on-device runtime is decided: Option B at targetSdk 34. The
owner delegated the choice. Device test S1 (below) is pending, and if it
fails the fallback is targetSdk 28. The server half of the local kernel
exists; its launch contract, boundary and limits are in
[`ANDROID_LOCAL_KERNEL.md`](ANDROID_LOCAL_KERNEL.md). The app connects to
URL kernels (server profiles, [`android/README.md`](../android/README.md)),
to SSH kernels through a tunnel it dials itself, and to This phone when the
build packages the runtime (the app's side of both is in
[`android/docs/SUPERVISOR_DESIGN.md`](../android/docs/SUPERVISOR_DESIGN.md)
§§5–6). The runtime packaging and the capability snapshot below are the
decided design that the app and server follow.

DSH Go's architecture is the reference. It is cited only through
[`DSH_GO_ANDROID_ANALYSIS.md`](DSH_GO_ANDROID_ANALYSIS.md); DSH Go is
AGPL-3.0, and none of its code is used.

## On-device runtime

**Problem.** "Complete" means the phone runs the real harness: the agent
loop, shell, git, code execution, stdio MCP servers, and skills with scripts
and arbitrary CLIs. Tofu is a Python server that starts helper processes
freely. The app targets SDK 34 and ships through GitHub releases. Since
Android 10, an app targeting 29 or higher cannot `execve()` files in its own
data directory ([behavior changes, Android 10](https://developer.android.com/about/versions/10/behavior-changes-10)).

**Decision: Option B, a proot userland, at targetSdk 34.**
- `libproot.so`, `libproot-loader.so` and `libtalloc.so` ship in `jniLibs`,
  16 KB-aligned, with `useLegacyPackaging`, so they are installed into
  `nativeLibraryDir`.
- An Ubuntu 24.04 arm64 rootfs, persistent and extensible with apt.
- A replaceable Tofu payload: python-build-standalone, a venv built from
  `uv.lock` with manylinux aarch64 wheels, the Tofu source, git, uv, and the
  tofu-db aarch64 prebuilt (`scripts/build_tofudb_prebuilt.py`).
- The unmodified Linux server runs inside it in local-kernel mode
  (`local_kernel.py`, [`ANDROID_LOCAL_KERNEL.md`](ANDROID_LOCAL_KERNEL.md)).
- One server code path for desktop and phone (principle #4). DSH Go makes
  the same choice: an unmodified engine inside proot Ubuntu, with the app as
  its client (analysis, "The one master idea" and §3).

**W^X evidence: why targetSdk 28 is not needed.**
- AOSP sepolicy still lets every untrusted app map its data files executable
  but not exec them:
  - [`untrusted_app_all.te`](https://android.googlesource.com/platform/system/sepolicy/+/refs/heads/main/private/untrusted_app_all.te)
    has `allow untrusted_app_all app_data_file:file { r_file_perms execute };`
    plus `auditallow … execute`, under "Some apps ship with shared libraries
    and binaries that they write out to their sandbox directory and then
    execute";
  - `execute_no_trans` on `app_data_file` appears only in
    [`untrusted_app_27.te`](https://android.googlesource.com/platform/system/sepolicy/+/refs/heads/main/private/untrusted_app_27.te),
    "for targetApi 26, 27, and 28";
  - [`app.te`](https://android.googlesource.com/platform/system/sepolicy/+/refs/heads/main/private/app.te)
    grants `x_file_perms` on `apk_data_file`, so files installed in
    `nativeLibraryDir` can be exec'd.
- proot needs exactly that. Each guest `execve` becomes an exec of the
  loader, which opens the guest ELF read-only and maps its segments. Only the
  loader is exec'd, from `nativeLibraryDir`. Because the tracer rewrites the
  syscall, raw-syscall callers such as Go binaries are covered too.
- Termux's Google Play build packages proot this way. Its recipe says "Use a
  loader bundled with the Termux app so that it can be executed", on proot
  5.1.107.98
  ([`packages/proot/build.sh`](https://raw.githubusercontent.com/termux-play-store/termux-packages/main/packages/proot/build.sh)).
- Known limits: every executable mapping is audit-logged (volume unmeasured);
  text relocations are denied; proot isolates nothing. If Android ever
  withdraws `execute` on `app_data_file`, proot and every app that loads
  extracted libraries break together.

**What B gives.** Agent loop, model calls, shell, code execution, git and
worktrees, stdio and HTTP MCP, skills with scripts, the scheduler while the
foreground service lives, any configured storage backend, and uploads all
work as on Linux. Browser tools (linux-arm64 Chromium under proot) are
unverified and not offered at first. Devices cannot attach to a phone kernel
(see "Deferred"), and LAN discovery stays off.

**Alternatives considered.**
- **A: embedded CPython in the app process.** Rejected. It cannot be
  complete: no shell, git, stdio MCP or skill scripts, and about 20 features
  that spawn processes would have to be replaced or disabled. It also forks an
  Android-only server variant with about a dozen cross-built native packages
  and a hypercorn patch. It is not the fallback.
- **C: a Termux companion.** Rejected as the product: a second app, manual
  setup, and every native dependency rebuilt against bionic. A Tofu that a
  user runs inside Termux can still be added as a URL or SSH kernel.
- **D: A for the server plus proot for tools.** Rejected: two runtimes and two
  build pipelines. If S1 finds proot's overhead on the server process too
  high, the fix stays inside B: start only the server through glibc's `ld.so`
  from `nativeLibraryDir` (DSH Go's DirectLaunch, analysis §3) and keep proot
  for child processes. That needs a spawn indirection, built only after
  measuring.
- **targetSdk 28.** Not needed, per the evidence above; it is the fallback if
  S1 fails. Its costs against today's 34:
  - ineligible for Google Play (irrelevant to GitHub releases);
  - the WebView loses the modern SameSite cookie semantics of targets 31+,
    which the gateway and SSO cookie flows rely on;
  - exported-component, PendingIntent-mutability and device-transfer backup
    rules of 31+ are lost;
  - the app regains legacy powers (exec from app data among them), which
    weakens its sandbox;
  - whether 34 → 28 installs as an update is unverified.

### Device test S1 (pending)

S1 cannot run on a server host. It confirms the evidence above on devices.
- **Build:** a test APK at targetSdk 34 with proot, its loader and talloc in
  `jniLibs`, an Ubuntu arm64 rootfs and a uv-installed Tofu.
- **Devices:** Android 14, Android 15 or 16, and a 16 KB-page device.
- **Measure:**
  1. `bash -lc 'git --version && python3 -c "import ssl" && uvx --version'`
     exits 0, with no AVC denials beyond the `auditallow` lines;
  2. time to boot to `/api/v4/meta`;
  3. one chat turn;
  4. `git status` on a repository of about 1,000 files, and a `uv pip install`;
  5. RSS, idle CPU and battery over one hour;
  6. APK size and extracted size;
  7. **a long run with several Workers and tool calls**, with the app in the
     background for part of it, counting the kernel's child processes and any
     that die (see "Risk: Android kills app child processes"). A first launch
     alone does not exercise that rule.
- **Decision rule:** if (1) fails, switch to targetSdk 28. If (7) loses
  processes, the risk below decides the next step. (2)–(6) are reported for
  judgement.

### Reading a device self-check report

The in-app self-check (`android/README.md` "Device self-check") produces one
JSON document per run (`selfcheckVersion: 1`) with a `summary`, the collected
`facts`, and `exec`, `kernel`, `stress` and `exitReasons` sections. It answers
the device-only questions of S1 without adb. How to read it:

| Report field | What it settles |
| --- | --- |
| `exec.checks[id=proot-version]` fails | proot cannot be exec'd from `nativeLibraryDir`. This is packaging (ABI, `extractNativeLibs`, a corrupt or mismatched loader), **not** W^X; targetSdk 28 would not help. |
| Any `exec.checks` under proot fails (`proot-true`, `proot-sh`, `proot-python`, `proot-git`) | S1 item 1 fails on this device: the loader-based execution of app-data files does not work at targetSdk 34. **Fall back to targetSdk 28** (the decision rule above). Read `stderr` for the cause: `PROOT_LOADER`, seccomp, a 16 KB-page mapping error, or an AVC denial. |
| `exec.checks[id=direct-exec].classification` | `denied` = W^X is enforced as expected at targetSdk ≥ 29 (errno 13). `permitted_interpreter_missing` = the exec was allowed and only glibc's interpreter was absent. `permitted` = the app-data exec actually ran. |
| `facts.pageSizeBytes` = 16384 and the proot checks pass | The proot pair and the guest ELFs load on a 16 KB-page device. |
| `kernel.timeToReadyMs` | S1 item 2 (time to `/api/v4/meta`); `kernel.firstInstall` says whether runtime extraction is included. `kernel.probes` proves readiness (200), default deny (401 without a credential) and the secret (200 with it). |
| `stress.phases[].deaths` non-empty | S1 item 7: processes died before cleanup. `firstVictim` = `proot` means the cap's documented order held; per-process `diedAtMs`, `signal` and `deathSource` give the timeline. Compare `foreground` with `background`: a foreground service does not exempt the cap. |
| `stress.phases[].samples.deviceIdle` / `deepSleepMs` | Whether the background phase actually covered Doze and deep sleep. |
| `exitReasons` with `reasonName: excessive_resource_usage` | The sustained-CPU rule fired: the app was killed with its children (the description reads "Caused by child process: excessive cpu …"). With the foreground service running this should not happen; if it does, the exemption does not hold on this device. |
| `status: interrupted` | The app process died mid-run. `interruptedRun` is the last report it wrote; `summary.appProcessExit` is the system's own exit record for it. |
| `stress.skipReason` | `runtime_not_in_this_build` (no runtime packaged), `rootfs_not_installed` (the kernel check never installed it), `local_kernel_active` (a kernel was already running; stop it and rerun), `local_kernel_busy`, or `skipped_by_user`. A skipped phase measures nothing — do not read it as survival. |
| `facts.phantomProcesses` | Only `monitorFlagGlobal` (`settings_enable_monitor_phantom_procs` in `Settings.Global`) is readable by an app. The developer-option system property and `activity_manager/max_phantom_processes` are not, so the effective switch is reported as not fully readable; zero deaths is **not** proof that the cap is off. |

Zero deaths in a short run is weak evidence: the cap kills only above 32
system-wide phantom processes, and the CPU rule needs the app to be backgrounded
without a foreground service for at least one 5-minute power check. Use the
15- or 60-minute preset for the Doze/OEM half of item 7.

## Risk: Android kills app child processes

Under proot every guest process (the server, TofuDB, MCP stdio servers, tool
shells) is a child process of the app. Since Android 12 the system tracks
such processes as *phantom processes* and kills them under two rules, so a
busy local kernel can lose processes in the middle of a task. The Android 12
behavior-change pages ([all apps](https://developer.android.com/about/versions/12/behavior-changes-all),
[targeting 12](https://developer.android.com/about/versions/12/behavior-changes-12))
do not mention this; the authority is the AOSP source.

**The rule.**
- **What counts.** Every process in an app process's cgroup that is not
  itself an app process ([`PhantomProcessList.java`](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-12.0.0_r1/services/core/java/com/android/server/am/PhantomProcessList.java)).
- **A system-wide cap.** At most `max_phantom_processes` phantom processes
  across all apps, default 32, read from `device_config` namespace
  `activity_manager`
  ([`ActivityManagerConstants.java`](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-12.0.0_r1/services/core/java/com/android/server/am/ActivityManagerConstants.java);
  the same default and key at `android-13.0.0_r1` and `android-14.0.0_r1`).
  Above the cap the surplus is killed ("Trimming phantom processes"), ordered
  by the parent app's oom adj, least important first. Among processes of
  equally important parents the earliest-tracked die first; for a local
  kernel those are likely proot, the server and TofuDB.
- **Sustained CPU in the background.** Every power-check interval (5 minutes)
  the system measures apps whose process state is `HOME` or less important.
  A phantom process whose CPU share over the interval reaches the limit is
  killed together with all its sibling phantom processes **and the app
  process itself**. The limit is 25 % for the first two intervals, then
  10 %, then 2 %, measured from when the app became unimportant
  ([`ActivityManagerService.java`](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-12.0.0_r1/services/core/java/com/android/server/am/ActivityManagerService.java)
  `checkExcessivePowerUsage`; limits in `ActivityManagerConstants.java`).

**What a foreground service changes.** The kernel's foreground service keeps
the app at `PROCESS_STATE_FOREGROUND_SERVICE` (4), more important than
`PROCESS_STATE_HOME` (14)
([`ProcessStateEnum.aidl`](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-12.0.0_r1/core/java/android/app/ProcessStateEnum.aidl)).
- The CPU rule therefore does not apply while the service runs.
- The cap still applies. The service only moves the app's children later in
  the kill order. If the device total exceeds the cap, or the kernel alone
  runs more than 32 children, the surplus dies anyway.

**User-side switches** (none is available to the app itself):
- **Android 14 and later:** Developer options → "Disable child process
  restrictions"
  ([`PhantomProcessPreferenceController.java`](https://android.googlesource.com/platform/packages/apps/Settings/+/refs/tags/android-14.0.0_r1/src/com/android/settings/development/PhantomProcessPreferenceController.java),
  title in [`strings.xml`](https://android.googlesource.com/platform/packages/apps/Settings/+/refs/tags/android-14.0.0_r1/res/values/strings.xml)).
  It sets the feature flag `settings_enable_monitor_phantom_procs` to false,
  which turns off both rules
  ([`PhantomProcessList.java` @14](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-14.0.0_r1/services/core/java/com/android/server/am/PhantomProcessList.java)
  `trimPhantomProcessesIfNecessary`,
  [`ActivityManagerService.java` @14](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-14.0.0_r1/services/core/java/com/android/server/am/ActivityManagerService.java)
  `checkExcessivePowerUsage`). Turning developer options off turns the
  restrictions back on. Android 13's Settings has no such controller.
- **Android 12 and 13:** raise the cap over ADB with
  `adb shell device_config put activity_manager max_phantom_processes 2147483647`.
  `adb shell device_config set_sync_disabled_for_tests persistent` keeps a
  server-side config sync from overwriting it
  ([`DeviceConfigService.java`](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-12.0.0_r1/packages/SettingsProvider/src/com/android/providers/settings/DeviceConfigService.java)
  help text). This raises the cap only; the CPU rule remains.
- **Android 12L and 13** also have the feature flag without a settings
  screen. `FeatureFlagUtils` reads `Settings.Global` first, so
  `adb shell settings put global settings_enable_monitor_phantom_procs false`
  turns off both rules there
  ([`FeatureFlagUtils.java` @14](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-14.0.0_r1/core/java/android/util/FeatureFlagUtils.java),
  the flag and the same gate since
  [`android-12.1.0_r1`](https://android.googlesource.com/platform/frameworks/base/+/refs/tags/android-12.1.0_r1/services/core/java/com/android/server/am/PhantomProcessList.java)).
  Android 12.0 has no flag.

No mitigation is built yet. S1 item 7 measures whether a real workload hits
the cap.

## Kernel kinds

| Kind | What it is | Transport |
|---|---|---|
| LOCAL | this phone's userland running `local_kernel.py` | `http://127.0.0.1:<port>/`, the install secret as the `tofu_session` cookie ([`ANDROID_LOCAL_KERNEL.md`](ANDROID_LOCAL_KERNEL.md)) |
| URL | today's server profiles, with their auth types (code-server password, interactive SSO, none) | the server's own URL |
| SSH | a Tofu server reached through an SSH tunnel the phone opens | a forward on `127.0.0.2:<port>` to the remote `127.0.0.1:<port>` |

- **One in front.** Many kernels can be configured; exactly one is shown.
  LOCAL may keep running in the background under its foreground service
  while another kernel is in front, so its schedules continue. Conversations
  belong to their kernel, and switching changes the WebView origin (DSH Go
  does the same, analysis §5.4).
- **The Kernels screen** lists "This phone" first (not installed, stopped,
  starting, running, safe mode or failed), then URL and SSH kernels. The QR
  scanner reads `tofu://kernel?url=…`; a QR code never carries a secret.
- **SSH.** The phone dials out, so it works through NAT (analysis §5.1). An
  Ed25519 key generated in the app or a password authenticates. On first
  connect the user confirms the host key's SHA256 fingerprint, which is then
  pinned; a mismatch is a hard stop. The kernel attaches to an already
  running Tofu (default port 15000). Over the tunnel the remote sees a
  loopback peer: an open-mode remote grants its synthetic local admin, the
  same trust as the SSH user's own browser on that host; a private-mode
  remote needs an API key, kept in the vault per profile.
- **Reconnection.** URL: the existing health probe and re-authentication.
  SSH: reconnect on network change; the forward port stays stable, and the
  SPA retries in-flight calls. LOCAL: the service supervises the process, and
  the WebView reloads after a restart.
- **Compatibility.** The `/api/v4/meta` gate (`apiMajor`, `minAndroidBuild`)
  runs for every kind, LOCAL included, where it catches a stale payload after
  a failed update. LOCAL's Tofu version is the APK's; self-update is refused.

### Deferred: device-based kernels and a relay

- **Devices (R2).** Tofu devices dial the server and poll it with an
  `agents:bridge` credential ([`modules/remote_execution.md`](modules/remote_execution.md)).
  A loopback-only phone kernel is unreachable to them by design, and reaching
  a phone on cellular would need LAN exposure or a relay, which breaks the
  local kernel's loopback-only rule. A local kernel therefore refuses device
  pairing ([`ANDROID_LOCAL_KERNEL.md`](ANDROID_LOCAL_KERNEL.md)).
- **A relay.** DSH Go's relay carrier (both ends dial a relay, an
  end-to-end-encrypted tunnel, account sign-in, an endpoint allow-list) is
  its answer to reaching a phone or desktop across NAT (analysis §5.2). The
  SSH kind covers remote servers without one, because the phone dials out.
  A relay adds an always-on service and a second authorization surface, and
  DSH Go's own cross-NAT paths are unverified on real devices.

## Capability snapshot (direction)

- **Problem.** `ToolSpec.gate` and `exposure_gate` exist
  (`lib/tools/registry/_spec.py`), but each tool builder decides environment
  availability itself. On a phone or a minimal remote, tools fail only when
  called.
- **Decision: one choke point on the server**, an environment-capability
  snapshot computed once at kernel boot. It probes the POSIX shell, git,
  python, uv/uvx, node/npx, a Playwright browser, whether devices can reach
  the kernel, whether LAN discovery is allowed, and local-kernel mode.
  - It changes only on an explicit refresh event, such as a package install
    by a tool or a user re-scan, so the tool set is stable within a boot and
    prompt caches stay valid (DSH Go's fixed phone-tool catalog, analysis §6).
  - Tool families declare the capabilities they need, and `exposure_gate`
    consults the snapshot. An unavailable capability returns an actionable
    error, for example "run `apt install nodejs` in this phone's userland"
    or "use an SSH kernel".
  - There is no second registry; tools, skills and MCP stay the only ones.
  - `/api/v1/capabilities` gains an `environment` section from the same
    snapshot. The app reads it from the active kernel to adapt native
    affordances.
- **Approvals.** Write approval, `ask_human` and the approval delegate already
  render in the SPA. While the app is in the background a gate can time out,
  so the app watches the active kernel's pending human gates and posts a
  notification that deep-links to the conversation; the decision is still
  made in the SPA. Notifications are scoped to their kernel.
- **Alternatives considered.** Per-tool environment checks (today's
  behaviour) fail late and vary the tool list; a phone-side capability
  registry duplicates the server's registries.

## Capability snapshot

The server half is built: `lib/tools/environment_capabilities.py`, decision
record in [modules/tools_execution.md](modules/tools_execution.md#environment-capabilities).
- **What the app reads.** `GET /api/v4/capabilities` (public) carries
  `environment: {revision, local_kernel, capabilities}`. Each of `shell`,
  `git`, `python`, `uv`, `node`, `browser`, `devices` and `lan_discovery` is
  `{available, reason}`; `reason` is `not_installed`, `local_kernel`,
  `loopback_bind`, `disabled` or null. No host paths. A local kernel reports
  `devices` and `lan_discovery` unavailable with reason `local_kernel`, so
  the app hides device pairing and LAN discovery for it.
- **When it changes.** One presence-only probe per boot, again after a model
  shell command finishes (the way packages get installed), and on the
  authenticated re-scan `POST /api/v4/capabilities/environment/refresh`.
  Equal revisions mean equal tool exposure; a change applies from the next
  turn, never to a running one.
- **What the agent sees.** Tools declare needs per tool
  (`ToolSpec.environment_requirements_by_name`): `run_command` needs
  `shell`, the integration checkpoint tools `git`, `browser_preview_page`
  `browser`, the device tools `devices`, and the motion-video check and
  render `node` and `browser`. An unmet tool is not exposed and is not
  callable; a call returns the fix, for example "In the phone's userland run
  `apt install git` (run_command can do it), or switch to an SSH or URL
  kernel."

## Security rules

1. **Local kernel auth** reuses the existing boundary in private mode, with
   default deny, a Host fence, no listener off loopback, no self-update and
   no device pairing. The exact contract is
   [`ANDROID_LOCAL_KERNEL.md`](ANDROID_LOCAL_KERNEL.md).
2. **The install secret:** 256 bits from SecureRandom, once per install,
   AES-GCM-wrapped by a non-exportable AndroidKeyStore key, ciphertext in
   app-private files. Passed to the kernel on stdin at each launch; never in
   argv, URLs, Room or saved state. The WebView receives it as the
   `tofu_session` cookie through CookieBridge, never as `/?token=`.
3. **Default deny everywhere:** every route and upgrade without a credential
   gets 401 outside a pinned public list, enforced by a route-enumeration
   test (DSH Go hit unauthenticated plugin routes on loopback, analysis §7).
4. **Cookie isolation between kernels.** Cookies are scoped by host and
   ignore the port. LOCAL uses `127.0.0.1`, SSH forwards use `127.0.0.2`
   (Linux loopback /8, to be confirmed on devices), and URL kernels use their
   own hosts, so the local secret is never sent to a remote kernel.
   CookieBridge's host-scoped expiry on switch stays. The app enforces it: no
   URL kernel may use 127.0.0.1 in any spelling, and the WebView requests
   127.0.0.1 only as the front LOCAL kernel's origin, so no page can hand the
   cookie to another listener on the phone; the cookie lives only while the
   kernel is ready on its port
   ([`android/docs/SUPERVISOR_DESIGN.md`](../android/docs/SUPERVISOR_DESIGN.md) K7).
   All SSH forwards share `127.0.0.2`, so a cookie one SSH kernel's Tofu sets
   is also sent to every other SSH kernel.
5. **Remote auth.** URL kernels are unchanged. SSH keys are Ed25519 with the
   private key Keystore-wrapped, or a password in the vault; host keys are
   pinned on first use. Private-mode remotes get an API key per profile. No
   secret goes into a QR code.
6. **Same uid.** Everything in the userland runs as the app's uid; agent
   tools can read the kernel's data, the same trust as on desktop. The app
   requests neither MANAGE_EXTERNAL_STORAGE nor accessibility.
7. **Data and backups.** Everything lives in app-private credential-encrypted
   storage. `allowBackup="false"` stays, and `dataExtractionRules` exclude
   everything from cloud backup and device transfer. Uninstall wipes the
   kernel.
8. **Release signing.** Release APKs are signed with the committed debug
   keystore (`android/app/build.gradle.kts`), so anyone can sign an update
   Android accepts. A kernel-bearing release must use a secret release key.
9. **Supply chain.** Runtime artifacts are SHA-256-pinned at build time.
   Licence notices and a source offer cover GPL-2.0 proot, LGPL talloc and
   the Ubuntu packages.

## Runtime layout and lifecycle

| Path (app-private) | Holds |
|---|---|
| `files/kernel/rootfs` | the persistent Ubuntu userland; apt installs survive |
| `files/kernel/payload/<version>`, `current` | the replaceable Tofu payload |
| `files/kernel/data` | Tofu's data directory (`--data-dir`), bind-mounted |
| `files/kernel/home` | the guest user's home (`$HOME`): what other programs write there |
| `cache/kernel/tmp` | `PROOT_TMP_DIR` |

- **Write surface.** The payload is immutable: an update replaces it, so it
  holds no state. Everything Tofu writes derives from the runtime base that
  `--data-dir` selects ([writable path selection](modules/infra_runtime.md#writable-path-selection)):
  the database, config, logs and uploads, and the reconstructible caches,
  such as the storage-boundary scan cache and the uv, npm and pip caches and
  uv tools in `data/mcp-cache`. Specs:
  `tests/runtime_paths/test_runtime_write_surface.py`.
- **Shipping.** The payload and the base rootfs ship in APK assets,
  SHA-256-pinned and verified on extraction.
- **Updates.** An app update extracts the new payload beside the old one and
  switches `current` only after the new kernel answers `/api/v4/meta`; the
  previous payload is kept. The base rootfs is replaced only when its pinned
  version changes, which loses apt installs.
- **Foreground service.** Type `specialUse` with a subtype describing the
  local server, as DSH Go does (analysis §3): the other types describe
  bounded work, and a server has no natural end. An ongoing notification
  offers Open and Stop; approvals use a separate high-importance channel.
- **Start and stop.** The kernel starts on demand from the foreground only,
  because apps targeting 31+ cannot start a foreground service from the
  background. Stop comes from the notification; leaving LOCAL stops it once no
  task or schedule is active, unless the user keeps it running.
- **Crashes.** The launcher restarts a crashed kernel within the restart
  budget of the launch contract and enters safe mode (no MCP autostart, no
  plugins) after repeated boot failures. The last-known-good payload is kept.
- **Doze.** The foreground service keeps the process alive. A partial wakelock
  is held only while the kernel reports running tasks. Android 15 refuses
  network access outside a valid process lifecycle, so kernel networking runs
  only under the service. The battery-optimisation exemption is offered only
  through a user-opened settings link.
- **WebView.** It loads `http://127.0.0.1:<port>/`; cleartext to loopback is
  already allowed by the network security config.
- **App owners.** `Profile` gains a `kind` (URL, LOCAL or SSH) and SSH
  endpoint fields, with secrets never in Room (database version 3 with an
  exported schema). A `KernelController` per kind provides `ensureRunning`,
  `stop` and a state flow, and session activation branches on the kind. A
  `runtime` package holds the local service, the installer, the kernel
  process (`ProcessBuilder` on `nativeLibraryDir/libproot.so` with bind
  mounts) and the secret.
