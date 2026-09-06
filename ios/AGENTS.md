# ios/ directory guide

iOS WebView client. The authoritative architecture and the shared session
invariants live in `ios/README.md`; this file is the operational map.

## Ownership and generated boundaries

- `Package.swift` + `Sources/TofuClientCore/` are the authority for all
  session/supervisor/reauth LOGIC. The core must stay Foundation-only: no
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

1. `python3 -m pytest tests/test_ios_client_structure.py` — structure pins
   (runs on this host, no Swift).
2. `swift test` in `ios/` — needs macOS; runs in CI
   (`.github/workflows/build-ios.yml`) on every `ios/**` change, plus an
   unsigned simulator build.
3. Manual device check only for WebKit behaviour XCTest can't see (cookie
   persistence across cold starts, TLS prompt, media capture).
