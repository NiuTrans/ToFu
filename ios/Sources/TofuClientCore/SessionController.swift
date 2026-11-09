import Foundation

/// Orchestrates every profile mutation the UI triggers, over the same seams
/// ``SessionManager`` uses (so it is unit-testable with fakes). Keeps the
/// SwiftUI layer thin: the UI validates via ``ProfileForm``, then calls one of
/// these. Port of SessionController.kt.
///
/// Ordering invariants that matter for correctness:
///  - ADD: store the secret BEFORE login (login reads it via the vault).
///  - EDIT with a URL origin change: delegate to
///    ``SessionManager/updateUrlAndReauth`` so the dead host's Domain-pinned
///    jar is purged before re-login.
///  - DELETE: remove the secret AND the row (never orphan a credential).
///  - EDIT/RENAME: reconcile alias-keyed secrets only after the intended row
///    is committed, before login, even when cancellation arrives in the write.
public final class SessionController: Sendable {

    public enum AddResult: Sendable {
        case added(profile: Profile, login: LoginResult)
        case duplicateAlias
        case invalidInput(message: String)
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

    /// Reuse a password only within the same HTTP origin and code-server
    /// prefix. Sibling proxy ports may share a gateway; separate deployments
    /// on another scheme, network port or prefix do not share credentials.
    public func findSharedSecret(_ baseUrl: String, excludeAlias: String? = nil) async -> String? {
        guard !Task.isCancelled, let server = ServerUrl.parse(baseUrl) else { return nil }
        let profiles = await store.getAllOnce()
        guard !Task.isCancelled else { return nil }
        for profile in profiles {
            guard !Task.isCancelled else { return nil }
            if profile.alias == excludeAlias { continue }
            guard let candidate = ServerUrl.parse(profile.baseUrl), candidate.origin == server.origin,
                  candidate.codeServerRootPath == server.codeServerRootPath, profile.authType == .codeServerPassword else { continue }
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
        guard !a.isEmpty, ServerUrl.parse(baseUrl) != nil else {
            return .invalidInput(message: "Use a named, credential-free http(s) server URL")
        }
        guard !Task.isCancelled else { return .invalidInput(message: "Save cancelled") }
        if await store.getByAlias(a) != nil { return .duplicateAlias }
        guard !Task.isCancelled else { return .invalidInput(message: "Save cancelled") }
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
            guard !Task.isCancelled else { return .invalidInput(message: "Save cancelled") }
            if !effective.isEmpty { secrets.putSecret(effective, for: a) }
        }
        let id = await store.insert(profile)
        var saved = profile
        saved.id = id
        // Nonthrowing persistence may finish after Cancel. Keep the committed
        // row/vault coherent, but never start a new sign-in for that intent.
        guard !Task.isCancelled else {
            return .added(profile: saved, login: .error(message: "Save cancelled after persistence"))
        }
        let result = await session.login(saved)
        return .added(profile: saved, login: result)
    }

    /// Edit an existing profile. Handles four cases in one entry point:
    ///  - rename (alias change) → move the secret key,
    ///  - new secret provided → overwrite it,
    ///  - URL origin change → purge + re-login (via updateUrlAndReauth),
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
        guard !a.isEmpty, let newServer = ServerUrl.parse(newUrl) else {
            return ProfileResult(login: .error(message: "Use a named, credential-free http(s) server URL"), persisted: current)
        }
        guard !Task.isCancelled else {
            return ProfileResult(login: .error(message: "Save cancelled"), persisted: current)
        }
        if let owner = await store.getByAlias(a), owner.id != current.id {
            return ProfileResult(login: .error(message: "A server with this name already exists"), persisted: current)
        }
        guard !Task.isCancelled else {
            return ProfileResult(login: .error(message: "Save cancelled"), persisted: current)
        }
        let oldSecret = secrets.secretFor(current.alias)
        let targetSecret = a == current.alias ? oldSecret : secrets.secretFor(a)
        let existingSecret = oldSecret ?? targetSecret
        let shared = newAuthType == .codeServerPassword && newSecret.isEmpty && (existingSecret ?? "").isEmpty
            ? await findSharedSecret(newServer.raw, excludeAlias: a) : nil
        guard !Task.isCancelled else {
            return ProfileResult(login: .error(message: "Save cancelled"), persisted: current)
        }
        let ppRaw = newProjectPath?.trimmingCharacters(in: .whitespacesAndNewlines)
        let pp = (ppRaw?.isEmpty == false) ? ppRaw : nil

        // Prepare, but do not mutate the vault before the row commits. Blank
        // keeps this profile's credential or reuses a same-origin password.
        let effectiveSecret = !newSecret.isEmpty ? newSecret
            : ((existingSecret ?? "").isEmpty ? (shared ?? existingSecret) : existingSecret)
        let reconcile: @Sendable (Profile) -> Bool = { [secrets] _ in
            secrets.applyProfileEdit(from: current.alias, to: a,
                expectedOldSecret: oldSecret, expectedNewSecret: targetSecret, secret: effectiveSecret)
        }

        // Legacy sanitization is an explicit edit projection, never a saved
        // auth migration. Retain the old safe authority for cookie purging.
        let oldUrl = ServerUrl.sanitizeForStorage(current.baseUrl)
        let oldOrigin = oldUrl.flatMap { ServerUrl.parse($0)?.origin }
        let newOrigin = newServer.origin
        var base = current
        if let oldUrl { base.baseUrl = oldUrl }
        base.alias = a
        base.authType = newAuthType
        base.projectPath = pp

        if let oldOrigin, oldOrigin != newOrigin {
            // URL origin changed → the purge-and-relogin path owns persistence,
            // so it also owns what the persisted row looks like.
            let reauth = await session.updateUrlAndReauth(base, newUrl: newServer.raw, onPersisted: reconcile)
            return ProfileResult(login: reauth.login, persisted: reauth.persisted)
        }
        let updated = ProfileForm.toProfile(
            id: current.id, alias: a, baseUrl: newServer.raw, authType: newAuthType,
            lastUsedAt: current.lastUsedAt, projectPath: pp
        )
        let saved = await session.persistEditAndLogin(updated, previous: current, onPersisted: reconcile)
        return ProfileResult(login: saved.login, persisted: saved.persisted)
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
        guard !Task.isCancelled else {
            return ProfileResult(login: .error(message: "Open cancelled"), persisted: profile)
        }
        guard await store.getById(profile.id) != nil else {
            return ProfileResult(login: .error(message: "This server was deleted. Return to the list."), persisted: profile)
        }
        guard !Task.isCancelled else {
            return ProfileResult(login: .error(message: "Open cancelled"), persisted: profile)
        }
        await store.touchLastUsed(profile.id, clock())
        guard let current = await store.getById(profile.id) else {
            return ProfileResult(login: .error(message: "This server was deleted. Return to the list."), persisted: profile)
        }
        guard !Task.isCancelled else {
            return ProfileResult(login: .error(message: "Open cancelled"), persisted: current)
        }
        return ProfileResult(login: await session.login(current), persisted: current)
    }

    /// Delete a profile and its stored secret (never orphan a credential).
    public func deleteProfile(_ profile: Profile) async {
        guard !Task.isCancelled else { return }
        secrets.removeSecret(profile.alias)
        await store.deleteById(profile.id)
    }
}
