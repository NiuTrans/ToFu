import Foundation

/// Read+write seam over the WebView cookie jar so session logic is testable
/// without a WKWebView. Port of CookieSink.kt. The production impl lives in
/// the app target (WKHTTPCookieStore-backed); tests supply a fake.
///
/// Async because WKHTTPCookieStore's API is completion-handler based.
public protocol CookieSink: Sendable {
    /// Inject [cookies] for [origin] (scheme://host), persisting them.
    func inject(origin: String, cookies: [TofuCookie]) async

    /// Hard-invalidate every cookie pinned to [host] (Domain-pinned re-provision path).
    func purgeHost(_ host: String) async

    /// The raw `Cookie:` header the jar holds for [origin], or nil when empty.
    ///
    /// Needed by the INTERACTIVE_SSO path: that login happens inside the
    /// WebView, so no URLSession response ever passes through ``inject`` and
    /// nothing would otherwise stamp `cookieHost`. Reading the jar back is the
    /// only way to observe that an interactive sign-in actually succeeded.
    func cookieHeader(_ origin: String) async -> String?
}
