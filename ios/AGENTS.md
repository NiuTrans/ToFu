# ios/ directory guide

iOS WebView client. The authoritative architecture and the shared session
invariants live in `ios/README.md`; this file is the operational map.

## Ownership and generated boundaries

- `Package.swift` + `Sources/TofuClientCore/` are the authority for all
  session/supervisor/reauth LOGIC. `README.md` maps the gateway, transport,
  cookie and repository owners. The core must stay Foundation-only: no
  SwiftUI/UIKit imports, WebKit only behind `#if canImport(WebKit)`
  (WebKitCookieSink). `swift test` must keep running without a simulator.
- `project.yml` is the authority for the app target. `TofuClient.xcodeproj/`
  and `TofuClient/Info.plist` are XcodeGen OUTPUTS — never edit or commit
  them; regenerate with `xcodegen generate`.
- `TofuClient/` is the only place UI frameworks may appear. Keep it wiring —
  any rule a unit test could express belongs in the core with an XCTest.

## Cross-repo contracts

- Session rules are a port of `android/` (`session/`, `ui/WebScreen.kt`):
  change a rule on one platform, port it on the other, same release.
- JS bridge wire names (`TofuNative.requestReauth`, `TofuDiag.deliver`,
  `tofu:native-visibility`, `window.tofuStartPending`,
  `SupervisorBridge.pendingDefaultsKey`) are shared with the SPA
  (`frontend/` native bridge). A rename must land in all three places.
- The supervisor HTTP contract matches the host-side `supervisor.py`
  (sibling proxied port, `projectPath` JSON body).

## Verification ladder

1. `swift test` in `ios/` — real core tests on macOS or Linux with system
   SQLite headers. Linux excludes Apple Keychain/WebKit adapters, never stubs them.
2. `python3 -m pytest tests/mobile/test_ios_client_structure.py` checks package
   and shell wiring. CI (`.github/workflows/build-ios.yml`) also runs the core
   tests on macOS and builds an unsigned simulator app.
3. Manual device check only for WebKit behaviour XCTest can't see (cookie
   persistence across cold starts, TLS prompt, media capture).
