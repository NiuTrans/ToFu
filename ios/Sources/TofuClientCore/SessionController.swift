import Foundation

/// Orchestrates every profile mutation the UI triggers, over the same seams
/// ``SessionManager`` uses (so it is unit-testable with fakes). Keeps the
/// SwiftUI layer thin: the UI validates via ``ProfileForm``, then calls one of
/// these. Port of SessionController.kt.
///
/// Ordering invariants that matter for correctness:
///  - ADD: store the secret BEFORE login (login reads it via the vault).
///  - EDIT with a URL host change: delegate to
///    ``SessionManager/updateUrlAndReauth`` so the dead host's Domain-pinned
///    jar is purged before re-login.
///  - DELETE: remove the secret AND the row (never orphan a credential).
///  - RENAME: the secret is alias-keyed, so a rename must MOVE the secret to
///    the new alias key or login would silently lose the credential.
public final class SessionController: Sendable {

    public enum AddResult: Sendable {
        case added(profile: Profile, login: LoginResult)
        case duplicateAlias
    }

    /// The login outcome plus the row as it was actually written to / read
    /// from the store. The UI navigates with `persisted`, never with a locally
    /// reconstructed or list-rendered copy.
    public struct ProfileResult: Sendable {
        public let login: LoginResult
        public let persisted: Profile

        public init(login: LoginResult, persisted: Profile) {
            self.login = login
            self.persisted = persisted
        }
    }

    private let store: ProfileStore
    private let secrets: SecretStore
    private let session: SessionManager
    private let clock: @Sendable () -> Int64

    public init(
        store: ProfileStore,
        secrets: SecretStore,
        session: SessionManager,
        clock: @escaping @Sendable () -> Int64 = { currentTimeMs() }
    ) {
        self.store = store
        self.secrets = secrets
        self.session = session
        self.clock = clock
    }

    /// Find a password already stored for ANOTHER profile on the SAME host as
    /// [baseUrl]. code-server auth is per-HOST (its session cookie is
    /// `Domain`-pinned to the host), so different `/proxy/PORT/` URLs on one
    /// host share one password. Returns the reusable secret, or nil if no
    /// same-host profile has one. [excludeAlias] skips the profile itself.
    public func findSharedSecret(_ baseUrl: String, excludeAlias: String? = nil) async -> String? {
        guard let host = ServerUrl.parse(baseUrl)?.host else { return nil }
        for profile in await store.getAllOnce() {
            if profile.alias == excludeAlias { continue }
            if ServerUrl.parse(profile.baseUrl)?.host != host { continue }
            if let secret = secrets.secretFor(profile.alias), !secret.isEmpty { return secret }
        }
        return nil
    }

    /// Add a new server, store its secret, then attempt the first login.
    public func addProfile(
        alias: String,
        baseUrl: String,
        authType: AuthType,
        secret: String,
        projectPath: String? = nil
    ) async -> AddResult {
        let a = alias.trimmingCharacters(in: .whitespacesAndNewlines)
        if await store.getByAlias(a) != nil { return .duplicateAlias }
        let profile = ProfileForm.toProfile(
            id: 0, alias: a, baseUrl: baseUrl, authType: authType,
            lastUsedAt: clock(), projectPath: projectPath
        )
        // Store the secret FIRST — login reads it via the vault. When the
        // field is left blank, REUSE a password already stored for the same
        // host (shared per-host code-server auth), so the user needn't
        // re-type it.
        if authType == .codeServerPassword {
            let effective = secret.isEmpty
                ? (await findSharedSecret(baseUrl, excludeAlias: a) ?? "")
                : secret
            if !effective.isEmpty { secrets.putSecret(effective, for: a) }
        }
        let id = await store.insert(profile)
        var saved = profile
        saved.id = id
        let result = await session.login(saved)
        return .added(profile: saved, login: result)
    }

    /// Edit an existing profile. Handles four cases in one entry point:
    ///  - rename (alias change) → move the secret key,
    ///  - new secret provided → overwrite it,
    ///  - URL host change → purge + re-login (via updateUrlAndReauth),
    ///  - plain field change → persist + re-login.
    ///
    /// Returns BOTH the login outcome and the row as actually PERSISTED. The
    /// caller must not reconstruct the edited profile itself: the two branches
    /// write different rows (the host-change path nils `cookieHost` and may
    /// refresh `instanceUuid`).
    public func editProfile(
        current: Profile,
        newAlias: String,
        newUrl: String,
        newAuthType: AuthType,
        newSecret: String,
        newProjectPath: String? = nil
    ) async -> ProfileResult {
        let a = newAlias.trimmingCharacters(in: .whitespacesAndNewlines)
        let ppRaw = newProjectPath?.trimmingCharacters(in: .whitespacesAndNewlines)
        let pp = (ppRaw?.isEmpty == false) ? ppRaw : nil

        // Rename: move the alias-keyed secret so the credential isn't orphaned.
        if a != current.alias, let existing = secrets.secretFor(current.alias) {
            secrets.putSecret(existing, for: a)
            secrets.removeSecret(current.alias)
        }
        // New secret overwrites; blank keeps the existing one. If blank AND
        // this profile has no secret of its own yet, reuse a same-host
        // password (shared per-host code-server auth).
        if !newSecret.isEmpty {
            secrets.putSecret(newSecret, for: a)
        } else if newAuthType == .codeServerPassword,
                  (secrets.secretFor(a) ?? "").isEmpty,
                  let shared = await findSharedSecret(newUrl, excludeAlias: a) {
            secrets.putSecret(shared, for: a)
        }

        let oldHost = ServerUrl.parse(current.baseUrl)?.host
        let newHost = ServerUrl.parse(newUrl)?.host
        var base = current
        base.alias = a
        base.authType = newAuthType
        base.projectPath = pp

        if let oldHost, let newHost, oldHost != newHost {
            // URL host changed → the purge-and-relogin path owns persistence,
            // so it also owns what the persisted row looks like.
            let reauth = await session.updateUrlAndReauth(base, newUrl: newUrl)
            return ProfileResult(login: reauth.login, persisted: reauth.persisted)
        }
        let updated = ProfileForm.toProfile(
            id: current.id, alias: a, baseUrl: newUrl, authType: newAuthType,
            lastUsedAt: current.lastUsedAt, projectPath: pp
        )
        await store.update(updated)
        return ProfileResult(login: await session.login(updated), persisted: updated)
    }

    /// Activate a profile (make it current): bump recency, then log in.
    ///
    /// [profile] comes from the rendered list, which lags any write not yet
    /// re-emitted. Two consequences, both fixed here:
    ///  - the recency bump is a TARGETED write, so it cannot reinstate stale
    ///    columns;
    ///  - the row is RE-READ before login, so the handshake and every
    ///    downstream decision see the store's truth rather than what the
    ///    screen happened to be showing.
    ///
    /// Returns the row actually used, so the caller navigates with that.
    public func activate(_ profile: Profile) async -> ProfileResult {
        await store.touchLastUsed(profile.id, clock())
        let current = await store.getById(profile.id) ?? profile
        return ProfileResult(login: await session.login(current), persisted: current)
    }

    /// One-time upgrade migration (idempotent, safe to run every launch): fix
    /// any persisted profile whose URL is a code-server proxy form but whose
    /// stored authType is the stale NONE default (see
    /// ``ServerUrl/needsProxyAuthFix``) — flip it so it can headless-login
    /// instead of being stranded on the code-server login page. Returns the
    /// count of rows fixed. Targeted writes only; bare-host NONE and any
    /// non-NONE auth are left as-is.
    @discardableResult
    public func migrateProxyAuthDefaults() async -> Int {
        var fixed = 0
        let all = await store.getAllOnce()
        for profile in all
        where ServerUrl.needsProxyAuthFix(rawUrl: profile.baseUrl, current: profile.authType) {
            await store.setAuthType(profile.id, .codeServerPassword)
            fixed += 1
        }
        return fixed
    }

    /// Delete a profile and its stored secret (never orphan a credential).
    public func deleteProfile(_ profile: Profile) async {
        secrets.removeSecret(profile.alias)
        await store.deleteById(profile.id)
    }
}
