import Foundation

/// Native bridge admission: only the configured HTTP origin's main frame.
/// WebKit passes its securityOrigin tuple, never a page-supplied origin string.
public enum TrustedWebBridge {
    public static func allows(serverURL: String, scheme: String, host: String, port: Int, isMainFrame: Bool) -> Bool {
        guard isMainFrame, let server = ServerUrl.parse(serverURL) else { return false }
        let scheme = scheme.lowercased()
        let actualPort = port == 0 ? (scheme == "https" ? 443 : 80) : port
        return scheme == server.scheme && host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]")) == server.host &&
            actualPort == (server.components.port ?? (server.scheme == "https" ? 443 : 80))
    }

    public static func allowsURL(_ url: URL?, serverURL: String, isMainFrame: Bool) -> Bool {
        guard let url, let parsed = ServerUrl.parseNavigation(url.absoluteString) else { return false }
        return allows(serverURL: serverURL, scheme: parsed.scheme, host: parsed.host,
                      port: parsed.components.port ?? 0, isMainFrame: isMainFrame)
    }
}
