import XCTest
@testable import TofuClientCore

final class CookieHeadersTests: XCTestCase {

    func test_parse_full_attributes() {
        let c = TofuCookie.parse(
            "code-server-session=abc; Domain=h.example; Path=/; Secure; HttpOnly; Max-Age=3600",
            nowMs: 1_000
        )!
        XCTAssertEqual(c.name, "code-server-session")
        XCTAssertEqual(c.value, "abc")
        XCTAssertEqual(c.path, "/")
        XCTAssertTrue(c.secure)
        XCTAssertTrue(c.httpOnly)
        XCTAssertEqual(c.expiresAtMs, 1_000 + 3_600_000)
        XCTAssertTrue(c.persistent)
    }

    func test_parse_session_cookie_has_no_expiry() {
        let c = TofuCookie.parse("code-server-session=abc; Path=/; HttpOnly")!
        XCTAssertNil(c.expiresAtMs)
        XCTAssertFalse(c.persistent)
    }

    func test_parse_rejects_junk() {
        XCTAssertNil(TofuCookie.parse("no-equals"))
        XCTAssertNil(TofuCookie.parse("=value"))
    }

    /// The cold-start-survival upgrade: a session cookie (no expiry) gets a
    /// Max-Age so the WebView keeps it across a cold start.
    func test_persistent_header_upgrades_session_cookie() {
        let c = TofuCookie(name: "code-server-session", value: "abc", path: "/",
                           expiresAtMs: nil, secure: true, httpOnly: true)
        XCTAssertEqual(
            CookieHeaders.toPersistentHeader(c),
            "code-server-session=abc; Path=/; Max-Age=\(CookieHeaders.persistSeconds); Secure; HttpOnly; SameSite=Lax"
        )
    }

    func test_persistent_header_keeps_existing_lifetime() {
        let now: Int64 = 100_000
        let c = TofuCookie(name: "n", value: "v", path: "/app",
                           expiresAtMs: now + 5_000_000, secure: false, httpOnly: false)
        XCTAssertEqual(
            CookieHeaders.toPersistentHeader(c, nowMs: now),
            "n=v; Path=/app; Max-Age=5000; SameSite=Lax"
        )
    }
}
