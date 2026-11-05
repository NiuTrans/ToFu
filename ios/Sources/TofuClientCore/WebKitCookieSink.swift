#if canImport(WebKit)
import Foundation
import WebKit

/// WebKit owns cookie persistence. Native writes await the store callback;
/// parsing/retention and URL matching are shared with the portable core tests.
public final class WebKitCookieSink: CookieSink, @unchecked Sendable {
    private let cookieStore: WKHTTPCookieStore
    private let dataStore: WKWebsiteDataStore

    public init(dataStore: WKWebsiteDataStore = .default()) {
        self.dataStore = dataStore
        self.cookieStore = dataStore.httpCookieStore
    }

    public func inject(origin: String, cookies: [TofuCookie]) async {
        guard let url = URL(string: origin) else { return }
        for cookie in BrowserCookies.make(cookies, responseURL: url) {
            guard !Task.isCancelled else { return }
            await cookieStore.setCookie(cookie)
        }
    }

    public func cookieHeader(_ target: String) async -> String? {
        guard let url = URL(string: target) else { return nil }
        return BrowserCookies.header(await cookieStore.allCookies(), requestURL: url)
    }

    public func purgeOrigin(_ origin: String, appURL: String) async {
        guard let url = URL(string: appURL), let host = url.host else { return }
        for cookie in await cookieStore.allCookies() where CookieScope.belongsToProfile(domain: cookie.domain, path: cookie.path, appURL: url) {
            await cookieStore.deleteCookie(cookie)
        }
        // Public WebKit records expose displayName, not scheme/port. Remove
        // only records naming this exact host; never broaden to its parent.
        // Cookies are handled above so shared IdP cookies remain untouched.
        let types = WKWebsiteDataStore.allWebsiteDataTypes().subtracting([WKWebsiteDataTypeCookies])
        let records = await dataStore.dataRecords(ofTypes: types)
        let stale = records.filter { $0.displayName.lowercased() == host.lowercased() }
        if !stale.isEmpty { await dataStore.removeData(ofTypes: types, for: stale) }
    }
}
#endif
