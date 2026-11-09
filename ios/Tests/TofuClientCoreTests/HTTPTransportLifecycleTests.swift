import XCTest
@testable import TofuClientCore

final class HTTPTransportLifecycleTests: XCTestCase {
    func test_redirect_policy_and_cookie_isolation_survive_concurrent_requests() async throws {
        let server = try LoopbackHTTPFixture()
        let client = URLSessionHTTPClient()
        async let raw = client.send(HTTPRequest(url: server.origin + "/redirect", followRedirects: false))
        async let followed = client.send(HTTPRequest(url: server.origin + "/redirect", followRedirects: true))
        let (redirect, landed) = try await (raw, followed)
        XCTAssertEqual(redirect.status, 302)
        XCTAssertEqual(landed.status, 200)
        XCTAssertEqual(landed.finalUrl.path, "/echo")
        let cookies = CookieHeaders.individualHeaders(redirect.headerValues("set-cookie"))
        XCTAssertEqual(cookies.count, 2)
        XCTAssertTrue(cookies[1].contains("Wed, 28 Oct"))
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(landed.body.utf8)) as? [String: String])
        XCTAssertEqual(body["cookie"], "")
    }

    func test_transient_callback_parameters_are_requests_not_saved_connections() async throws {
        let server = try LoopbackHTTPFixture()
        let target = server.origin + "/callback?code=transient&state=one"
        XCTAssertNil(ServerUrl.parse(target))
        let response = try await URLSessionHTTPClient().send(HTTPRequest(url: target))
        XCTAssertEqual(response.status, 200)
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(response.body.utf8)) as? [String: String])
        XCTAssertEqual(body["path"], "/callback?code=transient&state=one")
    }

    func test_declared_and_streaming_response_bounds_release_the_transport() async throws {
        let server = try LoopbackHTTPFixture()
        let client = URLSessionHTTPClient()
        for path in ["/oversized", "/chunked"] {
            do {
                _ = try await client.send(HTTPRequest(url: server.origin + path, maximumResponseBytes: 2048))
                XCTFail("oversized response accepted")
            } catch let error as URLError { XCTAssertEqual(error.code, .dataLengthExceedsMaximum) }
        }
        let response = try await client.send(HTTPRequest(url: server.origin + "/echo"))
        XCTAssertEqual(response.status, 200)
    }

    func test_cancelling_while_receiving_body_completes_and_pool_remains_usable() async throws {
        let server = try LoopbackHTTPFixture()
        let client = URLSessionHTTPClient()
        let request = Task { try await client.send(HTTPRequest(url: server.origin + "/slow")) }
        defer { request.cancel() }
        var started = false
        for _ in 0..<100 {
            let state = try await client.send(HTTPRequest(url: server.origin + "/state"))
            if state.body.contains("true") { started = true; break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertTrue(started)
        request.cancel()
        let finished = expectation(description: "cancelled request resumes")
        let observer = Task {
            do { _ = try await request.value; XCTFail("cancelled request returned a response") }
            catch { XCTAssertTrue(GatewayHTTP.isCancellation(error)) }
            finished.fulfill()
        }
        await fulfillment(of: [finished], timeout: 2)
        observer.cancel()
        _ = try await client.send(HTTPRequest(url: server.origin + "/release"))
        let response = try await client.send(HTTPRequest(url: server.origin + "/echo"))
        XCTAssertEqual(response.status, 200)
    }

    func test_real_gateway_handshake_and_cached_reuse_with_sqlite_repository() async throws {
        let server = try LoopbackHTTPFixture()
        let database = try SQLiteProfileStore(path: ":memory:")
        var profile = Profile(id: 0, alias: "loopback", baseUrl: server.origin + "/app/", authType: .codeServerPassword)
        profile.id = database.insert(profile)
        let secrets = FakeSecretStore()
        secrets.putSecret("local-test-password", for: profile.alias)
        let cookies = FakeCookieSink()
        let manager = SessionManager(store: database, secrets: secrets, cookies: cookies, http: URLSessionHTTPClient())
        let first = await manager.login(profile)
        XCTAssertEqual(first, .success(host: "127.0.0.1"))
        XCTAssertEqual(database.getById(profile.id)?.cookieHost, "127.0.0.1")
        secrets.removeSecret(profile.alias)
        let cached = await manager.login(profile)
        XCTAssertEqual(cached, .success(host: "127.0.0.1"))
    }
}
