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

## Feasibility spike findings (verified end-to-end through the public gateway)

1. **Layer 1 (gateway) enforces no interactive SSO on this path.** An unauth
   `GET …/proxy/15000/` 302s to a **relative** `./../../login` — code-server's
   own login, not an SSO IdP redirect. Confirmed by replaying the full loop
   through the public host.
2. **Layer 2 (code-server `--auth password`) is the sole gate and is fully
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
  LoginForm.kt        pure <form action> resolver (Gap-1)
  ProfileForm.kt      pure add/edit validation (alias/URL/secret rules)
  CookieBridge.kt     OkHttp cookie → WebView jar; Max-Age upgrade + flush; purgeHost
  TofuProbe.kt        pure Tofu↔gateway 401 discrimination + paste-time verdicts
                      (mirror of lib/desktop_agent/_probe.py: {"ok":false,"error":{…}}
                      vs {"error":"Unauthorized"}; 200+bootId is the Tofu signal)
  HealthProbe.kt      GET {base}/api/health WITH the session cookie — a cookie-less
                      probe would measure the gate, not the server
  SessionManager.kt   headless login (bounded transport retry + post-login
                      v4 meta preflight), URL-change purge+relogin, profile
                      update path
  ApiMetaGate.kt      pure GET /api/v4/meta verdict — blocks only on a
                      definitive apiMajor / minAndroidBuild mismatch, fail-open
                      on partial knowledge (404 / unparseable / transport)
  SessionController.kt orchestrates add/edit/delete/activate over DAO+vault+manager
                       (rename moves the alias-keyed secret; delete removes it;
                        host-change routes through updateUrlAndReauth)
  ReauthWebViewClient 302→/login or 401 ⇒ silent re-auth; latch clears on outcome
```

**Design invariants**
- Secret binds to `alias`, never to `baseUrl` → a re-provisioned sandbox reuses
  the credential after a one-tap URL edit.
- Cookie injection always `flush()`es (else a cold kill loses it).
- `SessionManager` uses `followRedirects(false)` — we need the 302 + Set-Cookie,
  not the redirect target.

## Distribution
Release APKs ship via **GitHub Releases** — NOT self-hosted from Tofu, NOT a
browser-extension bundle (an Android app has no host to embed in). The Tofu web
UI surfaces a download card in **Settings → General → About & Update** from
`GET /api/health` → `mobile_client_url`, which **defaults to a DIRECT APK deep
link**:

```
https://github.com/rangehow/tofu-android/releases/latest/download/tofu-android.apk
```

GitHub's `/releases/latest/download/<asset>` is a stable redirect that always
serves the newest release's asset and triggers a real download on tap — exactly
what a phone needs. `TOFU_MOBILE_CLIENT_URL` overrides it (e.g. to pin a
specific version's asset).

**Why the APK release stream stays on `rangehow/tofu-android` even though the
source moved into `rangehow/ToFu`:** `/releases/latest/download/<asset>`
resolves to the CHRONOLOGICALLY NEWEST release in the repo. Co-locating the
APK on the ToFu release stream would let any newer desktop-only release
(`v0.14.x`, which carries no `tofu-android.apk`) shadow the deep link into a
permanent 404. A dedicated repo has no competing release stream, so the link
can never be shadowed.

**Graceful-degradation tradeoff (deliberate):** before the first tagged release
this deep link **404s** — an honest "not published yet". We chose that over the
releases *page* (`/releases/latest`), which never 404s but on a phone dumps the
user into a list of wrong-platform desktop installers with no APK. Since the
user's need is an **on-device install**, a clean 404-until-published beats a
misleading wrong-platform page. The frontend still hides the link defensively if
the URL is blanked, so emptying `TOFU_MOBILE_CLIENT_URL` never yields a dead
button.

The URL's filename (`tofu-android.apk`) and the CI-published asset name are the
**same string**, kept in lockstep by `tests/test_mobile_client_apk_url.py`
(same repo: backend `MOBILE_CLIENT_APK_ASSET` ⇔ workflow publish list) so the
deep link can't silently rot into a 404.

## Building the APK & CI
`.github/workflows/build-android-apk.yml` (repo root, in the ToFu monorepo):
- **every push/PR touching `android/**`** → runs `./gradlew test` +
  `assembleDebug` and uploads the debug APK as a build artifact (so the build
  can't silently rot);
- **on an `android-v*` tag** → `assembleRelease`, **renames the output to
  `tofu-android.apk`** (Gradle emits `app-release[-unsigned].apk`, which would
  NOT match the deep link), and publishes exactly that asset **to the
  `rangehow/tofu-android` repo's Releases** (`fail_on_unmatched_files: true`,
  so a missing/misnamed APK fails the release loudly instead of silently
  shipping a 404 link). The tag prefix is `android-v*` — NOT bare `v*` — so
  desktop releases (`v0.14.x`) never fire an APK build.

### Release & signing
A release APK must be **signed** to install on a normal device. Because this is
**sideloaded GitHub-Release distribution** (not Play Store), the `release` build
type is signed with the SAME fixed, committed **debug keystore**
(`app/debug.keystore`) used for debug builds — see `signingConfig` in the
`release` block of `app/build.gradle.kts`. This makes `assembleRelease` produce
a signed, installable APK with no repo-secret setup, and — because the key never
changes across builds — every future release installs in-place over the previous
one. Signing with the debug *key* does NOT make the build debuggable
(`debuggable` stays false on `release`). Before any **Play Store** submission,
switch to a secret-backed `signingConfigs.release` (keystore base64 + passwords
as repo secrets). The workflow's release step publishes whatever
`assembleRelease` produces; an **unsigned** APK is inspection-only and rejected
by Android's installer.

> **First install may need one uninstall.** The in-place-update guarantee holds
> only between APKs signed with this committed key. The robot-icon build some
> users already have predates the fixed-keystore commit (`56115a3`) and any
> tagged release, so it was likely a hand-distributed debug build signed with an
> ephemeral key. Android rejects an update across a signature change
> (`INSTALL_FAILED_UPDATE_INCOMPATIBLE` / "App not installed"). Expected, not
> alarming: **uninstall the old app once, then install the new signed release;
> all subsequent updates are in-place.**

This signing setup + the on-device cookie-persistence test are the parts that
require a real Android SDK / device — the signed APK is first actually built by
the tag build in CI.

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
- **Cellular layer-1** (needs a phone): confirm a phone on cellular lands on the
  password page (fast path) vs an SSO screen (`AuthType.INTERACTIVE_SSO` handles
  it — WebView completes SSO once, jar persisted).
- **URL stability** (needs a re-provision): confirm whether stop→start reuses the UUID.
- **Next increment**: the profile-list / add-server / switcher Compose UI, and a
  signed release APK built on an Android-SDK machine (the one step that can't run here).

## Build
Standard Gradle/AGP 8.5.2 project (Gradle 8.9 wrapper committed), `minSdk 26`,
`targetSdk 34`, JDK 17. Open in Android Studio or `./gradlew :app:assembleDebug`.

## Running the unit tests
The canonical target is `./gradlew test` (needs the Android SDK). For a fast
proof without the SDK, `./test-local.sh` runs two tiers on a plain JDK 17 +
`kotlinc`:

- **Pure-JVM tier** (132 tests: `ServerUrl`, `LoginForm`, `CookieHeaders`,
  `ProfileForm`, `SessionManager` + `SessionController` via the `CookieSink` /
  `SecretVault` seams (incl. the login-retry and meta-preflight suites),
  `ApiMetaGate`, `InteractiveSso`, `ServerLifecycle`, `SupervisorUrl`,
  `TofuProbe`) —
  no Android runtime. `SupervisorRunner` stays Gradle-tier (it needs
  `SupervisorClient` → `android.webkit` + `org.json`, absent from the pure
  classpath).
- **Robolectric tier** (6 tests: `CookieBridge` against a shadow `CookieManager`;
  `ReauthWebViewClient` latch + consecutive-failure cap + dead-link routing) — runs headless on the JVM, no device/emulator.
  Needs the Robolectric jars + an instrumented `android-all` in `LIBS`.

The jars are fetched reproducibly by the committed `fetch-test-deps.sh` (pinned
versions, `MIRROR`-overridable with Maven Central fallback; it also extracts the
`classes.jar` from the androidx.test `.aar`s Robolectric needs). From a fresh
clone:

```
export JAVA_HOME=/path/to/jdk17            # e.g. Temurin 17
export KOTLINC=/path/to/kotlinc/bin/kotlinc  # kotlinc 1.9.24

./fetch-test-deps.sh /tmp/tofu-libs        # populate a LIBS dir from Maven
LIBS=/tmp/tofu-libs ./test-local.sh        # → 132 pure-JVM + 6 Robolectric green
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
  6 attempts (2s→8s, ≈28s worst case) because a cold container routinely
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

Publishing a new App build is a **deterministic, tag-triggered** flow — no
manual APK upload, no per-build keystore setup. The device download link the
Tofu backend serves (`DEFAULT_MOBILE_CLIENT_URL` →
`…/releases/latest/download/tofu-android.apk`) always points at whatever the
newest tagged release published, so cutting a version IS the delivery.

### Prerequisites
- A machine/terminal with **GitHub write access** to `rangehow/ToFu` (the
  source monorepo). The cross-repo publish to `rangehow/tofu-android` is done
  by CI via a PAT secret — see the workflow. The release APK is signed with
  the **committed** `app/debug.keystore` (see below), not a repo secret.

### Steps
1. **Bump the version** in `app/build.gradle.kts` `defaultConfig`:
   - `versionCode` — integer, MUST strictly increase (Android refuses a
     downgrade install); e.g. `12`.
   - `versionName` — human string, e.g. `"0.1.11"`.
   Commit the bump together with the change it ships.
2. **Tag and push** (fast-forward; never force):
   ```bash
   git push origin master
   git tag android-vX.Y.Z   # e.g. android-v0.1.11 — MUST match versionName
   git push origin android-vX.Y.Z
   ```
   The `android-v*` tag is what triggers the release path in
   `.github/workflows/build-android-apk.yml` (a plain push to `master` only
   builds/tests the debug APK — it does NOT publish a release).
3. **Watch CI** (Actions → the `vX.Y.Z` run). On the tag it runs, in order:
   `keystore hash guard` → `versionCode guard` → `Assemble release APK` →
   `Rename release APK to canonical asset name` → `Publish APK to GitHub
   Release`. All must be green (the guards are described below).
4. **Verify the asset name.** Open the `vX.Y.Z` Release page and confirm the
   attached asset is **exactly** `tofu-android.apk`. This is load-bearing: the
   backend deep link 404s on any other name. The coupling is guarded by the
   backend test `tests/test_mobile_client_apk_url.py` (asset name ==
   `MOBILE_CLIENT_APK_ASSET`), but eyeball it on the Release page too.
5. **Install / verify on device.** The published APK is directly installable and
   installs *over* any prior version (same signing key), so testers just tap the
   download link and update in place — no uninstall.

   **Failure mode — signature mismatch.** Android refuses an in-place update
   when the new APK is signed with a DIFFERENT key than the installed one,
   failing with `INSTALL_FAILED_UPDATE_INCOMPATIBLE` / "App not installed".
   With the current setup this should never happen — every tag is signed with
   the SAME committed `app/debug.keystore` (verify across two tags with
   `git rev-parse android-v<old>:android/app/debug.keystore` == `git rev-parse
   android-v<new>:android/app/debug.keystore`). It CAN happen if someone (a) migrated the
   release to a secret-backed `signingConfigs.release`, or (b) the tester's
   existing install came from a locally-built APK signed with a personal debug
   key. **Fix:** uninstall the old app first, then install the new APK
   (a one-time step; subsequent same-key updates install over each other
   normally). Uninstalling clears the app's local data (saved profiles), which
   on a fresh single-user setup is harmless.

### CI release guards (build-android-apk.yml)
Two guards in `.github/workflows/build-android-apk.yml` make the
in-place-update and version-monotonicity invariants fail loudly instead of
silently breaking:

- **`versionCode` guard (tag builds only).** On an `android-v*` tag, CI fetches
  the previous `android-v*` tag, parses `versionCode` from that tag's
  `android/app/build.gradle.kts`, and fails unless the current `versionCode` is
  **strictly greater** (Android refuses a downgrade install). The first tagged
  release has no previous tag, so the guard skips gracefully.
- **Keystore hash guard (all builds).** Release APKs are signed with the
  committed `app/debug.keystore`; if that file ever changes, in-place updates
  silently break (`INSTALL_FAILED_UPDATE_INCOMPATIBLE`). CI compares the
  keystore's sha256 against the previous `android-v*` tag's committed keystore
  and fails on any difference (skips when no previous tag exists). Changing the
  keystore is therefore a loud, deliberate act requiring a coordinated one-time
  uninstall — never a silent signature break.

### Signing (why no secret is needed)
`build.gradle.kts` binds BOTH the `debug` and `release` buildTypes to a fixed,
committed debug keystore (`app/debug.keystore`, `storePassword`/`keyPassword` =
`android`). Because the key never changes, every CI build — and every local
`./gradlew assembleRelease` — produces an APK with the SAME signature, so
release updates install over each other (and over debug installs) without
`INSTALL_FAILED_UPDATE_INCOMPATIBLE`. A committed debug keystore is standard
practice for a sideloaded, non-Play-Store test build and is **not** a secret.
Signing with the debug *key* does not make the build debuggable — release keeps
`isDebuggable=false`. Switch to a secret-backed `signingConfigs.release` only
before any Play Store submission.
