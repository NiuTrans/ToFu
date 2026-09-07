"""Structure pins for the iOS client (ios/).

There is no Swift toolchain on this host, so these pins are the local guard;
the macOS workflow (.github/workflows/build-ios.yml) compiles and runs the
XCTest suites. What is pinned here is what a structure-only review can still
catch — and each pin exists because of a concrete failure mode:

  * the core package must stay Foundation-only so `swift test` runs without
    a simulator (a SwiftUI/WebKit import in Sources/ would break that);
  * public type names must be unique across Sources + the app target
    (a duplicate CardKey-style type in two files is a compile error);
  * the transport contract is HTTPClient.send (a stale `.execute(` call site
    once survived a refactor of the HTTP seam);
  * the JS bridge wire names (TofuNative / TofuDiag / tofu:native-visibility /
    tofuStartPending) are a contract shared with the SPA — a rename on one
    side silently kills re-auth / diagnostics / start forgiveness;
  * a successful login must navigate into the web screen (an early
    AppViewModel swallowed .success and tapping a server did nothing).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
IOS = REPO_ROOT / "ios"
SOURCES = IOS / "Sources" / "TofuClientCore"
TESTS_DIR = IOS / "Tests" / "TofuClientCoreTests"
APP = IOS / "TofuClient"

REQUIRED_SOURCES = [
    "ServerUrl.swift",
    "Profile.swift",
    "ProfileStore.swift",
    "SQLiteProfileStore.swift",
    "SecretStore.swift",
    "KeychainSecretStore.swift",
    "HTTP.swift",
    "URLSessionHTTPClient.swift",
    "CookieSink.swift",
    "WebKitCookieSink.swift",
    "CookieHeaders.swift",
    "SessionManager.swift",
    "SessionController.swift",
    "LoginForm.swift",
    "LoginResult.swift",
    "ProfileForm.swift",
    "ApiMetaGate.swift",
    "TofuProbe.swift",
    "HealthProbe.swift",
    "InteractiveSso.swift",
    "ReauthCoordinator.swift",
    "SupervisorUrl.swift",
    "SupervisorClient.swift",
    "SupervisorBridge.swift",
    "SupervisorRunner.swift",
    "ServerLifecycle.swift",
    "ServerListViewModel.swift",
    "Regex.swift",
]

REQUIRED_APP_FILES = [
    "TofuClientApp.swift",
    "AppViewModel.swift",
    "RootView.swift",
    "ProfileListView.swift",
    "AddEditProfileView.swift",
    "WebScreenView.swift",
    "SupervisorControlsView.swift",
]

REQUIRED_TESTS = [
    "ServerUrlTests.swift",
    "SessionManagerTests.swift",
    "SessionControllerTests.swift",
    "ProfileFormTests.swift",
    "ApiMetaGateTests.swift",
    "TofuProbeTests.swift",
    "HealthProbeTests.swift",
    "InteractiveSsoTests.swift",
    "ReauthCoordinatorTests.swift",
    "SupervisorUrlTests.swift",
    "SupervisorBridgeTests.swift",
    "SupervisorRunnerTests.swift",
    "ServerLifecycleTests.swift",
    "ServerListViewModelTests.swift",
    "CookieHeadersTests.swift",
    "LoginFormTests.swift",
    "Fakes.swift",
]

TYPE_DECL = re.compile(r"\b(?:class|struct|enum|protocol)\s+([A-Z]\w*)")


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required file: {path.relative_to(REPO_ROOT)}"
    return path.read_text(encoding="utf-8")


def test_required_files_exist() -> None:
    for name in REQUIRED_SOURCES:
        _read(SOURCES / name)
    for name in REQUIRED_APP_FILES:
        _read(APP / name)
    for name in REQUIRED_TESTS:
        _read(TESTS_DIR / name)
    _read(IOS / "Package.swift")
    _read(IOS / "project.yml")
    _read(REPO_ROOT / ".github" / "workflows" / "build-ios.yml")


def test_core_stays_foundation_only() -> None:
    """Sources/ must never import SwiftUI/UIKit — `swift test` runs the core
    on a host without a simulator. WebKit is allowed ONLY behind the
    `#if canImport(WebKit)` guard in WebKitCookieSink."""
    offenders: list[str] = []
    for path in SOURCES.glob("*.swift"):
        text = path.read_text(encoding="utf-8")
        if "import SwiftUI" in text or "import UIKit" in text:
            offenders.append(path.name)
        if "import WebKit" in text and "#if canImport(WebKit)" not in text:
            offenders.append(f"{path.name} (unguarded WebKit)")
    assert not offenders, f"core imports UI frameworks: {offenders}"


def test_public_type_names_unique_across_targets() -> None:
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for folder in (SOURCES, APP):
        for path in sorted(folder.glob("*.swift")):
            for match in TYPE_DECL.finditer(path.read_text(encoding="utf-8")):
                name = match.group(1)
                # Nested/secondary declarations in one file are fine; the pin
                # is about the SAME top-level name in TWO files.
                if name in seen and seen[name] != path.name:
                    duplicates.append(f"{name}: {seen[name]} + {path.name}")
                else:
                    seen.setdefault(name, path.name)
    assert not duplicates, f"duplicate type declarations: {duplicates}"


def test_transport_contract_is_httpclient_send() -> None:
    for path in SOURCES.glob("*.swift"):
        text = path.read_text(encoding="utf-8")
        assert ".execute(" not in text, f"{path.name}: stale execute() call site"
    probe = _read(SOURCES / "HealthProbe.swift")
    assert "http.send(" in probe


def test_js_bridge_wire_names_match_spa_contract() -> None:
    web = _read(APP / "WebScreenView.swift")
    for needle in ("tofuNative", "tofuDiag", "tofu:native-visibility",
                   "SupervisorBridge.pendingDefaultsKey", "__tofuCollectDiagnostics"):
        assert needle in web, f"WebScreenView lost bridge wire name: {needle}"
    controls = _read(APP / "SupervisorControlsView.swift")
    assert "SupervisorBridge.pendingDefaultsKey" in controls, (
        "Start hand-off must commit the armed marker where WebScreenView reads it"
    )


def test_successful_login_navigates_into_web_screen() -> None:
    model = _read(APP / "AppViewModel.swift")
    assert "case .success:" in model
    assert "screen = .web(profile)" in model, (
        "a successful login must open the web screen — otherwise tapping a "
        "server with a working session visibly does nothing"
    )


def test_build_wiring() -> None:
    project = _read(IOS / "project.yml")
    assert "TofuClientCore" in project and "path: ." in project
    assert "deploymentTarget" in project
    workflow = _read(REPO_ROOT / ".github" / "workflows" / "build-ios.yml")
    assert "swift test" in workflow
    assert "CODE_SIGNING_ALLOWED=NO" in workflow
    # Generated artifacts must never be committed (project.yml is authority).
    gitignore = _read(IOS / ".gitignore")
    assert "TofuClient.xcodeproj" in gitignore
    assert not (IOS / "TofuClient.xcodeproj").exists()
