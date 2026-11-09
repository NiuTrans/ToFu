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
        case .badCredentials, .error, .incompatible:
            return false
        }
    }

    /// A landing page is only a candidate. A protected probe must prove the gate.
    public static func isCompletionCandidate(profile: Profile, finishedUrl: String, cookieHeader: String?) -> Bool {
        guard profile.authType == .interactiveSso, let header = cookieHeader,
              !header.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let own = ServerUrl.parse(profile.baseUrl), let landed = ServerUrl.parseNavigation(finishedUrl),
              own.origin == landed.origin else { return false }
        let base = own.components.percentEncodedPath.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let path = landed.components.percentEncodedPath.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        return (base.isEmpty || path == base || path.hasPrefix(base + "/")) && landed.path.split(separator: "/").last != "login"
    }

    public static func completedSignIn(profile: Profile, finishedUrl: String, cookieHeader: String?, gatewayVerified: Bool = false) -> Bool {
        gatewayVerified && isCompletionCandidate(profile: profile, finishedUrl: finishedUrl, cookieHeader: cookieHeader)
    }

    public static func profileForWebView(_ profile: Profile, result: LoginResult) -> Profile {
        var active = profile
        if case .needsInteractiveSso = result { active.authType = .interactiveSso }
        return active
    }

    public static func hostToStamp(_ profile: Profile) -> String? { ServerUrl.parse(profile.baseUrl)?.host }
}
