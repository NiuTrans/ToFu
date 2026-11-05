import XCTest
@testable import TofuClientCore

final class HTTPFormTransportTests: XCTestCase {
    func test_form_reserved_characters_survive_real_http_roundtrip() async throws {
        let server = try LoopbackHTTPFixture()
        let client = URLSessionHTTPClient()
        let secret = "a&b=c+d %豆腐?/#"
        let response = try await client.send(HTTPRequest(url: server.origin + "/form", method: "POST",
            body: .form([("password", secret), ("a&b", "x=y"), ("blank", "")]), followRedirects: false))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(response.body.utf8)) as? [String: Any])
        let fields = try XCTUnwrap(object["fields"] as? [String: [String]])
        XCTAssertEqual(fields, ["password": [secret], "a&b": ["x=y"], "blank": [""]])
        XCTAssertEqual(object["contentType"] as? String, "application/x-www-form-urlencoded")
    }
}
