import XCTest
@testable import TofuClientCore

final class GatewaySessionProbeTests: XCTestCase {
    func test_actual_v4_success_envelope_is_protected_session_evidence() {
        // lib/api_v4.py success_envelope places requestId in meta, not at the root.
        let body = #"{"data":{"items":[],"page":{"hasMore":false}},"meta":{"requestId":"session-proof","serverTimeMs":1}}"#
        XCTAssertTrue(GatewaySessionProbe.provesTofu(json(body)))
    }

    func test_public_or_incomplete_json_does_not_prove_the_gate() {
        for body in [
            #"{"ok":true}"#,
            #"{"data":{"items":[],"page":{}},"meta":{"serverTimeMs":1},"requestId":"root-only"}"#,
            #"{"data":{"items":[],"page":{}},"meta":{"requestId":" ","serverTimeMs":1}}"#,
            #"{"data":{"items":[],"page":{}},"meta":{"requestId":"session-proof"}}"#,
            #"{"data":{"items":[]},"meta":{"requestId":"session-proof","serverTimeMs":1}}"#
        ] {
            XCTAssertFalse(GatewaySessionProbe.provesTofu(json(body)), body)
        }
    }

    func test_authentication_problem_keeps_its_top_level_request_id() {
        let body = #"{"type":"urn:tofu:problem:auth-required","instance":"/api/v4/conversations","status":401,"requestId":"refusal-proof"}"#
        XCTAssertTrue(GatewaySessionProbe.provesTofu(json(body, status: 401)))
        XCTAssertFalse(GatewaySessionProbe.provesTofu(json(body, status: 403)))
    }

    private func json(_ body: String, status: Int = 200) -> HTTPResponse {
        makeResponse(status, body: body, headers: ["content-type": ["application/json"]])
    }
}
