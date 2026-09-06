import Foundation

/// Parsed view of a server base URL, e.g.
/// `https://<uuid>-vscode-dc1.codelab.example.com/proxy/15000/`.
///
/// Port of ServerUrl.kt. Two spike-established facts this encodes:
///  - the session cookie is `Domain`-pinned to the FULL host, so ``host`` is
///    the identity a cached jar is bound to;
///  - the code-server login lives at the code-server ROOT (`…/login`), which
///    for a `/proxy/PORT/` deploy is the origin root, NOT under the proxy
///    subpath. ``loginUrl`` therefore posts to `<scheme>://<host>/login`.
public struct ServerUrl: Equatable, Sendable {
    public let raw: String
    public let components: URLComponents

    /// Lowercased, matching okhttp's host normalisation (cookies are
    /// Domain-pinned case-insensitively).
    public var host: String { (components.host ?? "").lowercased() }

    public var scheme: String { (components.scheme ?? "https").lowercased() }

    /// Origin root, e.g. `https://<host>` — deliberately WITHOUT the port,
    /// mirroring the Kotlin original (the cookie jar keys on host alone).
    public var origin: String { "\(scheme)://\(host)" }

    /// code-server login endpoint (origin root + /login).
    public var loginUrl: String { "\(origin)/login" }

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
              let host = components.host, !host.isEmpty else {
            return nil
        }
        return ServerUrl(raw: raw, components: components)
    }

    // A code-server proxy path: `/proxy/<port>/…`. Its presence means the
    // whole origin sits behind code-server's `--auth password` gate, so the
    // app must replay the stored password. A bare `host:port` URL (no proxy
    // subpath) is a directly-exposed Tofu with no such gate.
    private static let proxyPath = #"/proxy/\d+(/|$)"#

    /// Sensible default auth for a freshly-typed server URL: proxy URL →
    /// code-server password, anything else → none. Pure and deterministic.
    public static func defaultAuthType(_ rawUrl: String) -> AuthType {
        let trimmed = rawUrl.trimmingCharacters(in: .whitespacesAndNewlines)
        return Rx.matches(proxyPath, in: trimmed) ? .codeServerPassword : .none
    }

    /// Upgrade-migration predicate: a persisted profile whose URL is a
    /// code-server proxy form but whose stored auth is the stale NONE default
    /// can never headless-login — flip it. Idempotent (after the flip,
    /// current != .none → false).
    public static func needsProxyAuthFix(rawUrl: String, current: AuthType) -> Bool {
        current == .none && defaultAuthType(rawUrl) == .codeServerPassword
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
        let port = Rx.group(#"/proxy/(\d+)"#, in: url.components.path)
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
