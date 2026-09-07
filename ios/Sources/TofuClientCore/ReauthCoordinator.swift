import Foundation

/// Headless re-auth coordination — the testable core of Android's
/// ReauthWebViewClient, with every WebKit hook replaced by a closure.
///
/// Session expiry inside the WebView surfaces three ways:
///   * the main frame 302s to `…/login` (code-server password gate bounced);
///   * the main frame answers a bare 401 (edge session died under the page);
///   * the page itself calls `TofuNative.requestReauth` after its API
///     transport saw the outer gateway's 401.
/// All three funnel through the same latched trigger: one headless
/// `SessionManager.login`, then a reload on success.
///
/// Invariants ported from ReauthWebViewClient.kt:
///   * INTERACTIVE_SSO is NEVER intercepted — its sign-in IS a sequence of
///     main-frame login-page navigations; a headless login cannot satisfy it,
///     so triggering would only latch the gate and freeze the user on a blank
///     surface.
///   * The in-flight latch clears on the observed OUTCOME (`settle`), never
///     on a timer — a slow or failed re-auth must not re-open the trigger and
///     resume a redirect storm.
///   * Consecutive failures are capped: an expired password would otherwise
///     retry forever behind the latch, burning the vscode tunnel and, on some
///     gates, marching toward account lockout.
public final class ReauthCoordinator {

    public enum Action: Equatable, Sendable {
        /// A headless login started; on success the WebView should reload.
        case reauthStarted
        /// MAX_CONSECUTIVE_FAILURES headless re-logins failed in a row — the
        /// session can no longer be re-established without the user (password
        /// changed, gateway down). Drop to the server list, which has the
        /// controls and copy to fix it.
        case exhausted
    }

    public static let maxConsecutiveFailures = 3

    private let authType: AuthType
    private let sink: (Action) -> Void
    public private(set) var inFlight = false
    public private(set) var consecutiveFailures = 0

    public init(authType: AuthType, sink: @escaping (Action) -> Void) {
        self.authType = authType
        self.sink = sink
    }

    /// The decision behind a main-frame navigation. Mirrors
    /// shouldOverrideUrlLoading's two interception rules.
    public enum NavigationVerdict: Equatable, Sendable {
        case allow
        /// Swallow the navigation and run headless re-auth; it will reload.
        case interceptForReauth
        /// Never load this in the WebView (non-http scheme like mailto:/tel:,
        /// or a gesture-driven main-frame hop to a foreign host — the shell
        /// has no chrome, so it would strand the user). Hand to the system.
        case openExternally
    }

    /// Main-frame redirect-to-login or bare 401 on the main frame.
    public func trigger() {
        guard authType != .interactiveSso, !inFlight else { return }
        inFlight = true
        sink(.reauthStarted)
    }

    /// Decide what to do with a main-frame navigation. `hasGesture`
    /// distinguishes a user-tapped link (external hops leave the shell) from
    /// a redirect (which must stay in place — including SSO IdP hops).
    public func navigationVerdict(
        url: String,
        isMainFrame: Bool,
        hasGesture: Bool,
        ownHost: String?
    ) -> NavigationVerdict {
        guard let comps = URLComponents(string: url),
              let scheme = comps.scheme?.lowercased()
        else { return .allow }

        if scheme != "http" && scheme != "https" {
            return .openExternally
        }
        if isMainFrame, looksLikeLogin(url), authType != .interactiveSso {
            trigger()
            return .interceptForReauth
        }
        if isMainFrame, hasGesture, authType != .interactiveSso,
           let own = ownHost, let host = comps.host, host != own {
            return .openExternally
        }
        return .allow
    }

    /// The login attempt resolved (success OR failure). Only now is the
    /// trigger re-armed — and the failure cap enforced.
    public func settle(succeeded: Bool) {
        inFlight = false
        if succeeded {
            consecutiveFailures = 0
            return
        }
        consecutiveFailures += 1
        if consecutiveFailures >= Self.maxConsecutiveFailures {
            consecutiveFailures = 0
            sink(.exhausted)
        }
    }

    /// `…/login`, `…/login?…`, `…/login#…` — code-server's gate bounce.
    public static func looksLikeLogin(_ url: String) -> Bool {
        url.hasSuffix("/login") || url.contains("/login?") || url.contains("/login#")
    }
}
