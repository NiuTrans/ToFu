import Foundation

/// URL authority shared by gateway discovery, cookies and supervisor routing.
/// Entry points: parse, sameOrigin, endpoint. Dependencies: Foundation only.
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
        Self.parse(url.absoluteString)?.origin == origin
    }

    public func isLoginURL(_ target: String) -> Bool {
        guard let landed = Self.parse(target), landed.origin == origin else { return false }
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

    /// Parse, returning nil unless the string is an absolute http(s) URL —
    /// same contract as okhttp's `toHttpUrlOrNull`.
    public static func parse(_ raw: String) -> ServerUrl? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let components = URLComponents(string: trimmed),
              let scheme = components.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = components.host, !host.isEmpty,
              components.user == nil, components.password == nil,
              components.port.map({ (1...65535).contains($0) }) ?? true,
              components.url != nil else {
            return nil
        }
        return ServerUrl(raw: trimmed, components: components)
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
    /// Non-MLP hosts fall back to `host:port/path`; unparseable input is
    /// returned as typed.
    public static func displayLabel(_ rawUrl: String) -> String {
        guard let url = parse(rawUrl) else {
            return rawUrl.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        let port = Rx.group(#"/(?:absproxy|proxy)/(\d+)"#, in: url.components.path)
        if let match = mlpHost(url.host) {
            var label = "\(match.uuidHead) · \(match.idc)"
            if let port { label += " : \(port)" }
            return label
        }
        var hostPort = url.host
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
