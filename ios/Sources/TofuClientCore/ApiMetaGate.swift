import Foundation

/// The GET /api/v4/meta preflight, as pure decisions. Port of ApiMetaGate.kt.
///
/// After a headless login establishes the gateway session, the app asks the
/// server which API major it requires and refuses to enter the WebView on a
/// DEFINITIVE mismatch. Fail-OPEN on partial knowledge: a 404 (a server that
/// predates the meta endpoint), a transport failure, or an unparseable body
/// never blocks — only a 200 carrying a contradicting apiMajor blocks.
/// (iOS has no min-build gate yet; the contract's minAndroidBuild applies to
/// Android only. The apiMajor check is the cross-platform one.)
public enum ApiMetaGate {

    /// Absolute meta URL under the profile's base path. Resolving the absolute
    /// META_PATH against the origin would DROP the `/proxy/<port>/` prefix of
    /// a vscode code-server deploy, so the path is appended to the base as typed.
    public static func metaUrl(_ baseUrl: String) -> String {
        var base = baseUrl
        while base.hasSuffix("/") { base.removeLast() }
        return base + "/" + ApiV4Contract.metaPath.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }

    /// nil = compatible or unknown (never blocks on partial knowledge).
    /// Non-nil = the user-facing reason this build refuses to proceed.
    public static func incompatibilityReason(status: Int, body: String?) -> String? {
        guard status == 200, let body, !body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return nil
        }
        guard let apiMajor = intField(body, "apiMajor") else { return nil }
        if apiMajor != ApiV4Contract.apiMajor {
            return "server speaks API v\(apiMajor) but this app requires " +
                "v\(ApiV4Contract.apiMajor) — update the app"
        }
        return nil
    }

    private static func intField(_ body: String, _ field: String) -> Int? {
        Rx.group(#""\#(field)"\s*:\s*(\d+)"#, in: body).flatMap(Int.init)
    }
}
