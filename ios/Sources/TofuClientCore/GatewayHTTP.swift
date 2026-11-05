import Foundation

/// Gateway transport policy: per-URL cookies, explicit redirects, bounded body
/// and cancellation checks before every side effect. No independent cookie jar.
struct GatewayHTTP: Sendable {
    static let maximumResponseBytes = 262_144
    let http: HTTPClient
    let cookies: CookieSink

    static func isCancellation(_ error: Error) -> Bool {
        error is CancellationError || (error as? URLError)?.code == .cancelled || Task.isCancelled
    }

    func send(_ url: String, method: String = "GET", body: HTTPRequest.Body? = nil) async throws -> HTTPResponse {
        try Task.checkCancellation()
        var headers: [String: String] = [:]
        if let cookie = await cookies.cookieHeader(url), !cookie.isEmpty { headers["Cookie"] = cookie }
        try Task.checkCancellation()
        let response = try await http.send(HTTPRequest(url: url, method: method, headers: headers, body: body,
                                                       followRedirects: false, maximumResponseBytes: Self.maximumResponseBytes))
        try Task.checkCancellation()
        guard response.body.utf8.count <= Self.maximumResponseBytes else { throw URLError(.dataLengthExceedsMaximum) }
        return response
    }

    func receiveCookies(_ response: HTTPResponse, url: URL) async throws -> [TofuCookie] {
        try Task.checkCancellation()
        let parsed = CookieHeaders.individualHeaders(response.headerValues("set-cookie"))
            .compactMap { TofuCookie.parse($0, responseURL: url) }
        if !parsed.isEmpty { await cookies.inject(origin: url.absoluteString, cookies: parsed) }
        try Task.checkCancellation()
        return parsed
    }
}
