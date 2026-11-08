# Kernels and the host supervisor

> **Status:** current state (2026-11-05). This is the authority for how the
> Android app models the Tofu servers it shows ("kernels"), how each kind is
> started and signed in, and how a URL kernel's server is started and stopped
> through the host supervisor. Code: `app/src/main/java/com/tofu/client/`.

A **kernel** is a Tofu server the app puts in front, in the WebView. The app
keeps many kernels and shows exactly one at a time; a kernel's conversations
live on that kernel, so switching kernels changes the WebView origin.

| Kind | What it is | Address | Lifecycle owner |
|---|---|---|---|
| `REMOTE_URL` | Today's server profiles, including gateway auth (code-server password, interactive SSO, none) | `Profile.baseUrl` | `UrlKernelController` (wraps `SupervisorClient`) |
| `LOCAL` | "This phone": the Tofu server running on the device | runtime, `http://127.0.0.1:<port>/` | `LocalKernelController` over `runtime/LocalKernel` (§5) |
| `SSH` | A remote Tofu reached through a tunnel the phone dials out | runtime, `http://127.0.0.2:<port>/` | `SshKernelController` (`ssh/`, §6) |

## 1. Data model (Room v3)

`data/Profile.kt`:

```kotlin
enum class KernelKind { REMOTE_URL, LOCAL, SSH }

data class SshEndpoint(
    val host: String,
    val port: Int = 22,                 // SshEndpoint.DEFAULT_PORT
    val user: String,
    val hostKeySha256: String? = null,  // public host-key fingerprint, pinned on first trust
    val remotePort: Int = 15000,        // SshEndpoint.DEFAULT_REMOTE_PORT, Tofu on the host
)

data class Profile(
    /* id, alias, instanceUuid, baseUrl, authType, cookieHost, lastUsedAt, projectPath */
    val kind: KernelKind = KernelKind.REMOTE_URL,   // column `kind`
    @Embedded(prefix = "ssh_") val ssh: SshEndpoint? = null,
)
```

- **Secrets never go in Room.** An SSH password or private key lives in the
  vault (`SecretVault`), keyed by the profile alias like every other secret.
  `hostKeySha256` is a public fingerprint, not a secret.
- **Only `REMOTE_URL` rows store an address.** LOCAL and SSH rows keep
  `baseUrl = ""`; their address is runtime state returned by their controller.
  The session works on `profile.boundTo(endpoint)` (address and cookie host
  replaced), which is never written back.
- **Migration.** `ProfileDatabase` is version 3. `MIGRATION_2_3` is additive:
  `kind TEXT NOT NULL DEFAULT 'REMOTE_URL'` plus nullable `ssh_host`,
  `ssh_port`, `ssh_user`, `ssh_host_key_sha256`, `ssh_remote_port`. Every
  existing row becomes a `REMOTE_URL` kernel with all other values intact. The
  app registers `ProfileDatabase.ALL_MIGRATIONS` (1→2→3); there is no
  destructive fallback, because dropping the table orphans alias-keyed secrets.
- **Exported schemas.** Room writes one JSON per version to
  `app/schemas/com.tofu.client.data.ProfileDatabase/` (KSP arg
  `room.schemaLocation`). `2.json` was generated from the shipped v2 entity
  (identity hash `995ed546811bf0f3997ab09ea054f0f5`), `3.json` from v3.
  Commit a new JSON with every version bump.

## 2. The controller seam

`session/KernelController.kt` (pure Kotlin, compiled by the pure-JVM tier):

```kotlin
interface KernelController {
    suspend fun ensureRunning(profile: Profile): KernelEndpoint
    suspend fun stop(profile: Profile)
    fun state(profile: Profile): StateFlow<KernelState>
    fun frontChanged(front: Profile) {}  // default: nothing
}
data class KernelEndpoint(val baseUrl: String, val cookieHost: String)
sealed interface KernelState {  // Stopped, Starting, Ready, Stopping, Failed(reason), SafeMode
    data object Stopped; data object Starting; data object Ready; data object Stopping
    data class Failed(val reason: String); data object SafeMode
}
class KernelUnavailableException(val reason: String) : Exception(reason)
fun Profile.boundTo(endpoint: KernelEndpoint): Profile
class KernelControllers(remoteUrl: KernelController, local: KernelController, ssh: KernelController) {
    fun of(kind: KernelKind): KernelController
    fun state(profile: Profile): StateFlow<KernelState>
    fun putInFront(front: Profile)  // every controller hears frontChanged once
}
class UnavailableKernelController(reason: String = "not available in this build yet") : KernelController
object KernelList { val THIS_PHONE: Profile; fun pinned(rows: List<Profile>): List<Profile> }
```

Contract every implementation keeps:

- `ensureRunning` is idempotent. It publishes `Starting`, returns once the
  kernel answers, and publishes `Ready` (or `SafeMode` when it came up
  degraded). It never returns a fake endpoint: on failure it publishes
  `Failed(reason)` and throws `KernelUnavailableException(reason)` with the
  same user-facing reason.
- `stop` stops what the app runs for the kernel (its process or tunnel) and
  publishes `Stopped`, or `Failed` if stopping failed, with `Stopping` while
  the stop drains; it returns once the kernel is stopped. It is a no-op for a
  kernel that is not running.
- `frontChanged(front)` tells every controller which kernel the WebView now
  shows; a controller whose kernel lives on only for the WebView may stop it
  once another kernel is in front.
- `state` is hot, always has a value, and returns the same flow for the same
  kernel on every call (the list collects it per card).
- `KernelEndpoint.cookieHost` keeps each kernel's session cookies apart:
  127.0.0.1 for LOCAL, 127.0.0.2 for SSH forwards, the server host for URL.
  Cookies are scoped by host, not port, so these hosts must differ.

`KernelControllers.of` is the only place a kind is mapped to its controller.
`MainActivity` wires
`UrlKernelController(SupervisorClient(...), login = session::login)` for
`REMOTE_URL`, `localKernelController(applicationContext, diag)` for `LOCAL`
(§5), and `SshKernelController.forApp(applicationContext, dao, secrets)` for
`SSH` (§6).

## 3. Activation, by kind

`SessionController.activate(profile)` bumps recency and re-reads the row, then:

- **`REMOTE_URL` is unchanged.** It signs in at the profile URL through
  `SessionManager.login` (gateway discovery, password replay or SSO hand-off,
  session-marker stamp, v4 meta gate) and never consults a controller.
- **`LOCAL` and `SSH`** call `kernels.of(kind).ensureRunning(row)`, then sign in
  at `row.boundTo(endpoint)`. `SessionManager.loginInternal` gives these kinds
  only the v4 meta gate (`ApiMetaGate`): no gateway handshake, no secret
  replay, no `cookie_host` stamp. Any session the kernel needs (for example the
  local kernel's `tofu_session` cookie) is the controller's job before it
  returns. A kernel that cannot start comes back as `LoginResult.Error(reason)`.
- `ProfileResult.persisted` is the row the WebView loads: the Room row for a
  URL kernel, the bound copy for LOCAL and SSH.
- When the WebView opens (`InteractiveSso.shouldOpenWebView`), that row is put
  in front: `kernels.putInFront(row)`. A kernel that failed to open does not
  move the front.

Other mutations: `addSshKernel(alias, endpoint, password)` stores an SSH kernel
without connecting (`AddResult.Saved`); a non-blank password (the SSH
account's) goes to the vault under the alias, never to Room.
`editSshKernel(current, alias, endpoint, newPassword)` moves the alias-keyed
secret on rename, replaces it when a new password is typed, and drops the
pinned host key when the host or port changes; the form can never set a pin.
`deleteProfile` stops a LOCAL or SSH kernel before removing its secret and row;
a URL server is someone else's and keeps running.

## 4. The Kernels list

`ui/ProfilesViewModel.kt` serves `kernelList` = `KernelList.pinned(rows)`:
"This phone" first (the stored LOCAL row, else the `THIS_PHONE` placeholder
with id 0, which is never persisted), then URL and SSH kernels by recency.
Onboarding still keys on the stored rows (`profiles`).

`ui/ProfileListScreen.kt` renders URL kernels with the existing server card
(supervisor chip and Start/Stop for managed profiles) and LOCAL/SSH kernels with
a card showing the controller's state and, for `Failed`, its reason. "This
phone" has no edit or delete. The header line "In front: <kernel>" shows the
last-opened kernel (`activeKernel`) with its state. "Add" offers *Server URL*
(the existing form) and *SSH tunnel* (`ui/SshKernelScreen.kt`: name, host, SSH
port, user, Tofu port, optional account password; this phone's public key with
*Copy public key*; the pinned key read-only; an edited kernel's last failure).
Saving an SSH kernel does not connect; tapping its card does. A first connect
to an unpinned host reopens the form with *Trust this host?* (§6). The "This
phone" card adds its own controls (§5).

Not built yet: a drawer switcher, and the kernel name and state inside the
WebView screen (`activeKernel` and `kernelState` are the hooks).

## 5. This phone (LOCAL)

`session/LocalKernelController.kt` adapts `runtime/LocalKernel`
([`README.md`](../README.md) "Local kernel") to §2 through a pure
`LocalKernelPort` (device: `AndroidLocalKernelPort`), so it runs on the plain
JVM. There is one local kernel: its state is keyed by kind, not by id (the
placeholder has id 0; a stored LOCAL row, which is optional, has another).

- **`ensureRunning`** refuses with a user-facing reason when the device has no
  `arm64-v8a` ABI, the build carries no runtime, or the runtime refuses a start
  outside the foreground. It waits for a draining stop (the runtime ignores a
  start while Stopping), starts, and awaits Ready, Failed, SafeMode or Stopped.
  It then sets up the session the launch contract defines
  ([`docs/ANDROID_LOCAL_KERNEL.md`](../../docs/ANDROID_LOCAL_KERNEL.md) §6):
  `LocalKernelCookie.install` writes the HttpOnly `tofu_session=<install
  secret>` cookie for `http://127.0.0.1:<port>` only (the secret is read off
  the main thread), and the existing `HealthProbe` must answer `TOFU` for
  `{endpoint}/api/health` with that cookie. The session layer then runs
  `ApiMetaGate`, so both gates pass before the first page load. The cookie is
  withdrawn (with that origin's web storage) as soon as the kernel leaves Ready
  on that port: a WebView still showing it keeps requesting the port, which any
  app may bind once the kernel has exited.
- **States.** Stopped, Starting, Ready and Stopping map one to one. The
  runtime's `Failed(cause)` and its `SafeMode` (not restarted after 5 crashes
  within 120 s, so a stopped kernel) both become `Failed(reason)` in the
  runtime's own wording. This controller never reports `SafeMode`, which
  promises a running kernel. A refusal of the adapter itself (not in the
  foreground, health check) is `Failed(reason)` until the runtime's state moves
  on. `stop` asks the runtime to stop and returns once it reports Stopped.
- **One credential host.** Cookies ignore the port, so 127.0.0.1 belongs to
  This phone alone (`LocalKernelCookie`): the cookie is written for no other
  host; a URL kernel on 127.0.0.1, in any IPv4 spelling the WebView accepts
  (`127.1`, `2130706433`, `0x7f.1`), is refused by `SessionManager` before any
  request and by `ProfileForm` (use `localhost`); `HealthProbe` and
  `SupervisorClient` never read that host's cookies by default; and
  `ReauthWebViewClient` keeps the WebView off 127.0.0.1 except the front LOCAL
  kernel's own origin, for navigations and subresources alike (a tapped link
  there opens in the browser, which holds no credential). SSH forwards are on
  127.0.0.2.
- **Lifecycle** (`session/LocalKernelLifecycle.kt`, the design's defaults).
  The kernel keeps running while a task runs, while a schedule is enabled, or
  while "Keep running in background" is on. Otherwise it stops once another
  kernel is put in front (`frontChanged`) or the app leaves the screen
  (`AppVisibility`: every activity stopped, outside a configuration change).
  Opening This phone puts it in front at once, before it is ready.
  Nothing is decided before the kernel is Ready, and an unknown answer keeps
  it running. The notification's Stop always works.
- **Running-work signal.** `GET /health/quiescence` (`routes/common.py`), the
  server's process-wide running-task count for lifecycle owners: the same
  judge as the update-restart guard, busy when its registry fails, and public
  on the local kernel (`/health/*`). Schedules: `GET /api/v1/scheduler/tasks`
  (enabled tasks only). Both are native calls with `Authorization: Bearer`.
  They are read on events (front, visibility, toggle, kernel state) and,
  only while the user is away from This phone and the kernel is Ready, at the
  app's existing server-status cadence, `ServerLifecycle.START_POLL_INTERVAL_MS`
  (also the desktop supervisor's cadence for the same probe). The answer
  drives `LocalKernel.setHasRunningWork`, the partial wakelock. Schedules are
  read again after a task finishes, since a one-shot schedule may have ended.
- **The card** shows "Keep running in background" and Stop (while it starts or
  runs) where the kernel can run here, and "Open-source licences"
  (`ui/LicencesScreen.kt`: proot, GPL-2.0-or-later conveyed under
  GPL-3.0-or-later; talloc, LGPL-3.0-or-later; the GPL-3.0 text; the source
  offer). The texts are `android/native/licenses`, packaged as assets in place
  (`app/build.gradle.kts`). The first start on Android 13+ asks for
  `POST_NOTIFICATIONS`. Denied, the kernel still runs, but its ongoing
  notification with Stop and its failure notices stay hidden, so Stop is on the
  card.
- **Wiring.** `localKernelController(context, diag)` returns the controller of
  the process-wide `LocalKernelHost` (controller, lifecycle, settings,
  visibility). The UI reads `LocalKernelHost.current`, which never creates one,
  so previews and screenshots render without it.

## 6. SSH kernels (`ssh/`)

An SSH kernel is a Tofu already running on a host the phone reaches over SSH.
The phone dials out, so it works through NAT; the host needs only sshd and Tofu
on its own loopback. Starting a Tofu that is not running is not built.

- **Library.** Apache MINA SSHD 2.20.1 (`sshd-core`, Apache-2.0) with
  `net.i2p.crypto:eddsa` 0.3.0 for Ed25519, which Android lacks at API 26; no
  BouncyCastle (K10). `useNetI2pEdDsa()` disables MINA's BouncyCastle registrar
  before first use, so JVM tests (Robolectric brings BouncyCastle) run the
  device's crypto path. Release shrinking keeps both libraries whole
  (`proguard-rules.pro`): they create classes by name.
- **Identity.** `SshIdentity` is one Ed25519 key per install, shared by every
  SSH kernel (K12), made on first use. Only its 32-byte seed is stored, sealed
  with AES-256-GCM by a non-exportable AndroidKeyStore key (`KeystoreSealer`,
  alias `tofu_ssh_identity`) in `noBackupFilesDir/ssh/identity_ed25519.sealed`.
  A seal that no longer opens fails the connect; it never replaces the key the
  hosts authorized. The form shows the `authorized_keys` line to copy.
- **Password.** Optional, typed in the form, kept in the vault under the alias
  (§3). Sign-in offers the phone's key first, then the password (`password`
  and `keyboard-interactive`).
- **Host key.** Checked during key exchange, before any credential is sent:
  `sshFingerprint` (`SHA256:<base64>`, as `ssh-keygen -lf` prints it) must equal
  `SshEndpoint.hostKeySha256`.
  - No pin: the connect is refused and the presented fingerprint remembered
    (`SshSetup.hostKeyToTrust`). `ProfilesViewModel.activate` then opens the
    SSH form with *Trust this host?*; *Trust and connect* calls
    `trustHostKey`, which pins exactly that fingerprint through
    `ProfileDao.pinSshHostKey` (first trust, same host and port) and connects.
  - Another key is a hard stop whose reason names both fingerprints; nothing
    re-pins. After a legitimate host reinstall, delete the kernel and add it
    again.
- **Tunnel.** `SshTunnel` binds `127.0.0.2:<ephemeral port>` on its first
  successful connect and keeps it until `stop`, so the endpoint
  `http://127.0.0.2:<port>` and the WebView origin survive reconnects (K11).
  Each accepted connection rides its own `direct-tcpip` channel to the host's
  `127.0.0.1:<remotePort>`; either side closing closes both. Every new session
  first opens and drops one probe channel, so "nothing listens there" or
  "sshd forbids forwarding" fails with that reason, not as a WebView reset.
- **Reconnect, without a timer.** `DefaultNetworkChanges` (the platform's
  default-network callback) reports a change of default network, and every
  tunnel then replaces its session in the background. A connection that
  arrives while no session is up (dropped by the server or by MINA's idle
  timeout) starts one itself. Requests in flight on a dead session fail; the
  SPA retries them.
- **Lifetime.** `AndroidSsh` holds the process's one `SshTunnels`; each
  Activity builds an `SshKernelController` over it (`forApp`), so a recreated
  Activity finds its tunnels, ports and states (K13). States are keyed by
  profile id and endpoint, so an edited kernel starts from `Stopped`.
- **Deadlines** are MINA's defaults, not new settings: TCP connect 1 minute,
  authentication 2 minutes, idle session 10 minutes; no keepalive.
- **Tofu sign-in** stays with the session layer (§3): an open-mode Tofu gives
  the tunnel's loopback peer its local admin; a private-mode Tofu needs an API
  key, which the app does not send yet.

Only a device can confirm that Android's loopback accepts the 127.0.0.2
listener, that the WebView keeps 127.0.0.1 and 127.0.0.2 cookies apart, how
reconnects behave across real network changes, and the AndroidKeyStore seal.
All SSH kernels share the cookie host `127.0.0.2`.

## 7. The host supervisor (URL kernels)

A stopped server cannot answer "start me", so starting and stopping a URL
kernel goes through `supervisor.py` (repository root): a small always-on daemon
that spawns `python server.py` and runs the project's own `stop.sh` for an
allow-listed project path.

- **Reachability.** It is proxied by the same code-server as Tofu, one port up
  (`…/proxy/15000/` → `…/proxy/15001/`, derived by `SupervisorUrl`), and the app
  reuses the profile's `code-server-session` cookie. Redirects are never
  followed with that cookie.
- **Endpoints.** `GET /health`, `GET /status?projectPath=`, `POST /start`,
  `POST /stop` (plus `/reload` and `/restart-deferred`, which the app does not
  call). `/start` is idempotent and returns immediately; the app polls
  `/status` (`ServerLifecycle.START_POLL_ATTEMPTS` × `START_POLL_INTERVAL_MS`).
- **Guard.** `projectPath` must exactly match a realpath in
  `TOFU_SUPERVISOR_PROJECTS` (403 otherwise). There is no supervisor token.
- **In the app.** A profile with a `projectPath` is *managed*:
  `ui/SupervisorControls.kt` shows Start/Stop/Check on its card, driven by the
  pure `ServerLifecycle` state machine and `executeSupervisorCall`
  (login-then-act, read-only automatic probes). `UrlKernelController.stop` is
  the same login-then-act stop behind the kernel interface; its
  `ensureRunning` only resolves the URL, so opening never starts a server.

## 8. Decisions

**K1 — one profile table with a `kind`.** Problem: three kinds of kernel, one
list, one switcher. Decision: a `kind` column on `profiles` with a
`REMOTE_URL` default, so every existing row migrates in place. Alternatives:
a table per kind (three DAOs, a union for the list, and a second home for
alias-keyed secrets); kind inferred from the URL scheme (`ssh://`), which
breaks every caller that parses `baseUrl` as HTTP.

**K2 — the SSH endpoint is a nullable embedded value without column
defaults.** Room reads an embedded value as null when all its columns are
NULL. A default on any `ssh_` column would give every URL row a half-built
endpoint. Alternative: flat nullable columns on `Profile`, which push a null
check per field onto every SSH consumer.

**K3 — a LOCAL or SSH address is runtime state.** The local port can change on
every launch and an SSH forward exists only while connected, so a stored
address would be stale and would invite the URL-kernel code paths (update
probes, cookie stamping) to treat it as a server. Alternative: persist the last
endpoint in `baseUrl`.

**K4 — the URL path does not consult its controller on open.** Opening has
never started a server; doing so through `ensureRunning` would change behaviour
for every managed profile. The URL controller exists so the list and later
lanes see one interface. Alternative: start managed servers on open.

**K5 — exported schemas are debug-only assets.** `MigrationTestHelper` reads
them through the app's assets, and an application module's unit tests only see
the tested variant's merged assets (AGP copies them; test source-set assets are
ignored). So `app/schemas` is a debug asset source: release APKs never ship it,
and `ProfileSchemaMigrationTest` is reported as skipped in the release
unit-test variant. Alternatives: main assets (ships test data in releases);
only the hand-built-DDL test (no proof against Room's own schema).

**K6 — `KernelState.Stopping` is its own state.** Problem: a local kernel's
stop drains for up to the launch contract's stop deadline (35 s), and the
runtime ignores a start meanwhile. Decision: a sixth state, shown as
"Stopping…", during which `ensureRunning` waits before it starts.
Alternatives: map it to Starting (claims the opposite for half a minute), to
Stopped (invites a start the runtime drops), or to Ready (offers a draining
kernel). Cost: an exhaustive `when` over `KernelState` needs one more branch.

**K7 — 127.0.0.1 is reserved for This phone.** Problem: the local kernel's
credential is a cookie, cookies ignore the port, and any app on the phone can
listen on another 127.0.0.1 port, or on the kernel's own port once it exits.
Decision: no URL kernel may use that host in any spelling, the WebView requests
it only as the front LOCAL kernel's own origin, and the cookie lives only while
the kernel is Ready on that port. Alternatives: purge the cookie whenever
another kernel is in front (the list's probes and the add form's health check
still read the jar, and a page can still redirect the WebView to another port);
a session cookie per launch (same exposure while it lives).

**K8 — the running-work signal is the server's quiescence probe, read on
events and at an existing cadence.** Problem: the lifecycle and the wakelock
need "is a task running", and the server pushes no process-wide signal (its
push hub carries per-task chat frames and per-conversation wake hints).
Decision: `GET /health/quiescence`, the judge lifecycle owners already use,
read when the front, visibility, toggle or kernel state change, and in between
only while the user is away from This phone, at
`ServerLifecycle.START_POLL_INTERVAL_MS`. Alternatives: a native push-hub
client (a second socket with its own reconnect policy, and every token delta
of every task); a signal from the SPA (absent exactly when This phone is not
in front); a new server event (a server change for a fact one probe gives).

**K9 — a start shows at once.** Problem: `LocalKernel.start` returned before
the foreground service ran the supervisor, so a caller awaiting "the outcome
of this start" saw the previous run's state; an identical failure was not
even an emission. Decision (runtime): `LocalKernel.start` starts the
supervisor itself, and the service only enters the foreground and follows it
(it also never starts a second generation after a fast failure).
Alternatives: a generation counter in the state (every consumer learns it);
awaiting a Starting emission (lost under StateFlow conflation).

**K10 — Apache MINA SSHD with net.i2p eddsa, no BouncyCastle.** Problem: an
Apache-2.0 SSH client that dexes at minSdk 26, speaks Ed25519 and
`direct-tcpip`, and leaves the app's security providers alone. Decision: MINA
SSHD 2.20.1 plus eddsa 0.3.0, without MINA's unused `jcl-over-slf4j`. Evidence:
R8 8.5.35 (AGP 8.5's D8) dexes the closure at min-api 26 without warnings into
1.51 MB of dex; it references no API newer than 26 (checked against
android-34's `api-versions.xml`); the APIs Android lacks sit on paths the
client never runs (server GSS/JAAS, `executeRemoteCommand`'s `java.rmi`,
`ExceptionUtils`' `javax.management` behind `OsUtils.isAndroid()`); MINA keeps
Android hooks (`OsUtils`, `PathUtils`, its `docs/android.md`). eddsa 0.3.0
carries CVE-2020-36843, signature malleability: a malleated signature still
signs the same session-unique exchange hash, so it cannot pass for another
host or session. Alternatives: sshj 0.41.2, which needs BouncyCastle (6.05 MB
of dex) and treats Android's stripped platform provider named "BC" as
BouncyCastle, so apps replace the process-wide "BC" provider; MINA with
BouncyCastle for Ed25519 (the same size and name collision).

**K11 — the app owns the forward's listener.** Problem: the forward's port must
outlive SSH sessions, because the WebView origin and the SPA's retries depend
on it. Decision: bind 127.0.0.2 once per tunnel and carry accepted sockets over
whichever session is current. Alternative: MINA's local port forwarding, whose
listener belongs to one session: it closes with every drop, refuses
connections until a re-bind, and the re-bind can lose the port.

**K12 — one SSH identity per install.** The user adds one public key to each
host, as with a desktop's `~/.ssh/id_ed25519`. Alternatives: a key per kernel
(one more key to copy for every host, and key files to move on rename); the
key inside AndroidKeyStore (no Ed25519 there at API 26).

**K13 — SSH tunnels belong to the process.** Problem: a configuration change
recreates `MainActivity` and its object graph; tunnels owned by the Activity
would leak their sockets and keep reconnecting unseen. Decision: `AndroidSsh`
holds one `SshTunnels` per process. Alternative: close tunnels in `onDestroy`,
which drops the user's session on every configuration change.

**Supervisor (owner-ratified):** port 15001; launched as a systemd user unit
with `Restart=always` (`supervisor.sh` + nohup only where lingering is
unavailable); no token, because the code-server password already gates the
proxy and its terminal can already run any command — the allow-list is the
only guard; no `/restart` (the app composes stop, poll, start); `/start`
returns immediately and the app polls. Rejected: a Bearer supervisor token
(added friction behind an already-locked door); serving start from Tofu itself
(a stopped server cannot answer).

## 9. Verification

- `data/ProfileSchemaMigrationTest` — 2→3 with `MigrationTestHelper` against
  `2.json`/`3.json`; every v2 value survives, rows become `REMOTE_URL`.
- `data/ProfileMigrationTest` — the 1→2→3 chain from a hand-built v1 database;
  SSH endpoint round trip; first-trust host-key pin; conditional session writes.
- `session/KernelControllerTest` — kind dispatch, the placeholder, list order.
- `session/SessionControllerKernelTest` — LOCAL/SSH activation through fakes,
  the meta gate, no write-back, delete stops, SSH add/edit rules, the SSH
  password's vault slot.
- `session/SessionControllerTest` — the URL path never touches a controller.
- `session/UrlKernelControllerTest` — no request to resolve; managed-only stop.
- `ui/ProfilesViewModelTest` — the Kernels list, placeholder state, SSH save,
  the kernel in front.
- `session/ProfileFormTest` — SSH form validation.
- `session/LocalKernelControllerTest` — availability refusals, the state
  mapping, waiting out a drain, the credential before the health check, front
  changes.
- `session/LocalKernelCookieTest` — the cookie's host, attributes and scope
  (OkHttp's RFC 6265 matcher), install refusals, reserved-host spellings, the
  WebView rule, the form refusal.
- `session/LocalKernelLifecycleTest` — the policy, event-driven decisions, the
  cadence, the wakelock signal, the activity endpoints and their parsing.
- `session/CookieBridgeRobolectricTest` — the jar holds the credential for
  127.0.0.1 only; `ReauthWebViewClient` blocks other 127.0.0.1 requests.
- `session/ThisPhoneAndroidTest` (Gradle tier) — Keep running persists, the
  one-time notification request, the packaged licence texts, `AppVisibility`.
- `runtime/LocalKernelServiceTest` — a start shows at once and the service
  starts no second generation.
- `ssh/SshKernelControllerTest` — against an in-process MINA SSHD server and a
  fake Tofu: key and password sign-in, first-trust pinning and the hard stop
  on a changed key (no credential sent either way), HTTP through the forward,
  the 127.0.0.2-only listener, the port kept across a network change and a
  dropped session, the probe's reason, stop. Gradle tier: `LIBS` has no MINA.
- `ssh/SshIdentityTest` — the sealed seed, a stable key, a seal that does not
  open, the `authorized_keys` line and `ssh-keygen` fingerprints.
- `ui/ProfilesViewModelTest` also covers the trust prompt: an unpinned host
  opens the form with the presented key; trusting pins it and connects.

Run `./test-local.sh`, then `./gradlew test` and `./gradlew :app:assembleDebug`.
