#if canImport(WebKit)
import Foundation
import WebKit

/// Production ``CookieSink``: bridges headless-obtained session cookies into
/// the shared WKWebView cookie jar. Port of CookieBridge.kt.
///
/// Two spike-driven behaviours are load-bearing:
///  1. **Persistence upgrade** — the gateway issues `code-server-session`
///     with NO `Max-Age`/`Expires` (a session cookie the jar drops on a cold
///     start), so session cookies are written with a bounded expiry
///     (``CookieHeaders/persistSeconds``).
///  2. **purgeHost hard-invalidates storage too** — Gap-4: Tofu caches
///     conversations in IndexedDB keyed by origin, so a re-provisioned
///     sandbox's new host must not inherit the dead host's jar OR its
///     localStorage/IndexedDB.
public final class WebKitCookieSink: CookieSink {

    private let cookieStore: WKHTTPCookieStore
    private let dataStore: WKWebsiteDataStore

    public init(dataStore: WKWebsiteDataStore = .default()) {
        self.dataStore = dataStore
        self.cookieStore = dataStore.httpCookieStore
    }

    public func inject(origin: String, cookies: [TofuCookie]) async {
        guard let host = URL(string: origin)?.host else { return }
        for c in cookies {
            var props: [HTTPCookiePropertyKey: Any] = [
                .name: c.name,
                .value: c.value,
                .domain: host,
                .path: c.path,
                .expires: c.expiresAtMs.map { Date(timeIntervalSince1970: Double($0) / 1000) }
                    ?? Date(timeIntervalSinceNow: TimeInterval(CookieHeaders.persistSeconds)),
            ]
            if c.secure { props[.secure] = true }
            guard let cookie = HTTPCookie(properties: props) else { continue }
            try? await cookieStore.setCookie(cookie)
        }
    }

    public func cookieHeader(_ origin: String) async -> String? {
        guard let host = URL(string: origin)?.host else { return nil }
        let matching = await cookies(for: host)
        guard !matching.isEmpty else { return nil }
        return matching.map { "\($0.name)=\($0.value)" }.joined(separator: "; ")
    }

    public func purgeHost(_ host: String) async {
        for cookie in await cookies(for: host) {
            try? await cookieStore.deleteCookie(cookie)
        }
        let records = await dataStore.dataRecords(
            ofTypes: WKWebsiteDataStore.allWebsiteDataTypes()
        )
        let stale = records.filter {
            $0.displayName == host || $0.displayName.hasSuffix(".\(host)")
        }
        if !stale.isEmpty {
            await dataStore.removeData(
                ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), for: stale
            )
        }
    }

    /// Cookies valid for `host`: exact host cookies plus domain cookies the
    /// host matches — the same set Android's CookieManager.getCookie(origin)
    /// returns for that origin.
    private func cookies(for host: String) async -> [HTTPCookie] {
        await cookieStore.allCookies().filter { cookie in
            let domain = cookie.domain.hasPrefix(".")
                ? String(cookie.domain.dropFirst())
                : cookie.domain
            return host == domain || host.hasSuffix(".\(domain)")
        }
    }
}
#endif
