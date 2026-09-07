import Foundation

/// Minimal Set-Cookie model — the OkHttp `Cookie` equivalent for the pure
/// layer. Only the fields the session logic consumes are parsed.
public struct TofuCookie: Equatable, Sendable {
    public let name: String
    public let value: String
    public let path: String
    /// Absolute expiry (epoch ms), nil for a session cookie.
    public let expiresAtMs: Int64?
    public let secure: Bool
    public let httpOnly: Bool

    /// True when the cookie carries a real expiry (Max-Age or Expires).
    public var persistent: Bool { expiresAtMs != nil }

    /// Parse one Set-Cookie header value. Returns nil when the first pair is
    /// not `name=value`.
    public static func parse(_ header: String, nowMs: Int64 = currentTimeMs()) -> TofuCookie? {
        let parts = header.components(separatedBy: ";")
        guard let first = parts.first,
              let eq = first.firstIndex(of: "=") else { return nil }
        let name = first[..<eq].trimmingCharacters(in: .whitespaces)
        let value = first[first.index(after: eq)...].trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return nil }

        var path = "/"
        var expiresAtMs: Int64?
        var secure = false
        var httpOnly = false
        for attr in parts.dropFirst() {
            let pair = attr.split(separator: "=", maxSplits: 1).map {
                $0.trimmingCharacters(in: .whitespaces)
            }
            let key = pair[0].lowercased()
            let val = pair.count > 1 ? pair[1] : ""
            switch key {
            case "path":
                if !val.isEmpty { path = val }
            case "max-age":
                if let seconds = Int64(val) { expiresAtMs = nowMs + seconds * 1000 }
            case "expires":
                if let date = Self.httpDate(val) { expiresAtMs = Int64(date.timeIntervalSince1970 * 1000) }
            case "secure":
                secure = true
            case "httponly":
                httpOnly = true
            default:
                break
            }
        }
        return TofuCookie(name: name, value: value, path: path,
                          expiresAtMs: expiresAtMs, secure: secure, httpOnly: httpOnly)
    }

    private static func httpDate(_ value: String) -> Date? {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "GMT")
        // RFC 1123 (`Sun, 06 Nov 1994 08:49:37 GMT`), the common Set-Cookie form.
        formatter.dateFormat = "EEE, dd MMM yyyy HH:mm:ss zzz"
        return formatter.date(from: value)
    }
}

public func currentTimeMs() -> Int64 {
    Int64(Date().timeIntervalSince1970 * 1000)
}

/// Pure Set-Cookie header formatting. Port of CookieHeaders.kt — the
/// cold-start-survival upgrade: the gateway issues `code-server-session` with
/// NO expiry (a session cookie the WebView drops on cold start), so a Max-Age
/// is appended on injection.
public enum CookieHeaders {

    /// One week; long enough to avoid re-login churn, short enough to bound staleness.
    public static let persistSeconds: Int64 = 7 * 24 * 60 * 60

    /// Serialise a cookie to a Set-Cookie header string. A cookie WITHOUT a
    /// real expiry gets a `Max-Age` so the WebView keeps it across a cold
    /// start; one with a far-future expiry keeps its own remaining lifetime.
    public static func toPersistentHeader(
        _ cookie: TofuCookie,
        nowMs: Int64 = currentTimeMs()
    ) -> String {
        var header = "\(cookie.name)=\(cookie.value); Path=\(cookie.path)"
        if let expiresAt = cookie.expiresAtMs {
            header += "; Max-Age=\((expiresAt - nowMs) / 1000)"
        } else {
            header += "; Max-Age=\(persistSeconds)"
        }
        if cookie.secure { header += "; Secure" }
        if cookie.httpOnly { header += "; HttpOnly" }
        // Top-level navigation → Lax is correct.
        header += "; SameSite=Lax"
        return header
    }
}
