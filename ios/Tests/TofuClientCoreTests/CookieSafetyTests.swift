import XCTest
@testable import TofuClientCore

final class CookieSafetyTests: XCTestCase {
    func test_empty_attributes_do_not_crash_parser() {
        XCTAssertEqual(TofuCookie.parse("session=value;; Path=/; ")?.value, "value")
    }

    func test_idp_session_lifetime_and_same_site_are_not_rewritten() {
        let cookie = TofuCookie.parse("idp=value; Path=/; SameSite=None; Secure")!
        let header = CookieHeaders.toPersistentHeader(cookie)
        XCTAssertFalse(header.contains("Max-Age"))
        XCTAssertTrue(header.contains("SameSite=None"))
        XCTAssertFalse(header.contains("SameSite=Lax"))
    }

    func test_max_age_wins_over_expires_regardless_of_order() {
        let cookie = TofuCookie.parse("s=v; Max-Age=0; Expires=Wed, 28 Oct 2037 00:00:00 GMT", nowMs: 1000)!
        XCTAssertLessThanOrEqual(cookie.expiresAtMs!, 1000)
    }

    func test_foundation_adapter_preserves_host_only_and_domain_cookie_scope() throws {
        let origin = URL(string: "https://h.example/team/login")!
        let hostOnly = BrowserCookies.make([TofuCookie.parse("code-server-session=local; Path=/team; Secure; HttpOnly", responseURL: origin)!], responseURL: origin)
        let cookie = try XCTUnwrap(hostOnly.first)
        XCTAssertTrue(cookie.isHTTPOnly)
        XCTAssertTrue(cookie.isSecure)
        XCTAssertNotNil(cookie.expiresDate)
        XCTAssertEqual(BrowserCookies.header(hostOnly, requestURL: URL(string: "https://h.example/team/app")!), "code-server-session=local")
        XCTAssertNil(BrowserCookies.header(hostOnly, requestURL: URL(string: "https://child.h.example/team/app")!))
        XCTAssertNil(BrowserCookies.header(hostOnly, requestURL: URL(string: "https://h.example/teammate")!))
        let shared = BrowserCookies.make([TofuCookie.parse("idp=shared; Domain=example; Path=/; SameSite=None; Secure", responseURL: origin)!], responseURL: origin)
        XCTAssertNil(shared.first?.expiresDate)
        XCTAssertEqual(BrowserCookies.header(shared, requestURL: URL(string: "https://other.example/app")!), "idp=shared")
    }

    #if canImport(Darwin)
    func test_apple_adapter_preserves_same_site_policy() throws {
        let url = URL(string: "https://h.example/team/")!
        for policy in ["Strict", "Lax"] {
            let source = TofuCookie.parse("s=value; SameSite=\(policy); Secure", responseURL: url)!
            let cookie = try XCTUnwrap(BrowserCookies.make([source], responseURL: url).first)
            XCTAssertEqual(cookie.sameSitePolicy?.rawValue, policy.lowercased())
        }
    }
    #endif
}
