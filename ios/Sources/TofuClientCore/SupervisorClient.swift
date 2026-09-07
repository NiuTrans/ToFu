import Foundation

public enum SupervisorResult: Equatable, Sendable {
    /// running state after the call (start/stop/status all report it).
    case ok(running: Bool)
    case failed(code: Int, message: String)
}

/// Client for the host-side supervisor daemon (see supervisor.py), which the
/// app uses to START and STOP the Tofu server for a profile's projectPath.
/// Port of SupervisorClient.kt.
///
/// Reachability: the supervisor sits behind the SAME code-server that proxies
/// Tofu, on a sibling proxied port (15000 → 15001), so it inherits the
/// `code-server-session` cookie the profile login already established. No
/// extra auth token — the code-server password already gates the whole proxy.
///
/// The HTTP-free URL/endpoint logic lives in ``SupervisorUrl`` so it is
/// unit-testable without a device.
public final class SupervisorClient: Sendable {

    private let http: HTTPClient
    /// Reads the WebView jar for an origin — the profile's live code-server
    /// session cookie (same host) is the only gate the supervisor relies on.
    private let cookieProvider: @Sendable (String) async -> String?

    public init(
        http: HTTPClient,
        cookieProvider: @escaping @Sendable (String) async -> String?
    ) {
        self.http = http
        self.cookieProvider = cookieProvider
    }

    /// GET /status — authoritative running state (used for polling after start).
    public func status(_ profile: Profile) async -> SupervisorResult {
        await call(profile, SupervisorUrl.status, method: "GET")
    }

    /// POST /start — idempotent; returns immediately, caller polls ``status``.
    public func start(_ profile: Profile) async -> SupervisorResult {
        await call(profile, SupervisorUrl.start, method: "POST")
    }

    /// POST /stop — runs the project's stop.sh via the supervisor.
    public func stop(_ profile: Profile) async -> SupervisorResult {
        await call(profile, SupervisorUrl.stop, method: "POST")
    }

    private func call(_ profile: Profile, _ endpoint: String, method: String) async -> SupervisorResult {
        guard let projectPath = profile.projectPath,
              !projectPath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return .failed(code: 0, message: "no project path configured")
        }
        guard let base = SupervisorUrl.fromServerUrl(profile.baseUrl) else {
            return .failed(code: 0, message: "cannot derive supervisor URL from \(profile.baseUrl)")
        }
        let url = SupervisorUrl.endpoint(
            base, endpoint,
            projectPathForQuery: method == "GET" ? projectPath : nil
        )

        var headers: [String: String] = [:]
        if let cookie = await cookieProvider(base.origin) { headers["Cookie"] = cookie }

        let body: HTTPRequest.Body? = method == "POST"
            ? .json(Self.jsonBody(["projectPath": projectPath]))
            : nil
        let request = HTTPRequest(url: url, method: method, headers: headers, body: body)

        do {
            let resp = try await http.send(request)
            let json = Self.jsonObject(resp.body)
            if (200...299).contains(resp.status), (json?["ok"] as? Bool) == true {
                return .ok(running: (json?["running"] as? Bool) ?? false)
            }
            return .failed(
                code: resp.status,
                message: (json?["error"] as? String) ?? "HTTP \(resp.status)"
            )
        } catch {
            return .failed(code: 0, message: error.localizedDescription)
        }
    }

    private static func jsonObject(_ body: String) -> [String: Any]? {
        guard let data = body.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) else { return nil }
        return object as? [String: Any]
    }

    private static func jsonBody(_ dictionary: [String: String]) -> String {
        // Cannot fail for a String-keyed/String-valued dictionary.
        let data = try! JSONSerialization.data(withJSONObject: dictionary)
        return String(data: data, encoding: .utf8)!
    }
}
