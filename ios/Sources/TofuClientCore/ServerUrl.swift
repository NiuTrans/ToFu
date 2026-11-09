import Foundation

/// URL authority shared by saved connections, gateway discovery and routing.
/// parse admits credential-free storage; parseNavigation is transient only.
/// Dependencies: Foundation only; origin/proxy semantics remain shared.
public struct ServerUrl: Equatable, Sendable {
    public let raw: String
    public let components: URLComponents

    /// Lowercased, matching okhttp's host normalisation (cookies are
    /// Domain-pinned case-insensitively).
    public var host: String { (components.host ?? "").lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]")) }

    public var scheme: String { (components.scheme ?? "https").lowercased() }

    /// HTTP origin retains non-default ports and IPv6 brackets.
    public var origin: String {
        let authority = host.contains(":") ? "[\(host)]" : host
        let port = components.port ?? Self.defaultPort(for: scheme)
        return "\(scheme)://\(authority)" + (port == Self.defaultPort(for: scheme) ? "" : ":\(port)")
    }

    public var codeServerRootPath: String {
        guard let range = components.percentEncodedPath.range(of: Self.proxyPath, options: .regularExpression) else { return "/" }
        return String(components.percentEncodedPath[..<range.lowerBound]) + "/"
    }

    public var loginUrl: String { "\(origin)\(codeServerRootPath)login" }

    public func sameOrigin(_ url: URL) -> Bool {
        Self.parseNavigation(url.absoluteString)?.origin == origin
    }

    public func isLoginURL(_ target: String) -> Bool {
        guard let landed = Self.parseNavigation(target), landed.origin == origin else { return false }
        return landed.path.trimmingCharacters(in: CharacterSet(charactersIn: "/")) ==
            URL(string: loginUrl)!.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }

    public func endpoint(_ relativePath: String) -> String {
        var result = components
        let base = components.percentEncodedPath.hasSuffix("/") ? components.percentEncodedPath : components.percentEncodedPath + "/"
        result.percentEncodedPath = (base.isEmpty ? "/" : base) + relativePath.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        result.query = nil
        result.fragment = nil
        return result.string!
    }

    /// MLP codelab instance id parsed from a `<uuid>-vscode-<idc>.…` host, or
    /// nil for non-MLP hosts. Recognises "same logical server, new URL".
    public var instanceUuid: String? {
        Rx.group(#"^([0-9a-fA-F-]{8,})-vscode-[^.]+\."#, in: host)
    }

    /// The URL path exactly as typed (trailing `/proxy/<port>/` included).
    public var path: String { components.path }

    /// Saved URLs, QR targets and paste must not contain credentials, even in
    /// encoded query/SPA fields or nested redirect URLs.
    public static func parse(_ raw: String) -> ServerUrl? {
        parse(raw, allowTransientSecrets: false)
    }

    /// Navigation/request syntax only: an IdP callback may carry code/token
    /// fields. This result never authorizes storing that URL in a Profile.
    public static func parseNavigation(_ raw: String) -> ServerUrl? {
        parse(raw, allowTransientSecrets: true)
    }

    /// Explicit edit projection for a legacy row, never a launch migration.
    /// Remove whole credential-bearing fields while preserving safe SPA state.
    public static func sanitizeForStorage(_ raw: String) -> String? {
        if let valid = parse(raw) { return valid.raw }
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !hasControls(text), var parts = URLComponents(string: text) else { return nil }
        parts.user = nil
        parts.password = nil
        parts.percentEncodedQuery = cleanFields(parts.percentEncodedQuery)
        if let fragment = parts.percentEncodedFragment, let separator = fragment.firstIndex(of: "?") {
            let route = String(fragment[..<separator])
            guard !hasSecretParameters(route) else { return nil }
            let fields = cleanFields(String(fragment[fragment.index(after: separator)...]))
            parts.percentEncodedFragment = fields.map { route + "?" + $0 } ?? (route.isEmpty ? nil : route)
        } else {
            parts.percentEncodedFragment = cleanFields(parts.percentEncodedFragment)
        }
        return parts.string.flatMap { parse($0)?.raw }
    }

    private static func parse(_ raw: String, allowTransientSecrets: Bool) -> ServerUrl? {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !hasControls(text), let parts = URLComponents(string: text),
              let scheme = parts.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = parts.host, !host.isEmpty,
              !host.contains(where: { $0.isWhitespace || "\\/@?#%".contains($0) }),
              parts.user == nil, parts.password == nil,
              parts.port.map({ (1...65535).contains($0) }) ?? true,
              parts.url != nil,
              allowTransientSecrets || (!hasSecretParameters(parts.percentEncodedQuery) &&
                  !hasSecretParameters(parts.percentEncodedFragment)) else { return nil }
        let normalized = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        let authority = normalized.contains(":") ? "[\(normalized)]" : normalized
        let canonical = "\(scheme)://\(authority)" + (parts.port.map { ":\($0)" } ?? "") +
            (parts.percentEncodedPath.isEmpty ? "/" : parts.percentEncodedPath) +
            (parts.percentEncodedQuery.map { "?" + $0 } ?? "") +
            (parts.percentEncodedFragment.map { "#" + $0 } ?? "")
        guard let components = URLComponents(string: canonical), components.url != nil else { return nil }
        return ServerUrl(raw: canonical, components: components)
    }

    static func hasControls(_ text: String) -> Bool {
        text.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
    }

    private static func cleanFields(_ fields: String?) -> String? {
        guard let fields else { return nil }
        let retained = fields.split(whereSeparator: { $0 == "&" || $0 == ";" })
            .filter { !hasSecretParameters(String($0)) }.joined(separator: "&")
        return retained.isEmpty ? nil : retained
    }

    private static let percentEscapes = try! NSRegularExpression(pattern: #"(?:%[0-9a-fA-F]{2})+"#)
    private static let browserUserinfo = #"(?i)(?:(?:https?|wss?|ftp):/*|/{2,})[^/?#]*@"#

    /// Decode to a fixed point: every pass removes percent-escape bytes, so
    /// deeply encoded credentials cannot hide beyond an arbitrary layer cap.
    /// Decode escape runs independently so a legitimate literal percent does
    /// not prevent inspecting another nested field.
    private static func decoded(_ value: String) -> String? {
        var result = value
        while true {
            // Decoding username escapes may turn an authority into a path;
            // inspect every representation, not only the final fixed point.
            let authority = result.replacingOccurrences(of: "\\", with: "/")
            guard !hasControls(result), !Rx.matches(browserUserinfo, in: authority) else { return nil }
            let matches = percentEscapes.matches(in: result, range: NSRange(result.startIndex..., in: result))
            if matches.isEmpty { return result }
            for match in matches.reversed() {
                guard let range = Range(match.range, in: result),
                      let replacement = String(result[range]).removingPercentEncoding else { return nil }
                result.replaceSubrange(range, with: replacement)
            }
        }
    }

    private static func hasSecretParameters(_ encoded: String?) -> Bool {
        guard let encoded else { return false }
        guard let text = decoded(encoded), !hasControls(text) else { return true }
        let exact: Set<String> = ["auth", "key", "code", "apikey", "session", "sessionid", "bearer",
                                  "passwd", "pwd", "pass", "ticket", "oauthcode", "authcode",
                                  "authorizationcode", "csrf", "accesskey", "privatekey",
                                  "signature", "sig", "jwt", "samlresponse"]
        for field in text.split(whereSeparator: { $0 == "&" || $0 == ";" || $0 == "?" || $0 == "#" }) {
            // A redirect value may itself be a query string without a '?' —
            // next=token%3Dsecret. Inspect each assignment's name, not its value.
            let assignments = field.split(separator: "=", omittingEmptySubsequences: false)
            let names = assignments.count > 1 ? assignments.dropLast() : assignments[...]
            for candidate in names {
                let name = candidate.lowercased().filter { $0.isLetter || $0.isNumber }
                if exact.contains(name) || ["password", "token", "secret", "credential", "authorization", "signature"].contains(where: { name.contains($0) }) ||
                    ["apikey", "session", "sessionid", "cookie", "passwd"].contains(where: { name.hasSuffix($0) }) {
                    return true
                }
            }
        }
        return false
    }

    // A code-server proxy path: `/proxy/<port>/…`. Its presence means the
    // whole origin sits behind code-server's `--auth password` gate, so the
    // app must replay the stored password. A bare `host:port` URL (no proxy
    // subpath) is a directly-exposed Tofu with no such gate.
    private static let proxyPath = #"/(?:absproxy|proxy)/\d+(/|$)"#

    /// Sensible default auth for a freshly-typed server URL: proxy URL →
    /// code-server password, anything else → none. Pure and deterministic.
    public static func defaultAuthType(_ rawUrl: String) -> AuthType {
        guard let url = parse(rawUrl) else { return .none }
        return Rx.matches(proxyPath, in: url.components.percentEncodedPath) ? .codeServerPassword : .none
    }

    /// A short, human-scannable label for a server URL. Compresses
    /// `<uuid>-vscode-<idc>.<domain>/proxy/<port>/` to `abc12345 · dc1 : 15000`
    /// — the three fields that actually differ between a user's sandboxes.
    /// Non-MLP hosts fall back to `host:port`; rejected input is never echoed.
    public static func displayLabel(_ rawUrl: String) -> String {
        guard let url = parse(rawUrl) else { return "Invalid server URL" }
        let port = Rx.group(#"/(?:absproxy|proxy)/(\d+)"#, in: url.components.path)
        if let match = mlpHost(url.host) {
            var label = "\(match.uuidHead) · \(match.idc)"
            if let port { label += " : \(port)" }
            return label
        }
        var hostPort = url.host.contains(":") ? "[\(url.host)]" : url.host
        if let p = url.components.port, p != defaultPort(for: url.scheme) {
            hostPort += ":\(p)"
        }
        return port.map { "\(hostPort) : \($0)" } ?? hostPort
    }

    private static func mlpHost(_ host: String) -> (uuidHead: String, idc: String)? {
        guard let uuid = Rx.group(#"^([0-9a-fA-F-]{8,})-vscode-([^.]+)\."#, in: host, index: 1),
              let idc = Rx.group(#"^([0-9a-fA-F-]{8,})-vscode-([^.]+)\."#, in: host, index: 2) else {
            return nil
        }
        return (String(uuid.prefix(8)), idc)
    }

    private static func defaultPort(for scheme: String) -> Int {
        scheme == "https" ? 443 : 80
    }
}
