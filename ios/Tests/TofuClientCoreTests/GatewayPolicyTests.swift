import XCTest
@testable import TofuClientCore

final class GatewayPolicyTests: XCTestCase {
    func test_only_one_recognized_visible_form_grants_password_authority() {
        let server = ServerUrl.parse("https://h.example:9443/team/proxy/15000/")!
        let page = URL(string: server.loginUrl + "?to=app")!
        let valid = gatewayForm(extra: "<input type='hidden' name='csrf' value='a&#38;b'>")
        let found = LoginForm.discover(valid, pageUrl: page, server: server)
        XCTAssertEqual(found?.target, page)
        XCTAssertEqual(found?.fields["csrf"], "a&b")
        for invalid in [valid + valid, "<!--\(valid)-->", "<script>\(valid)</script>",
                        valid.replacingOccurrences(of: "login-form", with: "idp-form"),
                        gatewayForm(action: "http://h.example:9443/login"),
                        gatewayForm(action: "https://user@h.example:9443/login"),
                        gatewayForm(extra: "<input type='hidden' name='base' value='duplicate'>"),
                        gatewayForm(extra: "<input type='hidden' name='password' value='injected'>"),
                        String(repeating: " ", count: 262145) + valid] {
            XCTAssertNil(LoginForm.discover(invalid, pageUrl: page, server: server))
        }
    }

    func test_probe_requires_structured_protected_metadata_or_tofu_auth_refusal() {
        XCTAssertTrue(GatewaySessionProbe.provesTofu(protectedMetadata()))
        for status in [401, 403] {
            let body = "{\"type\":\"urn:tofu:problem:auth-required\",\"status\":\(status),\"instance\":\"/api/v4/conversations\",\"requestId\":\"r\"}"
            XCTAssertTrue(GatewaySessionProbe.provesTofu(makeResponse(status, body: body, headers: ["content-type": ["application/problem+json"]])))
        }
        for body in ["<html>Sign in</html>", "{\"ok\":true}", "{\"data\":{\"apiMajor\":4}}",
                     protectedMetadata().body.replacingOccurrences(of: "\"serverTimeMs\":1", with: "\"serverTimeMs\":true"),
                     "{\"type\":\"urn:tofu:problem:csrf\",\"status\":403,\"instance\":\"/other\",\"requestId\":\"r\"}"] {
            XCTAssertFalse(GatewaySessionProbe.provesTofu(makeResponse(200, body: body, headers: ["content-type": ["application/json"]])))
            XCTAssertFalse(GatewaySessionProbe.provesTofu(makeResponse(403, body: body, headers: ["content-type": ["application/json"]])))
        }
        XCTAssertFalse(GatewaySessionProbe.provesTofu(makeResponse(200, body: protectedMetadata().body, headers: ["content-type": ["text/html"]])))
    }

    func test_cookie_scope_obeys_path_host_only_secure_and_expiry() {
        let request = URL(string: "https://sub.example:9443/team/api")!
        func matches(_ domain: String = "sub.example", hostOnly: Bool = true, path: String = "/team", secure: Bool = true, expires: Date? = nil, url: URL? = nil) -> Bool {
            CookieScope.matches(domain: domain, hostOnly: hostOnly, path: path, secure: secure, expires: expires, url: url ?? request)
        }
        XCTAssertTrue(matches())
        XCTAssertTrue(matches("example", hostOnly: false))
        XCTAssertFalse(matches("example"))
        XCTAssertFalse(matches(path: "/tea"))
        XCTAssertFalse(matches(path: "/else"))
        XCTAssertFalse(matches(url: URL(string: "https://sub.example/team%2Fprivate")!))
        XCTAssertTrue(matches(path: "/caf%C3%A9", url: URL(string: "https://sub.example/caf%C3%A9/app")!))
        XCTAssertFalse(matches(expires: Date(timeIntervalSince1970: 0)))
        XCTAssertFalse(matches(url: URL(string: "http://sub.example/team/api")!))
        XCTAssertTrue(matches(secure: false, url: URL(string: "http://sub.example/team/api")!))
        XCTAssertFalse(CookieScope.belongsToProfile(domain: ".example", path: "/", appURL: request))
        XCTAssertTrue(CookieScope.belongsToProfile(domain: "sub.example", path: "/team", appURL: request))
    }

    func test_cookie_parser_rejects_foreign_domain_and_handles_large_lifetimes() {
        let url = URL(string: "https://h.example/team/login")!
        XCTAssertNil(TofuCookie.parse("s=x; Domain=foreign.example", responseURL: url))
        XCTAssertEqual(TofuCookie.parse("s=x", responseURL: url)?.path, "/team")
        let huge = TofuCookie.parse("s=x; Max-Age=999999999999999999999999999", nowMs: 1000)!
        XCTAssertEqual(huge.expiresAtMs, 253402300799000)
        XCTAssertEqual(TofuCookie.parse("s=x; Max-Age=-999999999999999999999999999", nowMs: 1000)?.expiresAtMs, 0)
        XCTAssertNil(TofuCookie.parse("s=x\r\nInjected: value"))
    }

    func test_secret_sharing_requires_same_origin_and_code_server_prefix() async {
        let rig = GatewayRig(url: "https://h.example:9443/team/proxy/15000/")
        await rig.store.seed(rig.profile)
        let controller = SessionController(store: rig.store, secrets: rig.secrets, session: rig.manager)
        for target in ["https://h.example:9443/team/absproxy/16000/", "https://h.example/team/proxy/15000/", "http://h.example:9443/team/proxy/15000/", "https://h.example:9443/other/proxy/15000/"] {
            let shared = await controller.findSharedSecret(target)
            XCTAssertEqual(shared, target.contains("absproxy") ? "local-test-password" : nil)
        }
    }

    func test_redirect_loop_has_bounded_gets_and_no_password_submission() async {
        let rig = GatewayRig()
        for _ in 0..<9 { await rig.http.enqueue(makeResponse(302, headers: ["location": ["/loop"]])) }
        let result = await rig.manager.login(rig.profile)
        let requests = await rig.http.requests
        XCTAssertEqual(result, .needsInteractiveSso(url: rig.profile.baseUrl))
        XCTAssertEqual(requests.count, 9)
        XCTAssertTrue(requests.allSatisfy { $0.method == "GET" })
    }
}
