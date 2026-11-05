import XCTest
@testable import TofuClientCore

final class GatewayBoundaryTests: XCTestCase {
    func test_url_authority_retains_port_and_proxy_prefix() {
        let server = ServerUrl.parse("https://[::1]:9443/team/absproxy/15005/?q=1")!
        XCTAssertEqual(server.origin, "https://[::1]:9443")
        XCTAssertEqual(server.loginUrl, "https://[::1]:9443/team/login")
        XCTAssertEqual(ServerUrl.defaultAuthType(server.raw), .codeServerPassword)
        XCTAssertEqual(SupervisorUrl.fromServerUrl(server.raw)?.base,
                       "https://[::1]:9443/team/proxy/15006")
        XCTAssertNil(ServerUrl.parse("https://user:secret@host.example/proxy/15000/"))
        XCTAssertEqual(ServerUrl.defaultAuthType("https://host.example/?next=/proxy/15000/"), .none)
    }

    func test_explicit_none_is_not_promoted_at_startup() async {
        let rig = GatewayRig(auth: .none)
        await rig.store.seed(rig.profile)
        let controller = SessionController(store: rig.store, secrets: rig.secrets, session: rig.manager)
        _ = await controller.activate(rig.profile)
        let saved = await rig.store.getById(rig.profile.id)
        XCTAssertEqual(saved?.authType, AuthType.none)
    }

    func test_unknown_or_foreign_form_never_receives_password() async {
        for html in ["<form method='post' action='https://idp.example/login'></form>",
                     "<html>Unknown gateway</html>",
                     gatewayForm(action: "https://h.example:9443/login")] {
            let rig = GatewayRig()
            await rig.store.seed(rig.profile)
            await rig.http.enqueue(makeResponse(200, body: html, url: rig.profile.baseUrl))
            await rig.http.enqueue(makeResponse(200, body: "<html>Sign in</html>"))
            let result = await rig.manager.login(rig.profile)
            let requests = await rig.http.requests
            XCTAssertEqual(result, .needsInteractiveSso(url: rig.profile.baseUrl))
            XCTAssertFalse(requests.contains { $0.method == "POST" }, html)
            XCTAssertEqual(requests.first?.url, rig.profile.baseUrl)
            XCTAssertTrue(requests.allSatisfy { !$0.followRedirects })
        }
    }

    func test_foreign_redirect_is_one_request_handoff() async {
        let rig = GatewayRig()
        await rig.http.enqueue(makeResponse(302, headers: ["location": ["https://idp.example/sso"]]))
        let result = await rig.manager.login(rig.profile)
        let requests = await rig.http.requests
        XCTAssertEqual(result, .needsInteractiveSso(url: rig.profile.baseUrl))
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(requests.first?.method, "GET")
    }

    func test_cookie_and_landing_page_do_not_prove_session() async {
        let rig = GatewayRig(auth: .interactiveSso)
        await rig.store.seed(rig.profile)
        await rig.cookies.inject(origin: "https://h.example", cookies: [
            TofuCookie.parse("idp=opaque; Path=/")!
        ])
        await rig.http.enqueue(makeResponse(200, body: "<html>Sign in</html>"))
        let stamped = await rig.manager.noteInteractiveSignIn(rig.profile, finishedUrl: rig.profile.baseUrl)
        let saved = await rig.store.getById(rig.profile.id)
        XCTAssertFalse(stamped)
        XCTAssertNil(saved?.cookieHost)
    }

    func test_cancelled_transport_does_not_retry_or_stamp() async {
        let rig = GatewayRig()
        await rig.http.enqueueError(CancellationError())
        _ = await rig.manager.login(rig.profile)
        let requests = await rig.http.requests
        let writes = await rig.store.cookieHostWrites
        XCTAssertEqual(requests.count, 1)
        XCTAssertEqual(rig.sleeper.delays, [])
        XCTAssertTrue(writes.isEmpty)
    }
}

struct GatewayRig {
    let http = FakeHTTPClient()
    let cookies = FakeCookieSink()
    let store = FakeProfileStore()
    let secrets = FakeSecretStore()
    let sleeper = SleepRecorder()
    let manager: SessionManager
    let profile: Profile

    init(auth: AuthType = .codeServerPassword, url: String = "https://h.example/proxy/15000/") {
        profile = Profile(id: 1, alias: "gateway", baseUrl: url, authType: auth)
        secrets.putSecret("local-test-password", for: profile.alias)
        manager = SessionManager(store: store, secrets: secrets, cookies: cookies,
                                 http: http, sleeper: sleeper.hook)
    }
}

func gatewayForm(action: String = "", extra: String = "") -> String {
    """
    <form class="login-form" method="post" action="\(action)">
      <input type="hidden" name="base" value=".">
      <input type="password" name="password">\(extra)
    </form>
    """
}
