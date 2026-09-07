import Foundation

/// The two rules that make INTERACTIVE_SSO actually work, as pure functions.
/// Port of InteractiveSso.kt.
public enum InteractiveSso {

    /// Whether a login outcome must hand the user into the WebView. True for
    /// success (session replayed headlessly) AND for needsInteractiveSso,
    /// whose whole point is that sign-in can only complete interactively.
    public static func shouldOpenWebView(_ result: LoginResult) -> Bool {
        switch result {
        case .success, .needsInteractiveSso:
            return true
        case .badCredentials, .noCredential, .error, .incompatible:
            return false
        }
    }

    /// Whether an in-WebView sign-in should now be recorded as a real session.
    /// Both required: the finished page is back on the profile's OWN host, and
    /// the jar actually holds a cookie for that origin. Still sitting on the
    /// IdP — or on the login page — means the gate has not been passed.
    public static func completedSignIn(
        profile: Profile,
        finishedUrl: String,
        cookieHeader: String?
    ) -> Bool {
        guard profile.authType == .interactiveSso else { return false }
        guard let header = cookieHeader,
              !header.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return false }
        guard let own = ServerUrl.parse(profile.baseUrl)?.host,
              let landed = ServerUrl.parse(finishedUrl)?.host,
              landed == own else { return false }
        return !isLoginPage(finishedUrl)
    }

    /// The host to stamp on the profile once ``completedSignIn`` holds, or nil.
    public static func hostToStamp(_ profile: Profile) -> String? {
        ServerUrl.parse(profile.baseUrl)?.host
    }

    private static func isLoginPage(_ url: String) -> Bool {
        url.hasSuffix("/login") || url.contains("/login?") || url.contains("/login#")
    }
}
