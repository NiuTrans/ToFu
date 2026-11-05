import XCTest
@testable import TofuClientCore

final class ReauthBoundaryTests: XCTestCase {
    func test_foreign_login_redirect_cannot_trigger_saved_password_replay() {
        var actions: [ReauthCoordinator.Action] = []
        let coordinator = ReauthCoordinator(authType: .codeServerPassword) { actions.append($0) }
        let verdict = coordinator.navigationVerdict(url: "https://idp.example/login", isMainFrame: true,
            hasGesture: false, ownURL: "https://h.example/proxy/15000/")
        XCTAssertEqual(verdict, .allow)
        XCTAssertTrue(actions.isEmpty)
    }

    func test_native_bridge_requires_exact_origin_and_main_frame() {
        let server = "https://h.example:9443/team/proxy/15000/"
        XCTAssertTrue(TrustedWebBridge.allows(serverURL: server, scheme: "https", host: "h.example", port: 9443, isMainFrame: true))
        for (scheme, host, port, main) in [("http", "h.example", 9443, true), ("https", "h.example", 443, true),
                                           ("https", "idp.example", 9443, true), ("https", "h.example", 9443, false)] {
            XCTAssertFalse(TrustedWebBridge.allows(serverURL: server, scheme: scheme, host: host, port: port, isMainFrame: main))
        }
        XCTAssertTrue(TrustedWebBridge.allows(serverURL: "https://h.example/", scheme: "https", host: "h.example", port: 0, isMainFrame: true))
    }

    func test_browser_handoff_releases_latch_without_replaying_password() {
        var actions: [ReauthCoordinator.Action] = []
        let coordinator = ReauthCoordinator(authType: .codeServerPassword) { actions.append($0) }
        coordinator.trigger()
        coordinator.handoffToBrowser()
        coordinator.trigger()
        XCTAssertFalse(coordinator.inFlight)
        XCTAssertEqual(actions, [.reauthStarted])
        XCTAssertEqual(coordinator.navigationVerdict(url: "https://h.example/login", isMainFrame: true,
            hasGesture: false, ownURL: "https://h.example/proxy/15000/"), .allow)
    }

    func test_same_host_other_port_and_proxy_prefix_do_not_trigger_reauth() {
        var actions: [ReauthCoordinator.Action] = []
        let coordinator = ReauthCoordinator(authType: .codeServerPassword) { actions.append($0) }
        for url in ["https://h.example:9443/team/login", "https://h.example/other/login"] {
            XCTAssertEqual(coordinator.navigationVerdict(url: url, isMainFrame: true,
                hasGesture: false, ownURL: "https://h.example/team/proxy/15000/"), .allow)
        }
        XCTAssertTrue(actions.isEmpty)
    }
}
