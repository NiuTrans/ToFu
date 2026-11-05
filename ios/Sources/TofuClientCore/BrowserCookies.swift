import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Foundation cookie conversion and URL selection. WebKit only supplies the
/// asynchronous cookie store; these rules run in the portable core suite.
enum BrowserCookies {
    static func make(_ cookies: [TofuCookie], responseURL: URL) -> [HTTPCookie] {
        cookies.compactMap { cookie in
            guard let parsed = HTTPCookie.cookies(withResponseHeaderFields: ["Set-Cookie": CookieHeaders.toPersistentHeader(cookie)], for: responseURL).first,
                  var properties = parsed.properties else { return nil }
            // Foundation's version-0 cookies do not consistently turn Max-Age
            // into expiresDate. Supply the policy's absolute date explicitly.
            let expiry = cookie.expiresAtMs ?? (cookie.name == SessionManager.sessionCookieName
                ? currentTimeMs() + CookieHeaders.persistSeconds * 1000 : nil)
            properties[.maximumAge] = nil
            // The properties getter uses Bool and omits HttpOnly on some
            // Foundation versions; the initializer expects these string flags.
            properties[.secure] = cookie.secure ? "TRUE" : nil
            properties[HTTPCookiePropertyKey(rawValue: "HttpOnly")] = cookie.httpOnly ? "TRUE" : nil
            if let expiry {
                properties[.expires] = Date(timeIntervalSince1970: Double(expiry) / 1000)
                properties[.discard] = "FALSE"
            }
            #if canImport(Darwin)
            // Apple exposes strict/lax; nil/unknown denotes unrestricted.
            properties[.sameSitePolicy] = cookie.sameSite?.lowercased()
            #endif
            return HTTPCookie(properties: properties)
        }
    }

    static func header(_ cookies: [HTTPCookie], requestURL: URL) -> String? {
        let matching = cookies.filter { cookie in
            CookieScope.matches(domain: cookie.domain, hostOnly: !cookie.domain.hasPrefix("."),
                path: cookie.path, secure: cookie.isSecure, expires: cookie.expiresDate, url: requestURL)
        }.sorted { $0.path.count > $1.path.count }
        return matching.isEmpty ? nil : matching.map { "\($0.name)=\($0.value)" }.joined(separator: "; ")
    }
}
