# Tofu Android Client

> **Repository layout (monorepo):** the app's SOURCE lives here — `android/`
> inside the Tofu repo (`rangehow/ToFu`). Release APKs are still published to
> the SEPARATE `rangehow/tofu-android` repo's Releases (see *Distribution*
> below for why the release stream must not merge with the desktop one).

A thin native Kotlin **WebView shell** for the Tofu self-hosted assistant. It does
**not** re-implement the SPA — the existing vanilla-JS frontend renders inside a
WebView (it already derives `BASE_PATH` from `location.pathname` and carries the
SSE-through-proxy recovery nets). The native layer owns only what a browser
can't: **credential/session management** and **multi-server profiles**.

## Why this exists

Connecting to a cloud-IDE-hosted Tofu (`https://<uuid>-vscode-<idc>.codelab.example.com/proxy/15000/`)
means re-typing a long code-server password and copy-pasting UUID-scoped URLs.
This app remembers servers and authenticates once per profile.

## Observed Codelab password deployment

These observations describe the tested deployment, not every Codelab network or gateway policy.

1. **The observed gateway did not require interactive SSO on this path.** An unauth
   `GET …/proxy/15000/` 302s to a **relative** `./../../login` — code-server's
   own login, not an SSO IdP redirect. Confirmed by replaying the full loop
   through the public host.
2. **The observed code-server `--auth password` gate is fully
   replayable**: `POST /login` (`password`, `base=.`) → `302` +
   `Set-Cookie: code-server-session=…; Domain=<uuid-host>; Path=/; SameSite=Lax`;
   replaying that cookie on `GET /proxy/15000/` → `200`.
3. **The session cookie has NO `Max-Age`/`Expires`** → it's a session cookie a
   plain WebView drops on cold start. We author the injected cookie, so we
   **upgrade it to persistent** (`CookieBridge`), and keep the stored credential
   as the ultimate fallback.
4. **The cookie is `Domain`-pinned to the full UUID host.** On a re-provision the
   URL host changes and any cached jar is bound to the dead host → it MUST be
   hard-invalidated. Baked into `SessionManager.updateUrlAndReauth`:
   **URL change ⇒ purge old host jar ⇒ re-login from stored credential.**

## Architecture

```
MainActivity (single Activity, Compose)     routes on ProfilesViewModel.screen:
  ├─ ProfileListScreen   list / switcher — tap to activate, edit/delete, add FAB
  ├─ AddEditScreen       add/edit form (validated by ProfileForm)
  └─ WebScreen           WebView hosting the Tofu SPA + ReauthWebViewClient +
                         TofuNative bridge (reauth escape hatch +
                         tofu:native-visibility lifecycle signal)
ui/
  ProfilesViewModel.kt   reactive profile list + screen/status state; delegates
                         every mutation to SessionController (no session logic here)
  ProfileListScreen / AddEditScreen / WebScreen   thin Compose surfaces
data/
  Profile.kt          Room entity + DAO + DB
                      { alias (stable identity), instanceUuid?, baseUrl (editable),
                        authType, cookieHost }
session/
  SecretStore.kt      EncryptedSharedPreferences, secret keyed by ALIAS (not URL)
                      (impl of SecretVault: read+write; SecretLookup: read-only)
  ServerUrl.kt        host / origin / loginUrl / MLP-UUID parsing
  LoginForm.kt        recognizes same-origin code-server password forms
  ProfileForm.kt      pure add/edit validation (alias/URL/secret rules)
  CookieBridge.kt     OkHttp cookie → WebView jar; Max-Age upgrade + flush; purgeHost
  TofuProbe.kt        pure Tofu↔gateway 401 discrimination + paste-time verdicts
                      (mirror of lib/desktop_agent/_probe.py: {"ok":false,"error":{…}}
                      vs {"error":"Unauthorized"}; 200+bootId is the Tofu signal)
  HealthProbe.kt      GET {base}/api/health WITH the session cookie — a cookie-less
                      probe would measure the gate, not the server
  SessionManager.kt   gateway discovery / trusted password login (bounded retry +
                      v4 meta preflight), URL-change purge+relogin, profile
                      update path
  ApiMetaGate.kt      pure GET /api/v4/meta verdict — blocks only on a
                      definitive apiMajor / minAndroidBuild mismatch, fail-open
                      on partial knowledge (404 / unparseable / transport)
  SessionController.kt Room-first scoped saves + bounded vault reconciliation;
                       fresh-row activation and shared URL-change invalidation
  ReauthWebViewClient gateway expiry ⇒ bounded replay or interactive handoff
  TrustedWebBridge   origin/frame-scoped SPA channels; no Java bridge on IdP pages
  TofuApplication    process diagnostic owner, restart evidence and crash handoff
  DiagLog            bounded, redacted native event tail
  DiagnosticJournal two private rotating segments; bounded serial write queue
  DiagnosticHttp    HTTP stages/status/redirect/cookie scopes, no credential values
  ClientReport      bounded clipboard/share/export envelope and privacy filtering
```

**Design invariants**
- Secret binds to `alias`, never to `baseUrl` → a re-provisioned sandbox reuses
  the credential after a one-tap URL edit.
- Cookie injection always `flush()`es (else a cold kill loses it).
- `SessionManager` uses `followRedirects(false)` — we need the 302 + Set-Cookie,
  not the redirect target.

## Gateway authentication contract

**Problem.** A `/proxy/<port>/` URL identifies a forwarding layout, not its
login protocol. An outer SSO gateway may precede code-server; alternate ports,
path prefixes and cookie scopes must survive every native/browser handoff.

**Decision.** `ServerUrl`, `LoginForm`, `SessionManager` and
`ReauthWebViewClient` own one discovery/replay/handoff chain:

- `NONE` opens the browser application without a native request. A proxy URL
  defaults to password replay when possible; a blank password permits
  interactive sign-in. URL userinfo is rejected.
- Before replay, a cached cookie is checked against the protected Tofu metadata
  endpoint. Discovery starts at the configured app URL, follows at most eight
  same-origin redirects and selects cookies for each full request URL. A
  foreign origin (including a changed scheme/port) is handed to the WebView
  without requesting that target natively or sending it a password.
- A password POST requires one recognized code-server `login-form`, its
  `password` input and hidden `base` input, and a same-origin action. Missing
  actions post to the served page. Hidden fields, the `href` value and the
  redirect return query survive. Unknown/ambiguous HTML opens interactive
  sign-in. HTTP 200 only means bad credentials when the recognized login form
  is re-served. HTTP 429 never retries.
- A browser handoff changes the active WebView mode, not the saved profile's
  auth type. Explicit saved `NONE` choices survive launch/editing; proxy shape
  never migrates an existing user's choice. Gateway `/login` is allowed during interaction; Tofu's own
  `/proxy/<port>/login` remains browser-owned. After outer SSO returns to the
  code-server login, a saved password can resume through the same discovery
  path once per interactive attempt.
- Returning to the app with a cookie is only a candidate. A bounded JSON response from the protected, owner-scoped
  conversation metadata endpoint (one row, no transcript), or Tofu's own auth
  refusal at that endpoint, proves the outer gate was passed. Public health/meta
  endpoints are insufficient. For a stopped Tofu, a configured supervisor can
  instead prove its versioned control-plane status contract. HTML, CSRF cookies and redirects cannot promote
  a session. The repository's conditional session stamp compares id, alias,
  URL, saved auth and `REMOTE_URL` kind, rejecting edited/deleted/retyped rows
  without touching LOCAL/SSH state or other columns.
  Discovery rechecks the current profile and credential before password POST;
  a rejected late stamp cannot be reported as a successful sign-in.
- Native calls retain IPv6, explicit ports and the prefix before `/proxy/` or
  `/absproxy/`. Supervisor requests select cookies for their own sibling URL and never follow
  redirects. Its root control endpoints use the stripping `/proxy/` route,
  including when Tofu uses `/absproxy/`.
  A credential can be borrowed only from the same origin and code-server
  prefix, not another service on the same hostname.
- Raw response cookies retain Domain, Path, Secure, HttpOnly, SameSite and
  explicit expiry. Only an expiry-less `code-server-session` gets the existing
  seven-day local persistence policy; that does not extend server validity.
  Interactive IdP session cookies retain the IdP's lifetime. Cookies are flushed
  on completion/background/disposal. Origin changes clear old host/path
  cookies and storage for the actual scheme/port; shared parent-domain IdP
  cookies remain under the IdP's authority.
- The SPA's native channels use AndroidX WebMessage listeners restricted to
  the configured origin and main app frame. IdP pages/subframes receive no
  native reauthentication or diagnostic authority. Older WebViews without
  these APIs retain main-frame recovery; updating WebView restores the channels.
- A browser-owned sign-in window shares the WebView cookie jar and is destroyed
  when dismissed or disposed. **Open in browser** opens the complete Tofu URL
  for providers requiring the system browser or external apps. External browser
  cookies are not imported into WebView; there is no generic, provider-free
  OAuth session-transfer protocol.

**Alternatives considered.** Blindly POSTing the first form could send the
saved code-server password to an IdP. Treating unknown replies as authenticated
kept the password interception active and could block sign-in. Permanently
switching the saved profile to SSO would lose password continuation. Importing
external-browser cookies is not a supported authentication contract.

**Resource and lifecycle bounds.** Native authentication, update and form-health
responses share the 256 KiB decoded-body correctness boundary in
`BoundedResponse.kt`; redirects remain bounded to eight and simultaneous
sign-in popups to one. Health has an eight-second total-call liveness deadline.
Popups are created only on demand; idle screens hold none. HTTP callbacks buffer
the bounded response and close the socket before handoff; cancelling login,
update or form health cancels the live call, including a stalled body read.
Oversized bodies fail verification, but legitimate empty HTTP responses retain
gateway/waking status classification. Health detail/diagnostics never return a
server-controlled body or exception message. Cancellation prevents late
cookie/profile writes. Verified sessions
reuse protected-probe/meta requests and skip password POSTs. Test observations pin one
initial request for a foreign-SSO redirect, zero for `NONE` or missing-password
handoff, two for a valid cached session, and no writes/flushes after cancellation.

Verification: `SessionManagerGatewayTest`, `LoginFormTest`,
`CookieHeadersTest`, `ReauthWebViewClientFailureTest`, and the real Room query in
`ProfileMigrationTest`; run `./test-local.sh`, then `./gradlew test` and
`./gradlew :app:assembleDebug`. Actual Codelab cellular/VPN, IdP WebView policy,
external-app return, session expiry and process-kill restoration still require
on-device validation with the deployment's real gateway.

## Native URLs, review forms and operation ownership

**Problem.** Connection links and legacy addresses can contain sign-in data;
replayed intents can overwrite a draft, and a cancelled login or full-row save
can publish stale navigation, credentials or session metadata.

**Decision.** `ServerUrl.storageError` is the one credential-free admission rule
for saved profiles, QR/paste/deep-link prefills and native probes. It rejects
userinfo, credential query/fragment fields and nested return URLs/JSON, including
encoded keys, browser-style slash/backslash authorities and decoded controls.
Each decoding pass is inspected before the next pass: encoded username slashes
cannot disappear into a path. Invalid labels/errors never echo the input. The
storage rule does not replace transient gateway/SSO navigation parsing.

- URLs are bounded to 8,192 characters, connect payloads to 16,384, and nested
  decoding to four passes; ambiguous deeper encoding fails closed.
- REMOTE_URL admission also refuses both native credential hosts before a
  cookie lookup or native call. LOCAL/SSH have endpoint-free stored rows and
  controller-owned transient addresses. LOCAL's explicitly injected health
  cookie remains valid for its installed-session proof; generic remote probes
  cannot borrow it.
- Links open a **review form**, never save or log in. Accepted initializers and
  pending intents are separate observable state. A dirty or editing form offers
  **Replace form** / **Keep form**; keeping it preserves its current fields.
  Deliberate replacement changes the form identity; stale effects/callbacks
  cannot accept/dismiss a newer intent or restore an old draft.
- Noncredential draft fields survive Activity recreation in the retained
  ViewModel's memory, including safe partial typing. The password is only a
  local `remember` value: no password field exists in a draft, Bundle or
  `SavedStateHandle`. Process death does not restore a password/draft.
- A legacy unsafe address is refused on activation without rewriting Room or
  changing saved `NONE`. Edit offers a sanitized proposal and a correction
  message; only an explicit save replaces the row. Saved auth choices survive
  URL editing; URL-shaped defaults apply only to new profiles.
- `MainActivity` obtains its graph from `ViewModelProvider`, using application
  context rather than retaining the old Activity. Back, new selection, deletion
  and form edits cancel obsolete login intent. Coroutine and generation fences
  guard late reads, cookie/stamp writes and navigation. The exact owned Job's
  completion releases Room and owned authentication/update clients only after
  all children, including save reconciliation, settle. Form health owns a
  separate cancellable Job; disposal cancels it and releases its private HTTP
  dispatcher/pool after that Job settles. Supplied clients stay caller-owned.
- Room commits reviewed connection fields before vault changes. URL save
  compares id, kind, alias, URL, auth and project path; SSH save also compares
  the reviewed host/port/user/remote-port identity. Both preserve concurrent
  recency and unrelated owner columns. Same-connection cookie stamps and SSH
  trust pins survive; reviewed host/port changes clear only the corresponding
  pin. Changed/deleted/retyped rows are rejected. Network login does not hold
  the graph's save mutex.
  Readback after a lost commit result is noncancellable but bounded to **five
  seconds**. Vault compare-and-set preserves newer values; incomplete
  reconciliation is reported and never rolls back a committed row.

**Alternatives rejected.** Automatic link acceptance loses drafts; vault-first
writes orphan credentials after a failed insert; whole-row save/rollback
reinstates stale fields; swallowing cancellation permits late publication.

Verification: `UrlCredentialsTest`, `ProfileFormTest`, `SessionControllerTest`,
`UpdateProbeTest`, `HealthProbeTest`, `NativeHttpCancellationTest`, `SecretVaultTest` and the real
AndroidX `ProfilesViewModelTest`. `tests/test_profile_save_query.py` executes the
actual Room DAO SQL against in-memory SQLite, not generated Room bindings.
The local ladder does not prove Compose/Paparazzi/APK packaging, real gateway
policy, process-kill cookie persistence or on-device intent delivery.

## Distribution

**Problem:** a floating latest link and a source-version badge can describe
unrelated APKs; a public debug signing key lets anyone forge an update.
**Decision:** keep `rangehow/tofu-android` separate from desktop releases and
advertise only `android/published-release.json` through
`lib/mobile_distribution/` and `/api/health`. Settings → General → About &
Update uses that asset's version-pinned URL and matching badge. Health reads the
local receipt once per process; it makes no release-service requests and never
promotes a new receipt automatically.

The receipt currently names **legacy 0.1.15 / build 16**:

```
https://github.com/rangehow/tofu-android/releases/download/v0.1.15/tofu-android.apk
```

Its downloaded bytes and manifest were checked in the original verification;
it uses the public Android Debug certificate. It is **not this hardened source
build** and is **not a privately authenticated release**. Source versions may
advance without changing this download. Publication requires separate signing,
asset verification and receipt promotion; an asset URL alone is not byte or
signature evidence.

`TOFU_MOBILE_CLIENT_URL` overrides the Android asset. An alternate URL gets no
badge unless explicitly paired with `TOFU_MOBILE_CLIENT_VERSION`; a version-only
override cannot relabel the default artifact. Empty, invalid or credential-
bearing URLs disable downloads. iOS remains in development until a real signed
distribution is supplied through `TOFU_IOS_CLIENT_URL`.

Authenticated `/api/v4/mobile/android` and `/api/v4/mobile/android/apk` also
serve the operator's configured local APK (`TOFU_ANDROID_APK_PATH`). No browser
credential enters a download URL or QR; the phone signs in independently. Local
delivery does not inspect the APK signature/manifest on each request: validate
it before configuring that file. JSON links retain a configured ASGI root_path;
behind a stripping gateway, the scan page derives the exact prefix from browser
location and sends the same-host root explicitly as `base`.

**Alternatives rejected:** floating latest links can change the artifact;
source badges are not publication evidence; retaining public-key update
compatibility permits a forged update to read saved secrets.

## Building the APK & CI

`.github/workflows/build-android-apk.yml` runs `testDebugUnitTest` and
`assembleDebug` for Android changes. Debug builds use the separate package
**`com.tofu.client.debug`**, so the public debug key cannot replace a production
install. Debug verification needs no private release credentials.

An explicitly pushed `android-v<versionName>` tag stages an owner-only private
keystore, checks tag/build monotonicity, builds one release APK, verifies its
signer and manifest, and publishes APK plus receipt to the Android repository.
Missing configuration, a mismatched signer/tag/manifest, multiple APKs or missing
assets fail publication. Key cleanup runs after failure too. Ordinary source
pushes do not publish an APK.

### Private signing and migration

Local `assembleRelease` requires all five environment values:

- `TOFU_ANDROID_KEYSTORE_PATH`
- `TOFU_ANDROID_KEYSTORE_PASSWORD`
- `TOFU_ANDROID_KEY_ALIAS`
- `TOFU_ANDROID_KEY_PASSWORD`
- `TOFU_ANDROID_SIGNER_SHA256` — the release certificate's SHA-256 pin

Gradle resolves the signing file relative to `android/app` and passes its
**canonical absolute path** to the JDK-only `ValidateKeystore.java` guard
(any Java 11+ source launcher runs it, so its specs need no Android JDK).
That public path is the only argument; passwords remain in the environment.
The guard verifies the real private-key entry, certificate validity and pin,
including copied/renamed public debug keys. CI also requires
`TOFU_ANDROID_KEYSTORE_BASE64` and `TOFU_ANDROID_RELEASE_PAT` as secrets.
`validate_release.py` runs real `apksigner verify --print-certs` and
`aapt dump badging`: exactly one pinned non-debug signer, the production package,
expected version/build and a non-debuggable manifest are required. R8 and
resource shrinking remain enabled.

**Legacy migration is deliberate.** Android normally refuses an in-place update
from the public debug certificate to a private certificate. Record server
aliases/URLs and settings you need, then choose to uninstall the old production
app and install the verified private release once. Uninstalling deletes local
profiles, cookies, encrypted credentials and any other app-local data. Do not
uninstall until needed local files/projects are safely backed up. Re-enter
passwords, never export them in a QR/URL. No profile-export or signature-lineage migration is implemented.
Later in-place updates require the same protected key and an increasing build
number. Never uninstall the user's app automatically.

## Cleartext (http:// LAN servers)

The add/edit-server form accepts both `http://` and `https://` base URLs, and a
self-hosted Tofu on a private LAN is commonly reached over bare http
(`http://192.168.1.20:8080`, `http://mynas.local`). The manifest previously set
`android:usesCleartextTraffic="false"`, which made the WebView fail those URLs
with `ERR_CLEARTEXT_NOT_PERMITTED` — and blocked the OkHttp-backed headless
login the same way, since both consult Android's `NetworkSecurityPolicy`.

The fix is `res/xml/network_security_config.xml`, referenced from the manifest
via `android:networkSecurityConfig`. It permits cleartext at the **base config**
level, because Android's network-security-config can only scope cleartext by
**hostname** (exact match, or a single leading `*.example.com` wildcard). It has
no notion of an IP range / CIDR, so there is no way to express "cleartext only
for RFC1918 / LAN addresses" — a `10.*` domain entry is not valid (wildcards are
subdomain wildcards, not address-octet wildcards), and a raw IP literal could
only be listed one at a time, which is impossible for a user-typed URL.

Why that is safe here rather than "cleartext for arbitrary sites": the shell is
NOT a general-purpose browser. It has no address bar and its single navigation
entry point is `loadUrl(profile.baseUrl)` — the exact URL the user typed into
the profile form. OkHttp is likewise only ever pointed at the profile's own host
(`login`, health probe, supervisor). So cleartext is effectively limited to
user-configured `http://` profiles. If a future change adds general browsing, or
the app moves to Play Store, revisit this — the smallest scoped form would be
`base-config cleartextTrafficPermitted="false"` plus an explicit `domain-config`
per known LAN hostname (which still cannot cover raw IPs).

## Remote start/stop (supervisor)

Beyond "open" (the WebView), a profile can carry an optional **project path** so
the app can **start and stop** the Tofu server on the host. Because a stopped
server can't answer a "start me" request, this is driven by a separate always-on
daemon, `supervisor.py` (this repo's root), NOT by Tofu itself. Design +
rationale: [`docs/SUPERVISOR_DESIGN.md`](docs/SUPERVISOR_DESIGN.md).

- **Reachability:** the supervisor is proxied by the SAME code-server as Tofu,
  one port up. The app DERIVES the sibling from the profile URL's own
  `/proxy/<port>/` segment — `…/proxy/15000/` → `…/proxy/15001`, and a
  non-default deployment like `…/proxy/15005/` → `…/proxy/15006` — falling
  back to the conventional 15001 only when no numeric proxy segment exists
  (neuter: hardcode 15001 → `SupervisorUrlTest`'s non-default-port case
  derives the wrong base). The app reuses the profile's
  `code-server-session` cookie.
- **No auth:** Tofu is a personal app and the code-server password already gates
  the whole proxy (its terminal can already run any shell command), so the
  supervisor adds NO token — nothing to type in the app.
- **Safety:** `projectPath` is validated against a strict realpath allow-list
  (`TOFU_SUPERVISOR_PROJECTS`) on the host so it can't spawn an arbitrary cwd.
  This is CONFIG ("which projects may I manage"), not authentication.

**Run the supervisor on the host** (owner-ratified: a systemd user unit):
```bash
export TOFU_SUPERVISOR_PROJECTS=/abs/path/to/tofu   # ':'-separated allow-list
./supervisor.sh install     # systemd --user unit, Restart=always
# where user-lingering is unavailable:  ./supervisor.sh nohup
```
Then in the app: edit the server → set **Project path** to the same absolute
path → open it from the server list → use the **Start / Stop** controls.

## Local kernel (Tofu on this phone)

**Problem.** The phone itself should run the complete harness, not only a WebView
onto a remote server. Decision (Android design, Option B at targetSdk 34; the
device spike S1 is pending): the APK ships an Ubuntu 24.04 arm64 userland and a
Tofu payload, runs them under proot, and starts the normal Linux server in
local-kernel mode on `127.0.0.1`. The server half is
[`docs/ANDROID_LOCAL_KERNEL.md`](../docs/ANDROID_LOCAL_KERNEL.md) (launch
contract); proot itself is [`native/README.md`](native/README.md). The Kernels
list's "This phone" starts it through `session/LocalKernelController`, which
adapts the API below to the kernel model; the session cookie, the keep-running
policy, the running-work signal, the notification permission and the licences
screen are in [`docs/SUPERVISOR_DESIGN.md`](docs/SUPERVISOR_DESIGN.md) §5.

| Concern | Owner (`app/src/main/java/com/tofu/client/runtime/`) |
|---|---|
| Public API: `availability`, `start`, `stop`, `state`, `endpoint`, `secret`, `setHasRunningWork` | `LocalKernel.kt` |
| State machine, restart budget, stop protocol | `LocalKernelSupervisor.kt`, `LocalKernelState.kt` |
| Foreground service, notifications, wakelock | `LocalKernelService.kt` |
| Install, SHA-256 and manifest checks, payload pointers | `RuntimeInstaller.kt`, `TarReader.kt`, `RuntimeIndex.kt`, `KernelLayout.kt` |
| Per-install secret | `KernelSecret.kt`, `AndroidKeyStoreSecretWrapper.kt` |
| proot command, launch protocol, readiness, signals | `KernelProcess.kt`, `AndroidKernelRuntime.kt` |

**Building with the runtime.** Both inputs come from other build steps and are
passed as Gradle properties; each is validated when set:

```
./gradlew :app:assembleDebug \
  -Ptofu.localKernel.jniLibsDir=<proot BUILD_DIR>/jniLibs \
  -Ptofu.localKernel.assetsDir=<assets root holding kernel/index.json and its archives>
```

`jniLibsDir` must contain exactly `arm64-v8a/` with `libproot.so` and
`libproot-loader.so` (a matched pair from one proot build). Without the
properties the app builds without the runtime and LOCAL reports "Not available in
this build"; the same holds on devices without arm64 (no `libproot.so` in
`nativeLibraryDir`), so the APK stays installable everywhere for URL kernels.
`packaging.jniLibs.useLegacyPackaging = true` is unconditional: Android 10+ lets an
app exec files only from `nativeLibraryDir`, so the `.so` files must be extracted
at install (stored compressed in the APK).

**Runtime contract v1, consumer side.**
- `assets/kernel/index.json`: `{"format":1,"abi":"arm64-v8a","rootfs":{version,file,sha256,size},"payload":{…}}`.
  Versions become directory names and must match `[A-Za-z0-9][A-Za-z0-9._+-]*`.
- Each archive is a deterministic gzip-compressed tar named `rootfs-<version>.tgz`
  / `payload-<version>.tgz`. Never `*.gz`: AGP's asset merger gunzips every
  `*.gz` asset and drops the extension (sdk-common `AssetItem.shouldBeUnGzipped`),
  which would break the pinned name and SHA-256; the build refuses `*.gz` under
  `assetsDir` and stores `*.tgz` uncompressed (`androidResources.noCompress`).
  Its first entry, `.manifest.json` (frozen by the producer in
  `android/runtime/README.md` §2),
  is `{"format":1,"entries":[{"path","type","mode","size","sha256","target"}]}`:
  `type` is `file`/`dir`/`symlink`/`hardlink`, `mode` is a JSON string of four
  octal digits equal to the tar header's mode (`"0755"`, `"4755"`, `"1777"`;
  anything else is a malformed manifest), `size`+`sha256` are required for
  files and `target` for links, paths are archive-relative, and the producer's
  other keys (`abi`, `kind`, `version`, `inputs`) are ignored. Only those four entry
  types are accepted (ustar, PAX per-entry headers, GNU long names); devices,
  FIFOs and global PAX headers are refused.
- Layout, all under `files/kernel/` (nothing in `cache`, which Android may clear
  under a running process): `rootfs/` (+ `rootfs.version`), `payload/<version>/`
  with the `current` and `previous` symlinks, `data/` (bound at `/var/lib/tofu`),
  `home/` (`/root`), `proot-tmp/` (`PROOT_TMP_DIR`) and `shm/` (`/dev/shm`). The
  last two are emptied before every launch. The rootfs path never moves:
  proot's `--link2symlink` stores absolute host paths.
- Install: an archive's size and SHA-256 are checked against the index *before*
  extraction and a mismatch is refused. Extraction goes to a staging directory,
  every entry is checked against the manifest (type, mode, size, SHA-256, link
  target; nothing missing or extra) and the directory is renamed into place.
  Paths with `..`, absolute paths and writes through a symlink parent are refused.
  Permission bits and mtimes are applied; set-id bits are not representable
  through java.nio and are inert on `/data` (mounted `nosuid`) and under proot.
- Hard links become relative symlinks to their target: AOSP's
  `private/app_neverallows.te` forbids apps to create hard links.
- A new payload launches as *pending*. Only after its kernel answered
  `/api/v4/meta` does `commit` switch `current` (one atomic rename of a symlink)
  and keep the replaced payload as `previous`, the last-known-good; older payloads
  are deleted. A pending payload that ends in safe mode is rejected and the next
  start runs `current` again (with no committed payload it is retried). The
  rootfs is replaced only when its pinned version changes; packages installed with
  apt are lost then.

**Launch.** `libproot.so --kill-on-exit --link2symlink --root-id --rootfs=…
--bind=/dev --bind=/proc --bind=/sys --sysvipc --ashmem-memfd --bind=<shm>:/dev/shm
--bind=<payload>:/opt/tofu --bind=<data>:/var/lib/tofu --bind=<home>:/root
--cwd=/opt/tofu /opt/tofu/bin/tofu-local-kernel --data-dir /var/lib/tofu`, with
the complete environment `PROOT_LOADER`, `PROOT_TMP_DIR`, `PATH`, `HOME=/root`,
`LANG=C.UTF-8` (proot hands it to the guest; no `LD_*`). `--sysvipc`,
`--ashmem-memfd` and the `/dev/shm` bind are the proot lane's recommendations,
pending S1. The secret is written to stdin and stdin is closed; the single stdout
record gives the port and the server's pid; readiness is a credential-less
`GET /api/v4/meta` polled at the app's existing start-poll cadence
(`ServerLifecycle.START_POLL_INTERVAL_MS`). There is no boot deadline: a first
boot under proot is unmeasured, and Stop is always available.

**States.** `Stopped → Starting → Ready`, `Stopping` while a stop drains,
`Failed(cause)` and `SafeMode` until the user starts it again. The service is a
`specialUse` foreground service, started only while the app is in the foreground,
`START_NOT_STICKY`: after process death the kernel waits for the user.
`LocalKernel.start` asks for the service and starts the supervisor itself, so
`state` already shows the start when it returns `Requested` (a caller awaiting
its outcome never sees the previous run's); the service only enters the
foreground and follows the supervisor.

| End of a generation | Result |
|---|---|
| the app asked for the stop | `Stopped` |
| proot itself died by SIGKILL (status 0x80 + 9) | `Failed(KilledBySystem)`, guidance shown, no restart |
| status 0 (the in-app shutdown) | `Stopped` |
| status 2 (usage or secret format) | `Failed(UsageError)` |
| bad stdout record, readiness answered 1xx/3xx/4xx | `Failed(ProtocolViolation)` |
| `503 server_boot_failed`, or any other status | crash: restart at once; the 5th crash within 120 s → `SafeMode` |

proot exits with the status of the last guest that exited normally (255 if none),
so statuses other than proot's own SIGKILL are hints: the server's own exits
(0, 1, 2) come through, but a guest's signal death can surface as any earlier
status. Non-zero is restarted as a crash; a stale 0 stops without a restart loop.
The restart budget is the desktop lifecycle manager's (`server_manager.py`
`DEFAULT_MAX_FAILURES`, `DEFAULT_FAILURE_WINDOW`); a requested stop never spends
it and a user start resets it.

**Stopping.** proot ignores SIGTERM and must never be SIGKILLed alone (it sets no
`PTRACE_O_EXITKILL`, so its guests would run on untraced). A stop sends SIGTERM to
the server pid from the stdout record; `--kill-on-exit` ends the rest. Before that
record exists, or at the launch contract's stop deadline, 35 s after SIGTERM
(§7: `TOFU_SHUTDOWN_HARD_SECS` 30 s plus `server_manager.py`'s 5 s margin;
`LaunchProtocol.STOP_ESCALATION_MS`), the stop is forced with SIGQUIT to proot, found
as the app's direct child in `/proc`, which SIGKILLs every guest and exits.
Android's `java.lang.Process` has no `pid()`, and `destroy()` would only send the
ignored SIGTERM and close the pipes. Once proot has exited, and before every
launch, any remaining process of the app's uid (other than the app) is SIGKILLed:
those are guests a killed proot left behind.

**Killed by the system (Android 12+ phantom processes).** Verified in AOSP
(`PhantomProcessList.java`, `ActivityManagerService.java`,
`ActivityManagerConstants.java`):
- Every process an app forks counts against a system-wide limit of 32
  (`max_phantom_processes`, DeviceConfig namespace `activity_manager`). Above it,
  victims are taken from the app whose process is least important, oldest first,
  with SIGKILL; the app itself survives. A foreground service makes the app more
  important but does not exempt its children, and proot is this app's oldest.
- Children of apps in the background (process state `HOME` or lower priority) are
  also killed for sustained CPU use, together with the app. A running foreground
  service is state `FOREGROUND_SERVICE` and is exempt from that rule.
- Android 12L and later gate both rules behind the feature flag
  `settings_enable_monitor_phantom_procs`; Android 12 has no such flag.

Guidance shown with `KilledBySystem`: on Android 14+, Settings → System →
Developer options → **Disable child process restrictions** (AOSP Settings, first
in `android14-release`); on Android 12–13, from a computer:
`adb shell "/system/bin/device_config set_sync_disabled_for_tests persistent; /system/bin/device_config put activity_manager max_phantom_processes 2147483647"`
(both subcommands exist in `DeviceConfigService` of 12, 12L and 13; no
developer.android.com page documents the limit). Before Android 12 the app only
offers a restart.

**Secret.** 256 bits from SecureRandom as `tofu_admin_<64 hex>`, generated once
per install, kept only as AES-GCM ciphertext under a non-exportable AndroidKeyStore
key in `noBackupFilesDir`, written to the kernel's stdin and never to argv, the
environment, Room, URLs, saved state or logs (crash stderr tails are scrubbed of
it). A stored secret that no longer decrypts fails the start instead of being
replaced: the kernel's data already admitted it.

**Notifications, wakelock, backup.** Channels "Local kernel" (low importance;
the ongoing "Tofu is running on this phone" with Open and Stop, and failure
notices with guidance) and "Approval needed" (high importance, for approval
prompts). A partial wakelock is held only while `LocalKernel.setHasRunningWork`
is true and the kernel is ready; the app sets it from the kernel's
`/health/quiescence` while the user is away from This phone. The first start on
Android 13+ asks for `POST_NOTIFICATIONS`; if it is denied the kernel still runs,
but the ongoing notification (with Stop) and the failure notices stay hidden, so
the "This phone" card carries its own Stop. `allowBackup="false"` stays, and
`res/xml/data_extraction_rules.xml` excludes every domain from cloud backup and
device transfer (targetSdk 31+ no longer stops device transfer through
`allowBackup` alone).

**Decisions.**
- *Forced stop through proot, then a `/proc` sweep.* Rejected: `Process.destroy()`
  (SIGTERM, which proot ignores), SIGKILL to proot (guests survive untraced),
  reflection on the process pid (not public API on Android).
- *A system kill is not restarted.* Restarting would fight the platform's policy
  and, under a persistent limit, loop into a misleading safe mode; the user needs
  the guidance first.
- *Hard links as relative symlinks.* `Files.createLink` is denied on Android;
  copies would duplicate binaries such as git's dozens of hard-linked commands.
- *A JDK-only tar reader* instead of a dependency: four entry types and three
  header formats are all the producer emits.

**Only a device can verify:** proot launch at targetSdk 34 (W^X, 16 KB pages);
foreground-service survival, Doze, OEM process killers and background-start
limits; the signal and exit-status behaviour of proot above; the phantom-process
limit in a long run — several Workers and tool calls over an extended period, with
the developer option off and on; the recommended proot options; extraction time
and disk use; AndroidKeyStore wrap/unwrap; notifications with
`POST_NOTIFICATIONS` denied; the This phone flow end to end (the WebView loading
the local kernel with the cookie installed, no other 127.0.0.1 port reached,
the stop on leaving the app or switching kernels, Keep running across screen-off
and Doze, the wakelock following running tasks).

Verification on the host: the pure-JVM tier of `./test-local.sh` runs
`LocalKernelSupervisorTest` (fake process seams), `KernelProcessTest` (including a
real `/bin/sh` child for stdin, SIGTERM and SIGKILL) and `KernelSecretTest`; its
Robolectric tier runs `RuntimeInstallerTest` against real archives;
`./gradlew test` adds `LocalKernelServiceTest` and `LocalKernelManifestTest`
(merged manifest and backup rules). APK checks with stub libraries and assets:

```
mkdir -p /tmp/jni/arm64-v8a && : > /tmp/jni/arm64-v8a/libproot.so && : > /tmp/jni/arm64-v8a/libproot-loader.so
./gradlew :app:assembleDebug -Ptofu.localKernel.jniLibsDir=/tmp/jni -Ptofu.localKernel.assetsDir=<stub assets root>
unzip -lv app/build/outputs/apk/debug/app-debug.apk | grep -E 'lib/arm64-v8a/|assets/kernel/'  # .so Defl, .tgz Stored
$ANDROID_HOME/build-tools/35.0.0/zipalign -c -P 16 -v 4 app/build/outputs/apk/debug/app-debug.apk
$ANDROID_HOME/build-tools/35.0.0/aapt2 dump xmltree --file AndroidManifest.xml app/build/outputs/apk/debug/app-debug.apk | grep extractNativeLibs
```

## Device self-check

The on-device half of spike S1 ([docs/ANDROID_KERNELS.md](../docs/ANDROID_KERNELS.md))
lives in `app/src/main/java/com/tofu/client/selfcheck/`. Open it from the kernel
list, under **Copy diagnostics** → **Device self-check**; the screen is
`SelfCheckActivity`, the run is coordinated by `SelfCheckRun`, and
`SelfCheckService` is a `specialUse` foreground service so the background phase
runs under exactly the local kernel's conditions.

**Owner flow, three taps:** open **Device self-check**, tap **Run self-check**,
then share the report. When the foreground stress phase ends the screen asks you
to press Home; the background phase then runs under the foreground service, and
the completion notification carries **Share report** (it opens the share sheet
directly). The duration preset (1, 5, 15 or 60 minutes **per phase**, default 5)
is a diagnostic choice: 5 minutes is AOSP's `POWER_CHECK_INTERVAL`, so each phase
spans at least one system scan.

What a run measures, in order:

1. **Facts** (`AndroidDeviceFacts`): manufacturer/model/device, release, SDK and
   security patch, page size and clock tick (`Os.sysconf`), ABIs, targetSdk,
   versionName/buildId, `Os.uname()`, free storage and total RAM, battery-
   optimization exemption, standby bucket, background restriction, notification
   permission, the runtime versions in `assets/kernel/index.json`, and the
   SHA-256 of `libproot.so`/`libproot-loader.so` in `nativeLibraryDir`.
2. **Exec** (`ExecChecks`): `libproot.so --version` from `nativeLibraryDir`;
   under proot `/bin/true`, `/bin/sh -c 'echo ok'`, the payload's
   `/opt/tofu/python/bin/python3 -V` and `/usr/bin/git --version`; and a direct
   `execve()` of `rootfs/usr/bin/true` without proot — harmless, and expected to
   be refused at targetSdk ≥ 29. The guest checks reuse the kernel's own proot
   command line and environment, so they measure what the kernel will get.
3. **Kernel** (`KernelCheck`): start through `LocalKernel`, time to Ready, then
   `GET /api/v4/meta` and the cheap authenticated read `GET /api/v4/swarm/config`
   twice — without a credential (default deny must answer 401) and with
   `Authorization: Bearer <secret>` (200). A kernel the check started is stopped
   again; an already-ready one is only probed; a starting/stopping one is left
   alone and the check is skipped.
4. **Child-process stress** (`ChildStress`): one proot generation per phase with
   32 sleepers (AOSP's system-wide cap) plus 2 CPU-busy workers, and a deaths
   timeline with times and signals, sampled from `/proc` every second and from
   the orchestrator's `wait -n -p`. Every child is cleaned up afterwards
   (SIGQUIT to proot, then SIGKILL to survivors, matched by `/proc` start time).
   The stress test is skipped while a local kernel is active: its processes
   would be the cap's first victims and would corrupt the measurement.
5. **Exit reasons**: `ActivityManager.getHistoricalProcessExitReasons` for the
   app's own process on API 30+.

The report is one JSON document (`selfcheckVersion: 1`) with a `summary` that
maps onto the S1 questions, plus the human-readable summary screen. **Share
report** sends the JSON through the share sheet via the app's existing
FileProvider (`cache-path` already covers `cacheDir`). Reports contain no kernel
secret, tokens, cookies, account names, Wi-Fi names or file contents: collectors
never read them, HTTP probes keep only status and timings, and every string is
scrubbed at serialization (`SelfCheckRedactor`, tested in
`SelfCheckRedactionTest`).

Liveness bounds (the self-check's own contract, not tunables): 60 s per exec
command, 15 min for each kernel wait (start and stop), 1 s stress sampling,
15 s for stress cleanup. If the app process dies mid-run, the next open kills the
pids the run recorded and writes an `interrupted` report that embeds the last
one, with the app's exit reasons since it started.

Tests: the pure-JVM tier covers the report schema, redaction, the stress deaths
timeline and cleanup, and the runtime-missing skip paths with fake process
runners and system services; `AndroidSelfCheckFactsTest` (Robolectric) covers
the device facts against shadow power/usage/notification/activity services and
the share intent. See "Running the unit tests".

## Report a problem (error回传)

**Problem.** A tester may have only a phone, without adb, an emulator or a
reachable server. A WebView-only memory tail loses the native login failure and
all evidence after a crash.

**Decision.** Every native screen has a labeled **Copy diagnostic logs** action:
first-run onboarding, the server list, add/edit, the WebView (including loading
and failure pages), and the SSO popup. Room/vault initialization failures show
an independent recovery page with Copy and Share. Profile-storage errors are
visible on the list rather than hidden by onboarding. Copy needs neither a
server nor a responding JavaScript collector. The existing bug icon/report
sheet adds an optional note, web diagnostics, Share and Send to server.

**Captured evidence.** App version plus a 16-hex retained-source build ID,
Android/device/WebView version, current network transports/validation/metering,
camera/microphone permission state and foreground/background transitions;
profile operations/storage errors; authentication mode, retry/continuation and
verified-session outcomes; HTTP request IDs, sanitized endpoints, DNS/connect/
TLS stages, statuses, latency, response sizes, redirect destinations and cookie
scopes/attributes; WebView main/subresource errors, renderer loss, app console
errors and scoped-bridge support; supervisor action outcomes and file chooser,
external-app/download enqueue failures. HTTP request/response bodies and
credential header values are never inputs to native HTTP telemetry. Profile
metadata is the active screen's snapshot; authentication events record the
actual conditional repository-write outcome.

**Restart and privacy.** `TofuApplication` owns the ring and private journal
before constructing Room or the encrypted vault. Events are clipped and
redacted before memory/disk capture, and again at export. URL userinfo, queries
and fragments are removed; known password, cookie, OAuth token/client-secret,
API/private-key fields and Bearer values are masked, including quoted/escaped
JSON. The vault's password is never an input. The crash hook checkpoints the
redacted tail and delegates to Android's original uncaught-exception handler.
After restart the same Copy action includes previous events; Android 11+ also
reports the previous process-exit reason. A system kill/native abort cannot
promise a Java exception stack or preservation of an unflushed tail. A journal
write/read failure is included in the report and leaves in-memory copying
usable. Android 13+ marks copied reports sensitive to suppress clipboard previews.

**Bounds and ownership.** One launch-time JVM heap observation sizes the ring
at 200–300 events and each of two journal segments at 64–256 KiB. The upper
bounds serve the portable support/clipboard contract, not request concurrency.
Each event has at most 2,000 characters. A single lazy serial writer owns the
file stream; its pending tail is bounded to the ring capacity. It closes each
batch and performs no idle writes. Startup scans at most two segments and only
decodes the retained tail. Crash checkpoint waits at most two seconds; process
owner teardown joins the writer. Files remain under app-private `filesDir`,
are excluded from Android backup, and never share authority with profiles,
credentials or WebView cookies. Journal rotation replaces diagnostic cache only.

`ClientReport` retains at most 300 events/32 KiB of web text, clips each field
and bounds the entire serialized report to **192 KiB of UTF-8**, including JSON
escaping. Byte overflow removes old events/web detail, keeps the newest event
and marks `truncated`; ring eviction also marks it. This protects clipboard/
share Binder transactions with Unicode and large console output. Release
exception stacks may be obfuscated: retain the matching
`app/build/outputs/mapping/release/mapping.txt` beside each test APK, keyed by
its report build ID. The build ID hashes retained Android sources/resources
and build/shrinker configuration without requiring Git or device-side hashing.

**Alternatives considered.** Logcat alone needs tooling unavailable to the
phone tester. A server upload fails at the same moment as networking/auth.
Memory-only capture loses crash evidence. Dumping raw HTTP bodies/cookies risks
credentials; unbounded file queues and clipboard payloads fail under load.

**Measurement (host JDK 17, synthetic; not a phone benchmark).** 10,000 redacted
capture/enqueue operations took 211 ms (including the final queue barrier,
212 ms); the memory tail retained 300 entries, two journal segments
occupied 495720 bytes, restoring the retained tail took 26076 µs,
and exporting a 33181-byte report took 10000 µs. No Android emulator
was used. Tests cover restart recovery, corruption, rotation, journal write
failure, long/escaped secret values, Unicode export size, real loopback HTTP
stages/redirect privacy and actual Robolectric clipboard content/sensitive flag.
Run `./test-local.sh`, then `./gradlew test :app:assembleDebug :app:assembleRelease`.

**Serving side.** The endpoint lives in
`routes/api_v1/client_support.py`; reports are appended (JSONL, self-rotating,
newest-8-segments retained) to `<data_root>/client_reports/`
(`TOFU_CLIENT_REPORTS_DIR` overrides), and `GET
/api/v1/mobile/client-report?limit=N` lists the newest ones so the operator can
read what testers sent. The route is behind the normal auth gate, so on an
`open`-mode personal server it just works; a `private`-mode server that the app
has not authenticated to will answer 401/404 and the app degrades to
Share/Copy. Nothing here is trusted: the server re-clips every field and caps
the request body at 1 MiB.

## Open items
- **File upload — needs on-device confirmation** (2026-07-16): the "+" attach
  button in the SPA triggers a native `<input type="file">`, which a WebView will
  NOT turn into a system picker unless the host implements
  `WebChromeClient.onShowFileChooser`. That override was missing, so the upload
  window never opened in the app (Chrome worked). Fixed in `WebScreen.kt`
  (parks the `ValueCallback`, launches `FileChooserParams.createIntent()`, honors
  `multiple`, always resolves the callback — null on cancel/error — so the input
  can never wedge). NOT yet built/installed here (no network for Gradle);
  confirm on a tablet: tap "+" → picker opens → a selection yields a preview card
  → cancel-then-reopen works.
- **Camera capture**: the chooser now ALSO offers "take a photo" — the picker
  intent is wrapped in a chooser carrying `MediaStore.ACTION_IMAGE_CAPTURE`
  with a FileProvider-staged cache file (`res/xml/file_paths.xml`), and an
  OK-with-no-data result maps onto the parked capture uri. Shares the same
  pending on-device confirmation as the picker itself.
- **Cellular layer-1** (needs a phone): confirm whether cellular lands on the
  password page (fast path) vs an SSO screen (interactive handoff is implemented; the real IdP policy and cookie lifetime
  still require hardware validation).
- **URL stability** (needs a re-provision): confirm whether stop→start reuses the UUID.
- **On-device unverified**: QR scan, the deep-link cold/warm paths, the update
  banner's download, and the report sheet still need real hardware checks.
  Local JVM/Robolectric/lifecycle gates are not APK or device proof.
- **Next increment**: Play-Store signing.

## Installing & configuring

Three ways to open a server review form, all using the pure `DeepLink` parser
and the same storage URL authority:

1. **Scan a QR code** — ZXing's embedded `CaptureActivity` (dependency
   `com.journeyapps:zxing-android-embedded`, permission `CAMERA`). Shared by the
   first-run screen and the add form via `rememberQrScanner`.
2. **Paste a link** — reads the clipboard and accepts either a bare `http(s)://`
   URL or a `tofu://connect?url=…&name=…` link.
3. **Deep link** — `<activity>` declares a `tofu://connect` VIEW intent-filter
   with `launchMode="singleTop"`; `MainActivity.onNewIntent`/`onCreate` pre-fill
   the add form, warm or cold. Review its fields and choose **Save & connect**;
   receiving a link never adds a server or begins authentication by itself.

**First-run guidance.** When the app has no servers and setup has never been
completed (`data/OnboardingStore`, a plain SharedPreferences flag), the list is
replaced by `ui/OnboardingScreen` offering the three paths above plus "set up
later". Saving the first profile or choosing "set up later" retires onboarding
permanently, even if the list is later emptied. Merely receiving/scanning/pasting
a link opens review and does not mark setup complete.

**Official QR producer (server side).** So the app's scanner has a real source
instead of a hand-made code, Tofu serves one itself
(`routes/api_v1/mobile.py`):

- `GET /api/v4/mobile/connect` — a scan-to-connect page. Its QR is built from
  `location` in the browser, so the encoded address is correct behind any
  reverse-proxy prefix (code-server `/proxy/<port>/`, cloud IDE, …). Point the
  phone at it from the operator's authenticated browser.
- `GET /api/v4/mobile/connect/link?url=…&name=…` — JSON
  `{url, name, deepLink, qr}`: the `tofu://connect` deep link plus its PNG
  `data:` URI. `url` defaults to the request root but callers behind a proxy
  prefix should pass their exact address.

Both sit behind the normal auth gate. The deep link decodes to the same
`DeepLink.Connect` the scanner/paste paths use and opens the same review form.

**SSH kernels.** For a Tofu that runs on a machine you can SSH into (its
port stays on that machine's loopback): *Add → SSH tunnel*, enter host, SSH
port, user and the Tofu port (15000), then either copy *This phone's key* into
that machine's `~/.ssh/authorized_keys` or save the account password. The
first tap on the kernel shows the host's key fingerprint; compare it with
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the machine before
*Trust and connect*. A host whose key later changes is refused until the kernel
is deleted and added again. Design and limits:
[`docs/SUPERVISOR_DESIGN.md`](docs/SUPERVISOR_DESIGN.md) §6.

## Update check

`UpdateChecker` asks each configured server's `GET {base}/api/health`
concurrently. Health may be public inside Tofu but still gated by a proxy:
`UpdateProbe` selects cookies for the exact endpoint URL, disables automatic
cookie jars and all redirects, and accepts only a bounded JSON advertisement.
Decoded bodies are limited to 256 KiB, calls to eight seconds, and cancellation
closes the live socket. HTML, redirects, oversize bodies and transport failures
are unknown, not a successful “up to date” result. A valid newer advertisement
can still be offered when another server fails; partial knowledge cannot prove
that the whole server set is up to date.

A strictly newer `mobile_client_version` than `BuildConfig.VERSION_NAME` shows
an update banner opening `mobile_client_url`. Comparison is numeric and
segment-wise (`0.1.18 > 0.1.9`). Checks are not one-shot: failures have a 30-second
retry cooldown, successful checks a six-hour cooldown, and changed server URLs
or restored connectivity after a failure can retry. Only a started, visible
list runs the retry timer. Changed/deleted server sets fence old probe results.

Advertisements describe the published artifact, not an unbuilt source bump:
`published-release.json` currently records **0.1.15 / build 16**. The receipt-backed
backend metadata is independent of the local source's `BuildConfig` version;
source version 0.1.18/build 19 is not proof that such an APK was published.

## Performance, compatibility & localization

A hardening pass over the whole shell:

- **WebView teardown.** `AndroidView` never destroys its view on disposal, so
  leaving a server leaked a live `WebView` (and its renderer process) per visit.
  `WebScreen` now has `onRelease` that stops loading, detaches, and `destroy()`s.
- **One HTTP client per owner, not per recomposition.** `SupervisorControls`
  used a default `SupervisorClient()` parameter — re-evaluated on every
  recomposition, each allocating an OkHttp connection pool + dispatcher threads.
  It is now `remember`ed (the parameter is nullable and resolved once). The add
  form's `HealthProbe` is likewise `remember`ed instead of rebuilt per test tap.
- **Concurrent update probe.** `UpdateChecker` fans the `/api/health` probes out
  concurrently instead of serially, so a cold/dead fleet doesn't cost N×timeout.
- **Stable list callbacks.** The per-row status setter and the screen-level
  navigation callbacks are `remember`ed, and the header's "N running" aggregate
  is read inside the header, so a single card's status change no longer
  recomposes the whole list.
- **Scrollable onboarding.** `OnboardingScreen` gained `verticalScroll`, so the
  first-run content is reachable under large font scales / small screens.
- **Touch targets.** New inline controls (Scan QR / Paste link / Download / Set
  up later) use the shared 48dp `TouchTarget` (`Components.kt`).
- **Platform niceties.** `enableOnBackInvokedCallback` (Android 13+ predictive
  back) and a `<monochrome>` launcher layer for themed icons.
- **Bilingual UI.** Every user-facing string lives in a `values/strings*.xml`
  file with its `values-zh-rCN/` translation, selected by device locale
  (`StringResourcesTranslationTest` fails for a translatable default string
  without one). Session-
  layer diagnostics (`ServerLifecycle`, `TofuProbe`, retry copy) remain English
  by design — they are near-technical and pinned by pure tests.
- **Phone/tablet layout.** The server list is a `LazyVerticalGrid` with
  `GridCells.Adaptive(320dp)` — one column on a phone, two on a tablet — capped
  at `ListMaxWidth` (840dp) and centered. The add/edit form and onboarding cap a
  single column at `ContentMaxWidth` (600dp) and center it on wide screens
  (`CappedContent` / `CappedScrollContent` in `Components.kt`), with ruled
  section headers and wider spacing on the form. The list FAB is placed inside
  that capped column so it doesn't float off in a tablet corner. Onboarding now
  paints the same `background` as the rest of the shell (it previously sat on
  the bare `surface` colour). The WebView is deliberately left full-bleed — the
  SPA owns its own responsive layout.
- **Device-shape screenshots (Paparazzi).** `app/src/test/.../ScreenshotTest.kt`
  renders the native screens at phone (small/large/dark), tablet landscape and
  tablet portrait configs with the JVM `layoutlib` — no emulator. Run
  `./gradlew :app:recordPaparazziDebug` to (re)record golden PNGs under
  `app/src/test/snapshots/images/`; `./gradlew :app:verifyPaparazziDebug` then
  verifies against them. A plain `./gradlew test` only renders them and passes
  with stale goldens, so run the verify task after any change to a rendered
  screen. `TofuTheme`'s status-bar cast was made safe (`as? Activity`) so the theme
  renders off-device. The generated images are the artifact a reviewer opens to
  check phone-vs-tablet proportions.

## Build
Standard Gradle/AGP 8.5.2 project (Gradle 8.9 wrapper committed), `minSdk 26`,
`targetSdk 34`, JDK 17. Open in Android Studio or `./gradlew :app:assembleDebug`.

## Running the unit tests
The canonical target is `./gradlew test` (needs the Android SDK); rendered
screen changes also need `:app:verifyPaparazziDebug`. The offline `./test-local.sh`
runs actual sources with JDK 17 + `kotlinc`, cached Robolectric, genuine
AndroidX lifecycle and genuine SSH tiers. The full ladder requires every tier;
focused selectors run only the requested tier, never substitute a stub:

- **Pure-JVM tier** (`ServerUrl`, `LoginForm`, `CookieHeaders`,
  `ProfileForm`, `SessionManager` + `SessionController` via the `CookieSink` /
  `SecretVault` seams, kernel-kind dispatch, the local supervisor/process/secret
  and lifecycle suites, and the device self-check's report/redaction/stress/run
  suites). `HealthProbe`, `SupervisorRunner` and `UrlKernelController` compile
  in the actual dependency closure; platform behavior stays in Robolectric or
  the real SDK tier. No Android runtime is substituted.
- **Robolectric tier** (`CookieBridge` against a shadow `CookieManager`,
  including the local credential's host scope; bounded reauthentication and
  dead-link recovery; URL-kernel control; `RuntimeInstaller` against real archives). These use the
  actual Android test runtime, without a device/emulator. `LocalKernelService`,
  `ThisPhoneAndroidTest` and merged-manifest checks remain Gradle-tier because
  they need `Dispatchers.Main`, merged assets and AGP's merged manifest.
  Needs the Robolectric jars + an instrumented `android-all` in `LIBS`.
- **Genuine lifecycle tier**: set `LIFECYCLE_LIBS` to the verified local directory
  containing AndroidX ViewModel 2.8.6, annotation 1.8.0 and coroutines-android
  1.8.1 jars. It compiles the actual graph/ViewModel and uses real
  `ViewModelProvider`/`ViewModelStore` APIs, with no lifecycle/platform stubs.
- **Genuine SSH tier**: `SSH_CLASSPATH` supplies the declared MINA
  sshd-core/common 2.20.1, eddsa 0.3.0 and SLF4J jars. It compiles actual SSH
  carrier/identity/controller sources and tests trust/ownership semantics;
  it does not establish a live CodeLab login or cellular reachability.
- `PYTHON=/path/to/python3` selects the full ladder's SDK-free actual-DAO
  SQLite query specification. Its table is derived from the exported Room 3
  schema rather than a separately maintained table definition; the schema is
  included in the rechecked input manifest. This is SQL scope proof, not
  generated Room/APK proof.

Bare class arguments select the smallest pure-JVM suite; `--updates`, `--pure`,
`--robolectric`, `--lifecycle` or `--ssh` select one genuine tier and accept
that tier's class names. `TEST_OUTPUT=/path/to/output` retains compiler/JUnit
logs, source/dependency manifests, lifecycle class origins and an explicit exit
code. All recorded inputs are rechecked on success or failure; missing required
jars fail that tier rather than silently skipping it or fetching replacements.

The jars are fetched reproducibly by the committed `fetch-test-deps.sh` (pinned
versions, `MIRROR`-overridable with Maven Central fallback; it also extracts the
`classes.jar` from the androidx.test `.aar`s Robolectric needs). From a fresh
clone:

```
export JAVA_HOME=/path/to/jdk17            # e.g. Temurin 17
export KOTLINC=/path/to/kotlinc/bin/kotlinc  # kotlinc 1.9.24

./fetch-test-deps.sh /tmp/tofu-libs        # populate a LIBS dir from Maven
LIBS=/tmp/tofu-libs ./test-local.sh --pure
LIBS=/tmp/tofu-libs ./test-local.sh --robolectric
# Supply the official cached LIFECYCLE_LIBS and SSH_CLASSPATH, plus PYTHON,
# before running ./test-local.sh with no arguments for the full ladder.
```

(A JDK 17 + `kotlinc` on PATH are the only prerequisites the script does not
fetch. Verified end-to-end against a cold, empty `LIBS` dir.)

**Guarded invariants (each has a verified neuter check — the test fails if the
mechanism is removed):**
- `LoginForm.resolveAction` derives the login POST target from the served
  `<form action>` (resolved against the page URL), NOT the assumed origin-root —
  so a code-server behind a path prefix still authenticates (neuter: return
  origin-root → `LoginFormTest` subpath case fails). Gap-1.
- `CookieHeaders.toPersistentHeader` appends `Max-Age` to an expiry-less session
  cookie (neuter: drop the upgrade branch → `CookieHeadersTest` fails).
- `ReauthWebViewClient` clears its in-flight latch on the observed OUTCOME
  (`reauthSettled()`), not a timer (neuter: make `reauthSettled` a no-op →
  the Robolectric latch test fails). Gap-2.
- `CookieBridge.purgeHost` clears both cookies AND per-host web storage
  (`WebStorage.deleteOrigin`) for the dead host. Gap-4.
- `SessionManager.updateUrlAndReauth` calls `purgeHost(oldHost)` on a host change
  and not on a same-host edit (neuter: remove the purge call →
  `SessionManagerReauthTest` fails).
- `SessionController.editProfile` routes a URL host change through
  `updateUrlAndReauth` (neuter: bypass it → `SessionControllerTest` host-change
  purge case fails), and a rename MOVES the alias-keyed secret to the new alias
  (neuter: drop the move → the credential is orphaned and the rename test fails).
- `SessionController.deleteProfile` removes both the secret and the row (never
  orphans a credential).
- `TofuProbe.classify` splits a 401 on the envelope — Tofu's
  `{"ok":false,"error":{…}}` (error OBJECT) → `TOFU_AUTH`, the gateway's
  `{"error":"Unauthorized"}` (error STRING) → `GATEWAY` — and only 200 with a
  `bootId` counts as Tofu (neuter: drop the envelope check → `TofuProbeTest`'s
  discrimination case flips to `GATEWAY`).
- `SessionManager.login` retries only transient failures with a bounded
  backoff, on TWO ladders (`LoginRetryPolicy`): ordinary transport `Error`s
  get 3 attempts (1s/2.5s); a WARMING sandbox — the proxy edge's 502/503/504
  page, or a connect timeout (edge up, nothing behind the tunnel yet) — gets
  6 attempts (2s→8s, 28s of backoff plus request time) because a cold container routinely
  takes tens of seconds before anything listens. `BadCredentials` /
  `NeedsInteractiveSso` / `Success` are definitive answers and return
  immediately, even mid-ladder (neuter: retry definitive outcomes →
  `SessionManagerLoginRetryTest` fails on the wrong-password path re-POSTing;
  collapse the ladders → its warming cases fail the 6-attempt schedule).
- `TofuProbe.classify` maps 502/503/504 to WAKING (distinct from
  UNREACHABLE): the add/edit screen tells the user to wait and re-test rather
  than edit a correct URL (neuter: fold WAKING into UNREACHABLE →
  `TofuProbeTest`'s waking-guidance case fails).
- `ReauthWebViewClient` reports MAIN-FRAME failures to the host's recovery
  overlay: the proxy edge's 5xx page while the sandbox wakes MUST surface as
  the app's retry UI (the WebView's own error page is a dead end on a
  phone), while sub-resource failures, aborted navigations and the 401
  re-auth path must NOT blanket the page (neuter: report every failure →
  `ReauthWebViewClientFailureTest`'s sub-resource case fails).
- The post-login v4 meta preflight swaps `Success` for `Incompatible` ONLY on a
  definitive refusal (wrong `apiMajor`, or `minAndroidBuild` above this build);
  404 / unparseable / transport failure keep `Success` (fail-open), and
  `AuthType.NONE` stays a zero-request short-circuit with NO preflight (neuter:
  drop `withApiPreflight` → `SessionManagerPreflightTest`'s mismatch cases stay
  `Success`; fire it for NONE → the degrade suite's zero-HTTP pin fails).


## Release / cutting a version

1. Configure the private signing values and cross-repository PAT in CI. Protect
   the key/pin for all future production updates; never commit or print them.
2. Increase `versionCode` beyond both the previous Android tag and the published
   receipt (build 16 remains a baseline even with no Android tags). Set the
   matching `versionName` and `android-v<versionName>` tag.
3. Only with explicit publication authorization, push the reviewed source/tag
   and require debug tests/build plus release/signature/manifest checks to pass.
   Source implementation does not authorize tag pushes or release publication.
4. Download APK and receipt from that exact release. Compare size/SHA-256,
   verify signature/certificate and manifest, and perform device install/update
   checks. Merely finding an asset name is not signature evidence.
5. Only then promote the receipt into `android/published-release.json` in a
   reviewed commit. The receipt is also included by `tofu.spec`; normal operator
   restart/redeployment is needed for health to advertise the new artifact.
   Publication alone cannot change the pinned download.

`python3 validate_release.py --help` describes local validation inputs. A receipt
from the SDK's real `apksigner` and `aapt` is evidence of the local APK, not proof
that uploaded/downloaded bytes match. Root tests include real JDK private/public
certificate and relative-path checks plus simulated native-tool failures. They
are not a signed release, SDK/Compose build or device install test.
