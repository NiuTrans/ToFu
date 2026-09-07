import XCTest
@testable import TofuClientCore

final class ServerUrlTests: XCTestCase {

    private let proxy = "https://5665bc99-279b-4edf-8553-c7b7804c6e02-vscode-zw05.mlp.sankuai.com/proxy/15000/"

    func test_parse_accepts_absolute_http_urls() {
        XCTAssertNotNil(ServerUrl.parse(proxy))
        XCTAssertNotNil(ServerUrl.parse("http://127.0.0.1:15000"))
        XCTAssertNil(ServerUrl.parse("not a url"))
        XCTAssertNil(ServerUrl.parse("ftp://host/x"))
        XCTAssertNil(ServerUrl.parse("host.example.com/proxy/15000/"))
    }

    func test_host_origin_loginUrl() {
        let url = ServerUrl.parse(proxy)!
        XCTAssertEqual(url.host, "5665bc99-279b-4edf-8553-c7b7804c6e02-vscode-zw05.mlp.sankuai.com")
        XCTAssertEqual(url.origin, "https://\(url.host)")
        // The login lives at the code-server ROOT, not under /proxy/15000/.
        XCTAssertEqual(url.loginUrl, "https://\(url.host)/login")
    }

    func test_instanceUuid_from_mlp_host() {
        let url = ServerUrl.parse(proxy)!
        XCTAssertEqual(url.instanceUuid, "5665bc99-279b-4edf-8553-c7b7804c6e02")
        XCTAssertNil(ServerUrl.parse("https://example.com/proxy/15000/")!.instanceUuid)
    }

    func test_defaultAuthType() {
        XCTAssertEqual(ServerUrl.defaultAuthType(proxy), .codeServerPassword)
        XCTAssertEqual(ServerUrl.defaultAuthType("https://host.example:15000"), .none)
        XCTAssertEqual(ServerUrl.defaultAuthType("https://host.example/proxy/15001"), .codeServerPassword)
    }

    func test_needsProxyAuthFix_is_one_way() {
        XCTAssertTrue(ServerUrl.needsProxyAuthFix(rawUrl: proxy, current: .none))
        XCTAssertFalse(ServerUrl.needsProxyAuthFix(rawUrl: proxy, current: .codeServerPassword))
        XCTAssertFalse(ServerUrl.needsProxyAuthFix(rawUrl: "https://host:15000", current: .none))
    }

    func test_displayLabel_compresses_mlp_proxy_url() {
        XCTAssertEqual(ServerUrl.displayLabel(proxy), "5665bc99 · zw05 : 15000")
    }

    func test_displayLabel_falls_back_for_plain_hosts() {
        XCTAssertEqual(ServerUrl.displayLabel("https://example.com/proxy/15000/"), "example.com : 15000")
        XCTAssertEqual(ServerUrl.displayLabel("http://127.0.0.1:15000"), "127.0.0.1:15000")
        XCTAssertEqual(ServerUrl.displayLabel("junk"), "junk")
    }
}
