import XCTest
@testable import TofuClientCore

final class SessionManagerTests: XCTestCase {
    private func ready(auth: AuthType = .codeServerPassword, url: String = "https://h.example/proxy/15000/") async -> GatewayRig {
        let rig = GatewayRig(auth: auth, url: url)
        await rig.store.seed(rig.profile)
        return rig
    }

    private func success(_ rig: GatewayRig, meta: HTTPResponse = makeResponse(404)) async {
        await rig.http.enqueue(makeResponse(200, body: gatewayForm(action: "/login")))
        await rig.http.enqueue(makeResponse(302, headers: ["location": ["/"], "set-cookie": ["code-server-session=abc; Path=/; HttpOnly"]]))
        await rig.http.enqueue(meta)
    }

    func test_none_auth_is_zero_request_success() async {
        let rig = await ready(auth: .none)
        let result = await rig.manager.login(rig.profile)
        let requests = await rig.http.requests
        XCTAssertEqual(result, .success(host: "h.example"))
        XCTAssertTrue(requests.isEmpty)
    }

    func test_interactive_sso_hands_off_without_requests() async {
        let rig = await ready(auth: .interactiveSso)
        let result = await rig.manager.login(rig.profile)
        let requests = await rig.http.requests
        XCTAssertEqual(result, .needsInteractiveSso(url: rig.profile.baseUrl))
        XCTAssertTrue(requests.isEmpty)
    }

    func test_missing_credential_hands_off_without_requests() async {
        let rig = await ready()
        rig.secrets.removeSecret(rig.profile.alias)
        let result = await rig.manager.login(rig.profile)
        let requests = await rig.http.requests
        XCTAssertEqual(result, .needsInteractiveSso(url: rig.profile.baseUrl))
        XCTAssertTrue(requests.isEmpty)
    }

    func test_successful_login_injects_cookie_stamps_host_and_preflights() async {
        let rig = await ready()
        await success(rig)
        let result = await rig.manager.login(rig.profile)
        let requests = await rig.http.requests
        let saved = await rig.store.getById(rig.profile.id)
        XCTAssertEqual(result, .success(host: "h.example"))
        XCTAssertEqual(saved?.cookieHost, "h.example")
        XCTAssertEqual(requests.map(\.url), [rig.profile.baseUrl, "https://h.example/login", "https://h.example/proxy/15000/api/v4/meta"])
        XCTAssertTrue(requests.allSatisfy { !$0.followRedirects && $0.maximumResponseBytes == 262144 })
        XCTAssertEqual(requests.last?.headers["Cookie"], "code-server-session=abc")
        guard case .form(let pairs) = requests[1].body else { return XCTFail("missing form body") }
        XCTAssertEqual(Dictionary(uniqueKeysWithValues: pairs), ["base": ".", "password": "local-test-password"])
    }

    func test_served_form_preserves_prefix_hidden_fields_and_return_target() async {
        let rig = await ready(url: "https://h.example:9443/team/absproxy/15000/")
        let page = "https://h.example:9443/team/login?to=%2Fteam%2Fabsproxy%2F15000%2F"
        await rig.http.enqueue(makeResponse(302, headers: ["location": [page]]))
        await rig.http.enqueue(makeResponse(200, body: gatewayForm(extra: "<input type='hidden' name='href' value='old'><input type='hidden' name='csrf' value='a&amp;b'>"), url: page))
        await rig.http.enqueue(makeResponse(302, headers: ["set-cookie": ["code-server-session=abc; Path=/team/; Secure"]]))
        await rig.http.enqueue(makeResponse(404))
        let result = await rig.manager.login(rig.profile)
        let requests = await rig.http.requests
        XCTAssertEqual(result, .success(host: "h.example"))
        XCTAssertEqual(requests[2].url, page)
        guard case .form(let pairs) = requests[2].body else { return XCTFail("missing form") }
        let fields = Dictionary(uniqueKeysWithValues: pairs)
        XCTAssertEqual(fields["href"], page)
        XCTAssertEqual(fields["csrf"], "a&b")
        XCTAssertEqual(requests.last?.headers["Cookie"], "code-server-session=abc")
    }

    func test_bad_password_is_definitive_only_for_recognized_form() async {
        for body in [gatewayForm(), "<html>Identity provider</html>"] {
            let rig = await ready()
            await rig.http.enqueue(makeResponse(200, body: gatewayForm()))
            await rig.http.enqueue(makeResponse(200, body: body))
            let result = await rig.manager.login(rig.profile)
            XCTAssertEqual(result, body == gatewayForm() ? .badCredentials : .needsInteractiveSso(url: rig.profile.baseUrl))
            XCTAssertEqual(rig.sleeper.delays, [])
        }
    }

    func test_unconfirmed_status_and_cookie_do_not_stamp() async {
        for status in [200, 302, 401, 403, 404, 500] {
            let rig = await ready()
            await rig.http.enqueue(makeResponse(200, body: gatewayForm()))
            await rig.http.enqueue(makeResponse(status, headers: ["set-cookie": ["idp=abc; Path=/"]]))
            let result = await rig.manager.login(rig.profile)
            let saved = await rig.store.getById(rig.profile.id)
            XCTAssertEqual(result, .needsInteractiveSso(url: rig.profile.baseUrl))
            XCTAssertNil(saved?.cookieHost)
        }
    }

    func test_foreign_redirect_after_password_does_not_grant_session() async {
        let rig = await ready()
        await rig.http.enqueue(makeResponse(200, body: gatewayForm()))
        await rig.http.enqueue(makeResponse(302, headers: ["location": ["https://idp.example/sso"], "set-cookie": ["code-server-session=abc; Path=/"]]))
        let result = await rig.manager.login(rig.profile)
        let writes = await rig.store.cookieHostWrites
        XCTAssertEqual(result, .needsInteractiveSso(url: rig.profile.baseUrl))
        XCTAssertTrue(writes.isEmpty)
    }

    func test_transport_errors_retry_with_backoff() async {
        let rig = await ready()
        await rig.http.enqueueError(FakeError.transport("reset"))
        await success(rig)
        let result = await rig.manager.login(rig.profile)
        XCTAssertEqual(result, .success(host: "h.example"))
        XCTAssertEqual(rig.sleeper.delays, [1000])
    }

    func test_retry_exhaustion_returns_error() async {
        let rig = await ready()
        for _ in 0..<3 { await rig.http.enqueueError(FakeError.transport("reset")) }
        let result = await rig.manager.login(rig.profile)
        guard case .error = result else { return XCTFail("expected transport failure") }
        XCTAssertEqual(rig.sleeper.delays, [1000, 2500])
        let requests = await rig.http.requests
        XCTAssertEqual(requests.count, 3)
    }

    func test_warming_status_and_timeout_use_longer_ladder() async {
        for timeout in [false, true] {
            let rig = await ready()
            if timeout { await rig.http.enqueueError(URLError(.timedOut)) }
            else { await rig.http.enqueue(makeResponse(503)) }
            await success(rig)
            let result = await rig.manager.login(rig.profile)
            XCTAssertEqual(result, .success(host: "h.example"))
            XCTAssertEqual(rig.sleeper.delays, [2000])
        }
    }

    func test_warming_exhaustion_runs_full_ladder() async {
        let rig = await ready()
        for _ in 0..<6 { await rig.http.enqueue(makeResponse(502)) }
        let result = await rig.manager.login(rig.profile)
        guard case .error(let message) = result else { return XCTFail("expected warming failure") }
        XCTAssertTrue(message.contains("waking"))
        XCTAssertEqual(rig.sleeper.delays, [2000, 4000, 6000, 8000, 8000])
    }

    func test_warming_then_bad_credentials_or_rate_limit_stops() async {
        for status in [200, 429] {
            let rig = await ready()
            await rig.http.enqueue(makeResponse(503))
            await rig.http.enqueue(makeResponse(200, body: gatewayForm()))
            await rig.http.enqueue(makeResponse(status, body: gatewayForm()))
            let result = await rig.manager.login(rig.profile)
            if status == 200 { XCTAssertEqual(result, .badCredentials) }
            else { guard case .error(let message) = result else { return XCTFail("expected rate limit") }; XCTAssertTrue(message.contains("429")) }
            XCTAssertEqual(rig.sleeper.delays, [2000])
        }
    }

    func test_preflight_mismatch_blocks_and_transport_failure_is_unknown() async {
        let rig = await ready()
        let wrong = ApiV4Contract.apiMajor + 1
        await success(rig, meta: makeResponse(200, body: "{\"data\":{\"apiMajor\":\(wrong)}}"))
        let result = await rig.manager.login(rig.profile)
        guard case .incompatible(let message) = result else { return XCTFail("expected version mismatch") }
        XCTAssertTrue(message.contains("v\(wrong)"))
    }

    func test_cached_session_uses_two_gets_without_password() async {
        let rig = await ready()
        await rig.cookies.inject(origin: rig.profile.baseUrl, cookies: [TofuCookie.parse("idp=opaque; Path=/")!])
        await rig.http.enqueue(protectedMetadata())
        await rig.http.enqueue(makeResponse(404))
        let result = await rig.manager.login(rig.profile)
        let requests = await rig.http.requests
        XCTAssertEqual(result, .success(host: "h.example"))
        XCTAssertEqual(requests.map(\.method), ["GET", "GET"])
        XCTAssertTrue(requests[0].url.hasSuffix("/api/v4/conversations?limit=1&meta=1"))
    }

    func test_note_interactive_sign_in_requires_protected_proof_and_preserves_edits() async {
        for stale in [false, true] {
            let rig = await ready(auth: .interactiveSso)
            await rig.cookies.inject(origin: rig.profile.baseUrl, cookies: [TofuCookie.parse("idp=opaque; Path=/")!])
            await rig.http.enqueue(protectedMetadata())
            if stale {
                await rig.http.onSend { _ in
                    var edited = rig.profile; edited.baseUrl = "https://new.example/proxy/15000/"
                    await rig.store.update(edited)
                }
            }
            let stamped = await rig.manager.noteInteractiveSignIn(rig.profile, finishedUrl: rig.profile.baseUrl)
            let saved = await rig.store.getById(rig.profile.id)
            XCTAssertEqual(stamped, !stale)
            XCTAssertEqual(saved?.cookieHost, stale ? nil : "h.example")
            if stale { XCTAssertEqual(saved?.baseUrl, "https://new.example/proxy/15000/") }
        }
    }

    func test_note_interactive_sign_in_rejects_foreign_authority_and_other_paths() async {
        let rig = await ready(auth: .interactiveSso)
        await rig.cookies.inject(origin: rig.profile.baseUrl, cookies: [TofuCookie.parse("idp=opaque; Path=/")!])
        for url in ["https://idp.example/", "https://h.example:9443/proxy/15000/", "http://h.example/proxy/15000/", "https://h.example/login", "https://h.example/other/"] {
            let stamped = await rig.manager.noteInteractiveSignIn(rig.profile, finishedUrl: url)
            XCTAssertFalse(stamped)
        }
        let requests = await rig.http.requests
        XCTAssertTrue(requests.isEmpty)
    }

    func test_update_url_invalidates_origin_before_relogin() async {
        for new in ["https://new.example/proxy/15000/", "https://h.example:9443/proxy/15000/", "http://h.example/proxy/15000/"] {
            let rig = await ready(auth: .none)
            let result = await rig.manager.updateUrlAndReauth(rig.profile, newUrl: new)
            let origins = await rig.cookies.purgedOrigins
            let saved = await rig.store.getById(rig.profile.id)
            XCTAssertEqual(origins, ["https://h.example"])
            XCTAssertEqual(result.persisted.baseUrl, new)
            XCTAssertEqual(saved?.baseUrl, new)
            XCTAssertNil(saved?.cookieHost)
        }
    }

    func test_profile_deleted_before_post_never_receives_password() async {
        let rig = await ready()
        await rig.http.enqueue(makeResponse(200, body: gatewayForm()))
        await rig.http.onSend { _ in await rig.store.deleteById(rig.profile.id) }
        let result = await rig.manager.login(rig.profile)
        let requests = await rig.http.requests
        XCTAssertEqual(result, .needsInteractiveSso(url: rig.profile.baseUrl))
        XCTAssertEqual(requests.count, 1)
    }

    func test_profile_changed_during_post_is_not_stamped() async {
        let rig = await ready()
        await success(rig)
        await rig.http.onSend { request in
            if request.method == "POST" { var updated = rig.profile; updated.authType = .none; await rig.store.update(updated) }
        }
        let result = await rig.manager.login(rig.profile)
        let saved = await rig.store.getById(rig.profile.id)
        XCTAssertEqual(result, .needsInteractiveSso(url: rig.profile.baseUrl))
        XCTAssertNil(saved?.cookieHost)
    }
}

func protectedMetadata() -> HTTPResponse {
    makeResponse(200, body: #"{"requestId":"test-request","data":{"items":[],"page":{}},"meta":{"serverTimeMs":1}}"#,
                 headers: ["content-type": ["application/json"]])
}
