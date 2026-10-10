import Foundation

/// The outcome of ``SessionManager/updateUrlAndReauth``: the login result plus
/// the row as actually written. Callers navigate with `persisted` rather than
/// a locally-rebuilt copy, which would miss the `cookieHost = nil` invalidation.
public struct ReauthResult: Sendable {
    public let login: LoginResult
    public let persisted: Profile

    public init(login: LoginResult, persisted: Profile) {
        self.login = login
        self.persisted = persisted
    }
}

/// Owns the credential-replay lifecycle proven in the feasibility spike:
///
///   POST <origin>/login (password, base=.)  →  302 + Set-Cookie
///     →  inject into the WebView jar (with Max-Age upgrade)  →  load baseUrl.
///
/// And the re-provision path: when a profile's URL host changes, purge the
/// dead host's jar BEFORE the new login (the cookie is Domain-pinned to the
/// host). Port of SessionManager.kt.
public final class SessionManager: Sendable {

    public static let sessionCookieName = "code-server-session"

    private let store: ProfileStore
    private let secrets: SecretLookup
    private let cookies: CookieSink
    private let http: HTTPClient
    private let sleeper: @Sendable (Int64) async -> Void

    public init(
        store: ProfileStore,
        secrets: SecretLookup,
        cookies: CookieSink,
        http: HTTPClient,
        sleeper: @escaping @Sendable (Int64) async -> Void = { ms in
            try? await Task.sleep(nanoseconds: UInt64(max(ms, 0)) * 1_000_000)
        }
    ) {
        self.store = store
        self.secrets = secrets
        self.cookies = cookies
        self.http = http
        self.sleeper = sleeper
    }

    /// Establish a session for [profile]. The login POST does NOT follow the
    /// 302 (we only need the Set-Cookie).
    public func login(_ profile: Profile) async -> LoginResult {
        guard let server = ServerUrl.parse(profile.baseUrl) else {
            return .error(message: "Invalid URL: \(profile.baseUrl)")
        }

        switch profile.authType {
        case .none:
            // No outer gateway to unlock: the WebView reaches Tofu directly,
            // so the SPA's own 426/upgrade handling surfaces a version
            // mismatch in-page. A headless preflight here would only add a
            // tunnel round-trip (and double the dead-server wait) for a
            // verdict the WebView gives anyway — stay zero-request.
            return .success(host: server.host)
        case .interactiveSso:
            // Layer-1 SSO can't be replayed headlessly; hand off to the WebView.
            return .needsInteractiveSso(url: profile.baseUrl)
        case .codeServerPassword:
            break
        }

        guard let secret = secrets.secretFor(profile.alias) else {
            return .noCredential
        }

        // The login POST rides the outer tunnel (vscode proxy / VPN); a
        // transient reset there surfaces as a transport error, not an HTTP
        // status, so bounded retry with backoff is the difference between
        // "tap Open twice on a flaky tunnel" and "it just works". WARMING
        // outcomes (the proxy's 502/503/504, or a connect timeout — the
        // sandbox behind the tunnel is still booting) get a longer ladder:
        // a cold sandbox routinely takes tens of seconds to serve, and
        // bouncing the user back to the list after 3.5s just makes them tap
        // Open again by hand. Definitive answers (badCredentials /
        // needsInteractiveSso / success) never retry.
        var attempt = 0
        var previousKind: AttemptKind = .definitive
        var result: LoginResult
        repeat {
            attempt += 1
            if attempt > 1 {
                await sleeper(LoginRetryPolicy.backoffMs(attempt - 1, warming: previousKind == .warming))
            }
            let outcome = await attemptLogin(profile: profile, server: server, secret: secret)
            previousKind = outcome.kind
            result = outcome.result
        } while previousKind != .definitive &&
            attempt < LoginRetryPolicy.maxAttempts(warming: previousKind == .warming)
        return await withApiPreflight(server: server, baseUrl: profile.baseUrl, result: result)
    }

    /// One login attempt: resolve the form target, POST the password, classify
    /// the response. Transport failures and proxy-edge warming statuses are
    /// retried by ``login`` on the matching ladder; every other HTTP-status
    /// branch is definitive.
    private func attemptLogin(profile: Profile, server: ServerUrl, secret: String) async -> Attempt {
        // Gap-1: derive the real login POST target from the served login form,
        // falling back to the origin-root only when no <form action> is found.
        let loginUrl = await resolveLoginUrl(server)

        let request = HTTPRequest(
            url: loginUrl,
            method: "POST",
            body: .form([("password", secret), ("base", ".")]),  // hidden field code-server posts
            followRedirects: false
        )

        do {
            let resp = try await http.send(request)

            // Detect layer-1 SSO: a redirect to an ABSOLUTE, foreign origin.
            if isSsoRedirect(resp.header("location"), server: server) {
                return Attempt(.needsInteractiveSso(url: profile.baseUrl), .definitive)
            }

            let setCookies = resp.headerValues("set-cookie")
            if resp.status == 302 && !setCookies.isEmpty {
                let sessionCookies = setCookies
                    .compactMap { TofuCookie.parse($0) }
                    .filter { $0.name == Self.sessionCookieName }
                if sessionCookies.isEmpty {
                    // A 302 that carries cookies but none of them is the
                    // code-server session cookie means this server is not
                    // gated by a code-server password we can replay (bare
                    // Tofu, a different gate, or a changed login form). This
                    // is NOT a failure: don't hard-error and strand the user
                    // on the profile list. Degrade gracefully — let the web
                    // screen load baseUrl so the server's own login page can
                    // take over inside the WebView if auth is really needed.
                    return Attempt(.success(host: server.host), .definitive)
                }
                await cookies.inject(origin: server.origin, cookies: sessionCookies)
                await store.setCookieHost(profile.id, server.host)
                return Attempt(.success(host: server.host), .definitive)
            }

            // code-server re-serves the login page (200) on a bad password.
            // Keep that as the confirmed bad-credentials signal so the user
            // sees "wrong password" rather than being silently dropped into
            // the WebView.
            if resp.status == 200 { return Attempt(.badCredentials, .definitive) }

            // The vscode proxy answers 502/503/504 while the sandbox behind
            // the tunnel is still booting — nothing is listening yet. That is
            // a WARMING condition, not an "unconfirmed gate": retry on the
            // longer ladder instead of degrading into a WebView that would
            // render the proxy's raw error page with no way to retry from a
            // phone.
            if LoginRetryPolicy.isWarmingStatus(resp.status) {
                return Attempt(
                    .error(message: LoginRetryPolicy.warmingMessage(
                        "the proxy answered HTTP \(resp.status)")),
                    .warming
                )
            }

            // ANY other status is an outcome we cannot confirm as either a
            // replayable code-server gate (302 handled above) or a bad
            // password (200). A bare Tofu server returns 401 HTML when
            // unauthenticated; a fronting gateway may answer 4xx/5xx. Same
            // posture as the 302-without-cookie branch: do NOT hard-error —
            // degrade gracefully, letting the WebView take over. Hard error is
            // reserved for real transport failure (the catch below).
            return Attempt(.success(host: server.host), .definitive)
        } catch {
            // A connect/read timeout against the proxy has the same meaning
            // as its 5xx: the edge is up but the sandbox behind the tunnel
            // isn't answering yet — longer ladder. Any other transport
            // failure (refused, reset, DNS) stays on the short one.
            if LoginRetryPolicy.isWarmingTransport(error) {
                return Attempt(
                    .error(message: LoginRetryPolicy.warmingMessage(error.localizedDescription)),
                    .warming
                )
            }
            return Attempt(.error(message: error.localizedDescription), .transient)
        }
    }

    /// v4 meta preflight on a successful login: swap success for incompatible
    /// ONLY when the server definitively refuses this build. Never blocks on
    /// partial knowledge — see ``ApiMetaGate``.
    private func withApiPreflight(server: ServerUrl, baseUrl: String, result: LoginResult) async -> LoginResult {
        guard case .success = result else { return result }
        guard let reason = await preflightApiCompatibility(server: server, baseUrl: baseUrl) else {
            return result
        }
        return .incompatible(message: reason)
    }

    /// GET the meta endpoint through the SAME gateway the login just unlocked
    /// (the session cookie rides along when the jar has one).
    private func preflightApiCompatibility(server: ServerUrl, baseUrl: String) async -> String? {
        var headers: [String: String] = [:]
        if let cookieHeader = await cookies.cookieHeader(server.origin),
           !cookieHeader.trimmingCharacters(in: .whitespaces).isEmpty {
            headers["Cookie"] = cookieHeader
        }
        let request = HTTPRequest(
            url: ApiMetaGate.metaUrl(baseUrl),
            method: "GET",
            headers: headers,
            followRedirects: true
        )
        guard let resp = try? await http.send(request) else { return nil }  // preflight skipped
        return ApiMetaGate.incompatibilityReason(status: resp.status, body: resp.body)
    }

    /// Record that an INTERACTIVE_SSO sign-in completed INSIDE the WebView.
    ///
    /// The headless ``login`` path stamps `cookieHost` when it injects a
    /// cookie it obtained itself. An interactive sign-in never passes through
    /// that path — the cookie is set by the browser engine — so without this
    /// nothing would ever stamp the profile, `isSignedIn` would stay false
    /// forever, and the supervisor's Start/Stop would remain unusable.
    /// Called from the WebView's page-finished callback.
    ///
    /// Returns true when the profile was actually updated. Idempotent: a
    /// profile already stamped for this host is left alone (no write on every
    /// page load).
    @discardableResult
    public func noteInteractiveSignIn(_ profile: Profile, finishedUrl: String) async -> Bool {
        guard let host = InteractiveSso.hostToStamp(profile) else { return false }
        if profile.cookieHost == host { return false }
        let header = await cookies.cookieHeader("https://\(host)")
        guard InteractiveSso.completedSignIn(profile: profile, finishedUrl: finishedUrl, cookieHeader: header) else {
            return false
        }
        // Targeted write: this must NOT overwrite the whole row. The caller's
        // `profile` is the snapshot the web screen was opened with, and an
        // SSO sign-in can take minutes — anything edited meanwhile would be
        // silently rolled back by a full-row update.
        await store.setCookieHost(profile.id, host)
        return true
    }

    /// Update a profile's URL. If the URL host changed, HARD-PURGE the old
    /// host's cookie jar first (cookie is Domain-pinned) — this is the
    /// re-provision invariant, baked into the update path. Then re-login
    /// against the new host from the stored credential.
    ///
    /// Returns the login outcome AND the row as persisted — this path nils
    /// `cookieHost` and may refresh `instanceUuid`.
    public func updateUrlAndReauth(_ profile: Profile, newUrl: String) async -> ReauthResult {
        guard let newServer = ServerUrl.parse(newUrl) else {
            return ReauthResult(login: .error(message: "Invalid URL: \(newUrl)"), persisted: profile)
        }

        let oldHost = profile.cookieHost ?? ServerUrl.parse(profile.baseUrl)?.host
        if let oldHost, oldHost != newServer.host {
            await cookies.purgeHost(oldHost)
        }

        var updated = profile
        updated.baseUrl = newUrl
        updated.instanceUuid = newServer.instanceUuid ?? profile.instanceUuid
        updated.cookieHost = nil  // invalidated until the fresh login re-stamps it
        await store.update(updated)
        return ReauthResult(login: await login(updated), persisted: updated)
    }

    /// Gap-1: GET the login page and resolve its `<form action>` to the real
    /// POST target. Falls back to the origin-root `/login` on any failure or
    /// when the page has no form action. Follows redirects so a relative 302
    /// to the login page resolves before we parse the form.
    private func resolveLoginUrl(_ server: ServerUrl) async -> String {
        let request = HTTPRequest(url: server.loginUrl, method: "GET", followRedirects: true)
        guard let resp = try? await http.send(request) else { return server.loginUrl }
        return LoginForm.resolveAction(resp.body, pageUrl: resp.finalUrl)?.absoluteString ?? server.loginUrl
    }

    /// True when [location] points at a different origin than the code-server one → SSO IdP.
    private func isSsoRedirect(_ location: String?, server: ServerUrl) -> Bool {
        guard let location, !location.trimmingCharacters(in: .whitespaces).isEmpty else { return false }
        // Relative redirects (./login, ./../../login) are code-server's own — not SSO.
        guard let base = URL(string: server.origin + "/"),
              let resolved = URL(string: location, relativeTo: base)?.absoluteURL,
              let host = resolved.host else { return false }
        return host.lowercased() != server.host
    }
}

/// How one login attempt's outcome steers the retry loop in ``SessionManager/login``.
private enum AttemptKind {
    /// A real answer (success, bad password, SSO handoff) — never retried.
    case definitive
    /// A transport failure with no usable HTTP response — short ladder.
    case `transient`
    /// The sandbox behind the tunnel is still booting — long ladder.
    case warming
}

private struct Attempt {
    let result: LoginResult
    let kind: AttemptKind

    init(_ result: LoginResult, _ kind: AttemptKind) {
        self.result = result
        self.kind = kind
    }
}

/// Bounded retry for transient transport failures of the login POST.
/// Port of LoginRetryPolicy.kt.
public enum LoginRetryPolicy {
    public static let maxAttempts = 3

    /// Longer ladder for a WARMING sandbox: a cold container behind the
    /// vscode proxy routinely takes tens of seconds before anything listens,
    /// so the short 3-attempt/3.5s ladder would hand the user back a dead
    /// page long before the host is up.
    public static let maxWarmingAttempts = 6

    public static func maxAttempts(warming: Bool) -> Int {
        warming ? maxWarmingAttempts : maxAttempts
    }

    /// Delay before the next attempt, after [failedAttempts] failures.
    public static func backoffMs(_ failedAttempts: Int, warming: Bool = false) -> Int64 {
        if warming { return min(2_000 * Int64(failedAttempts), 8_000) }
        return failedAttempts <= 1 ? 1_000 : 2_500
    }

    /// The wire contract for "sandbox waking" lives in ``TofuProbe``.
    public static func isWarmingStatus(_ code: Int) -> Bool {
        TofuProbe.isWakingStatus(code)
    }

    /// A socket timeout against the proxy means the edge is up but the
    /// sandbox behind the tunnel isn't answering yet — same as its 5xx.
    /// Refused / reset / DNS failures stay on the short ladder.
    public static func isWarmingTransport(_ error: Error) -> Bool {
        if let urlError = error as? URLError {
            return urlError.code == .timedOut
        }
        let message = error.localizedDescription.lowercased()
        return message.contains("timed out") || message.contains("timeout")
    }

    /// User-facing text for a warming retry that eventually exhausted.
    public static func warmingMessage(_ cause: String) -> String {
        "The sandbox is still waking up (\(cause)) — it usually comes up within " +
            "half a minute. Give it a few more seconds and tap Open again."
    }
}
