import XCTest
@testable import TofuClientCore

final class ConnectionIntentTests: XCTestCase {
    private func draft(_ host: String) throws -> ConnectionDraft {
        try XCTUnwrap(ConnectionOnboarding.parse("https://\(host).example/proxy/15000/"))
    }

    func test_closed_form_accepts_only_review_metadata() throws {
        var review = ConnectionReviewState()
        let incoming = try draft("one")
        XCTAssertTrue(review.receive(incoming, formIsOpen: false))
        XCTAssertEqual(review.accepted, incoming)
        XCTAssertNil(review.pending)
    }

    func test_warm_link_preserves_form_until_explicit_use_link() throws {
        var review = ConnectionReviewState()
        let original = try draft("one")
        let incoming = try draft("two")
        _ = review.receive(original, formIsOpen: false)
        XCTAssertFalse(review.receive(incoming, formIsOpen: true))
        XCTAssertEqual(review.accepted, original)
        XCTAssertEqual(review.pending, incoming)
        XCTAssertTrue(review.usePending())
        XCTAssertEqual(review.accepted, incoming)
        XCTAssertNil(review.pending)
    }

    func test_keep_form_discards_pending_without_replacing_the_form() throws {
        var review = ConnectionReviewState()
        let original = try draft("one")
        _ = review.receive(original, formIsOpen: false)
        _ = review.receive(try draft("two"), formIsOpen: true)
        review.keepForm()
        XCTAssertEqual(review.accepted, original)
        XCTAssertNil(review.pending)
        XCTAssertFalse(review.usePending())
    }

    func test_manual_or_existing_edit_and_paste_share_the_review_policy() throws {
        var review = ConnectionReviewState()
        let incoming = try draft("paste")
        XCTAssertFalse(review.receive(incoming, formIsOpen: true))
        XCTAssertNil(review.accepted)
        XCTAssertEqual(review.pending, incoming)
        review.keepForm()
        XCTAssertNil(review.accepted)
        XCTAssertNil(review.pending)
    }

    func test_newest_pending_link_never_replaces_accepted_form() throws {
        var review = ConnectionReviewState()
        let original = try draft("one")
        _ = review.receive(original, formIsOpen: false)
        _ = review.receive(try draft("two"), formIsOpen: true)
        let newest = try draft("three")
        _ = review.receive(newest, formIsOpen: true)
        XCTAssertEqual(review.accepted, original)
        XCTAssertEqual(review.pending, newest)
        review.reset()
        XCTAssertNil(review.accepted)
        XCTAssertNil(review.pending)
    }

    func test_cancel_back_edit_link_delete_or_new_selection_invalidates_old_generation() {
        var policy = ProfileOperationGeneration()
        let old = policy.advance()
        XCTAssertTrue(policy.isCurrent(old, isCancelled: false))
        XCTAssertFalse(policy.isCurrent(old, isCancelled: true))
        let newer = policy.advance()
        XCTAssertFalse(policy.isCurrent(old, isCancelled: false))
        XCTAssertTrue(policy.isCurrent(newer, isCancelled: false))
    }

    func test_missing_replaced_or_edited_store_row_cannot_complete() {
        var policy = ProfileOperationGeneration()
        let generation = policy.advance()
        let expected = Profile(id: 1, alias: "one", baseUrl: "https://one.example/",
                               authType: .none, projectPath: "/srv/tofu")
        XCTAssertFalse(policy.canComplete(generation, isCancelled: false, expected: expected, stored: nil))
        var edits: [Profile] = []
        var changed = expected; changed.id = 2; edits.append(changed)
        changed = expected; changed.alias = "renamed"; edits.append(changed)
        changed = expected; changed.baseUrl = "https://two.example/"; edits.append(changed)
        changed = expected; changed.authType = .interactiveSso; edits.append(changed)
        changed = expected; changed.projectPath = "/srv/other"; edits.append(changed)
        for edited in edits {
            XCTAssertFalse(policy.canComplete(generation, isCancelled: false, expected: expected, stored: edited))
        }
        XCTAssertFalse(policy.canComplete(generation, isCancelled: true, expected: expected, stored: expected))
        _ = policy.advance()
        XCTAssertFalse(policy.canComplete(generation, isCancelled: false, expected: expected, stored: expected))
    }

    func test_new_intent_rejects_late_browser_handoff_from_real_controller() async {
        let rig = GatewayRig()
        await rig.store.seed(rig.profile)
        await rig.http.enqueue(makeResponse(302, headers: ["location": ["https://idp.example/sso"]]))
        let gate = IntentResponseGate()
        let arrived = expectation(description: "old selection reached transport")
        await rig.http.onSend { _ in arrived.fulfill(); await gate.wait() }
        let controller = SessionController(store: rig.store, secrets: rig.secrets, session: rig.manager)
        var policy = ProfileOperationGeneration()
        let old = policy.advance()
        let login = Task { await controller.activate(rig.profile) }
        await fulfillment(of: [arrived], timeout: 2)
        // Deliberately let non-cooperative work finish: generation fencing must
        // suppress even a valid browser handoff, not only cancelled failures.
        _ = policy.advance()
        await gate.release()
        let result = await login.value
        XCTAssertEqual(result.login, .needsInteractiveSso(url: rig.profile.baseUrl))
        let stored = await rig.store.getById(rig.profile.id)
        XCTAssertFalse(policy.canComplete(old, isCancelled: false, expected: result.persisted, stored: stored))
    }

    func test_login_stamps_are_not_configuration_edits() {
        var policy = ProfileOperationGeneration()
        let generation = policy.advance()
        let expected = Profile(id: 1, alias: "one", baseUrl: "https://one.example/", authType: .none)
        var stamped = expected
        stamped.cookieHost = "one.example"
        stamped.instanceUuid = "instance"
        stamped.lastUsedAt = 123
        XCTAssertTrue(policy.canComplete(generation, isCancelled: false, expected: expected, stored: stamped))
    }
}

private actor IntentResponseGate {
    private var continuation: CheckedContinuation<Void, Never>?
    private var released = false
    func wait() async {
        if released { return }
        await withCheckedContinuation { continuation = $0 }
    }
    func release() {
        released = true
        continuation?.resume()
        continuation = nil
    }
}
