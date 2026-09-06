import Foundation

/// Auth mechanism used to obtain a session for a server.
/// Raw values match the Android enum names so persisted data stays
/// cross-platform readable.
public enum AuthType: String, Codable, CaseIterable, Sendable {
    /// code-server `--auth password`: headless POST /login is replayable.
    case codeServerPassword = "CODE_SERVER_PASSWORD"
    /// Layer-1 interactive SSO: WebView completes login once, we persist the jar.
    case interactiveSso = "INTERACTIVE_SSO"
    /// No auth (bare Tofu / trusted network).
    case none = "NONE"
}

/// A remembered server — the port of Android's Room `Profile` entity.
///
/// The [alias] is the STABLE logical identity and the switcher key. The secret
/// (Keychain, see ``KeychainSecretStore``) is keyed by [alias], NOT by
/// [baseUrl] — so when a sandbox is re-provisioned the user edits [baseUrl] in
/// one tap and the saved credential is reused.
///
/// [cookieHost] records the exact host the current cached session cookie is
/// `Domain`-pinned to. On a [baseUrl] change we compare against it: a mismatch
/// means the cached jar is bound to a dead host and MUST be hard-invalidated.
public struct Profile: Codable, Equatable, Identifiable, Sendable {
    public var id: Int64
    public var alias: String
    public var instanceUuid: String?
    public var baseUrl: String
    public var authType: AuthType
    public var cookieHost: String?
    /// Epoch milliseconds; matches Android's `System.currentTimeMillis()` stamps.
    public var lastUsedAt: Int64
    /// Absolute host path of the Tofu project this server runs from, used by
    /// the supervisor to start/stop it remotely. Nil → open-only profile.
    public var projectPath: String?

    public init(
        id: Int64 = 0,
        alias: String,
        instanceUuid: String? = nil,
        baseUrl: String,
        authType: AuthType = .none,
        cookieHost: String? = nil,
        lastUsedAt: Int64 = 0,
        projectPath: String? = nil
    ) {
        self.id = id
        self.alias = alias
        self.instanceUuid = instanceUuid
        self.baseUrl = baseUrl
        self.authType = authType
        self.cookieHost = cookieHost
        self.lastUsedAt = lastUsedAt
        self.projectPath = projectPath
    }

    /// Snake-case keys mirror the Room column names (`instance_uuid`,
    /// `base_url`, …) so a future shared/sync'd store reads both platforms.
    enum CodingKeys: String, CodingKey {
        case id, alias
        case instanceUuid = "instance_uuid"
        case baseUrl = "base_url"
        case authType = "auth_type"
        case cookieHost = "cookie_host"
        case lastUsedAt = "last_used_at"
        case projectPath = "project_path"
    }
}
