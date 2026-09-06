# Tofu iOS client

The iOS WebView shell for Tofu — a port of `android/` with the same session
architecture: the app handles sign-in, server management and diagnostics; the
Tofu SPA itself renders inside a WKWebView.

**Status: in development.** CI builds unsigned simulator binaries only; there
is no TestFlight/App Store distribution yet. The settings page's iOS row
flips from "in development" to a real download link once
`TOFU_IOS_CLIENT_URL` points at a published build.

## Layout

```
Package.swift            SwiftPM authority for TofuClientCore (Foundation-only)
project.yml              XcodeGen authority for the app target — the .xcodeproj
                         and Info.plist are GENERATED (never committed, see
                         .gitignore); regenerate with `xcodegen generate`
Sources/TofuClientCore/  the ported logic, no UI frameworks:
  ├─ ServerUrl / LoginForm / ApiMetaGate / TofuProbe / HealthProbe
  │    URL + handshake + reachability rules (the vscode port-forwarding
  │    discrimination: edge-401 ≠ Tofu-401 ≠ not-Tofu)
  ├─ SessionManager / SessionController / InteractiveSso / ReauthCoordinator
  │    login replay with bounded retry, host-change jar purge, SSO hand-off,
  │    latched + failure-capped headless re-auth
  ├─ ProfileStore (SQLite) / SecretStore (Keychain) / CookieSink (WKWebView jar)
  ├─ SupervisorClient / SupervisorUrl / SupervisorBridge / SupervisorRunner
  │    start/stop via the sibling-port supervisor + the start-pending
  │    forgiveness handshake
  └─ ServerLifecycle / ServerListViewModel / ProfileForm
       pure state machines the SwiftUI layer renders
Tests/TofuClientCoreTests/  XCTest ports of android/app/src/test
TofuClient/              the SwiftUI shell (the only UIKit/WebKit/SwiftUI code):
  ├─ TofuClientApp       entry point, builds the dependency graph
  ├─ RootView            list / add-edit / web navigation switch
  ├─ AppViewModel        wiring + status (all session logic lives in the core)
  ├─ ProfileListView     server list/switcher + status chips
  ├─ AddEditProfileView  validated form + live connection probe
  ├─ SupervisorControlsView  Start/Stop/Check + start-pending commit
  └─ WebScreenView       WKWebView + re-auth delegate + TofuNative/TofuDiag
                         bridges + tofu:native-visibility + TLS gate
```

## Build & test

Requires a Mac with Xcode 15+ (iOS 15 deployment target).

```bash
cd ios
swift test                       # core unit tests, no simulator needed
brew install xcodegen
xcodegen generate                # project.yml → TofuClient.xcodeproj + Info.plist
open TofuClient.xcodeproj        # or: xcodebuild build -scheme TofuClient \
                                 #   -destination 'generic/platform=iOS Simulator' \
                                 #   CODE_SIGNING_ALLOWED=NO
```

From the repo root (no Swift toolchain needed), the structure pins:

```bash
python3 -m pytest tests/test_ios_client_structure.py
```

CI (`.github/workflows/build-ios.yml`) runs both gates on every `ios/**`
change: `swift test` on a macOS runner, then an unsigned simulator build.

## Invariants shared with android/ (port both ways or neither)

- The session cookie is `Domain`-pinned to the full host; a baseUrl host
  change hard-purges the old host's jar (and website data) BEFORE re-login.
- The secret is keyed by ALIAS, so a re-provisioned sandbox (new URL, same
  logical server) reuses it after a one-tap URL edit.
- code-server auth is per-host: a blank password on a host that already has
  one saved reuses it.
- INTERACTIVE_SSO is never intercepted by the re-auth machinery.
- The supervisor sits on the sibling proxied port behind the same
  code-server gate; the sibling is DERIVED from the profile URL's own
  `/proxy/<port>/` segment (15000 → 15001, and any non-default port → its
  own +1), falling back to 15001 only without a numeric segment. Start
  commits a 45 s start-pending marker the web screen stamps into the page at
  document start.
- Login retry distinguishes a WARMING sandbox (proxy-edge 502/503/504, or a
  connect timeout — the edge is up but nothing listens behind the tunnel
  yet) from an ordinary flaky tunnel: warming rides a longer 6-attempt
  ladder (2s→8s) because a cold container takes tens of seconds; definitive
  answers (badCredentials / SSO hand-off / success) never retry, even
  mid-ladder. `TofuProbe` surfaces the same 5xx as a WAKING verdict with
  wait-and-retest guidance instead of an unreachable error.
- Wire names `TofuNative.requestReauth`, `TofuDiag.deliver`,
  `tofu:native-visibility`, `window.tofuStartPending` are a contract with
  `frontend/`'s native bridge — rename nowhere or everywhere.

## Distribution

Planned: a signed ipa via a secrets-bearing workflow (TestFlight), then the
settings-page iOS card points at it through `TOFU_IOS_CLIENT_URL`
(`routes/common.py`). Until then the app is sideloaded by building from
source on a Mac.
