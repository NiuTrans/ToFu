import XCTest
@testable import TofuClientCore

final class SupervisorBridgeTests: XCTestCase {

    /// The TTL must outlive the 30 s start-poll window plus a page reload, or
    /// the login bounce lands after the forgiveness expired.
    func test_ttl_covers_start_poll_window() {
        XCTAssertEqual(SupervisorBridge.ttlMs, 45_000)
        XCTAssertGreaterThan(SupervisorBridge.ttlMs, Int64(ServerLifecycle.startPollWindowSeconds) * 1000)
    }

    func test_arming_script_is_valid_assignment() {
        XCTAssertEqual(
            SupervisorBridge.armingScript(untilMs: 46_000),
            "window.tofuStartPending=46000;"
        )
        XCTAssertEqual(SupervisorBridge.pendingDefaultsKey, "tofu.supervisor.startPending.untilMs")
    }

    func test_parse_pending_accepts_plain_and_quoted() {
        XCTAssertEqual(SupervisorBridge.parsePending("46000"), 46_000)
        XCTAssertEqual(SupervisorBridge.parsePending("\"46000\""), 46_000)
        XCTAssertEqual(SupervisorBridge.parsePending(" 46000\n"), 46_000)
    }

    func test_parse_pending_rejects_garbage() {
        XCTAssertNil(SupervisorBridge.parsePending(nil))
        XCTAssertNil(SupervisorBridge.parsePending(""))
        XCTAssertNil(SupervisorBridge.parsePending("undefined"))
        XCTAssertNil(SupervisorBridge.parsePending("12.5"))
    }

    /// Forgiveness is bounded: an expired marker must not forgive a bounce.
    func test_is_armed_only_within_ttl() {
        XCTAssertTrue(SupervisorBridge.isArmed(rawPending: "46000", nowMs: 45_999))
        XCTAssertFalse(SupervisorBridge.isArmed(rawPending: "46000", nowMs: 46_000))
        XCTAssertFalse(SupervisorBridge.isArmed(rawPending: nil, nowMs: 1_000))
    }
}
