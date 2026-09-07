import Foundation

/// Pure model of a server's lifecycle state, and the rules for which controls
/// are legal in each state. Port of ServerLifecycle.kt.
public enum ServerState: String, Sendable {
    /// No projectPath configured — this profile is open-only.
    case unmanaged
    /// Managed, but no authoritative answer yet (never polled, or no session
    /// cookie). Deliberately NOT a locked state: code-server (the proxy) stays
    /// up while Tofu is down, so any control can do login-then-act.
    case unknown
    case running
    case stopped
    /// A start/stop is in flight, or we're polling for the port to bind.
    case transitioning
    /// The last supervisor call failed (daemon down, path not allow-listed…).
    case unreachable
}

/// What the UI may offer in a given ``ServerState``.
public struct ServerCapabilities: Equatable, Sendable {
    public let canStart: Bool
    public let canStop: Bool
    public let canRefresh: Bool
    /// Opening the WebView is pointless while the server is known-stopped —
    /// but stays available: opening is how a user discovers the server is down.
    public let canOpen: Bool
}

/// What caused a supervisor call. NOT cosmetic: a USER tap licenses side
/// effects (login-then-act, visible errors); an AUTO probe must stay READ-ONLY.
public enum ProbeTrigger: Sendable { case auto, user }

/// Whether a supervisor call may log in first, and whether its failure may be
/// surfaced. Derived by ``ServerLifecycle/probePlan(trigger:signedIn:)``.
public struct ProbePlan: Equatable, Sendable {
    /// Run the call at all. False = skip silently.
    public let proceed: Bool
    /// Allowed to POST /login when no cookie is held.
    public let mayLogIn: Bool
    /// Allowed to set the failed flag / show an error message.
    public let reportFailure: Bool
}

/// Which supervisor call to make. An enum so the dispatch switch is exhaustive.
public enum SupervisorAction: Sendable { case start, stop, status }

/// What a finished call should do to the UI.
public struct CallCompletion: Equatable, Sendable {
    /// Hand the user into the WebView (a start that reached RUNNING).
    public let handOff: Bool
    /// Show the "still booting" copy (a start whose poll window expired).
    public let showTimeout: Bool
}

public enum ServerLifecycle {

    /// True when [profile] opted into supervisor control by setting a project path.
    public static func isManaged(_ profile: Profile) -> Bool {
        guard let path = profile.projectPath else { return false }
        return !path.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// Whether the profile currently holds a session cookie valid for its OWN
    /// host. A stale cookieHost from a previous URL must NOT count as signed in.
    public static func isSignedIn(_ profile: Profile) -> Bool {
        guard let host = ServerUrl.parse(profile.baseUrl)?.host else { return false }
        return profile.cookieHost == host
    }

    /// Resolve the displayable state. [running] is nil when never polled;
    /// [busy] marks an in-flight call; [failed] marks the last call errored.
    public static func resolve(
        profile: Profile,
        running: Bool?,
        busy: Bool = false,
        failed: Bool = false
    ) -> ServerState {
        if !isManaged(profile) { return .unmanaged }
        if busy { return .transitioning }
        if failed { return .unreachable }
        // A poll result OUTRANKS the cookie check: if the supervisor answered,
        // we demonstrably reached it, so report the truth.
        if running == true { return .running }
        if running == false { return .stopped }
        return .unknown
    }

    public static func capabilities(_ state: ServerState) -> ServerCapabilities {
        switch state {
        case .unmanaged:
            return ServerCapabilities(canStart: false, canStop: false, canRefresh: false, canOpen: true)
        case .unknown:
            // Every control stays live: the supervisor rides the code-server
            // session, and code-server is up even while Tofu is down.
            return ServerCapabilities(canStart: true, canStop: true, canRefresh: true, canOpen: true)
        case .running:
            return ServerCapabilities(canStart: false, canStop: true, canRefresh: true, canOpen: true)
        case .stopped:
            return ServerCapabilities(canStart: true, canStop: false, canRefresh: true, canOpen: true)
        case .transitioning:
            // Open stays ENABLED on purpose: a start poll can outlast the window,
            // and taking Open away would leave no actionable control at all.
            return ServerCapabilities(canStart: false, canStop: false, canRefresh: false, canOpen: true)
        case .unreachable:
            return ServerCapabilities(canStart: true, canStop: true, canRefresh: true, canOpen: true)
        }
    }

    /// Decide what a supervisor call is permitted to do, given who asked.
    ///
    /// The rule that matters: an AUTO probe against a profile with NO session
    /// is SKIPPED entirely — otherwise merely opening the home screen fires one
    /// POST /login per unsigned server (an auto-retry loop toward lockout on a
    /// bad password; impossible on SSO), painting healthy cards red on every
    /// cold start. "Not signed in yet" is not "unreachable".
    public static func probePlan(trigger: ProbeTrigger, signedIn: Bool) -> ProbePlan {
        switch trigger {
        case .user:
            return ProbePlan(proceed: true, mayLogIn: true, reportFailure: true)
        case .auto:
            return ProbePlan(proceed: signedIn, mayLogIn: false, reportFailure: false)
        }
    }

    /// Decide what a COMPLETED call does to the UI. [stillCurrent] guards
    /// against yanking the user into a WebView they navigated away from while
    /// a start poll ran. Only a START can hand off or time out.
    public static func completionFor(
        action: SupervisorAction,
        running: Bool,
        stillCurrent: Bool
    ) -> CallCompletion {
        if !stillCurrent || action != .start {
            return CallCompletion(handOff: false, showTimeout: false)
        }
        return CallCompletion(handOff: running, showTimeout: !running)
    }

    /// How long to wait for `server.py` to bind its port after `/start`.
    /// EXPIRING THE WINDOW IS NOT AN ERROR — see ``startTimeoutMessage()``.
    public static let startPollAttempts = 15
    public static let startPollIntervalMs: Int64 = 2_000

    /// Total start-poll window in seconds, for user-facing copy.
    public static var startPollWindowSeconds: Int {
        startPollAttempts * Int(startPollIntervalMs) / 1000
    }

    /// Shown when the start poll window expires without the port coming up.
    /// Explicitly NOT a failure — the user is left with something to DO.
    public static func startTimeoutMessage() -> String {
        "Started, but the server hasn't answered in \(startPollWindowSeconds)s — " +
            "it may still be booting. Tap Check again, or Open to watch it come up."
    }

    /// True when a login outcome BLOCKS the supervisor call that follows it.
    /// needsInteractiveSso counts as blocking: it yields no cookie, so
    /// proceeding would 401 and misreport an un-completed sign-in as "the
    /// daemon isn't responding".
    public static func isLoginBlocking(_ result: LoginResult) -> Bool {
        switch result {
        case .success: return false
        case .badCredentials, .noCredential, .needsInteractiveSso, .error, .incompatible:
            return true
        }
    }

    /// Why a login-then-act attempt could not reach the supervisor.
    public static func explainLoginBlock(_ result: LoginResult) -> String {
        switch result {
        case .badCredentials:
            return "Wrong password for this server — edit it and try again."
        case .noCredential:
            return "No saved password for this server, so it can't be controlled from here."
        case .needsInteractiveSso:
            return "This server needs an interactive sign-in first — tap Open, sign in " +
                "once, then Start and Stop will work from here."
        case .error(let message):
            return "Can't reach this server: \(message)"
        case .incompatible(let message):
            return message
        case .success:
            return "Signed in."
        }
    }

    /// Short status word for the state chip.
    public static func label(_ state: ServerState) -> String {
        switch state {
        case .unmanaged: return "Open only"
        case .unknown: return "Tap to check"
        case .running: return "Running"
        case .stopped: return "Stopped"
        case .transitioning: return "Working…"
        case .unreachable: return "Unreachable"
        }
    }
}
