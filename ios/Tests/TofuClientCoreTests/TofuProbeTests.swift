import XCTest
@testable import TofuClientCore

final class TofuProbeTests: XCTestCase {

    func test_health_with_bootId_is_tofu() {
        XCTAssertEqual(
            TofuProbe.classify(status: 200, body: "{\"ok\":true,\"bootId\":\"abc\"}"),
            .tofu
        )
    }

    func test_200_html_landing_page_is_not_tofu() {
        XCTAssertEqual(TofuProbe.classify(status: 200, body: "<html>gateway</html>"), .notTofu)
    }

    /// The vscode proxy edge refuses with a STRING error — never mistake it
    /// for Tofu's own auth refusal.
    func test_gateway_edge_401_is_gateway_not_tofu_auth() {
        XCTAssertEqual(
            TofuProbe.classify(status: 401, body: "{\"error\":\"Unauthorized\"}"),
            .gateway
        )
    }

    func test_tofu_envelope_401_is_tofu_auth() {
        XCTAssertEqual(
            TofuProbe.classify(status: 401, body: "{\"ok\":false,\"error\":{\"code\":\"x\"}}"),
            .tofuAuth
        )
    }

    func test_transport_failure_and_non_edge_5xx_are_unreachable() {
        XCTAssertEqual(TofuProbe.classify(status: 0, body: nil), .unreachable)
        XCTAssertEqual(TofuProbe.classify(status: 500, body: "server error"), .unreachable)
    }

    /// A cold sandbox behind the proxy edge answers 502/503/504 — that is
    /// "waking up", NOT "wrong URL", and the guidance must say wait.
    func test_proxy_edge_5xx_is_waking_not_unreachable() {
        for status in [502, 503, 504] {
            XCTAssertEqual(TofuProbe.classify(status: status, body: "bad gateway"), .waking)
        }
        XCTAssertTrue(TofuProbe.isWakingStatus(502))
        XCTAssertFalse(TofuProbe.isWakingStatus(500))
        XCTAssertTrue(TofuProbe.isProblem(.waking, authType: .none, hasSecret: false))
        XCTAssertTrue(TofuProbe.guidance(.waking, authType: .none, hasSecret: false)
            .contains("waking up"))
    }

    func test_gateway_is_a_problem_only_without_a_working_auth_plan() {
        XCTAssertTrue(TofuProbe.isProblem(.gateway, authType: .none, hasSecret: false))
        XCTAssertTrue(TofuProbe.isProblem(.gateway, authType: .codeServerPassword, hasSecret: false))
        XCTAssertFalse(TofuProbe.isProblem(.gateway, authType: .codeServerPassword, hasSecret: true))
        XCTAssertFalse(TofuProbe.isProblem(.gateway, authType: .interactiveSso, hasSecret: false))
        XCTAssertFalse(TofuProbe.isProblem(.tofu, authType: .none, hasSecret: false))
        XCTAssertTrue(TofuProbe.isProblem(.notTofu, authType: .none, hasSecret: true))
    }

    func test_guidance_names_the_fix() {
        XCTAssertTrue(TofuProbe.guidance(.gateway, authType: .codeServerPassword, hasSecret: false)
            .contains("password"))
        XCTAssertTrue(TofuProbe.guidance(.notTofu, authType: .none, hasSecret: false)
            .contains("isn't Tofu"))
    }
}
