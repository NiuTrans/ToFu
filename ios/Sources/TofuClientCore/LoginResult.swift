import Foundation

/// Outcome of an attempt to establish a session for a profile.
/// Port of SessionManager.kt's sealed LoginResult.
public enum LoginResult: Equatable, Sendable {
    /// Session cookie obtained and injected into the WebView jar.
    case success(host: String)
    /// Credentials rejected (code-server re-served the login page).
    case badCredentials
    /// Layer-1 SSO detected — caller must open the WebView interactively.
    case needsInteractiveSso(url: String)
    /// No stored credential for this alias.
    case noCredential
    /// Transport / parse failure.
    case error(message: String)
    /// The server's v4 meta endpoint definitively refuses this client build.
    /// Fail-closed at login so the user gets an actionable message instead of
    /// a half-broken SPA.
    case incompatible(message: String)
}
