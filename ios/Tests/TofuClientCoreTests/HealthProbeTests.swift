import XCTest
@testable import TofuClientCore

final class HealthProbeTests: XCTestCase {

    private let proxyUrl = "https://h.example/proxy/15000/"

    private func rig() -> (HealthProbe, FakeHTTPClient, FakeCookieSink) {
        let http = FakeHTTPClient()
        let cookies = FakeCookieSink()
        return (HealthProbe(http: http, cookies: cookies), http, cookies)
    }

    private func seedSession(_ cookies: FakeCookieSink) async {
        await cookies.inject(origin: "https://h.example", cookies: [
            TofuCookie(name: "code-server-session", value: "abc", path: "/",
                       expiresAtMs: nil, secure: true, httpOnly: true),
        ])
    }

    func test_invalid_url_never_touches_the_network() async {
        let (probe, http, _) = rig()
        let outcome = await probe.probe("junk")
        XCTAssertEqual(outcome.verdict, .unreachable)
        XCTAssertEqual(outcome.status, 0)
        XCTAssertEqual(await http.requests.count, 0)
    }

    /// The cookie is the whole point: behind the vscode proxy a cookie-less
    /// probe measures the GATE. It must ride the health request, and the
    /// trailing `/` of the typed URL must not double the path separator.
    func test_tofu_up_forwards_session_cookie() async {
        let (probe, http, cookies) = rig()
        await seedSession(cookies)
        await http.enqueue(makeResponse(
            200, body: #"{"ok":true,"data":{"bootId":"b-1"}}"#,
            url: "https://h.example/proxy/15000/api/health"
        ))
        let outcome = await probe.probe(proxyUrl)
        XCTAssertEqual(outcome.verdict, .tofu)
        XCTAssertEqual(outcome.status, 200)
        let requests = await http.requests
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(requests[0].url, "https://h.example/proxy/15000/api/health")
        XCTAssertEqual(requests[0].headers["Cookie"], "code-server-session=abc")
        XCTAssertTrue(requests[0].followRedirects)
    }

    func test_tofu_envelope_401_is_tofus_own_gate() async {
        let (probe, http, _) = rig()
        await http.enqueue(makeResponse(401, body: #"{"ok":false,"error":{"code":"AUTH_REQUIRED"}}"#))
        let outcome = await probe.probe(proxyUrl)
        XCTAssertEqual(outcome.verdict, .tofuAuth)
        XCTAssertEqual(outcome.status, 401)
    }

    /// The edge's string-error 401 — Tofu never saw the request.
    func test_edge_401_is_the_gateway() async {
        let (probe, http, _) = rig()
        await http.enqueue(makeResponse(401, body: #"{"error":"Unauthorized"}"#))
        let outcome = await probe.probe(proxyUrl)
        XCTAssertEqual(outcome.verdict, .gateway)
    }

    func test_landing_page_200_is_not_tofu() async {
        let (probe, http, _) = rig()
        await http.enqueue(makeResponse(200, body: "<html><title>Sign in</title></html>"))
        let outcome = await probe.probe(proxyUrl)
        XCTAssertEqual(outcome.verdict, .notTofu)
    }

    func test_waking_status_non_edge_5xx_and_transport_failure() async {
        let (probe, http, _) = rig()
        // The proxy edge's 502/503/504 = the sandbox behind the tunnel is
        // still booting — "waking up", a distinct verdict since the fix is
        // to wait, not to edit the URL.
        await http.enqueue(makeResponse(502, body: "Bad Gateway"))
        var outcome = await probe.probe(proxyUrl)
        XCTAssertEqual(outcome.verdict, .waking)
        XCTAssertEqual(outcome.status, 502)

        await http.enqueue(makeResponse(500, body: "Internal Server Error"))
        outcome = await probe.probe(proxyUrl)
        XCTAssertEqual(outcome.verdict, .unreachable)
        XCTAssertEqual(outcome.status, 500)

        await http.enqueueError(FakeError.transport("tunnel reset"))
        outcome = await probe.probe(proxyUrl)
        XCTAssertEqual(outcome.verdict, .unreachable)
        XCTAssertEqual(outcome.status, 0)
        XCTAssertFalse(outcome.detail.isEmpty)
    }

    func test_empty_jar_sends_no_cookie_header() async {
        let (probe, http, _) = rig()
        await http.enqueue(makeResponse(401, body: #"{"error":"Unauthorized"}"#))
        _ = await probe.probe(proxyUrl)
        let requests = await http.requests
        XCTAssertNil(requests[0].headers["Cookie"])
    }
}
