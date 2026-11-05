import Foundation

public struct ReauthResult: Sendable {
    public let login: LoginResult
    public let persisted: Profile
    public init(login: LoginResult, persisted: Profile) { self.login = login; self.persisted = persisted }
}

/// Session orchestration over explicit gateway, probe, repository and cookie
/// owners. Passwords never leave trusted discovery; stale work cannot stamp a
/// changed/deleted profile. Public results remain nonthrowing for UI callers.
public final class SessionManager: Sendable {
    public static let sessionCookieName = "code-server-session"
    private let store: ProfileStore
    private let secrets: SecretLookup
    private let cookies: CookieSink
    private let transport: GatewayHTTP
    private let sleeper: @Sendable (Int64) async throws -> Void

    public init(store: ProfileStore, secrets: SecretLookup, cookies: CookieSink, http: HTTPClient,
                sleeper: @escaping @Sendable (Int64) async throws -> Void = { ms in
                    try await Task.sleep(nanoseconds: UInt64(max(ms, 0)) * 1_000_000)
                }) {
        self.store = store; self.secrets = secrets; self.cookies = cookies
        self.transport = GatewayHTTP(http: http, cookies: cookies); self.sleeper = sleeper
    }

    public func login(_ profile: Profile) async -> LoginResult {
        do { return try await establish(profile) }
        catch { return .error(message: GatewayHTTP.isCancellation(error) ? "Sign-in cancelled" : error.localizedDescription) }
    }

    private func establish(_ profile: Profile) async throws -> LoginResult {
        try Task.checkCancellation()
        guard let server = ServerUrl.parse(profile.baseUrl) else { return .error(message: "Invalid server URL") }
        if profile.authType == .none { return .success(host: server.host) }
        if let header = await cookies.cookieHeader(GatewaySessionProbe.url(server)), !header.isEmpty,
           try await GatewaySessionProbe(transport: transport).verify(profile) {
            return try await complete(profile, server: server)
        }
        guard profile.authType == .codeServerPassword, let secret = secrets.secretFor(profile.alias), !secret.isEmpty else {
            return interactive(profile)
        }
        var previous: AttemptKind = .definitive
        var attempt = 0
        repeat {
            attempt += 1
            try Task.checkCancellation()
            if attempt > 1 { try await sleeper(LoginRetryPolicy.backoffMs(attempt - 1, warming: previous == .warming)) }
            try Task.checkCancellation()
            let outcome = try await attemptLogin(profile, server: server, secret: secret)
            previous = outcome.kind
            if previous == .definitive || attempt >= LoginRetryPolicy.maxAttempts(warming: previous == .warming) { return outcome.result }
        } while true
    }

    private func attemptLogin(_ profile: Profile, server: ServerUrl, secret: String) async throws -> Attempt {
        do {
            let gateway = GatewayLogin(transport: transport)
            switch try await gateway.discover(server) {
            case .interactive: return Attempt(interactive(profile))
            case .warming(let status): return warming(status)
            case .rateLimited: return rateLimited()
            case .unrecognized:
                let verified = try await GatewaySessionProbe(transport: transport).verify(profile)
                return Attempt(verified ? try await complete(profile, server: server) : interactive(profile))
            case .form(let form):
                try Task.checkCancellation()
                guard let current = await store.getById(profile.id), current.baseUrl == profile.baseUrl,
                      current.alias == profile.alias, current.authType == profile.authType,
                      secrets.secretFor(current.alias) == secret else { return Attempt(interactive(profile)) }
                switch try await gateway.submit(form, secret: secret, server: server) {
                case .accepted: return Attempt(try await complete(profile, server: server))
                case .rejected: return Attempt(.badCredentials)
                case .interactive: return Attempt(interactive(profile))
                case .warming(let status): return warming(status)
                case .rateLimited: return rateLimited()
                }
            }
        } catch {
            if GatewayHTTP.isCancellation(error) { throw CancellationError() }
            if LoginRetryPolicy.isWarmingTransport(error) {
                return Attempt(.error(message: LoginRetryPolicy.warmingMessage(error.localizedDescription)), .warming)
            }
            return Attempt(.error(message: error.localizedDescription), .transient)
        }
    }

    private func complete(_ profile: Profile, server: ServerUrl) async throws -> LoginResult {
        try Task.checkCancellation()
        guard await store.setCookieHostIfCurrent(profile, server.host) else { return interactive(profile) }
        try Task.checkCancellation()
        do {
            let response = try await transport.send(server.endpoint(ApiV4Contract.metaPath))
            if let reason = ApiMetaGate.incompatibilityReason(status: response.status, body: response.body) { return .incompatible(message: reason) }
        } catch {
            if GatewayHTTP.isCancellation(error) { throw CancellationError() }
        }
        return .success(host: server.host)
    }

    @discardableResult
    public func noteInteractiveSignIn(_ profile: Profile, finishedUrl: String) async -> Bool {
        do {
            try Task.checkCancellation()
            guard let server = ServerUrl.parse(profile.baseUrl) else { return false }
            let header = await cookies.cookieHeader(GatewaySessionProbe.url(server))
            guard InteractiveSso.isCompletionCandidate(profile: profile, finishedUrl: finishedUrl, cookieHeader: header),
                  try await GatewaySessionProbe(transport: transport).verify(profile),
                  let current = await store.getById(profile.id), current.baseUrl == profile.baseUrl,
                  current.alias == profile.alias, current.authType != .none else { return false }
            try Task.checkCancellation()
            return await store.setCookieHostIfCurrent(current, server.host)
        } catch { return false }
    }

    /// Resume the stored password policy only after the browser returns to the
    /// configured code-server login. Browser HTML never supplies the POST target.
    public func resumePasswordSignIn(_ profile: Profile, finishedUrl: String) async -> LoginResult? {
        guard let server = ServerUrl.parse(profile.baseUrl), server.isLoginURL(finishedUrl),
              let current = await store.getById(profile.id), current.baseUrl == profile.baseUrl,
              current.authType == .codeServerPassword, !(secrets.secretFor(current.alias) ?? "").isEmpty else { return nil }
        return await login(current)
    }

    public func updateUrlAndReauth(_ profile: Profile, newUrl: String) async -> ReauthResult {
        guard let newServer = ServerUrl.parse(newUrl), !Task.isCancelled else {
            return ReauthResult(login: .error(message: "Invalid server URL or cancelled edit"), persisted: profile)
        }
        if let old = ServerUrl.parse(profile.baseUrl), old.origin != newServer.origin {
            await cookies.purgeOrigin(old.origin, appURL: profile.baseUrl)
        }
        guard !Task.isCancelled else { return ReauthResult(login: .error(message: "Edit cancelled"), persisted: profile) }
        var updated = profile
        updated.baseUrl = newUrl; updated.instanceUuid = newServer.instanceUuid ?? profile.instanceUuid; updated.cookieHost = nil
        await store.update(updated)
        return ReauthResult(login: await login(updated), persisted: updated)
    }

    private func interactive(_ profile: Profile) -> LoginResult { .needsInteractiveSso(url: profile.baseUrl) }
    private func warming(_ status: Int) -> Attempt {
        Attempt(.error(message: LoginRetryPolicy.warmingMessage("the proxy answered HTTP \(status)")), .warming)
    }
    private func rateLimited() -> Attempt {
        Attempt(.error(message: "The sign-in gateway is limiting attempts (HTTP 429). Wait before trying again."))
    }
    private enum AttemptKind { case definitive, transient, warming }
    private struct Attempt {
        let result: LoginResult; let kind: AttemptKind
        init(_ result: LoginResult, _ kind: AttemptKind = .definitive) { self.result = result; self.kind = kind }
    }
}
