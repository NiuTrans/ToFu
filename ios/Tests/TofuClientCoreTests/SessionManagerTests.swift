import XCTest
@testable import TofuClientCore

final class SessionManagerTests: XCTestCase {

    private func pwProfile(_ url: String = "https://h.example/proxy/15000/") -> Profile {
        Profile(id: 1, alias: "a", baseUrl: url, authType: .codeServerPassword)
    }

    private struct Rig {
        let http = FakeHTTPClient()
        let cookies = FakeCookieSink()
        let store = FakeProfileStore()
        let secrets = FakeSecretStore()
        let sleeper = SleepRecorder()
        let manager: SessionManager

        init() {
            manager = SessionManager(
                store: store, secrets: secrets, cookies: cookies,
                http: http, sleeper: sleeper.hook
            )
        }
    }

    private func scriptLoginFormFallback(_ http: FakeHTTPClient) async {
        // GET /login 404s → resolveLoginUrl falls back to the origin root.
        await http.enqueue(makeResponse(404, url: "https://h.example/login"))
    }

    private func scriptSuccessfulPost(
        _ http: FakeHTTPClient,
        metaStatus: Int = 404,
        metaBody: String = ""
    ) async {
        await http.enqueue(makeResponse(302, url: "https://h.example/login", headers: [
            "location": ["./"],
            "set-cookie": ["code-server-session=abc; Path=/; HttpOnly"],
        ]))
        await http.enqueue(makeResponse(
            metaStatus, body: metaBody,
            url: "https://h.example/proxy/15000/api/v4/meta"
        ))
    }

    func test_none_auth_is_zero_request_success() async {
        let rig = Rig()
        let p = Profile(id: 1, alias: "a", baseUrl: "https://h.example:15000", authType: .none)
        let result = await rig.manager.login(p)
        guard case .success = result else { return XCTFail("expected success, got \(result)") }
        XCTAssertEqual(await rig.http.requests.count, 0)
    }

    func test_interactive_sso_hands_off_without_requests() async {
        let rig = Rig()
        let p = Profile(id: 1, alias: "a", baseUrl: "https://h.example/proxy/15000/", authType: .interactiveSso)
        let result = await rig.manager.login(p)
        XCTAssertEqual(result, .needsInteractiveSso(url: p.baseUrl))
        XCTAssertEqual(await rig.http.requests.count, 0)
    }

    func test_missing_credential_short_circuits() async {
        let rig = Rig()
        let result = await rig.manager.login(pwProfile())
        XCTAssertEqual(result, .noCredential)
        XCTAssertEqual(await rig.http.requests.count, 0)
    }

    func test_successful_login_injects_cookie_stamps_host_and_preflights() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        await scriptLoginFormFallback(rig.http)
        await scriptSuccessfulPost(rig.http)

        let result = await rig.manager.login(pwProfile())
        guard case .success(let host) = result else { return XCTFail("expected success, got \(result)") }
        XCTAssertEqual(host, "h.example")

        let injected = await rig.cookies.injected
        XCTAssertEqual(injected.count, 1)
        XCTAssertEqual(injected[0].origin, "https://h.example")
        XCTAssertEqual(injected[0].cookies.map(\.name), ["code-server-session"])

        let stamps = await rig.store.cookieHostWrites
        XCTAssertEqual(stamps.map(\.host), ["h.example"])

        let requests = await rig.http.requests
        XCTAssertEqual(requests.map(\.method), ["GET", "POST", "GET"])
        XCTAssertTrue(requests[0].followRedirects)
        XCTAssertFalse(requests[1].followRedirects)   // we need the raw 302
        guard case .form(let fields)? = requests[1].body else {
            return XCTFail("expected form body")
        }
        XCTAssertEqual(fields.count, 2)
        XCTAssertEqual(fields[0].0, "password"); XCTAssertEqual(fields[0].1, "pw")
        XCTAssertEqual(fields[1].0, "base"); XCTAssertEqual(fields[1].1, ".")
        // Preflight rides the same gateway with the fresh cookie.
        XCTAssertEqual(requests[2].url, "https://h.example/proxy/15000/api/v4/meta")
        XCTAssertEqual(requests[2].headers["Cookie"], "code-server-session=abc")
        XCTAssertTrue(rig.sleeper.delays.isEmpty)
    }

    func test_login_post_target_comes_from_the_served_form() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        await rig.http.enqueue(makeResponse(
            200, body: "<html><form method=\"post\" action=\"./login\">",
            url: "https://h.example/some/prefix/"
        ))
        await rig.http.enqueue(makeResponse(302, url: "https://h.example/some/login", headers: [
            "set-cookie": ["code-server-session=abc; Path=/"],
        ]))
        await rig.http.enqueue(makeResponse(404))

        let result = await rig.manager.login(pwProfile())
        guard case .success = result else { return XCTFail("expected success, got \(result)") }
        let requests = await rig.http.requests
        XCTAssertEqual(requests[1].url, "https://h.example/some/login")
    }

    /// code-server re-serves the login page (200) on a bad password — a
    /// definitive answer that must NOT be retried.
    func test_bad_password_is_definitive() async {
        let rig = Rig()
        rig.secrets.putSecret("wrong", for: "a")
        await scriptLoginFormFallback(rig.http)
        await rig.http.enqueue(makeResponse(200, body: "<html>login again</html>"))

        let result = await rig.manager.login(pwProfile())
        XCTAssertEqual(result, .badCredentials)
        XCTAssertEqual(await rig.http.requests.count, 2)
        XCTAssertTrue(rig.sleeper.delays.isEmpty)
    }

    /// A 302 carrying cookies but no code-server session cookie: not a
    /// replayable gate — degrade to the WebView rather than stranding the user.
    func test_302_without_session_cookie_degrades() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        await scriptLoginFormFallback(rig.http)
        await rig.http.enqueue(makeResponse(302, headers: ["set-cookie": ["other=x; Path=/"]]))
        await rig.http.enqueue(makeResponse(404))   // preflight still runs on success

        let result = await rig.manager.login(pwProfile())
        guard case .success = result else { return XCTFail("expected success, got \(result)") }
        XCTAssertEqual(await rig.cookies.injected.count, 0)
        XCTAssertEqual(await rig.store.cookieHostWrites.count, 0)
    }

    /// Bare Tofu answers 401 HTML — same degrade posture, NOT an error.
    func test_unconfirmed_status_degrades() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        await scriptLoginFormFallback(rig.http)
        await rig.http.enqueue(makeResponse(401, body: "<html>unauthorized</html>"))
        await rig.http.enqueue(makeResponse(404))

        let result = await rig.manager.login(pwProfile())
        guard case .success = result else { return XCTFail("expected success, got \(result)") }
    }

    func test_foreign_absolute_redirect_is_sso() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        await scriptLoginFormFallback(rig.http)
        await rig.http.enqueue(makeResponse(302, headers: [
            "location": ["https://idp.foreign.example/auth?next=1"],
        ]))

        let result = await rig.manager.login(pwProfile())
        XCTAssertEqual(result, .needsInteractiveSso(url: pwProfile().baseUrl))
        XCTAssertEqual(await rig.http.requests.count, 2)
    }

    func test_relative_redirect_is_not_sso() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        await scriptLoginFormFallback(rig.http)
        await scriptSuccessfulPost(rig.http)   // Location "./" — code-server's own
        let result = await rig.manager.login(pwProfile())
        guard case .success = result else { return XCTFail("expected success, got \(result)") }
    }

    /// The login POST rides the outer tunnel (vscode proxy); a transient reset
    /// is a transport error, so bounded retry with backoff applies.
    func test_transport_errors_retry_with_backoff() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        await scriptLoginFormFallback(rig.http)
        await rig.http.enqueueError(FakeError.transport("reset"))
        await scriptLoginFormFallback(rig.http)
        await rig.http.enqueueError(FakeError.transport("reset"))
        await scriptLoginFormFallback(rig.http)
        await scriptSuccessfulPost(rig.http)

        let result = await rig.manager.login(pwProfile())
        guard case .success = result else { return XCTFail("expected success, got \(result)") }
        XCTAssertEqual(rig.sleeper.delays, [1_000, 2_500])
        // Each attempt redoes form discovery: GET+POST per attempt, then meta.
        XCTAssertEqual(await rig.http.requests.count, 7)
    }

    func test_retry_exhaustion_returns_error() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        for _ in 0..<LoginRetryPolicy.maxAttempts {
            await scriptLoginFormFallback(rig.http)
            await rig.http.enqueueError(FakeError.transport("down"))
        }
        let result = await rig.manager.login(pwProfile())
        guard case .error(let message) = result else { return XCTFail("expected error, got \(result)") }
        XCTAssertFalse(message.isEmpty)
        XCTAssertEqual(rig.sleeper.delays, [1_000, 2_500])
        XCTAssertEqual(await rig.http.requests.count, 6)
    }

    /// A cold sandbox behind the vscode proxy answers 502/503/504 — that is
    /// "waking up", not a broken server: the login rides the LONGER ladder
    /// (a cold container routinely takes tens of seconds) instead of handing
    /// back a dead page after 3.5s.
    func test_warming_status_retries_on_the_longer_ladder() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        await scriptLoginFormFallback(rig.http)
        await rig.http.enqueue(makeResponse(502, body: "Bad Gateway"))
        await scriptLoginFormFallback(rig.http)
        await rig.http.enqueue(makeResponse(503, body: "Service Unavailable"))
        await scriptLoginFormFallback(rig.http)
        await scriptSuccessfulPost(rig.http)

        let result = await rig.manager.login(pwProfile())
        guard case .success = result else { return XCTFail("expected success, got \(result)") }
        XCTAssertEqual(rig.sleeper.delays, [2_000, 4_000])
        // GET+POST per attempt, then the meta preflight.
        XCTAssertEqual(await rig.http.requests.count, 7)
    }

    /// Warming exhaustion runs the full 6-attempt ladder and the final error
    /// tells the user to wait and tap Open again — not to edit anything.
    func test_warming_exhaustion_runs_the_full_ladder() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        for _ in 0..<LoginRetryPolicy.maxWarmingAttempts {
            await scriptLoginFormFallback(rig.http)
            await rig.http.enqueue(makeResponse(502, body: "Bad Gateway"))
        }
        let result = await rig.manager.login(pwProfile())
        guard case .error(let message) = result else { return XCTFail("expected error, got \(result)") }
        XCTAssertTrue(message.contains("waking up"))
        XCTAssertEqual(rig.sleeper.delays, [2_000, 4_000, 6_000, 8_000, 8_000])
        XCTAssertEqual(await rig.http.requests.count, 12)
    }

    /// A connect timeout against the proxy has the same meaning as its 5xx:
    /// the edge is up but nothing listens behind the tunnel yet. Other
    /// transport failures (reset, refused) stay on the short ladder.
    func test_connect_timeout_is_warming() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        await scriptLoginFormFallback(rig.http)
        await rig.http.enqueueError(URLError(.timedOut))
        await scriptLoginFormFallback(rig.http)
        await scriptSuccessfulPost(rig.http)

        let result = await rig.manager.login(pwProfile())
        guard case .success = result else { return XCTFail("expected success, got \(result)") }
        XCTAssertEqual(rig.sleeper.delays, [2_000])
    }

    /// A definitive answer mid-ladder stops the retry: after one warming 502,
    /// a bad-password 200 must surface as badCredentials immediately.
    func test_warming_then_definitive_stops() async {
        let rig = Rig()
        rig.secrets.putSecret("wrong", for: "a")
        await scriptLoginFormFallback(rig.http)
        await rig.http.enqueue(makeResponse(502, body: "Bad Gateway"))
        await scriptLoginFormFallback(rig.http)
        await rig.http.enqueue(makeResponse(200, body: "<html>login again</html>"))

        let result = await rig.manager.login(pwProfile())
        XCTAssertEqual(result, .badCredentials)
        XCTAssertEqual(rig.sleeper.delays, [2_000])
        XCTAssertEqual(await rig.http.requests.count, 4)
    }

    func test_preflight_mismatch_blocks() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        let wrong = ApiV4Contract.apiMajor + 1
        await scriptLoginFormFallback(rig.http)
        await scriptSuccessfulPost(rig.http, metaStatus: 200, metaBody: "{\"data\":{\"apiMajor\":\(wrong)}}")

        let result = await rig.manager.login(pwProfile())
        guard case .incompatible(let message) = result else {
            return XCTFail("expected incompatible, got \(result)")
        }
        XCTAssertTrue(message.contains("v\(wrong)"))
    }

    func test_note_interactive_sign_in_stamps_once() async {
        let rig = Rig()
        let profile = Profile(id: 7, alias: "s", baseUrl: "https://own.example/proxy/15000/",
                              authType: .interactiveSso)
        await rig.cookies.inject(origin: "https://own.example", cookies: [
            TofuCookie(name: "code-server-session", value: "x", path: "/",
                       expiresAtMs: nil, secure: true, httpOnly: true),
        ])

        let stamped = await rig.manager.noteInteractiveSignIn(
            profile, finishedUrl: "https://own.example/proxy/15000/"
        )
        XCTAssertTrue(stamped)
        XCTAssertEqual(await rig.store.cookieHostWrites.map(\.host), ["own.example"])

        // Idempotent: an already-stamped profile writes nothing more.
        var done = profile
        done.cookieHost = "own.example"
        let again = await rig.manager.noteInteractiveSignIn(
            done, finishedUrl: "https://own.example/proxy/15000/"
        )
        XCTAssertFalse(again)
        XCTAssertEqual(await rig.store.cookieHostWrites.count, 1)
    }

    func test_note_interactive_sign_in_rejects_idp_and_login_page() async {
        let rig = Rig()
        let profile = Profile(id: 7, alias: "s", baseUrl: "https://own.example/proxy/15000/",
                              authType: .interactiveSso)
        await rig.cookies.inject(origin: "https://own.example", cookies: [
            TofuCookie(name: "s", value: "x", path: "/", expiresAtMs: nil, secure: true, httpOnly: true),
        ])
        XCTAssertFalse(await rig.manager.noteInteractiveSignIn(
            profile, finishedUrl: "https://idp.foreign.example/callback"))
        XCTAssertFalse(await rig.manager.noteInteractiveSignIn(
            profile, finishedUrl: "https://own.example/login"))
        XCTAssertEqual(await rig.store.cookieHostWrites.count, 0)
    }

    /// The re-provision invariant: a host change purges the dead host's jar
    /// BEFORE the fresh login, and the persisted row's cookieHost is cleared.
    func test_updateUrlAndReauth_purges_dead_host_then_relogs() async {
        let rig = Rig()
        rig.secrets.putSecret("pw", for: "a")
        let old = Profile(id: 3, alias: "a", baseUrl: "https://old.example/proxy/15000/",
                          authType: .codeServerPassword, cookieHost: "old.example")
        await rig.http.enqueue(makeResponse(404, url: "https://new.example/login"))
        await rig.http.enqueue(makeResponse(302, url: "https://new.example/login", headers: [
            "set-cookie": ["code-server-session=fresh; Path=/"],
        ]))
        await rig.http.enqueue(makeResponse(404))

        let reauth = await rig.manager.updateUrlAndReauth(old, newUrl: "https://new.example/proxy/15000/")
        XCTAssertEqual(await rig.cookies.purgedHosts, ["old.example"])
        XCTAssertNil(reauth.persisted.cookieHost)
        XCTAssertEqual(reauth.persisted.baseUrl, "https://new.example/proxy/15000/")
        guard case .success = reauth.login else {
            return XCTFail("expected success, got \(reauth.login)")
        }
        // The fresh login re-stamps the stored row for the new host.
        XCTAssertEqual(await rig.store.getById(3)?.cookieHost, "new.example")
        XCTAssertEqual(await rig.http.requests[0].url, "https://new.example/login")
    }
}
