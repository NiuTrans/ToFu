import Foundation

/// Cookie wire attributes shared by gateway parsing and the WebKit adapter.
/// Response URL validation belongs here; the browser remains the cookie store.
public struct TofuCookie: Equatable, Sendable {
    public let name: String
    public let value: String
    public let path: String
    public let expiresAtMs: Int64?
    public let secure: Bool
    public let httpOnly: Bool
    /// nil means host-only; an explicit Domain is never widened on injection.
    public let domain: String?
    public let sameSite: String?
    public var persistent: Bool { expiresAtMs != nil }

    public init(name: String, value: String, path: String, expiresAtMs: Int64?,
                secure: Bool, httpOnly: Bool, domain: String? = nil, sameSite: String? = nil) {
        self.name = name; self.value = value; self.path = path
        self.expiresAtMs = expiresAtMs; self.secure = secure; self.httpOnly = httpOnly
        self.domain = domain; self.sameSite = sameSite
    }

    public static func parse(_ header: String, nowMs: Int64 = currentTimeMs(), responseURL: URL? = nil) -> TofuCookie? {
        guard !header.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }) else { return nil }
        let parts = header.components(separatedBy: ";")
        guard let first = parts.first, let eq = first.firstIndex(of: "=") else { return nil }
        let name = first[..<eq].trimmingCharacters(in: .whitespaces)
        let value = first[first.index(after: eq)...].trimmingCharacters(in: .whitespaces)
        let separators = CharacterSet(charactersIn: "()<>@,;:\\\"/[]?={} \t")
        guard !name.isEmpty, name.unicodeScalars.allSatisfy({ $0.isASCII && !separators.contains($0) }) else { return nil }
        var attrs: [String: String] = [:]
        for attr in parts.dropFirst() {
            let pair = attr.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            let key = pair[0].trimmingCharacters(in: .whitespaces).lowercased()
            if !key.isEmpty { attrs[key] = pair.count > 1 ? pair[1].trimmingCharacters(in: .whitespaces) : "" }
        }
        let domain = attrs["domain"].map { $0.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: ".")) }
        if let domain {
            guard !domain.isEmpty, !domain.contains("/"), !domain.contains(":") else { return nil }
            if let host = responseURL?.host?.lowercased(), host != domain && !host.hasSuffix(".\(domain)") { return nil }
        }
        let requestPath = responseURL.map(CookieScope.requestPath) ?? "/"
        let defaultPath = requestPath.lastIndex(of: "/").map { String(requestPath[..<$0]) }.flatMap { $0.isEmpty ? nil : $0 } ?? "/"
        let path = attrs["path"].flatMap { $0.hasPrefix("/") ? $0 : nil } ?? defaultPath
        var expiry = attrs["expires"].flatMap(httpDate).map { Int64($0.timeIntervalSince1970 * 1000) }
        if let raw = attrs["max-age"], raw.range(of: #"^-?\d+$"#, options: .regularExpression) != nil {
            let maximum: Int64 = 253_402_300_799_000 // Browser date ceiling: year 9999, not a resource bound.
            let seconds = Int64(raw) ?? (raw.hasPrefix("-") ? Int64.min : Int64.max)
            if seconds <= 0 { expiry = 0 }
            else if nowMs >= maximum || seconds > (maximum - max(nowMs, 0)) / 1000 { expiry = maximum }
            else { expiry = nowMs + seconds * 1000 }
        }
        let sameSite = attrs["samesite"].flatMap { value in
            ["lax": "Lax", "strict": "Strict", "none": "None"][value.lowercased()]
        }
        return TofuCookie(name: name, value: value, path: path, expiresAtMs: expiry,
                          secure: attrs["secure"] != nil, httpOnly: attrs["httponly"] != nil,
                          domain: domain, sameSite: sameSite)
    }

    private static func httpDate(_ value: String) -> Date? {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "GMT")
        for format in ["EEE, dd MMM yyyy HH:mm:ss zzz", "EEEE, dd-MMM-yy HH:mm:ss zzz", "EEE MMM d HH:mm:ss yyyy"] {
            formatter.dateFormat = format
            if let date = formatter.date(from: value) { return date }
        }
        return nil
    }
}

public func currentTimeMs() -> Int64 { Int64(Date().timeIntervalSince1970 * 1000) }

/// Serialization preserves gateway attributes. Only the local code-server
/// session receives a bounded cold-start lifetime; IdP sessions keep theirs.
public enum CookieHeaders {
    private static let combinedHeaderSeparator = try! NSRegularExpression(pattern: #",\s*(?=[!#$%&'*+.^_`|~0-9A-Za-z-]+=)"#)
    public static let persistSeconds: Int64 = 7 * 24 * 60 * 60

    public static func toPersistentHeader(_ cookie: TofuCookie, nowMs: Int64 = currentTimeMs()) -> String {
        var header = "\(cookie.name)=\(cookie.value); Path=\(cookie.path)"
        if let domain = cookie.domain { header += "; Domain=\(domain)" }
        if let expiry = cookie.expiresAtMs {
            header += "; Max-Age=\(max(0, expiry / 1000 - nowMs / 1000))"
        } else if cookie.name == SessionManager.sessionCookieName {
            header += "; Max-Age=\(persistSeconds)"
        }
        if cookie.secure { header += "; Secure" }
        if cookie.httpOnly { header += "; HttpOnly" }
        if let sameSite = cookie.sameSite { header += "; SameSite=\(sameSite)" }
        return header
    }

    /// URLSession may combine repeated Set-Cookie fields. Split only at a
    /// following cookie-pair, never at the comma inside an Expires date.
    public static func individualHeaders(_ values: [String]) -> [String] {
        values.flatMap { value in
            var start = value.startIndex
            var headers: [String] = []
            for match in combinedHeaderSeparator.matches(in: value, range: NSRange(value.startIndex..., in: value)) {
                guard let range = Range(match.range, in: value) else { continue }
                headers.append(String(value[start..<range.lowerBound]))
                start = range.upperBound
            }
            headers.append(String(value[start...]))
            return headers
        }
    }
}
