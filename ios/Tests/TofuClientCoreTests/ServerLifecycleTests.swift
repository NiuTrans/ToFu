import XCTest
@testable import TofuClientCore

final class ServerLifecycleTests: XCTestCase {

    private func managed() -> Profile {
        Profile(id: 1, alias: "a", baseUrl: "https://h.example/proxy/15000/", projectPath: "/data/x")
    }

    func test_isManaged_requires_a_project_path() {
        XCTAssertTrue(ServerLifecycle.isManaged(managed()))
        XCTAssertFalse(ServerLifecycle.isManaged(Profile(id: 1, alias: "a", baseUrl: "https://h")))
        XCTAssertFalse(ServerLifecycle.isManaged(Profile(id: 1, alias: "a", baseUrl: "https://h", projectPath: "  ")))
    }

    func test_isSignedIn_requires_cookie_for_OWN_host() {
        var p = managed()
        p.cookieHost = "h.example"
        XCTAssertTrue(ServerLifecycle.isSignedIn(p))
        // A stale cookieHost from a previous URL must NOT count.
        p.cookieHost = "dead.example"
        XCTAssertFalse(ServerLifecycle.isSignedIn(p))
        p.cookieHost = nil
        XCTAssertFalse(ServerLifecycle.isSignedIn(p))
    }

    func test_resolve_precedence() {
        let openOnly = Profile(id: 1, alias: "a", baseUrl: "https://h")
        XCTAssertEqual(ServerLifecycle.resolve(profile: openOnly, running: true), .unmanaged)
        XCTAssertEqual(ServerLifecycle.resolve(profile: managed(), running: nil, busy: true), .transitioning)
        XCTAssertEqual(ServerLifecycle.resolve(profile: managed(), running: nil, failed: true), .unreachable)
        // A poll result OUTRANKS the cookie check: the supervisor answered.
        XCTAssertEqual(ServerLifecycle.resolve(profile: managed(), running: true), .running)
        XCTAssertEqual(ServerLifecycle.resolve(profile: managed(), running: false), .stopped)
        XCTAssertEqual(ServerLifecycle.resolve(profile: managed(), running: nil), .unknown)
    }

    func test_capabilities_matrix() {
        let unmanaged = ServerLifecycle.capabilities(.unmanaged)
        XCTAssertFalse(unmanaged.canStart); XCTAssertFalse(unmanaged.canStop)
        XCTAssertFalse(unmanaged.canRefresh); XCTAssertTrue(unmanaged.canOpen)

        // unknown: code-server (the proxy) stays up while Tofu is down, so
        // every control can do login-then-act.
        let unknown = ServerLifecycle.capabilities(.unknown)
        XCTAssertTrue(unknown.canStart); XCTAssertTrue(unknown.canStop); XCTAssertTrue(unknown.canRefresh)

        let running = ServerLifecycle.capabilities(.running)
        XCTAssertFalse(running.canStart); XCTAssertTrue(running.canStop)

        let stopped = ServerLifecycle.capabilities(.stopped)
        XCTAssertTrue(stopped.canStart); XCTAssertFalse(stopped.canStop)

        // transitioning: Open stays ENABLED — a start poll can outlast the
        // window, and removing Open would leave no actionable control.
        let busy = ServerLifecycle.capabilities(.transitioning)
        XCTAssertFalse(busy.canStart); XCTAssertFalse(busy.canStop)
        XCTAssertFalse(busy.canRefresh); XCTAssertTrue(busy.canOpen)
    }

    /// The rule that stops the cold-start lockout loop: an AUTO probe against
    /// a profile with NO session is SKIPPED entirely — otherwise merely
    /// opening the home screen fires one POST /login per unsigned server.
    func test_probePlan_auto_unsigned_skips_entirely() {
        let plan = ServerLifecycle.probePlan(trigger: .auto, signedIn: false)
        XCTAssertFalse(plan.proceed); XCTAssertFalse(plan.mayLogIn); XCTAssertFalse(plan.reportFailure)
    }

    func test_probePlan_auto_signed_in_is_read_only() {
        let plan = ServerLifecycle.probePlan(trigger: .auto, signedIn: true)
        XCTAssertTrue(plan.proceed); XCTAssertFalse(plan.mayLogIn); XCTAssertFalse(plan.reportFailure)
    }

    func test_probePlan_user_licenses_side_effects() {
        let plan = ServerLifecycle.probePlan(trigger: .user, signedIn: false)
        XCTAssertTrue(plan.proceed); XCTAssertTrue(plan.mayLogIn); XCTAssertTrue(plan.reportFailure)
    }

    func test_completionFor_only_start_can_hand_off_or_time_out() {
        XCTAssertEqual(
            ServerLifecycle.completionFor(action: .start, running: true, stillCurrent: true),
            CallCompletion(handOff: true, showTimeout: false)
        )
        XCTAssertEqual(
            ServerLifecycle.completionFor(action: .start, running: false, stillCurrent: true),
            CallCompletion(handOff: false, showTimeout: true)
        )
        XCTAssertEqual(
            ServerLifecycle.completionFor(action: .start, running: true, stillCurrent: false),
            CallCompletion(handOff: false, showTimeout: false)
        )
        XCTAssertEqual(
            ServerLifecycle.completionFor(action: .stop, running: false, stillCurrent: true),
            CallCompletion(handOff: false, showTimeout: false)
        )
        XCTAssertEqual(
            ServerLifecycle.completionFor(action: .status, running: true, stillCurrent: true),
            CallCompletion(handOff: false, showTimeout: false)
        )
    }

    func test_isLoginBlocking() {
        XCTAssertFalse(ServerLifecycle.isLoginBlocking(.success(host: "h")))
        XCTAssertTrue(ServerLifecycle.isLoginBlocking(.badCredentials))
        XCTAssertTrue(ServerLifecycle.isLoginBlocking(.noCredential))
        // Yields no cookie — proceeding would 401 and misreport the cause.
        XCTAssertTrue(ServerLifecycle.isLoginBlocking(.needsInteractiveSso(url: "u")))
        XCTAssertTrue(ServerLifecycle.isLoginBlocking(.error(message: "x")))
        XCTAssertTrue(ServerLifecycle.isLoginBlocking(.incompatible(message: "x")))
    }

    func test_explainLoginBlock_is_actionable() {
        XCTAssertTrue(ServerLifecycle.explainLoginBlock(.badCredentials).contains("Wrong password"))
        XCTAssertTrue(ServerLifecycle.explainLoginBlock(.needsInteractiveSso(url: "u")).contains("Open"))
        XCTAssertTrue(ServerLifecycle.explainLoginBlock(.error(message: "boom")).contains("boom"))
    }

    func test_start_timeout_copy_is_not_a_failure() {
        XCTAssertEqual(ServerLifecycle.startPollWindowSeconds, 30)
        XCTAssertTrue(ServerLifecycle.startTimeoutMessage().contains("still be booting"))
    }

    func test_labels() {
        for state: ServerState in [.unmanaged, .unknown, .running, .stopped, .transitioning, .unreachable] {
            XCTAssertFalse(ServerLifecycle.label(state).isEmpty)
        }
    }
}
