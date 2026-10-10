import XCTest
@testable import TofuClientCore

final class ServerListViewModelTests: XCTestCase {

    private func card(
        id: Int64 = 1,
        projectPath: String? = "/data/x",
        state: ServerState = .stopped
    ) -> ServerCardUiState {
        ServerCardUiState(
            profile: Profile(id: id, alias: "a", baseUrl: "https://h/proxy/15000/",
                             projectPath: projectPath),
            serverState: state,
            failureText: nil,
            showTransitioning: state == .transitioning,
            showUnreachable: state == .unreachable,
            capabilities: ServerLifecycle.capabilities(state)
        )
    }

    private final class CommitRecorder {
        var commits: [(id: Int64, untilMs: Int64)] = []
    }

    private func make(
        now: Int64 = 1_000,
        recorder: CommitRecorder
    ) -> ServerListViewModel {
        ServerListViewModel(
            isManaged: { $0.projectPath?.isEmpty == false },
            nowMs: { now },
            commitStartPending: { id, until in recorder.commits.append((id, until)) }
        )
    }

    /// Click alone must NOT arm the page bridge — arming on click would
    /// forgive a login bounce for a server that never actually started.
    func test_click_parks_but_does_not_commit() {
        let rec = CommitRecorder()
        let vm = make(recorder: rec)
        vm.onStartClick(card())
        XCTAssertNotNil(vm.pendingStart)
        XCTAssertTrue(rec.commits.isEmpty)
    }

    /// Hand-off commits armedAt + TTL, keyed by the card identity.
    func test_hand_off_commits_bounded_marker() {
        let rec = CommitRecorder()
        let vm = make(now: 2_000, recorder: rec)
        vm.onStartClick(card(id: 7))
        vm.onHandOff()
        XCTAssertEqual(rec.commits.count, 1)
        XCTAssertEqual(rec.commits[0].id, 7)
        XCTAssertEqual(rec.commits[0].untilMs, 2_000 + SupervisorBridge.ttlMs)
        XCTAssertNil(vm.pendingStart)
    }

    /// The guarded start path: a card that cannot start must never park a
    /// pending expectation — otherwise a later unrelated hand-off arms it.
    func test_blocked_start_click_parks_nothing() {
        let rec = CommitRecorder()
        let vm = make(recorder: rec)
        vm.onStartClick(card(state: .running))            // canStart == false
        vm.onStartClick(card(projectPath: nil, state: .stopped))  // unmanaged
        XCTAssertNil(vm.pendingStart)
        vm.onHandOff()
        XCTAssertTrue(rec.commits.isEmpty)
    }

    /// A start that times out leaves no armed marker behind: the page must
    /// NOT forgive a bounce for a server that never came up.
    func test_timeout_lapses_silently() {
        let rec = CommitRecorder()
        let vm = make(recorder: rec)
        vm.onStartClick(card())
        vm.onStartTimeout()
        XCTAssertNil(vm.pendingStart)
        vm.onHandOff()
        XCTAssertTrue(rec.commits.isEmpty)
    }

    func test_ui_state_projection() {
        let vm = make(recorder: CommitRecorder())
        let stopped = vm.uiState(for: card().profile, serverState: .stopped, failureText: "Stopped")
        XCTAssertEqual(stopped.failureText, "Stopped")
        XCTAssertTrue(stopped.capabilities.canStart)
        XCTAssertFalse(stopped.capabilities.canStop)

        // running/transitioning carry no failure text — nothing to act on.
        let running = vm.uiState(for: card().profile, serverState: .running, failureText: "junk")
        XCTAssertNil(running.failureText)

        let busy = vm.uiState(for: card().profile, serverState: .transitioning)
        XCTAssertTrue(busy.showTransitioning)
        XCTAssertTrue(busy.capabilities.canOpen)   // Open survives a long boot

        let dead = vm.uiState(for: card().profile, serverState: .unreachable)
        XCTAssertTrue(dead.showUnreachable)
        XCTAssertEqual(dead.failureText, ServerLifecycle.label(.unreachable))
    }
}
