# Tofu iOS client

The iOS WebView shell for Tofu — a port of `android/` with the same session
architecture: the app handles sign-in, server management and diagnostics; the
Tofu SPA itself renders inside a WKWebView.

**Status: in development.** CI builds unsigned simulator binaries only; there
is no TestFlight/App Store distribution yet. The settings page's iOS row
flips from "in development" to a real download link once
`TOFU_IOS_CLIENT_URL` points at a published build.

## Ownership

`Package.swift` owns the core library and tests. `project.yml` owns the app
build; XcodeGen's project and Info.plist are generated outputs.

| Owner | Responsibility |
|---|---|
| `ServerUrl`, `SupervisorUrl`, `FormEncoding` | Credential-free saved URLs, transient navigation, proxy prefixes and encoding |
| `ConnectionOnboarding`, `ConnectionReviewState`, `ProfileOperationGeneration` | Review-only inbound metadata and UI-intent fencing |
| `SessionManager`, `LoginRetryPolicy` | Session orchestration, retries, stale-operation checks |
| `GatewayLogin`, `LoginForm` | Bounded discovery and recognized password submission |
| `GatewaySessionProbe`, `GatewayHTTP` | Protected endpoint evidence, per-URL cookies, explicit redirects |
| `URLSessionHTTPClient`, `HTTPExchangeDelegate` | One reusable pool, per-request policy, response bounds and cancellation |
| `CookieHeaders`, `BrowserCookies`, `CookieScope` | Cookie attributes, retention and request selection |
| `ProfileStore`, `SQLiteProfileStore`, `SecretStore` | Atomic profile writes and alias-keyed credentials; Keychain is Apple-only |
| `InteractiveSso`, `ReauthCoordinator`, `TrustedWebBridge` | Browser handoff, reauthentication and main-frame/origin admission |
| `SupervisorClient`, `SupervisorRunner`, `ServerLifecycle` | Remote control and current-card lifecycle |

`TofuClient/` owns SwiftUI/WebKit wiring. Browser policy belongs in the core;
WebKit adapters await store callbacks and release outstanding screen tasks on
disposal. The SPA remains the application UI authority.

Problem: a single session implementation mixed URL guesses, password replay,
cookie storage and authentication proof, while structure checks could pass even
when the Swift code did not compile. Decision: use explicit owners and real
Foundation, HTTP and SQLite tests, following Android's maintained
[Gateway authentication contract](../android/README.md#gateway-authentication-contract).
Alternatives rejected: arbitrary first-form submission, public health probes as
proof of login, unconditional profile stamps, and platform stubs that conceal
production compile or lifecycle failures.

## Authentication contract

- Saved `NONE` remains an explicit choice and performs zero native requests.
  Proxy/absproxy defaults apply only to new profiles/drafts, never saved rows.
  Saved, QR and pasted URLs reject userinfo and credential-like query/fragment
  fields, including encoded names and nested redirect/query values. Canonical
  URLs preserve IPv6, ports, proxy prefixes and safe SPA parameters. Rejected
  labels never echo raw input. Legacy URLs are sanitized only for explicit
  editing; activation never rewrites their URL or authentication choice.
- `tofu://connect?url=<encoded-http(s)-url>&name=<optional-name>` and the Paste
  action open review metadata only: no imported passwords, project paths,
  persistence, probes or sign-in. The connection-link protocol accepts up to
  8 KiB of input and an optional 80-character name. Every open form, including
  an untouched edit, retains its identity and fields until **Use link** replaces
  it or **Keep form** discards the suggestion. Only the latest pending metadata
  is retained. Submitted passwords and disappearing/replaced form drafts are
  cleared; no password is stored in connection-review state.
- `AppViewModel` owns one cancellable Add/Edit/Open/Delete operation. Cancel,
  Back, form edits, links, review decisions and new selections invalidate its
  generation. Results can publish errors or navigate only while that intent is
  current and the actual stored configuration still matches. Recency/cookie/
  instance stamps are not configuration edits. A deleted row, including a
  saved `NONE` snapshot, can never fall back to the rendered list copy.
  Edit credentials are reconciled only after the intended row is read back,
  even if Cancel arrives during a nonthrowing commit; changed vault keys are
  preserved with an error instead of overwritten, and SQLite/Keychain still
  cannot provide a shared crash-atomic transaction.
- Discovery starts at the configured app URL and follows at most eight
  same-origin redirects. Cookies are selected for each full request URL. A
  foreign redirect, absent password or unknown/ambiguous form opens interactive
  sign-in. Saved credentials are submitted only to one recognized code-server
  form, preserving hidden fields and the return target.
- Cached cookies and browser landing pages are candidates. Authentication
  requires protected `/api/v4/conversations?limit=1&meta=1` metadata with the
  maintained success envelope's `meta.requestId` or Tofu's own authentication
  refusal (whose `requestId` remains top-level). A stopped app may use the
  configured supervisor's versioned status response. Public health/meta, HTML and unrelated JSON do not
  prove the gate has been passed. A valid issued code-server cookie after a
  trusted password submission is also accepted, as on Android.
- Cookie host stamps compare profile id, alias, URL and saved auth atomically;
  a changed/deleted profile cannot be promoted by an old request. Recency and
  project path changes are preserved. Password sharing requires the same HTTP
  origin and code-server prefix.
- Interactive mode and credential-bearing SSO callbacks are temporary screen
  state; `parseNavigation` admits callbacks but cannot authorize persistence.
  They do not overwrite saved auth. Returning to the configured login page may
  resume password sign-in once. Main-frame messages and native reauth accept only the configured
  origin, including scheme and port. Wire names `TofuNative.requestReauth`,
  `TofuDiag.deliver`, `tofu:native-visibility`, `window.tofuStartPending` and
  `SupervisorBridge.pendingDefaultsKey` stay shared with the SPA.
- Authentication responses have a fixed 256 KiB protocol bound, enforced while
  receiving data. HTTP 429 is definitive. Transient failures use three attempts;
  warming 502/503/504 and timeouts use six. Cancellation stops retries and
  prevents a late response from injecting cookies or stamping a profile.
- Only an expiry-less `code-server-session` receives seven days of local
  retention. Other session cookies remain session cookies; explicit expiry,
  Domain/host-only, Path, Secure, HttpOnly and SameSite policy are retained.
  Empty attributes and oversized Max-Age numbers cannot crash the parser.
  Foundation conversion sets the absolute expiry and string flags explicitly;
  reusing `HTTPCookie.properties` alone loses flags on some Foundation builds.

## Build and verification

Core tests require Swift 5.9+, Python 3 for the loopback fixture, and system
SQLite headers on Linux. They execute production Foundation/URLSession and
SQLite code without Keychain or WebKit substitutes:

```bash
swift test --package-path ios
python3 -m pytest tests/mobile/test_ios_client_structure.py
```

`SecurityRegressionsTests` and `ConnectionIntentTests` cover saved/transient URL
separation, legacy editing, review choices and superseded UI intents. Compiled
fault injection also exercises a late controller handoff, deletion during a
real SQLite recency write and cancellation after a password request reaches the
transport. Existing Foundation pool/response-bound and atomic SQLite tests
remain part of the core gate; source structure or syntax checks are not builds.

CI runs the core on Linux and macOS. The Apple-only SameSite adapter test runs
on macOS. The existing macOS job also builds an unsigned iOS simulator app.
A local simulator build requires Xcode 15+ and XcodeGen:

```bash
cd ios
xcodegen generate
xcodebuild build -project TofuClient.xcodeproj -scheme TofuClient \
  -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO
```

Linux success does not validate Keychain, actual WKWebView persistence, TLS
prompts, media capture or simulator linkage. Device checks remain necessary,
including cold/warm OS or QR links, Use link/Keep form on dirty edits, password
draft cleanup, SSO callbacks and cancellation without late screen changes.
WebKit's public website-data records are grouped by domain and do not expose
scheme/port: old-profile cookie removal is exact-host/path aware and preserves
parent-domain IdP cookies, while storage cleanup removes only records naming
the exact old host. It cannot promise Android's exact-origin storage deletion;
broader domain deletion was rejected because it affects other profiles. A
profile-scoped WebKit data-store design needs a separate migration and device
validation. Popup-based SSO still uses the system browser; cookies are never
imported from that browser into the app.

## Distribution

Planned: a signed ipa via a secrets-bearing workflow (TestFlight), then the
settings-page iOS card points at it through `TOFU_IOS_CLIENT_URL`
(`routes/common.py`). Until then the app is sideloaded by building from
source on a Mac.
