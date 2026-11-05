import Foundation

/// Asynchronous port over the browser-owned cookie jar. Requests use full URLs
/// for path/secure matching; injection preserves the response URL's authority.
public protocol CookieSink: Sendable {
    func inject(origin: String, cookies: [TofuCookie]) async
    /// Purge old application scope while preserving shared parent-domain IdP cookies.
    func purgeOrigin(_ origin: String, appURL: String) async
    func cookieHeader(_ url: String) async -> String?
}
