import XCTest
@testable import TofuClientCore

final class ReauthCoordinatorTests: XCTestCase {

    private func make(
        _ authType: AuthType = .codeServerPassword
    ) -> (ReauthCoordinator, ActionRecorder) {
        let recorder = ActionRecorder()
        let coordinator = ReauthCoordinator(authType: authType) { recorder.actions.append($0) }
        return (coordinator, recorder)
    }

    private final class ActionRecorder {
        var actions: [ReauthCoordinator.Action] = []
    }

    func test_login_redirect_intercepts_and_latches() {
        let (c, rec) = make()
        let verdict = c.navigationVerdict(
            url: "https://h.example/login", isMainFrame: true,
            hasGesture: false, ownHost: "h.example"
        )
        XCTAssertEqual(verdict, .interceptForReauth)
        XCTAssertTrue(c.inFlight)
        XCTAssertEqual(rec.actions, [.reauthStarted])

        // Latched: a second trigger while in-flight must not fire again.
        _ = c.navigationVerdict(
            url: "https://h.example/login", isMainFrame: true,
            hasGesture: false, ownHost: "h.example"
        )
        XCTAssertEqual(rec.actions, [.reauthStarted])
    }

    /// INTERACTIVE_SSO's sign-in IS a sequence of main-frame login-page
    /// navigations. Intercepting them freezes the user on a blank surface.
    func test_sso_is_never_intercepted() {
        let (c, rec) = make(.interactiveSso)
        let verdict = c.navigationVerdict(
            url: "https://h.example/login", isMainFrame: true,
            hasGesture: false, ownHost: "h.example"
        )
        XCTAssertEqual(verdict, .allow)
        c.trigger()
        XCTAssertFalse(c.inFlight)
        XCTAssertTrue(rec.actions.isEmpty)
    }

    func test_settle_success_rearms_and_clears_failures() {
        let (c, _) = make()
        c.trigger()
        c.settle(succeeded: false)
        c.trigger()
        XCTAssertEqual(c.consecutiveFailures, 1)
        c.settle(succeeded: true)
        XCTAssertFalse(c.inFlight)
        XCTAssertEqual(c.consecutiveFailures, 0)
        c.trigger()   // re-armed
        XCTAssertTrue(c.inFlight)
    }

    /// An expired password must not storm the login endpoint forever behind
    /// the latch: after the cap, the host is told to give up.
    func test_consecutive_failures_exhaust() {
        let (c, rec) = make()
        for i in 1...ReauthCoordinator.maxConsecutiveFailures {
            c.trigger()
            XCTAssertTrue(c.inFlight)
            c.settle(succeeded: false)
            XCTAssertEqual(rec.actions.count, i)   // no .exhausted yet unless last
        }
        XCTAssertEqual(rec.actions.last, .exhausted)
        XCTAssertEqual(c.consecutiveFailures, 0)   // counter reset after giving up
        // The trigger is re-armed after exhaustion — the next page event gets
        // one more chance (the user may have fixed the password meanwhile).
        c.trigger()
        XCTAssertTrue(c.inFlight)
    }

    func test_non_http_schemes_open_externally() {
        let (c, _) = make()
        XCTAssertEqual(
            c.navigationVerdict(url: "mailto:dev@example.com", isMainFrame: true,
                                hasGesture: true, ownHost: "h.example"),
            .openExternally
        )
        XCTAssertEqual(
            c.navigationVerdict(url: "tel:+123", isMainFrame: true,
                                hasGesture: true, ownHost: "h.example"),
            .openExternally
        )
    }

    /// A user-TAPPED link to a foreign host leaves the shell (no chrome, no
    /// way back); a redirect to the same URL stays in place.
    func test_external_host_requires_gesture() {
        let (c, _) = make()
        XCTAssertEqual(
            c.navigationVerdict(url: "https://other.example/page", isMainFrame: true,
                                hasGesture: true, ownHost: "h.example"),
            .openExternally
        )
        XCTAssertEqual(
            c.navigationVerdict(url: "https://other.example/page", isMainFrame: true,
                                hasGesture: false, ownHost: "h.example"),
            .allow
        )
        // Same host with a gesture is just navigation.
        XCTAssertEqual(
            c.navigationVerdict(url: "https://h.example/page", isMainFrame: true,
                                hasGesture: true, ownHost: "h.example"),
            .allow
        )
        // SSO IdP hops are foreign-host navigations that MUST stay in place.
        let (sso, _) = make(.interactiveSso)
        XCTAssertEqual(
            sso.navigationVerdict(url: "https://idp.foreign.example/auth", isMainFrame: true,
                                  hasGesture: true, ownHost: "h.example"),
            .allow
        )
    }

    func test_subframe_login_is_not_a_reauth_trigger() {
        let (c, rec) = make()
        let verdict = c.navigationVerdict(
            url: "https://h.example/login", isMainFrame: false,
            hasGesture: false, ownHost: "h.example"
        )
        XCTAssertEqual(verdict, .allow)
        XCTAssertTrue(rec.actions.isEmpty)
    }

    func test_looks_like_login() {
        XCTAssertTrue(ReauthCoordinator.looksLikeLogin("https://h/login"))
        XCTAssertTrue(ReauthCoordinator.looksLikeLogin("https://h/login?to=/"))
        XCTAssertTrue(ReauthCoordinator.looksLikeLogin("https://h/login#x"))
        XCTAssertFalse(ReauthCoordinator.looksLikeLogin("https://h/loginpage"))
        XCTAssertFalse(ReauthCoordinator.looksLikeLogin("https://h/api/login_stats"))
    }
}
