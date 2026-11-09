import XCTest
@testable import TofuClientCore

final class ConnectionOnboardingTests: XCTestCase {
    func test_paste_and_connect_link_produce_the_same_credential_free_draft() throws {
        let raw = "https://h.example:8443/team/proxy/15000/?workspace=one%20two#/chat?conversation=c1"
        let pasted = try XCTUnwrap(ConnectionOnboarding.parse("  " + raw + "  "))
        var link = URLComponents(string: "tofu://connect")!
        link.queryItems = [URLQueryItem(name: "url", value: raw), URLQueryItem(name: "name", value: "Training")]
        let draft = try XCTUnwrap(ConnectionOnboarding.parse(link.string!))
        XCTAssertEqual(draft.baseUrl, pasted.baseUrl)
        XCTAssertEqual(draft.authType, .codeServerPassword)
        XCTAssertEqual(draft.alias, "Training")
    }

    func test_connection_link_protocol_bounds() {
        let prefix = "https://h.example/?label="
        let largest = prefix + String(repeating: "x", count: 8192 - prefix.utf8.count)
        XCTAssertNotNil(ConnectionOnboarding.parse(largest))
        XCTAssertNil(ConnectionOnboarding.parse(largest + "x"))
        var link = URLComponents(string: "tofu://connect")!
        link.queryItems = [URLQueryItem(name: "url", value: "https://h.example/"),
                           URLQueryItem(name: "name", value: String(repeating: "n", count: 81))]
        XCTAssertNil(ConnectionOnboarding.parse(link.string!))
    }

    func test_import_rejects_credentials_duplicates_unknown_fields_and_other_routes() {
        for raw in [
            "tofu://other?url=https%3A%2F%2Fh.example",
            "tofu://connect?url=https%3A%2F%2Fh.example&password=x",
            "tofu://connect?url=https%3A%2F%2Fh.example&url=https%3A%2F%2Fother.example",
            "tofu://connect?url=https%3A%2F%2Fu%3Ap%40h.example",
            "tofu://user@connect?url=https%3A%2F%2Fh.example",
            "tofu://connect/path?url=https%3A%2F%2Fh.example",
            "tofu://connect?url=https%3A%2F%2Fh.example#fragment",
            "https://h.example/?token=x", "https://h.example/#/chat?api_key=x",
            "https://h.example/?workspace=%0Asecret"
        ] {
            XCTAssertNil(ConnectionOnboarding.parse(raw), raw)
        }
    }
}
