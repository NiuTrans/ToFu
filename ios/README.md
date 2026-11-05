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
| `ServerUrl`, `SupervisorUrl`, `FormEncoding` | URL authority, proxy prefixes, endpoint and form encoding |
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
  Proxy/absproxy defaults apply only when creating a profile. URL userinfo is
  rejected; IPv6, non-default ports and code-server prefixes are preserved.
- Discovery starts at the configured app URL and follows at most eight
  same-origin redirects. Cookies are selected for each full request URL. A
  foreign redirect, absent password or unknown/ambiguous form opens interactive
  sign-in. Saved credentials are submitted only to one recognized code-server
  form, preserving hidden fields and the return target.
- Cached cookies and browser landing pages are candidates. Authentication
  requires protected `/api/v4/conversations?limit=1&meta=1` metadata or Tofu's own
  authentication refusal. A stopped app may use the configured supervisor's
  versioned status response. Public health/meta, HTML and unrelated JSON do not
  prove the gate has been passed. A valid issued code-server cookie after a
  trusted password submission is also accepted, as on Android.
- Cookie host stamps compare profile id, alias, URL and saved auth atomically;
  a changed/deleted profile cannot be promoted by an old request. Recency and
  project path changes are preserved. Password sharing requires the same HTTP
  origin and code-server prefix.
- Interactive mode is temporary screen state; it does not overwrite saved
  auth. Returning to the configured login page may resume password sign-in
  once. Main-frame messages and native reauth accept only the configured
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
prompts, media capture or simulator linkage. Device checks remain necessary.
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
