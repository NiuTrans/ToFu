import XCTest
@testable import TofuClientCore

final class SupervisorUrlTests: XCTestCase {

    private let proxy = "https://h.example/proxy/15000/"

    func test_derives_sibling_port_base() {
        let sup = SupervisorUrl.fromServerUrl(proxy)!
        XCTAssertEqual(sup.base, "https://h.example/proxy/15001")
        XCTAssertEqual(sup.origin, "https://h.example")
    }

    /// The Tofu port comes out of the URL's own `/proxy/<port>/` segment: a
    /// deployment on any other port derives its ACTUAL sibling, not a
    /// hardcoded 15001.
    func test_derives_the_actual_sibling_of_a_non_default_tofu_port() {
        let sup = SupervisorUrl.fromServerUrl("https://h.example/proxy/15005/")!
        XCTAssertEqual(sup.base, "https://h.example/proxy/15006")
        XCTAssertEqual(sup.origin, "https://h.example")
    }

    /// No proxy segment at all → the conventional default supervisor port.
    func test_no_proxy_segment_falls_back_to_the_default() {
        let sup = SupervisorUrl.fromServerUrl("https://h.example:15000/")!
        XCTAssertEqual(sup.base, "https://h.example:15000/proxy/15001")
        XCTAssertEqual(sup.origin, "https://h.example:15000")
    }

    /// A `/proxy/` segment whose tail is not a number derives nothing.
    func test_non_numeric_proxy_segment_falls_back_to_the_default() {
        let sup = SupervisorUrl.fromServerUrl("https://h.example/proxy/abc/")!
        XCTAssertEqual(sup.base, "https://h.example/proxy/15001")
    }

    func test_preserves_non_default_port() {
        let sup = SupervisorUrl.fromServerUrl("https://h.example:8443/proxy/15000/")!
        XCTAssertEqual(sup.origin, "https://h.example:8443")
        XCTAssertEqual(sup.base, "https://h.example:8443/proxy/15001")
    }

    func test_rejects_invalid_urls() {
        XCTAssertNil(SupervisorUrl.fromServerUrl("junk"))
        XCTAssertNil(SupervisorUrl.fromServerUrl("ftp://h.example"))
    }

    func test_endpoint_query_encoding() {
        let sup = SupervisorUrl.fromServerUrl(proxy)!
        XCTAssertEqual(
            SupervisorUrl.endpoint(sup, SupervisorUrl.status, projectPathForQuery: "/data/my proj"),
            "https://h.example/proxy/15001/status?projectPath=%2Fdata%2Fmy+proj"
        )
        // POST endpoints carry the path in the body, not the query.
        XCTAssertEqual(SupervisorUrl.endpoint(sup, SupervisorUrl.start), "https://h.example/proxy/15001/start")
    }

    /// The most common failure: a 5xx from the code-server proxy means nothing
    /// is listening on the supervisor port. The message must name the fix.
    func test_explainFailure_5xx_names_the_daemon_fix() {
        let msg = SupervisorUrl.explainFailure(code: 502, rawMessage: "Bad Gateway")
        XCTAssertTrue(msg.contains("supervisor.py"))
        XCTAssertTrue(msg.contains("./supervisor.sh install"))
        XCTAssertTrue(msg.contains("15001"))
    }

    func test_explainFailure_known_codes() {
        XCTAssertTrue(SupervisorUrl.explainFailure(code: 403, rawMessage: "x").contains("TOFU_SUPERVISOR_PROJECTS"))
        XCTAssertTrue(SupervisorUrl.explainFailure(code: 401, rawMessage: "x").contains("Open"))
        XCTAssertTrue(SupervisorUrl.explainFailure(code: 404, rawMessage: "x").contains("404"))
        XCTAssertTrue(SupervisorUrl.explainFailure(code: 0, rawMessage: "timeout").contains("timeout"))
        XCTAssertEqual(SupervisorUrl.explainFailure(code: 418, rawMessage: "teapot"), "teapot")
    }
}
