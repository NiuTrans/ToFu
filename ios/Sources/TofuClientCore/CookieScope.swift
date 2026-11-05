import Foundation

/// Browser cookie request matching shared by the WebKit adapter and portable
/// specifications. Ports do not scope cookies; scheme, host, path and expiry do.
enum CookieScope {
    static func matches(domain: String, hostOnly: Bool, path: String, secure: Bool,
                        expires: Date?, url: URL, now: Date = Date()) -> Bool {
        guard let host = url.host?.lowercased(), expires.map({ $0 > now }) ?? true,
              !secure || url.scheme?.lowercased() == "https" else { return false }
        let domain = domain.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
        guard host == domain || (!hostOnly && host.hasSuffix(".\(domain)")) else { return false }
        return pathMatches(cookiePath: path, requestPath: requestPath(url))
    }

    static func pathMatches(cookiePath: String, requestPath: String) -> Bool {
        requestPath == cookiePath || (requestPath.hasPrefix(cookiePath) &&
            (cookiePath.hasSuffix("/") || requestPath.dropFirst(cookiePath.count).hasPrefix("/")))
    }

    static func belongsToProfile(domain: String, path: String, appURL: URL) -> Bool {
        domain.trimmingCharacters(in: CharacterSet(charactersIn: ".")).lowercased() == appURL.host?.lowercased() &&
            pathMatches(cookiePath: path, requestPath: requestPath(appURL))
    }

    static func requestPath(_ url: URL) -> String {
        let path = URLComponents(url: url, resolvingAgainstBaseURL: true)?.percentEncodedPath ?? ""
        return path.isEmpty ? "/" : path
    }
}
