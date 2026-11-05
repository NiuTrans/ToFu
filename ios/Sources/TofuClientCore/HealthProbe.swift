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
        let healthUrl = server.endpoint("api/health")

        var headers = [String: String]()
        if let cookie = await cookies.cookieHeader(healthUrl), !cookie.isEmpty {
            headers["Cookie"] = cookie
        }
        do {
            // A gateway redirect remains visible; a probe never forwards its
            // cookie header to a different origin.
            let resp = try await http.send(HTTPRequest(
                url: healthUrl, method: "GET", headers: headers,
                followRedirects: false
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
