import Foundation

/// The network half of ``TofuProbe``: a single `GET {base}/api/health`
/// carrying the profile's session cookie when one is in the jar.
///
/// The cookie is not optional decoration: behind a VS Code port-forwarding
/// proxy the edge 401s EVERY cookie-less request (the desktop agent's
/// never-reached-Tofu incident), so a probe without the session measures the
/// GATE, not the server. With the cookie attached, the verdicts read true:
/// 200+bootId is Tofu up, a Tofu-envelope 401 is Tofu's own auth, anything
/// else refused is the proxy edge.
public struct HealthProbe: Sendable {

    public struct Outcome: Equatable, Sendable {
        public let verdict: TofuProbe.Verdict
        /// HTTP status, or 0 when no response arrived (transport failure).
        public let status: Int
        /// Body prefix (≤200 chars) or the transport error, for diagnostics.
        public let detail: String
    }

    private let http: HTTPClient
    private let cookies: CookieSink

    /// The probe budget belongs to the transport: callers that want the
    /// Android 8s probe timeout inject an HTTPClient configured for it.
    public init(http: HTTPClient, cookies: CookieSink) {
        self.http = http
        self.cookies = cookies
    }

    /// Probe `{serverUrl}/api/health` once. Never throws.
    public func probe(_ rawUrl: String) async -> Outcome {
        guard let server = ServerUrl.parse(rawUrl) else {
            return Outcome(verdict: .unreachable, status: 0, detail: "invalid URL")
        }
        // Request URL keeps the explicit port; the cookie lookup keys on the
        // port-less origin (the jar is Domain-pinned to host alone).
        var base = "\(server.scheme)://\(server.host)"
        if let port = server.components.port { base += ":\(port)" }
        base += server.path
        while base.hasSuffix("/") { base.removeLast() }
        let healthUrl = base + "/api/health"

        var headers = [String: String]()
        if let cookie = await cookies.cookieHeader(server.origin), !cookie.isEmpty {
            headers["Cookie"] = cookie
        }
        do {
            // followRedirects: a 302-to-login lands on the login PAGE (200
            // HTML, no bootId) and classifies notTofu — the honest reading.
            let resp = try await http.send(HTTPRequest(
                url: healthUrl, method: "GET", headers: headers,
                followRedirects: true
            ))
            return Outcome(
                verdict: TofuProbe.classify(status: resp.status, body: resp.body),
                status: resp.status,
                detail: String(resp.body.prefix(200))
            )
        } catch {
            return Outcome(verdict: .unreachable, status: 0, detail: String(describing: error))
        }
    }
}
