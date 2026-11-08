# DSH Go (DeepSeek Harness on Android) — Architecture Learnings for Tofu Android

Sources analyzed (static reading only; no third-party binary was run and no APK was installed or unpacked):

- **DSH Go**: `ccch1mneyyy/dsh-Go` at `main` `20447ce63b` (2026-11-04T19:36Z). The latest release, `v1.0.10` (tag commit `b4f2d24055`, published 2026-11-03), is 217 commits earlier. Eleven cited files changed after the tag: `build.gradle.kts` (a debug-signing block only; `minSdk`, `targetSdk` and `useLegacyPackaging` at `build.gradle.kts:41,44,76` are unchanged), `DshProcess.kt`, `LastKnownGood.kt`, `PhoneControl.kt`, `hosts_state.dart`, `connection_state.dart`, `chat_state.dart`, `dsh_client`'s `connection.dart`, `tunnel_carrier.dart` (allow-list entries unchanged) and the `01-chat`/`10-settings` guide pages. Line numbers in those files describe `main`, not necessarily the shipped APK; `AndroidManifest.xml` and every other cited file are identical at the tag. Unprefixed `path:line` citations refer to this commit, and `K/` abbreviates `app/android/app/src/main/kotlin/io/github/ccch1mneyyy/dsh_app/`.
- **DSHR**: `liguobao/ds-harness-remote` `main` `219db1357b` (2026-11-04), `README.md` blob `e761be256b`. This is the remote protocol that DSH Go implements in Dart.
- **DSH**: `deepseek-ai/deepseek-harness` `master` `fcc0ad4621` (2026-11-04).
- **Android platform pages** (fetched): [Android 10 behavior changes](https://developer.android.com/about/versions/10/behavior-changes-10), [Android 14 foreground-service types](https://developer.android.com/about/versions/14/changes/fgs-types-required) and [Android 14 behavior changes](https://developer.android.com/about/versions/14/behavior-changes-all).
- **GitHub REST metadata** (repository, release, languages, repository search), read 2026-11-04.

Status: this is an external, point-in-time design input for the Tofu Android "full local harness + remote kernel" work, not an authority. When that design lands, move whatever is still valid into `android/README.md` / `android/docs/` and delete this file (see [`README.md`](README.md#documentation-lifecycle)).

---

## The one master idea

**DSH Go does not port the harness. It ships the official Node-based harness, unchanged apart from build-time patches, inside a proot Linux userland, and makes the Android app a native client of a "host".**

- The on-device engine, a computer reached over SSH and a computer reached through an end-to-end-encrypted relay are three implementations of one `HostConnection`.
- Chat, settings, approvals and plugins speak the same DSH HTTP/WebSocket API to whichever host is active.
- Android-specific engineering is concentrated in three places:
  - getting executables to run: `jniLibs` plus `targetSdk 28`;
  - keeping the server alive: a `specialUse` foreground service;
  - fencing the shared loopback: a route guard, cookie auth and a unix-socket bridge.

For Tofu the transferable shape is **one host abstraction with three carriers** (on-device loopback, SSH tunnel, relay). The Tofu server stays the harness and the Tofu web client stays the UI. Neither is re-implemented in Kotlin.

---

## 1. Identity

| Field | Value | Source |
|---|---|---|
| Repository | `github.com/ccch1mneyyy/dsh-Go`. Description: "dsh-go — DeepSeek Harness 装进口袋：手机本机运行，也能遥控你的电脑。" (DSH in your pocket: runs on the phone and can also remote-control your computer) | GitHub repo API |
| License | AGPL-3.0 | GitHub repo API |
| Created / last push | Created 2026-10-25, last pushed 2026-11-04. 9,390 stars and 431 forks when read; not a fork | GitHub repo API |
| Maintainer | `ccch1mneyyy`, the sole listed contributor; all git author identities resolve to that account | GitHub contributors API, `git shortlog` |
| Latest release | `v1.0.10`, 2026-11-03. Assets: `dsh-go-1.0.10-arm64.apk` (192,768,115 B), `SHA256SUMS`, `update.json` | GitHub releases API |
| App id / version | `io.github.ccch1mneyyy.dsh_app`, version `1.0.10+1` | `app/android/app/build.gradle.kts:39`, `app/pubspec.yaml:23` |
| Languages | Dart 20.0 MB, JavaScript 2.4 MB, Kotlin 2.3 MB, Python 0.55 MB. No Go code: "Go" is branding | GitHub languages API |
| Relation to DSH | A community **wrapper/distribution**: it bundles the official `@deepseek-ai/dsh` npm package (pinned `0.2.1-rc.4`) with build-time patches and adds a native Flutter client. It is not a fork, not a Go rewrite and not upstream-endorsed: DSH `master` README/docs never mention it | `runtime/build-runtime.sh:40`, `runtime/patches/`, `docs/architecture.md:9-12`; grep of DSH `fcc0ad4621` |
| Relation to DSHR | Implements the ds-harness-remote client protocol natively in Dart, with the MIT notice vendored. DSHR lists DSH Go as a supported client and says DSH Go can also act as a Host | `packages/dsh_remote/README.md:3`, `THIRD_PARTY_NOTICES.md:120-128`; DSHR `README.md:80,117-119,250` |

Other candidates found by GitHub repository search for `dsh-go`, `dsh go android`, `deepseek harness android`, `deepseek-harness mobile` and `dsh 安卓` (2026-11-04). Architecture was inspected only for the match.

| Repository | License | Self-description | Match? |
|---|---|---|---|
| `ccch1mneyyy/dsh-Go` | AGPL-3.0 | Runs DSH on the phone and also remote-controls a computer | **Yes.** It has both properties the request describes; analysed below |
| `xiazhi88/dshgo` | MIT | "DSH Go —— 在手机/平板上用 DeepSeek Harness。Android App + 让电脑可达的 DSH 插件" (an Android app plus a DSH plugin that makes your computer reachable); 2026-09, 18★ | Same name, earlier and remote-focused; not inspected |
| `DSH-APP/DSHA` | MIT | "免 ROOT 免 Termux，在手机上跑 DeepSeek Harness。完整 Ubuntu 环境 + proroot 零 ptrace 开销" (DSH on a phone without root or Termux, in a full Ubuntu userland); 2026-08, about 3.0k★ | The same on-device idea under another name, on-device only by its description; not inspected |
| `mionio233-collab/dsharness-go` | AGPL-3.0 | "DSHarness Go", a native Android client that also runs on-device models; 7★ | A different project; not inspected |
| `lxsfful/dsh-go`, `Mrsirlila/dsh-gocket`, `tierry-vcode/dsh-Go-ptbr` | AGPL-3.0 | Derivatives of DSH Go (a GitHub fork created 2026-11-03; a merge with dsh-pocket; a pt-BR translation) | Derivatives |
| `ccch1mneyyy/dsh-go-market` | AGPL-3.0 | DSH Go plugin catalog with pinned versions and risk checks | Companion repository |
| `liguobao/ds-harness-remote` | MIT (`package.json`) | End-to-end-encrypted P2P remote access to a local DSH through a Host plugin; ships its own remote-only Android app | The remote protocol DSH Go uses |
| `jackwener/DSH-Android`, `kelai141/dsh-mobile-apk`, `Soodok/Deepseek-Harness-Local-Android`, `shaobeichen/dsh-pocket` | Apache-2.0, MIT, MIT, GPL-2.0 | Other DSH-on-Android or phone-to-PC projects | Not "DSH Go"; not inspected |

---

## 2. Architecture

```
Android app io.github.ccch1mneyyy.dsh_app   (minSdk 26, targetSdk 28; release ships arm64-v8a)
┌───────────────────────────────────────────────────────────────────────────────┐
│ Flutter UI (Dart, Riverpod): native chat / settings / plugins / hosts screens │
│   └ WebView fallback: official DSH Web at http://127.0.0.1:<port>/?token=…    │
│ dsh_client (pure Dart, no Flutter): DSH HTTP + WebSocket API, cookie, no Origin│
│ HostConnection — exactly one active — kind ∈ {local, ssh, relay}              │
│ Kotlin layer: RuntimeInstaller · DshProcess · DshService (FGS specialUse)     │
│               PhoneControl (MCP server · SafetyGate · accessibility)          │
└──────┬──────────────────────────────┬─────────────────────────┬───────────────┘
       │ local                        │ ssh                     │ relay
       ▼                              ▼                         ▼
 proot (lib*.so in nativeLibraryDir)  dartssh2 → remote sshd    dsh_remote (pure Dart)
 Ubuntu 24.04 rootfs in app files     host-key pinned on first  REST /api/v1 + control WS
   Node 24 + @deepseek-ai/dsh         trust; phone              → DSHR Server: accounts,
   `dsh web --host 127.0.0.1          127.0.0.1:<eph> ⇄ remote    device directory,
     --port 3080 --no-open`           127.0.0.1:<port>            P2P signalling, relay
   + Python 3.12, git, pnpm           managed `dsh web` under   Noise IK end to end;
   + build-time route-guard patch     nohup, outlives SSH       WebRTC LAN → P2P → TURN,
   + phone relay.cjs ── abstract unix socket ──► Kotlin MCP     else WebSocket relay
       (SO_PEERCRED + per-launch token)                         → Host: DSH + Remote plugin
```

- **Layers.**
  - Flutter UI with Riverpod.
  - A Kotlin layer that unpacks the runtime, manages the proot process and runs the foreground service.
  - A pure-Dart `dsh_client` with no Flutter dependency, shared by local and remote connections.
  - The local runtime: Ubuntu 24.04 + Node + `@deepseek-ai/dsh` under proot, bundled in the app, needing no root and no Termux.

  Source: `docs/architecture.md:9-12`. The official DSH Web stays available as a WebView fallback (`docs/architecture.md:31`).
- **Host abstraction.**
  - `HostKind { local, ssh, relay }` (`app/lib/state/hosts_state.dart:25-30`).
  - `HostConnection { client, isLocal, kind, hostId, name, workingDirectory }` (`app/lib/state/connection_state.dart:20-30`).
  - Both remote modes attach to the same `HostConnection`, and a drawer switches between this phone, a PC and a server (`docs/roadmap.md:131`). The relay client exposes the same `DshClient` API as the local engine (`packages/dsh_remote/lib/dsh_remote.dart:1-20`).
- **Authority.**
  - The engine owns sessions, tools, plugins and settings.
  - App features are read from the active host's client and recomputed on host events (`app/lib/state/features_state.dart:8-29`).

---

## 3. Runtime packaging on Android

| Concern | DSH Go's answer | Evidence |
|---|---|---|
| Language/runtime | Flutter (Dart) UI plus a Kotlin plugin. The harness is the official Node package inside proot Ubuntu. Not gomobile, not Node embedded in ART, not the Termux app | `docs/architecture.md:9-12`; `runtime/build-runtime.sh:40-47` |
| Pinned runtime | DSH 0.2.1-rc.4, Node 24.22.1, pnpm 11.28.5, Ubuntu 24.04.5, proot 5.1.107.98, Python 3.12.15 (python-build-standalone), plus git, ca-certificates and tzdata. Downloads are sha256-pinned; tar mtimes are fixed | `runtime/build-runtime.sh:40-80` |
| Exec despite W^X | Two mechanisms. (1) proot and its helper binaries ship as `lib*.so` in `jniLibs` with `useLegacyPackaging = true`, so the installer extracts them to `nativeLibraryDir`, where exec is allowed. (2) `targetSdk = 28`, so binaries *inside the rootfs* in writable app storage stay executable. Android 10 forbids `execve()` on app-home files only for apps targeting it, and recommends loading only binary code embedded in the APK | `app/android/app/build.gradle.kts:40-44,72-79`; `docs/architecture.md:78-79`; Android 10 page |
| Direct launch | Optional explicit glibc-loader launch without proot, falling back to proot on failure. Safe mode always uses proot | `K/runtime/DirectLaunch.kt:11-30`; `K/runtime/DshProcess.kt:21-24,62` |
| Background | `DshService` is a `specialUse` foreground service with subtype "Runs the user's local DeepSeek Harness agent server", a wake lock and a network callback; the app also requests a battery-optimisation exemption. The guide tells users to keep the engine notification and lock the app in Recents, and admits Android may still stop the engine | `app/android/app/src/main/AndroidManifest.xml:224-232`; `K/runtime/DshService.kt:22-37`; `docs/guide/en/11-faq.md:20-27` |
| Crash handling | A safe-mode rescue profile. After an external kill, at most three restarts (5 s, 15 s, 45 s) within ten minutes. A last-known-good snapshot of enabled bundles, installed packages and the settings layer | `K/runtime/SafeMode.kt:11-33`; `K/runtime/LastKnownGood.kt:7-22` |
| SDK / ABIs | `minSdk 26` (for `java.nio.file` symlinks while unpacking the rootfs), `targetSdk 28`. Two flavors: arm64 (`arm64-v8a`) and x64 (`x86_64`, for emulators), each carrying only its own runtime and `jniLibs`. Releases publish arm64 only | `build.gradle.kts:40-69`; GitHub releases API |
| Size | 192,768,115 B arm64 APK, with the runtime bundled in assets | GitHub releases API; `build.gradle.kts:61-69` |
| Permissions | INTERNET, ACCESS_NETWORK_STATE, FOREGROUND_SERVICE (+ `_SPECIAL_USE`), POST_NOTIFICATIONS, POST_PROMOTED_NOTIFICATIONS, WAKE_LOCK, RECEIVE_BOOT_COMPLETED, REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, RECORD_AUDIO, SET_ALARM, WRITE_SECURE_SETTINGS. READ/WRITE_EXTERNAL_STORAGE and MANAGE_EXTERNAL_STORAGE serve an optional "phone files" mode that bind-mounts shared storage at `/sdcard` inside the Linux environment. Optional ones: Shizuku `API_V23` for the default-off "do not disturb" mode (a shell-uid channel), SYSTEM_ALERT_WINDOW for the pet overlay, REQUEST_INSTALL_PACKAGES for in-app update. READ_PHONE_STATE, BLUETOOTH and MODIFY_AUDIO_SETTINGS, merged in by libraries, are removed. An accessibility service reads and drives the screen for phone control. `allowBackup="false"` (the rootfs holds plaintext credentials) and `requestLegacyExternalStorage` are set | `AndroidManifest.xml:4-57,244-256` |

Platform constraints:

- The Android 14 foreground-service-type requirement applies to apps that target Android 14. A `specialUse` service must declare a `PROPERTY_SPECIAL_USE_FGS_SUBTYPE` whose free-form text explains the need to reviewers (fgs-types-required page). DSH Go already declares it while targeting 28.
- Android 14 refuses to install apps targeting below API 23, so target 28 still installs (behavior-changes-all page).
- DSH Go's own roadmap says it must raise `targetSdk` and adopt a W^X-compatible exec method before listing in Chinese app stores (`docs/roadmap.md:145`). Not done.

---

## 4. Local mode

The whole official DSH engine runs on the device, so local capabilities are DSH's capabilities:

| Capability | On the device | Evidence |
|---|---|---|
| Model providers | DeepSeek account or API key, ChatGPT and Claude subscription login, custom channels. Requests still leave the device for the provider | `docs/official-parity.md:77`; `SECURITY.en.md:32` |
| Tool loop, shell, files | DSH's own agent loop. Settings expose parallel tool calls, shell timeout and output cap. Commands run inside the proot Ubuntu with git, Python and Node present | `docs/official-parity.md:78-81`; `runtime/build-runtime.sh:50-52` |
| Plugins, skills, MCP | Plugin manager (install, enable, disable, remove) and market; `/skill-name` skills. MCP goes through DSH's `@deepseek-ai/dsh-mcp-client`, which DSH Go itself uses for phone tools. Bundled skills such as `dsh-go-support` are installed over a private channel reserved for the app's own skills | `docs/official-parity.md:24,87-90`; `K/phone/PhoneGuest.kt:7-19`; `docs/guide/en/11-faq.md:18`; `K/runtime/ManagedSkillInstaller.kt:81` |
| Compaction, plan, goal | DSH slash commands through `commands/execute` | `docs/official-parity.md:20` |
| Automations | Native UI, model-created schedules, separate user authorization | `docs/official-parity.md:64-67`; `docs/guide/en/01-chat.md:70-72` |
| Browser | Not offered. DSH's browser sidebar is desktop-only and off by default in the official web | `docs/official-parity.md:51` |
| Persistence | `$DSH_HOME` inside the rootfs, including the engine's `.credentials.yaml` | `packages/dsh_client/lib/src/connection.dart:16-22`; `app/lib/state/provider_keys.dart:9` |
| Phone control | `mcp__phone__*` tools (see §6) | `K/phone/PhoneToolCatalog.kt:3-24` |

Configuration:

- **Settings.** Native settings pages wrap DSH's settings API (`settings/mutate`): the default permission preset; agent presets (list, set default, view YAML; create, copy and edit through a Cordis plugin); providers and engine parameters (`docs/official-parity.md:73-81`; `docs/guide/en/10-settings.md:25`).
- **Launch overlays.** The engine launch stacks `--patch` overlays for message search and phone control on top of the profile (`K/runtime/DshProcess.kt:118,135`).
- **Engine lifecycle.** Install, reset or uninstall the runtime. "Start when DSH Go opens" is on by default, even while a remote host is active (`docs/guide/en/03-engine-hosts.md:21-44`).
- **Import/export.**
  - `.dshgo` chat files (replay and continue), a self-contained web page, or Markdown (`docs/guide/en/07-sharing.md:28,49-53`).
  - A redacted diagnostics bundle (`docs/guide/en/11-faq.md:16`).
  - Whole-profile backup/restore did not appear in the guide pages read (unverified).

---

## 5. Remote "kernel" mode

DSH Go calls the remote side a **host**: the "engine host" is either *This phone* or a *Remote host* (`docs/guide/en/03-engine-hosts.md:5-12`). A remote host is a complete DSH engine, and the app is only its client. There are two native carriers. An earlier stopgap was a WebView of a hosted site (`dsh.r2049.cn`) (`docs/roadmap.md:121-127`).

### 5.1 SSH host (developer path)

- **Direction.** The phone connects out to the host's SSH server through `dartssh2`. The host needs SSH enabled and DSH installed (`npm i -g @deepseek-ai/dsh`) (`docs/guide/en/03-engine-hosts.md:62-73`; `docs/roadmap.md:124`).
- **Authentication.**
  - Password, or private key plus passphrase, kept in the app's secret store and never in the persisted host config (`app/lib/remote/ssh/ssh_host_config.dart:3-23`).
  - The host key is shown as a `SHA256:<base64>` fingerprint, confirmed by the user and pinned on first trust (`app/lib/remote/ssh/ssh_transport.dart:30-49`; `app/lib/remote/ssh/ssh_link.dart:12-24`).
- **Kernel lifecycle.**
  - The app finds or starts a managed `dsh web --host 127.0.0.1 --port 0 --no-open` under `nohup`, with its state in `$HOME/.dsh-app-remote`.
  - It parses the launch token from the `dsh web: http://…/?token=` line.
  - The process outlives the SSH session and is reused on the next connect (`app/lib/remote/ssh/remote_bootstrap.dart:8-75`). Upstream `dsh web` prints exactly that URL line (DSH `apps/cli/tests/built-bin.e2e.ts:807`).
- **Data path.**
  - A phone-side listener on `127.0.0.1:<ephemeral>` is piped over SSH to the remote loopback port.
  - The listener and base URL stay stable across SSH reconnects; new sockets wait for the reconnect (`app/lib/remote/ssh/local_forward.dart:10-26`).
  - Above the tunnel, the client uses the same DSH HTTP/WebSocket API and cookie login as local mode (`packages/dsh_client/lib/src/connection.dart:1-22`). The remote DSH Web also opens in the app through the forward (`docs/official-parity.md:136`).
- **Verification status (self-reported).** End-to-end tests ran against a temporary local `sshd` and a development machine's real `sshd` running DSH 0.2.0-rc.2 (`docs/roadmap.md:127`).

### 5.2 DSH Remote (relay, end-to-end encrypted)

- **What the remote side is.** A computer running DSH with the ds-harness-remote **Host plugin**, signed in to a DSHR **Server** account. The Server handles accounts, the device directory, P2P coordination and the relay; a minimal self-hostable Server exists (DSHR `README.md:45-46,128-134,169-175`).
- **Direction.** Both the Host and the phone connect out. The Host needs no public port or IP (DSHR `README.md:45,175`).
- **Protocol stack** (`packages/dsh_remote/lib/dsh_remote.dart:1-20`; `lib/src/transport/transport.dart:1-25`; `lib/src/crypto/noise_ik.dart:1-18`; `lib/src/carrier/tunnel_carrier.dart:1-30`; DSHR `README.md:204-220`):
  1. REST `/api/v1/*`: providers, QR or password login, device registration, token refresh, Host directory.
  2. A control WebSocket: hello, connect, relay frames, ping/pong, WebRTC signalling.
  3. Transport: a WebRTC DataChannel `dsh` tried in the order LAN → P2P → TURN, falling back to the WebSocket relay.
  4. Encryption: `Noise_IK_25519_ChaChaPoly_SHA256`, with DSHF fragmentation and replay protection.
  5. RPC: `harness.remote.call`, `harness.remote.stream.*`, `harness.remote.transfer.*`.
- **Pairing and authentication.**
  - Sign in with the same provider and account as the Host: GitHub, Zhihu or Google OAuth, or a password account that needs an invite code.
  - Pick an online Host, check its identity fingerprint against the one the Host plugin shows, then tap **Trust and connect** (`docs/guide/en/03-engine-hosts.md:75-81`; DSHR `README.md:128-130,206`).
  - The Server authorizes each client↔Host connection (`connect.accepted`), and the Noise handshake always runs over the Server control channel (`lib/src/transport/transport.dart:1-16`).
  - Device identity, tokens and Host pins live in Keystore-backed secure storage (`lib/src/account/secret_store.dart:1-12`).
- **What the Server sees.** Session traffic is ciphertext. Routing metadata — account and device ownership, online status — is visible (DSHR `README.md:226-227,246`).
- **Exposure.** A fixed allow-list of 99 DSH API endpoints is mirrored on both ends. Per the client's comment, the Host refuses everything else and the client refuses it locally first; the Host plugin's source was not read (`lib/src/carrier/tunnel_carrier.dart:19-123`).
  - It is not conversation-only. Besides sessions, goals, subagents and user questions, it covers settings and credential changes (`settings/*`, `credentials/set|unset`), plugin enable/disable, schedules, workspace file reads and, when the Host enables them, remote terminals (`terminal/create|write|…`; `TERMINAL_DISABLED` when off). Plugin installation is not on it.
  - Limits over DSH Remote: image attachments only, no DSH Web, plugins can only be switched on or off, no DS Bot, no chat-log export (`docs/guide/en/03-engine-hosts.md:85`).
  - Uploads are chunked `workspace.upload.*`. The protocol ceiling is 1 GiB, the app budget 200 MiB, and the Host is authoritative (`packages/dsh_remote/README.md:134-138`).
- **Verification status (self-reported).**
  - Noise reproduces the upstream golden vectors.
  - The relay was tested end to end against a self-hosted upstream Server plus the Host plugin.
  - As of 10-23, login with real accounts on the online service was unverified.
  - As of 10-31, the WebRTC direct path was wired in and passed real P2P and relay-only probes on an Android 35 x86_64 emulator. Real devices, cross-NAT internet paths and real TURN were still unverified.

  Source: `docs/roadmap.md:127-129`.

### 5.3 Where each part runs

| Part | This phone | SSH host | DSH Remote |
|---|---|---|---|
| UI | Phone (Flutter; WebView fallback) | Phone (also the remote DSH Web through the forward) | Phone (no web UI) |
| Agent loop | Phone: DSH in proot | Remote DSH | Remote DSH |
| Tool execution | Phone rootfs, plus phone MCP through the bridge | Remote machine | Remote machine (allow-listed API only) |
| Sessions/storage | `$DSH_HOME` in the phone rootfs | Remote `$DSH_HOME` | Remote |
| Provider keys | Engine `.credentials.yaml`, plus a Keystore copy used to heal it | Remote only | Remote only |
| Connection secrets | Launch token → session cookie (a bearer credential; the code says to keep it in secure storage) | SSH password or key in secure storage; pinned host key | Device identity, tokens and Host pins in secure storage |

Sources: `app/lib/state/provider_keys.dart:6-15`; `packages/dsh_client/lib/src/connection.dart:16-22`; `app/lib/remote/ssh/ssh_host_config.dart:3-23`; `packages/dsh_remote/lib/src/account/secret_store.dart:1-12`.

### 5.4 Reconnection, offline behaviour, multiple kernels

- **One host at a time.** The app keeps one active host reference (`activeHostProvider`) and derives a single `activeConnectionProvider` from it (`app/lib/state/hosts_state.dart:300-313,330`; `app/lib/state/connection_state.dart:403-415`). Over DSH Remote, switching disconnects the previous Host (`docs/guide/en/03-engine-hosts.md:85`). The hosts list holds the phone, saved SSH hosts and relay devices (`app/lib/state/hosts_state.dart:25-30`). The last host can be reconnected on launch (`docs/guide/en/03-engine-hosts.md:46-60`).
- **Relay reconnects.**
  - Reconnects use DshMux backoff from 0.5 s to 10 s, a new connection and a new `$events` generation; streams are reopened and `watch()` is resynchronised from a snapshot (`isResync=true`).
  - Terminal failures stop reconnecting so that two devices never fight over a Host; the user calls `reconnect()` (`packages/dsh_remote/README.md:97-120`).
- **Host-scoped approvals.** A same-host reconnect keeps a pending answer; switching host retires it (`app/lib/state/chat_state.dart:976-994`).
- **Sessions belong to the host engine.** Switching hosts resets the open chat to a new conversation, because "the open conversation belongs to the old host" (`app/lib/state/hosts_state.dart:300-307`). No session migration between hosts was found (unverified negative).
- **No push service.** Alerts from a remote host need the app running and the connection online (`docs/guide/en/11-faq.md:20-27`).
- **The phone can also be the kernel.**
  - The local engine can keep running while a remote host is active (`docs/guide/en/03-engine-hosts.md:21`).
  - DSH Go can itself be a DSHR Host that a PC connects to (DSHR `README.md:119,250`).

---

## 6. Capability system

- **Registration.**
  - Capabilities are engine capabilities: DSH plugins/bundles, skills, MCP servers and agent presets. DSH Go adds no parallel registry.
  - App-specific capabilities enter as engine config overlays at launch. A `--patch` YAML inserts DSH's own MCP client as server `phone` at the root of the config tree, so every agent preset inherits it (`K/phone/PhoneGuest.kt:7-19`; `K/runtime/DshProcess.kt:118,135`).
  - Phone tools are not injected in safe mode or when phone control is off (`K/runtime/DshProcess.kt:135`; `K/phone/PhoneGuest.kt:18-19`).
- **What the model sees.**
  - A FIXED phone tool catalog, exposed as `mcp__phone__<name>`: `apps`, `open`, `intent`, `notify`, `clipboard`, `screen`, `collect`, `tap`, `type`, `scroll`, `key`, `tap_xy`, `handoff`.
  - The catalog never varies with permission state, because tool names, descriptions and schemas enter every request and any change invalidates the provider's KV cache.
  - A call that cannot run returns an actionable explanation, and a JVM test pins a hash of the catalog.

  Source: `K/phone/PhoneToolCatalog.kt:3-24`.
- **Discovery per host.**
  - UI features come from the active host's client and refresh on host events (`app/lib/state/features_state.dart:8-29`).
  - Native plugin UI needs the `dsh-go` bridge on the host. The phone engine has it built in, remote hosts usually do not, and it never works over DSH Remote (`docs/guide/en/08-plugins.md:58`).
  - The DSH Remote allow-list narrows the API further; for example, plugins can be switched on or off there but not installed (`packages/dsh_remote/lib/src/carrier/tunnel_carrier.dart:19-123`; `docs/guide/en/03-engine-hosts.md:85`).
- **Approval on mobile.**
  - Approval cards show the command, the files and the reason, with **Allow once**, **Decline** and **Allow for chat**. Allow for chat auto-approves ordinary approvals for the rest of the chat and resets when the app restarts.
  - Scheduled tasks need a separate authorization that shows the full instructions and trigger.

  Source: `docs/guide/en/01-chat.md:66-72`. On the phone engine, the "full access" preset is the app's own switch (`docs/official-parity.md:73`).
- **Phone safety gate.**
  - These hard rules hold unless the user lifts one:
    - secret fields (passwords, codes, payments) are never filled or logged;
    - system pages are read-only;
    - bank, broker and payment apps are neither read nor written by default;
    - WeChat writes need confirmation every time.
  - Everything below the hard rules only asks (`K/phone/SafetyGate.kt:49-243`).
  - Three confirmation modes: every time, this task, always (`docs/roadmap.md:140`).
- **Plugin supply chain.**
  - The market lists Official and Adapted plugins with pinned versions, risk flags, native features and a minimum app version.
  - The package digest is compared with npm registry metadata before install or update; a mismatch stops before the old version is removed.
  - The bytes the host actually downloads are not verified.

  Source: `docs/guide/en/08-plugins.md:60-64`.

---

## 7. Security

- **Local server exposure.**
  - The engine binds loopback (`--host 127.0.0.1 --port 3080`).
  - Each start prints a launch token on stdout. The app exchanges it for a signed cookie bound to the `host:port` authority, valid 30 days by default, whose secret lives in `$DSH_HOME/.credentials.yaml`.
  - The native client never sends `Origin` (`docs/architecture.md:84-86`; `packages/dsh_client/lib/src/connection.dart:1-34`).
  - Cleartext HTTP is permitted only to `127.0.0.1` and `localhost` (`app/android/app/src/main/res/xml/network_security_config.xml:1-8`).
- **Loopback is shared by every app.**
  - The problem: DSH's webserver only dispatches, and plugin-registered routes skipped authentication. 13 of 15 sampled plugins register routes, and one route spends the user's model quota. Browser pages can also send simple requests (CSRF, DNS rebinding).
  - The fix:
    - a build-time patch makes every matched route and WebSocket upgrade pass `requestRejection` (Host/Origin fence plus cookie or API credential);
    - the patch is pinned to the original file's sha256 per DSH version;
    - a build-time test enumerates registered routes and asserts 401 without a cookie.

    Source: `docs/design/plugin-seams.md:299-317`; `runtime/patches/route-guard.diff`.
  - The app cannot guard remote hosts (`docs/design/plugin-seams.md:315`).
- **Native bridge.**
  - An abstract-namespace unix socket with a random per-launch name. Every connection is checked with `SO_PEERCRED` and must open with a hello line carrying the per-launch token; after that it speaks MCP JSON-RPC.
  - A loopback TCP plus token fallback exists only on request and is weaker.
  - Same-uid code — anything in the rootfs — passes these checks by design. The real gate is the person's confirmation (`K/phone/PhoneBridge.kt:24-39`; `K/phone/PhoneControl.kt:228`).
- **Sandboxing.** There is none beyond the app uid. proot translates paths; it does not isolate. Agent tools run as the app uid with whatever the app was granted, including all-files access (`K/phone/PhoneBridge.kt:37-38`; `AndroidManifest.xml:24-28`).
- **Secrets.**
  - Local provider keys live in Keystore-backed storage, only to heal the engine's own store. Keys typed while a remote host is active go to that host and are never kept on the phone (`app/lib/state/provider_keys.dart:6-15`).
  - The engine's own store is plaintext inside the rootfs, so Android backup is disabled (`android:allowBackup="false"`); `dataExtractionRules` remains a TODO for targetSdk ≥ 31 (`AndroidManifest.xml:47-50`).
  - SSH secrets live in the secret store (`app/lib/remote/ssh/ssh_host_config.dart:3-23`); DSHR identity and tokens in secure storage (`packages/dsh_remote/lib/src/account/secret_store.dart:1-12`).
  - Diagnostics bundles exclude keys and tokens (`docs/guide/en/11-faq.md:16`).
- **Remote authentication.** SSH uses host keys pinned on first trust. DSHR uses account sign-in, device registration, fingerprint confirmation and Noise IK end-to-end encryption (§5).

---

## 8. Evidence table

| # | Claim | Source | Confidence |
|---|---|---|---|
| E1 | DSH Go is `ccch1mneyyy/dsh-Go`: AGPL-3.0, single maintainer, `v1.0.10` (2026-11-03), arm64 APK of 192,768,115 B | GitHub repo/release API, 2026-11-04 | High |
| E2 | Flutter/Dart UI, Kotlin native layer, pure-Dart client; no Go code | `docs/architecture.md:9-12`; GitHub languages API | High |
| E3 | Bundles the official `@deepseek-ai/dsh` 0.2.1-rc.4 on Node 24 inside proot Ubuntu 24.04, with build-time patches | `runtime/build-runtime.sh:40-47`; `runtime/patches/` | High |
| E4 | Not mentioned by upstream DSH README/docs | grep of DSH `fcc0ad4621` | High (negative search) |
| E5 | Executables ship as `lib*.so` in `jniLibs` with `useLegacyPackaging`, so they exec from `nativeLibraryDir` | `build.gradle.kts:72-79`; `docs/architecture.md:78` | High |
| E6 | `targetSdk 28` (same at `v1.0.10`) keeps rootfs binaries executable; `minSdk 26` | `build.gradle.kts:40-44`; tag `b4f2d24055` | High |
| E7 | Android 10 forbids `execve()` on app-home files for apps targeting it and recommends APK-embedded binaries | Android 10 behavior-changes page | High |
| E8 | The local engine runs `dsh web --host 127.0.0.1 --port 3080 --no-open`; its launch token is exchanged for a cookie | `docs/architecture.md:84-86`; `connection.dart:16-22`; DSH `apps/cli/tests/built-bin.e2e.ts:807` | High |
| E9 | A `specialUse` foreground service, wake lock and network callback keep the engine alive; Android may still kill it | `AndroidManifest.xml:224-232`; `K/runtime/DshService.kt:22-37`; `11-faq.md:20-27` | High |
| E10 | Cleartext traffic is allowed only to loopback | `network_security_config.xml:1-8` | High |
| E11 | Plugin routes were unauthenticated on the shared loopback; a sha256-pinned route-guard patch fixes this | `docs/design/plugin-seams.md:299-317`; `runtime/patches/route-guard.diff` | High for design and patch; the route-enumeration test was not run |
| E12 | One active `HostConnection`; host kinds are local, ssh and relay | `hosts_state.dart:25-30,300-330`; `connection_state.dart:20-30,403-415` | High |
| E13 | SSH host: host key pinned on first trust, managed remote `dsh web` on loopback under `nohup`, stable phone-side forward | `ssh_transport.dart:30-49`; `remote_bootstrap.dart:8-75`; `local_forward.dart:10-26` | High |
| E14 | The relay client uses REST `/api/v1`, a control WebSocket and WebRTC, with Noise IK and allow-listed RPC | `dsh_remote.dart:1-20`; `transport.dart:1-25`; `noise_ik.dart:1-18`; `tunnel_carrier.dart:1-30` | High for the code read; the cryptography was not audited |
| E15 | The Host connects out with no public port; the relay sees ciphertext plus routing metadata | DSHR `README.md:45,175,226-227,246` | Medium (upstream claim) |
| E16 | Pairing is same-account sign-in plus fingerprint confirmation | `03-engine-hosts.md:75-81`; DSHR `README.md:128-130,206` | High as documented; flow not run |
| E17 | Relay reconnects resynchronise from a snapshot; terminal failures stop reconnecting | `packages/dsh_remote/README.md:97-120` | Medium (documentation of code) |
| E18 | Phone tools form a fixed MCP catalog, `mcp__phone__*`, independent of permission state | `K/phone/PhoneToolCatalog.kt:3-24` | High |
| E19 | Bridge: abstract unix socket, `SO_PEERCRED`, per-launch token; same-uid code passes and confirmation is the gate | `K/phone/PhoneBridge.kt:24-39` | High |
| E20 | SafetyGate has hard rules and three confirmation modes | `K/phone/SafetyGate.kt:49-243`; `docs/roadmap.md:140` | High for the code; real-device testing is pending |
| E21 | Keys are in Keystore-backed storage; keys for a remote host are never cached on the phone | `provider_keys.dart:6-15`; `secret_store.dart:1-12`; `ssh_host_config.dart:3-23` | High |
| E22 | Approvals offer allow once, decline and allow for chat, and are scoped to the host | `01-chat.md:66-72`; `chat_state.dart:976-994` | High |
| E23 | Safe mode, bounded restarts and a last-known-good snapshot | `K/runtime/SafeMode.kt:11-33`; `K/runtime/LastKnownGood.kt:7-22` | High (code headers) |
| E24 | The plugin-market digest is checked against registry metadata only | `08-plugins.md:60-64` | High (documentation) |
| E25 | A parity ☑️ means code and tests, not real-device testing; online DSHR login was unverified (10-23); real devices, cross-NAT paths and real TURN were unverified (10-31) | `docs/official-parity.md:7`; `docs/roadmap.md:127-129` | High (self-reported) |
| E26 | DSH is MIT. DSHR is MIT via `package.json` and the vendored notice, but has no root LICENSE file | DSH `LICENSE`; GitHub API; `THIRD_PARTY_NOTICES.md:120-128` | Medium |
| E27 | DSH Go can act as a DSHR Host, so the phone can be the kernel for a PC | DSHR `README.md:119,250` | Medium (upstream claim) |
| E28 | Android 14 requires foreground-service types for apps targeting it; `specialUse` needs a subtype property explained for review | Android fgs-types-required page | High |
| E29 | The DSH Remote allow-list has 99 endpoints, including settings and credential changes and Host-enabled remote terminals, but not plugin installation | `tunnel_carrier.dart:19-123` | High for the client list; Host-side enforcement not read |
| E30 | Android backup is disabled because the rootfs holds plaintext credentials | `AndroidManifest.xml:47-50` | High |

---

## 9. Adoption candidates for Tofu Android

Tofu context:

- Tofu Android is a thin native WebView shell with multi-server profiles and per-profile credentials (`android/AGENTS.md:5`; `android/README.md:8-12`).
- The Tofu server is a Python application. `tofu_agent/` is the existing embeddable runtime that needs no application storage (`docs/README.md:22`).
- Whether the Tofu server itself can run on a phone is the server-portability lane's question. The ideas below are about the shape DSH Go shows to be workable.

| # | Idea | Why | Evidence |
|---|---|---|---|
| 1 | Turn the profile into a **host** with a kind — `remote-url` (today), `on-device`, `ssh` — and keep exactly one active connection. The WebView UI and the server stay unchanged | DSH Go gets local and remote from one client by treating every engine as a host behind one connection type: no second UI, no second harness | `hosts_state.dart:25-30`; `connection_state.dart:20-30`; `roadmap.md:131` |
| 2 | Build the on-device engine as proot plus a glibc rootfs plus the interpreter, shipped per ABI. Put executables in `jniLibs` as `lib*.so` (`useLegacyPackaging`) and start the engine from a `specialUse` foreground service. Settle the targetSdk/W^X strategy before writing code | This is a proven way to run an unmodified Linux userland (Node, Python 3.12, git) on stock Android without root or Termux. `targetSdk 28` is the debt DSH Go now carries | `build.gradle.kts:40-79`; `build-runtime.sh:40-52`; `AndroidManifest.xml:224-232`; `roadmap.md:145`; Android 10 page |
| 3 | Loopback server contract: bind 127.0.0.1 and hand a per-launch token to the app, which exchanges it for an authority-bound session cookie kept in the vault. Allow cleartext only to loopback. Put **every** route and WebSocket upgrade — including extension, MCP and plugin routes — behind authentication, enforced by a test that enumerates registered routes | Android loopback is reachable from every app and from browser pages. DSH Go found unauthenticated plugin routes that could spend model quota | `docs/architecture.md:84-86`; `connection.dart:16-22`; `network_security_config.xml:1-8`; `plugin-seams.md:301-317` |
| 4 | Build SSH kernel mode before any relay: pin the host key on first trust, start or reuse a managed server on the remote loopback (pid and state dir; it survives SSH drops), and keep the phone-side forward port stable across reconnects | Delivers "connect to a remote kernel" with no server-side infrastructure and no public port, and reuses the existing authenticated web client | `remote_bootstrap.dart:8-75`; `local_forward.dart:10-26`; `ssh_transport.dart:30-49`; `03-engine-hosts.md:62-73` |
| 5 | If NAT traversal without SSH becomes necessary: an outbound-only Host agent, a coordination/relay server, Noise IK end-to-end encryption, WebRTC LAN → P2P → TURN with WebSocket relay fallback, an endpoint allow-list enforced on both ends, and fingerprint confirmation | The relay operator never sees content and the Host opens no port. The allow-list makes the remote surface explicit and reviewable; DSHR's still includes credential changes and, when enabled, remote terminals, so it is not containment | DSHR `README.md:45-46,204-227`; `tunnel_carrier.dart:19-123`; `transport.dart:1-25` |
| 6 | Expose phone capabilities to the agent as a fixed MCP tool catalog served by the native layer over an abstract unix socket (`SO_PEERCRED` plus per-launch token), injected through config, with a native safety gate and human confirmation | Reuses the harness's existing MCP path instead of new machinery. Stable schemas keep prompt caches valid, and the native side holds the real authority | `PhoneToolCatalog.kt:3-24`; `PhoneBridge.kt:24-39`; `PhoneGuest.kt:7-19`; `SafetyGate.kt:49-243` |
| 7 | Let a per-host capability descriptor drive UI gating, and document each connection type's limits | Remote carriers legitimately expose less (DSH Remote: no web UI, no export). The UI must not offer what the active kernel cannot do | `features_state.dart:8-29`; `03-engine-hosts.md:85`; `08-plugins.md:58` |
| 8 | Scope secrets and approvals to the host identity: keys typed for a remote go only to that remote; pending approvals survive a same-host reconnect and are retired on a host switch | Prevents a key or an approval from reaching the wrong kernel | `provider_keys.dart:6-15`; `chat_state.dart:976-994` |
| 9 | Supervise the engine: a rescue profile without extensions, bounded restarts after external kills, a last-known-good snapshot of extensions and settings, and a redacted diagnostics bundle the agent itself can read | A phone engine fails in ways a server does not (OEM process killers, full storage, broken plugins), and users cannot SSH in to repair it | `SafeMode.kt:11-33`; `LastKnownGood.kt:7-22`; `11-faq.md:7-18` |
| 10 | Keep the protocol client a UI-free library shared by the on-device and remote carriers, testable off-device; make the carrier pluggable beneath the existing generated API client | DSH Go's pure-Dart `dsh_client` serves the local engine, SSH and relay unchanged, so every carrier gets the same API semantics and tests | `docs/architecture.md:11`; `dsh_remote.dart:1-20`; `connection.dart:1-3` |

Avoid:

- **Pinning `targetSdk 28`.** It blocks mainstream store distribution, and DSH Go lists the migration as open work (`docs/roadmap.md:145`). Design the exec path for API 29+ from the start. Which W^X-compatible loader works was not verified here.
- **Assuming loopback is private** (`docs/design/plugin-seams.md:303`).
- **Broad permissions in a first version.** MANAGE_EXTERNAL_STORAGE, WRITE_SECURE_SETTINGS, REQUEST_INSTALL_PACKAGES and accessibility automation (`AndroidManifest.xml:4-45,244-256`) widen review and abuse surface. Store-policy consequences were not verified here.
- **Treating proot as a sandbox** (`K/phone/PhoneBridge.kt:37-38`).
- **Copying code.** DSH Go is AGPL-3.0, so treat it as reference only unless the owner accepts AGPL obligations. The DSHR client logic is MIT, but GitHub shows no root LICENSE file.
- **A WebView of a hosted relay site as the remote mode.** That was DSH Go's own stopgap, since replaced by native carriers (`docs/roadmap.md:121-127`).
- **Bundling the whole runtime without measuring it.** DSH Go's arm64 APK, with Ubuntu, Node and Python bundled, is 193 MB. A Python plus native-storage Tofu runtime is unmeasured.

---

## 10. Open unknowns

- **Real-device behaviour.** In DSH Go's parity table ☑️ means code plus tests, not real-device testing, and phone control is still pending real-device testing (`docs/official-parity.md:7`; `docs/roadmap.md:140`).
- **Production DSHR service.**
  - DSH Go had not verified login with real accounts as of 10-23 (`docs/roadmap.md:127`).
  - The DSHR Server (`apps/server`) was not read.
  - The Noise implementation's correctness was not independently checked.
- **The shipped APK.** It was not downloaded or inspected. Manifest facts come from source at `main`; the packaging lines are unchanged at the `v1.0.10` tag.
- **Unmeasured costs.** Battery, memory, cold-start time and on-device rootfs size are not measured in any source read.
- **Auto-start at boot.** Unverified whether the engine starts at boot. A boot receiver re-arms `PrivilegedKeepJob` (`AndroidManifest.xml:284-286`).
- **W^X-compatible exec for targetSdk ≥ 29.** Neither DSH Go nor this analysis has a verified approach.
- **Not found.** Whole-profile backup/restore, and session migration between hosts.
- **Upstream route guard.** Unknown whether DSH will accept the route-guard proposal (`docs/design/plugin-seams.md:317`).
