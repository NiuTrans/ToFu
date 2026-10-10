import XCTest
@testable import TofuClientCore

final class ApiMetaGateTests: XCTestCase {

    /// The /proxy/<port>/ prefix must survive — resolving the absolute
    /// META_PATH against the origin would drop it and hit the wrong server.
    func test_metaUrl_keeps_proxy_prefix() {
        XCTAssertEqual(
            ApiMetaGate.metaUrl("https://h.example/proxy/15000/"),
            "https://h.example/proxy/15000/api/v4/meta"
        )
        XCTAssertEqual(ApiMetaGate.metaUrl("https://h.example"), "https://h.example/api/v4/meta")
    }

    func test_matching_major_is_compatible() {
        XCTAssertNil(ApiMetaGate.incompatibilityReason(
            status: 200,
            body: "{\"data\":{\"apiMajor\":\(ApiV4Contract.apiMajor),\"minAndroidBuild\":999}}"
        ))
    }

    func test_api_major_mismatch_blocks() {
        let wrong = ApiV4Contract.apiMajor + 1
        let reason = ApiMetaGate.incompatibilityReason(
            status: 200,
            body: "{\"data\":{\"apiMajor\":\(wrong)}}"
        )
        XCTAssertNotNil(reason)
        XCTAssertTrue(reason!.contains("v\(wrong)"))
    }

    /// Fail-open on partial knowledge: 404 (pre-meta server), transport-shaped
    /// bodies, and unparseable JSON never block.
    func test_partial_knowledge_never_blocks() {
        XCTAssertNil(ApiMetaGate.incompatibilityReason(status: 404, body: nil))
        XCTAssertNil(ApiMetaGate.incompatibilityReason(status: 200, body: ""))
        XCTAssertNil(ApiMetaGate.incompatibilityReason(status: 200, body: "<html>"))
        XCTAssertNil(ApiMetaGate.incompatibilityReason(status: 200, body: "{\"data\":{}}"))
        XCTAssertNil(ApiMetaGate.incompatibilityReason(status: 0, body: nil))
    }
}
