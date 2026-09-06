import Foundation

/// The identity of one server card, as far as supervisor work is concerned.
/// Port of CardKey.kt.
///
/// A named type rather than an ad-hoc tuple: the four fields that make a
/// result belong to a card are written into the type, so a future field cannot
/// be silently forgotten at one of the several places this key is built.
///
/// `cookieHost` and `projectPath` are part of the identity because editing
/// either changes what a supervisor call MEANS, while leaving `id` untouched.
public struct CardKey: Equatable, Sendable {
    public let id: Int64
    public let baseUrl: String
    public let cookieHost: String?
    public let projectPath: String?

    public init(id: Int64, baseUrl: String, cookieHost: String?, projectPath: String?) {
        self.id = id
        self.baseUrl = baseUrl
        self.cookieHost = cookieHost
        self.projectPath = projectPath
    }

    public static func of(_ profile: Profile) -> CardKey {
        CardKey(
            id: profile.id,
            baseUrl: profile.baseUrl,
            cookieHost: profile.cookieHost,
            projectPath: profile.projectPath
        )
    }
}

/// Whether a finished call still belongs to what is on screen.
///
/// Trivial by design — the POINT is that it is a named, testable step. Both
/// previous Android versions of this guard were wrong not because the rule was
/// hard but because the values fed into it were computed wrongly. Naming the
/// comparison lets a test assert the inputs, not just the rule.
public func isStillCurrent(_ started: CardKey, _ current: CardKey?) -> Bool {
    started == current
}

/// What the UI should apply once a supervisor call has finished.
///
/// Returned as DATA rather than applied via callbacks so the whole execution
/// flow can be unit-tested off-device.
public struct RunOutcome: Equatable, Sendable {
    /// New running state, or nil to leave it unchanged.
    public var running: Bool?
    public var failed: Bool
    public var message: String?
    /// Hand the user into the WebView (a start that came up while still current).
    public var handOff: Bool

    public init(running: Bool? = nil, failed: Bool = false, message: String? = nil, handOff: Bool = false) {
        self.running = running
        self.failed = failed
        self.message = message
        self.handOff = handOff
    }
}

/// Executes one supervisor call — the login-then-act handshake, the call
/// itself, and the post-start poll — and reports what the UI should do.
/// Port of executeSupervisorCall.kt.
///
/// Every side-effecting dependency is a parameter so the flow is testable
/// with plain fakes. [isCurrent] is deliberately a FUNCTION, not a captured
/// value: the caller must wire it to state later renders mutate, otherwise it
/// degenerates into comparing a value with itself.
///
/// Throws ONLY ``CancellationError`` (leaving the screen is not an error; the
/// task dies normally). Every other failure becomes a ``RunOutcome``.
public func executeSupervisorCall(
    profile: Profile,
    action: SupervisorAction,
    plan: ProbePlan,
    signedIn: Bool,
    login: @Sendable (Profile) async -> LoginResult,
    call: @Sendable (SupervisorAction, Profile) async -> SupervisorResult,
    isCurrent: @Sendable () -> Bool,
    pollAttempts: Int = ServerLifecycle.startPollAttempts,
    pollIntervalMs: Int64 = ServerLifecycle.startPollIntervalMs
) async throws -> RunOutcome {
    do {
        // Establish the session FIRST when we don't hold one. The supervisor
        // rides the code-server cookie, and code-server (the proxy) is up even
        // while Tofu is down — so this handshake works on a STOPPED server.
        if plan.mayLogIn && !signedIn {
            let result = await login(profile)
            // Includes needsInteractiveSso: it yields no cookie, so pressing on
            // would 401 and misreport an un-completed sign-in as "the daemon
            // isn't responding".
            if ServerLifecycle.isLoginBlocking(result) {
                return RunOutcome(
                    failed: true,
                    message: ServerLifecycle.explainLoginBlock(result)
                )
            }
        }

        switch await call(action, profile) {
        case .ok(let initialRunning):
            var running = initialRunning
            // /start returns before the port binds (by design), so poll until
            // the server reports itself up rather than leaving the card
            // claiming it is still stopped.
            if action == .start && !running {
                for _ in 0..<pollAttempts {
                    try await Task.sleep(nanoseconds: UInt64(max(pollIntervalMs, 0)) * 1_000_000)
                    if case .ok(let up) = await call(.status, profile), up {
                        running = true
                        break
                    }
                }
            }
            let completion = ServerLifecycle.completionFor(
                action: action,
                running: running,
                stillCurrent: isCurrent()
            )
            return RunOutcome(
                running: running,
                handOff: completion.handOff,
                message: completion.showTimeout ? ServerLifecycle.startTimeoutMessage() : nil
            )
        case .failed(let code, let message):
            // An AUTO probe stays silent: "couldn't reach it just now" is not
            // worth painting the card red when nobody asked.
            return RunOutcome(
                failed: plan.reportFailure,
                message: plan.reportFailure
                    ? SupervisorUrl.explainFailure(code: code, rawMessage: message)
                    : nil
            )
        }
    } catch is CancellationError {
        throw CancellationError()
    } catch {
        // A real transport failure. Without this the error escaped and the
        // card silently reverted to its previous state — the user tapped
        // Start and nothing visibly happened.
        return RunOutcome(
            failed: plan.reportFailure,
            message: plan.reportFailure
                ? SupervisorUrl.explainFailure(code: 0, rawMessage: error.localizedDescription)
                : nil
        )
    }
}
