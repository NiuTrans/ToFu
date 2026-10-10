import XCTest
@testable import TofuClientCore

final class SupervisorRunnerTests: XCTestCase {

    private let profile = Profile(
        id: 1, alias: "a", baseUrl: "https://h.example/proxy/15000/",
        cookieHost: "h.example", projectPath: "/data/x"
    )
    private let userPlan = ProbePlan(proceed: true, mayLogIn: true, reportFailure: true)
    private let autoPlan = ProbePlan(proceed: true, mayLogIn: false, reportFailure: false)

    func test_isStillCurrent_compares_whole_key() {
        let key = CardKey.of(profile)
        XCTAssertTrue(isStillCurrent(key, key))
        XCTAssertFalse(isStillCurrent(key, nil))
        var edited = profile
        edited.projectPath = "/data/other"
        XCTAssertFalse(isStillCurrent(key, CardKey.of(edited)))
    }

    func test_login_block_short_circuits_with_explanation() async throws {
        var calls = 0
        let outcome = try await executeSupervisorCall(
            profile: profile, action: .status, plan: userPlan, signedIn: false,
            login: { _ in .badCredentials },
            call: { _, _ in calls += 1; return .ok(running: true) },
            isCurrent: { true }
        )
        XCTAssertTrue(outcome.failed)
        XCTAssertTrue(outcome.message?.contains("Wrong password") ?? false)
        XCTAssertEqual(calls, 0)
    }

    /// needsInteractiveSso yields no cookie — proceeding would 401 and
    /// misreport an un-completed sign-in as "the daemon isn't responding".
    func test_needs_interactive_sso_also_blocks() async throws {
        let outcome = try await executeSupervisorCall(
            profile: profile, action: .start, plan: userPlan, signedIn: false,
            login: { _ in .needsInteractiveSso(url: "u") },
            call: { _, _ in .ok(running: true) },
            isCurrent: { true }
        )
        XCTAssertTrue(outcome.failed)
        XCTAssertTrue(outcome.message?.contains("Open") ?? false)
        XCTAssertNil(outcome.running)
    }

    func test_start_polls_until_running_then_hands_off() async throws {
        var statuses = 0
        let outcome = try await executeSupervisorCall(
            profile: profile, action: .start, plan: userPlan, signedIn: true,
            login: { _ in .success(host: "h.example") },
            call: { action, _ in
                switch action {
                case .start: return .ok(running: false)
                case .status:
                    statuses += 1
                    return .ok(running: statuses >= 2)
                case .stop: return .ok(running: false)
                }
            },
            isCurrent: { true },
            pollAttempts: 5, pollIntervalMs: 0
        )
        XCTAssertEqual(outcome.running, true)
        XCTAssertTrue(outcome.handOff)
        XCTAssertNil(outcome.message)
        XCTAssertFalse(outcome.failed)
    }

    /// An expired poll window is NOT an error — the message leaves the user
    /// with something to DO.
    func test_start_timeout_shows_message_not_failure() async throws {
        let outcome = try await executeSupervisorCall(
            profile: profile, action: .start, plan: userPlan, signedIn: true,
            login: { _ in .success(host: "h.example") },
            call: { _, _ in .ok(running: false) },
            isCurrent: { true },
            pollAttempts: 2, pollIntervalMs: 0
        )
        XCTAssertEqual(outcome.running, false)
        XCTAssertFalse(outcome.handOff)
        XCTAssertFalse(outcome.failed)
        XCTAssertEqual(outcome.message, ServerLifecycle.startTimeoutMessage())
    }

    /// A finished call must never yank the user into a WebView they navigated
    /// away from while the poll ran.
    func test_stale_card_never_hands_off() async throws {
        let outcome = try await executeSupervisorCall(
            profile: profile, action: .start, plan: userPlan, signedIn: true,
            login: { _ in .success(host: "h.example") },
            call: { _, _ in .ok(running: true) },
            isCurrent: { false }
        )
        XCTAssertFalse(outcome.handOff)
        XCTAssertNil(outcome.message)
    }

    /// An AUTO probe stays silent: "couldn't reach it just now" is not worth
    /// painting the card red when nobody asked.
    func test_auto_probe_failure_stays_silent() async throws {
        let outcome = try await executeSupervisorCall(
            profile: profile, action: .status, plan: autoPlan, signedIn: true,
            login: { _ in XCTFail("auto probe must never log in"); return .noCredential },
            call: { _, _ in .failed(code: 502, message: "Bad Gateway") },
            isCurrent: { true }
        )
        XCTAssertFalse(outcome.failed)
        XCTAssertNil(outcome.message)
    }

    func test_user_failure_is_explained() async throws {
        let outcome = try await executeSupervisorCall(
            profile: profile, action: .start, plan: userPlan, signedIn: true,
            login: { _ in .success(host: "h.example") },
            call: { _, _ in .failed(code: 502, message: "Bad Gateway") },
            isCurrent: { true }
        )
        XCTAssertTrue(outcome.failed)
        XCTAssertTrue(outcome.message?.contains("supervisor.py") ?? false)
    }

    func test_stop_never_polls_and_never_hands_off() async throws {
        var calls = 0
        let outcome = try await executeSupervisorCall(
            profile: profile, action: .stop, plan: userPlan, signedIn: true,
            login: { _ in .success(host: "h.example") },
            call: { _, _ in calls += 1; return .ok(running: false) },
            isCurrent: { true },
            pollIntervalMs: 0
        )
        XCTAssertEqual(outcome.running, false)
        XCTAssertFalse(outcome.handOff)
        XCTAssertEqual(calls, 1)
    }

    /// Leaving the screen is not an error: cancellation propagates so the task
    /// dies normally instead of being swallowed into a RunOutcome.
    func test_cancellation_rethrows() async {
        do {
            _ = try await executeSupervisorCall(
                profile: profile, action: .start, plan: userPlan, signedIn: true,
                login: { _ in .success(host: "h.example") },
                call: { _, _ in throw CancellationError() },
                isCurrent: { true }
            )
            XCTFail("expected CancellationError")
        } catch is CancellationError {
            // expected
        } catch {
            XCTFail("wrong error: \(error)")
        }
    }

    /// A transport-level throw becomes a reported outcome, not a silent
    /// revert to the card's previous state.
    func test_unexpected_throw_becomes_outcome() async throws {
        let outcome = try await executeSupervisorCall(
            profile: profile, action: .start, plan: userPlan, signedIn: true,
            login: { _ in .success(host: "h.example") },
            call: { _, _ in throw FakeError.transport("socket closed") },
            isCurrent: { true }
        )
        XCTAssertTrue(outcome.failed)
        XCTAssertNotNil(outcome.message)
    }
}
