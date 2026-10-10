import Foundation

/// Pure derivation of the supervisor's base URL from a profile's Tofu server
/// URL, and endpoint building. Port of SupervisorUrl.kt.
///
/// The supervisor is proxied by the SAME code-server as Tofu, one port up
/// (Tofu 15000 → supervisor 15001): `https://<host>/proxy/15000/` maps to
/// `https://<host>/proxy/15001` and the control endpoints hang off that.
/// The Tofu port is parsed out of the URL's own `/proxy/<port>/` segment, so
/// a deployment on any other port (`…/proxy/15005/`) derives its actual
/// sibling (`…/proxy/15006`) instead of a hardcoded 15001. When the path has
/// no proxy segment (a non-proxy deployment) we fall back to the
/// conventional default.
public struct SupervisorUrl: Equatable, Sendable {
    /// e.g. `https://<host>/proxy/15001` (no trailing slash).
    public let base: String
    /// origin root `https://<host>` — used to look up the session cookie.
    public let origin: String

    public static let tofuPort = "15000"
    public static let supervisorPort = "15001"

    public static let status = "status"
    public static let start = "start"
    public static let stop = "stop"

    /// Derive the supervisor base from a Tofu server URL, or nil if the URL is
    /// not a valid absolute http(s) URL.
    public static func fromServerUrl(_ serverUrl: String) -> SupervisorUrl? {
        let trimmed = serverUrl.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let components = URLComponents(string: trimmed),
              let scheme = components.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = components.host, !host.isEmpty else { return nil }
        var origin = "\(scheme)://\(host.lowercased())"
        if let port = components.port, port != (scheme == "https" ? 443 : 80) {
            origin += ":\(port)"
        }
        // The supervisor rides one port up from whatever port the URL's
        // `/proxy/<port>/` segment names; only a URL with no proxy segment
        // at all falls back to the conventional default.
        let segments = components.path.split(separator: "/").map(String.init)
        var siblingPort = supervisorPort
        if let proxyIndex = segments.firstIndex(of: "proxy"),
           proxyIndex + 1 < segments.count,
           let tofuPort = Int(segments[proxyIndex + 1]) {
            siblingPort = String(tofuPort + 1)
        }
        return SupervisorUrl(base: "\(origin)/proxy/\(siblingPort)", origin: origin)
    }

    /// Build a full endpoint URL. For GET /status the projectPath is passed as
    /// a query param; for POST it is sent in the body (pass nil here).
    public static func endpoint(
        _ supervisor: SupervisorUrl,
        _ name: String,
        projectPathForQuery: String? = nil
    ) -> String {
        let root = "\(supervisor.base)/\(name)"
        guard let projectPathForQuery else { return root }
        return "\(root)?projectPath=\(formEncode(projectPathForQuery))"
    }

    /// java.net.URLEncoder parity: unreserved set is [A-Za-z0-9.-_*], space → +.
    private static func formEncode(_ value: String) -> String {
        let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_*")
        return value
            .addingPercentEncoding(withAllowedCharacters: allowed)?
            .replacingOccurrences(of: "%20", with: "+") ?? value
    }

    /// Turn an opaque supervisor call failure (HTTP status + raw message) into
    /// an ACTIONABLE explanation. The most common failure is a 5xx from the
    /// code-server proxy: nothing listening on the supervisor port, i.e. the
    /// always-on supervisor.py daemon is NOT running on the host (supervisor.py
    /// itself never emits 5xx). The fix lives on the host, so say what to run.
    public static func explainFailure(code: Int, rawMessage: String) -> String {
        if (500...599).contains(code) {
            return "The start/stop daemon isn't responding (HTTP \(code)). This almost " +
                "always means supervisor.py (proxied on port \(supervisorPort)) is " +
                "not running on the host — a 5xx comes from the proxy when nothing " +
                "is listening there, not from the supervisor itself. On the host, " +
                "start it once with:  ./supervisor.sh install  (systemd, keeps it " +
                "always-on). Until it runs, Start/Stop can't work."
        }
        switch code {
        case 404:
            return "Supervisor endpoint not found (HTTP 404). The daemon may be an " +
                "older version, or nothing is serving the /proxy/\(supervisorPort) " +
                "path. Check that supervisor.py is running on the host."
        case 403:
            return "This project path isn't allow-listed on the host. Add its absolute " +
                "path to TOFU_SUPERVISOR_PROJECTS and restart the supervisor " +
                "(./supervisor.sh install), then try again."
        case 401:
            return "Not signed in to this server yet — tap Open first to log in " +
                "with your saved password, then retry Start/Stop."
        case 0:
            return "Couldn't reach the supervisor: \(rawMessage). Check the server URL " +
                "and that the supervisor daemon is running on the host."
        default:
            return rawMessage
        }
    }
}
