package com.tofu.client.session

import android.util.Log
import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Cookie
import okhttp3.FormBody
import okhttp3.OkHttpClient
import okhttp3.Request

/** Outcome of an attempt to establish a session for a profile. */
sealed interface LoginResult {
    /** Session cookie obtained and injected into the WebView jar. */
    data class Success(val host: String) : LoginResult
    /** Credentials rejected (code-server re-served the login page). */
    data object BadCredentials : LoginResult
    /** Layer-1 SSO detected — caller must open the WebView interactively. */
    data class NeedsInteractiveSso(val url: String) : LoginResult
    /** No stored credential for this alias. */
    data object NoCredential : LoginResult
    /** Transport / parse failure. */
    data class Error(val message: String) : LoginResult
    /**
     * The server's v4 meta endpoint definitively refuses this client build
     * (API major mismatch, or minAndroidBuild above us). Fail-closed at login
     * so the user gets an actionable message instead of a half-broken SPA.
     */
    data class Incompatible(val message: String) : LoginResult
}

/**
 * The outcome of [SessionManager.updateUrlAndReauth]: the login result plus the
 * row as actually written. Callers navigate with [persisted] rather than a
 * locally-rebuilt copy, which would miss the `cookieHost = null` invalidation.
 */
data class ReauthResult(val login: LoginResult, val persisted: Profile)

/**
 * Owns the credential-replay lifecycle proven in the feasibility spike:
 *
 *   POST <origin>/login (password, base=.)  →  302 + Set-Cookie
 *     →  inject into WebView jar (with Max-Age upgrade)  →  load baseUrl.
 *
 * And the re-provision path: when a profile's URL host changes, purge the dead
 * host's jar BEFORE the new login (the cookie is Domain-pinned to the host).
 */
class SessionManager(
    private val dao: ProfileDao,
    private val secrets: SecretLookup,
    private val cookies: CookieSink = CookieBridge,
    private val http: OkHttpClient = defaultClient(),
    /**
     * This app's versionCode, checked against the server's minAndroidBuild in
     * the post-login preflight. Int.MAX_VALUE = "build unknown" (pure-tier
     * tests) and never blocks.
     */
    private val appVersionCode: Int = Int.MAX_VALUE,
    /** Sleep hook for the login retry backoff; tests inject a recorder. */
    private val sleeper: suspend (Long) -> Unit = { kotlinx.coroutines.delay(it) },
) {

    /**
     * Establish a session for [profile]. Does NOT follow the 302 (we only need
     * the Set-Cookie), so redirects are disabled on [http].
     */
    suspend fun login(profile: Profile): LoginResult = withContext(Dispatchers.IO) {
        val server = ServerUrl.parse(profile.baseUrl)
            ?: return@withContext LoginResult.Error("Invalid URL: ${profile.baseUrl}")

        if (profile.authType == AuthType.NONE) {
            // No outer gateway to unlock: the WebView reaches Tofu directly,
            // so the SPA's own 426/upgrade handling surfaces a version
            // mismatch in-page. A headless preflight here would only add a
            // tunnel round-trip (and double the dead-server wait) for a
            // verdict the WebView gives anyway — stay zero-request.
            return@withContext LoginResult.Success(server.host)
        }
        if (profile.authType == AuthType.INTERACTIVE_SSO) {
            // Layer-1 SSO can't be replayed headlessly; hand off to the WebView.
            return@withContext LoginResult.NeedsInteractiveSso(profile.baseUrl)
        }

        val secret = secrets.secretFor(profile.alias)
            ?: return@withContext LoginResult.NoCredential

        // The login POST rides the outer tunnel (vscode proxy / VPN); a
        // transient reset there surfaces as a transport Error, not an HTTP
        // status, so bounded retry with backoff is the difference between
        // "tap Open twice on a flaky tunnel" and "it just works". WARMING
        // outcomes (the proxy's 502/503/504, or a connect timeout — the
        // sandbox behind the tunnel is still booting) get a longer ladder:
        // a cold sandbox routinely takes tens of seconds to serve, and
        // bouncing the user back to the list after 3.5s just makes them tap
        // Open again by hand. Definitive answers (BadCredentials /
        // NeedsInteractiveSso / Success) never retry.
        var attempt = 0
        var previousKind = AttemptKind.DEFINITIVE
        var result: LoginResult
        do {
            attempt += 1
            if (attempt > 1) {
                val backoff = LoginRetryPolicy.backoffMs(
                    attempt - 1, warming = previousKind == AttemptKind.WARMING,
                )
                Log.i(TAG, "login attempt $attempt alias=${profile.alias} " +
                    "after ${backoff}ms backoff (${previousKind.name.lowercase()})")
                sleeper(backoff)
            }
            val outcome = attemptLogin(profile, server, secret)
            previousKind = outcome.kind
            result = outcome.result
        } while (previousKind != AttemptKind.DEFINITIVE &&
            attempt < LoginRetryPolicy.maxAttempts(previousKind == AttemptKind.WARMING))
        return@withContext withApiPreflight(server, profile.baseUrl, result)
    }

    /**
     * One login attempt: resolve the form target, POST the password, classify
     * the response. Blocking — [login] calls it on Dispatchers.IO. Transport
     * failures and proxy-edge warming statuses are retried by [login] on the
     * matching ladder; every other HTTP-status branch is definitive.
     */
    private suspend fun attemptLogin(
        profile: Profile,
        server: ServerUrl,
        secret: String,
    ): Attempt {
        // Gap-1: derive the real login POST target from the served login form,
        // falling back to the origin-root only when no <form action> is found.
        val loginUrl = resolveLoginUrl(server)

        val form = FormBody.Builder()
            .add("password", secret)
            .add("base", ".")   // hidden field code-server's login form posts
            .build()
        val req = Request.Builder()
            .url(loginUrl)
            .post(form)
            .build()

        try {
            http.newCall(req).execute().use { resp ->
                // Detect layer-1 SSO: a redirect to an ABSOLUTE, foreign origin.
                val location = resp.header("Location")
                if (isSsoRedirect(location, server)) {
                    return Attempt(
                        LoginResult.NeedsInteractiveSso(profile.baseUrl),
                        AttemptKind.DEFINITIVE,
                    )
                }

                val setCookies = resp.headers("Set-Cookie")
                if (resp.code == 302 && setCookies.isNotEmpty()) {
                    val sessionCookies = setCookies.mapNotNull { raw ->
                        Cookie.parse(server.httpUrl, raw)
                    }.filter { it.name == SESSION_COOKIE }
                    if (sessionCookies.isEmpty()) {
                        // A 302 that carries cookies but none of them is the
                        // code-server session cookie means this server is not
                        // gated by a code-server password we can replay (bare
                        // Tofu, a different gate, or a changed login form). This
                        // is NOT a failure: don't hard-Error and strand the user
                        // on the profile list. Degrade gracefully — skip the
                        // headless handshake and let WebScreen load baseUrl so
                        // the server's own login page / ReauthWebViewClient can
                        // take over inside the WebView if auth is really needed.
                        Log.i(TAG, "login: 302 without $SESSION_COOKIE for " +
                            "alias=${profile.alias} host=${server.host}; " +
                            "no code-server gate to replay, deferring to WebView")
                        return Attempt(LoginResult.Success(server.host), AttemptKind.DEFINITIVE)
                    }
                    cookies.inject(server.origin, sessionCookies)
                    dao.setCookieHost(profile.id, server.host)
                    Log.i(TAG, "login ok alias=${profile.alias} host=${server.host}")
                    return Attempt(LoginResult.Success(server.host), AttemptKind.DEFINITIVE)
                }

                // code-server re-serves the login page (200) on a bad password.
                // Keep that as the confirmed BadCredentials signal so the user
                // sees "wrong password" rather than being silently dropped into
                // the WebView.
                if (resp.code == 200) {
                    return Attempt(LoginResult.BadCredentials, AttemptKind.DEFINITIVE)
                }

                // The vscode proxy answers 502/503/504 while the sandbox
                // behind the tunnel is still booting — nothing is listening
                // yet. That is a WARMING condition, not an "unconfirmed
                // gate": retry on the longer ladder instead of degrading into
                // a WebView that would render the proxy's raw error page with
                // no way to retry from a phone.
                if (LoginRetryPolicy.isWarmingStatus(resp.code)) {
                    Log.i(TAG, "login: proxy edge ${resp.code} for " +
                        "alias=${profile.alias}; sandbox still warming")
                    return Attempt(
                        LoginResult.Error(
                            LoginRetryPolicy.warmingMessage(
                                "the proxy answered HTTP ${resp.code}",
                            ),
                        ),
                        AttemptKind.WARMING,
                    )
                }
                // ANY other status is an outcome we cannot confirm as either a
                // replayable code-server gate (302 handled above) or a bad
                // password (200). A bare Tofu server returns 401 HTML when
                // unauthenticated; a fronting gateway may answer 4xx/5xx; a
                // changed code-server may respond differently. Same posture as
                // the 302-without-cookie branch: do NOT hard-Error and strand
                // the user on the profile list — degrade gracefully, letting
                // WebScreen load baseUrl so the server's own login page /
                // ReauthWebViewClient can take over. Hard Error is reserved for
                // real transport/network failure (the catch below).
                Log.i(TAG, "login: unconfirmed status ${resp.code} for " +
                    "alias=${profile.alias} host=${server.host}; " +
                    "no replayable code-server gate, deferring to WebView")
                return Attempt(LoginResult.Success(server.host), AttemptKind.DEFINITIVE)
            }
        } catch (e: Exception) {
            Log.w(TAG, "login failed alias=${profile.alias}: ${e.message}")
            // A connect/read timeout against the proxy has the same meaning
            // as its 5xx: the edge is up but the sandbox behind the tunnel
            // isn't answering yet — longer ladder. Any other transport
            // failure (refused, reset, DNS) stays on the short one.
            val msg = e.message ?: "network error"
            return if (LoginRetryPolicy.isWarmingTransport(msg)) {
                Attempt(
                    LoginResult.Error(LoginRetryPolicy.warmingMessage(msg)),
                    AttemptKind.WARMING,
                )
            } else {
                Attempt(LoginResult.Error(msg), AttemptKind.TRANSIENT)
            }
        }
    }

    /**
     * v4 meta preflight on a successful login: swap [LoginResult.Success] for
     * [LoginResult.Incompatible] ONLY when the server definitively refuses
     * this build. Never blocks on partial knowledge — see [ApiMetaGate].
     */
    private fun withApiPreflight(
        server: ServerUrl,
        baseUrl: String,
        result: LoginResult,
    ): LoginResult {
        if (result !is LoginResult.Success) return result
        val reason = preflightApiCompatibility(server, baseUrl) ?: return result
        Log.w(TAG, "v4 meta preflight refuses this build: $reason")
        return LoginResult.Incompatible(reason)
    }

    /**
     * GET the meta endpoint through the SAME gateway the login just unlocked
     * (the session cookie rides along when the jar has one). Redirect-following
     * clone of [http], mirroring [resolveLoginUrl].
     */
    private fun preflightApiCompatibility(server: ServerUrl, baseUrl: String): String? {
        val req = Request.Builder()
            .url(ApiMetaGate.metaUrl(baseUrl))
            .get()
            .apply {
                val header = cookies.cookieHeader(server.origin)
                if (!header.isNullOrBlank()) addHeader("Cookie", header)
            }
            .build()
        return try {
            http.newBuilder().followRedirects(true).followSslRedirects(true).build()
                .newCall(req).execute().use { resp ->
                    ApiMetaGate.incompatibilityReason(
                        status = resp.code,
                        body = resp.body?.string(),
                        appVersionCode = appVersionCode,
                    )
                }
        } catch (e: Exception) {
            Log.w(TAG, "v4 meta preflight skipped: ${e.message}")
            null
        }
    }

    /**
     * Record that an INTERACTIVE_SSO sign-in completed INSIDE the WebView.
     *
     * The headless [login] path stamps `cookieHost` when it injects a cookie it
     * obtained itself. An interactive sign-in never passes through that path —
     * the cookie is set by the browser engine — so without this nothing would
     * ever stamp the profile, `isSignedIn` would stay false forever, and the
     * supervisor's Start/Stop would remain unusable no matter how many times
     * the user signed in. Called from the WebView's page-finished callback.
     *
     * Returns true when the profile was actually updated. Idempotent: a profile
     * already stamped for this host is left alone (so it does not write on
     * every page load).
     */
    suspend fun noteInteractiveSignIn(profile: Profile, finishedUrl: String): Boolean {
        val host = InteractiveSso.hostToStamp(profile) ?: return false
        if (profile.cookieHost == host) return false
        val header = cookies.cookieHeader("https://$host")
        if (!InteractiveSso.completedSignIn(profile, finishedUrl, header)) return false
        // Targeted write: this must NOT overwrite the whole row. The caller's
        // `profile` is the snapshot WebScreen was opened with, and an SSO
        // sign-in can take minutes — anything edited meanwhile would be
        // silently rolled back by a full-row update.
        dao.setCookieHost(profile.id, host)
        Log.i(TAG, "interactive sign-in recorded alias=${profile.alias} host=$host")
        return true
    }

    /**
     * Update a profile's editable fields. If the URL host changed, HARD-PURGE
     * the old host's cookie jar first (cookie is Domain-pinned) — this is the
     * re-provision invariant, baked into the update path, not an afterthought.
     * Then re-login against the new host from the stored credential.
     *
     * Returns the login outcome AND the row as persisted — this path nulls
     * `cookieHost` and may refresh `instanceUuid`, so a caller that rebuilt the
     * profile itself would hold a row that disagrees with the database.
     */
    suspend fun updateUrlAndReauth(profile: Profile, newUrl: String): ReauthResult {
        val newServer = ServerUrl.parse(newUrl)
            ?: return ReauthResult(LoginResult.Error("Invalid URL: $newUrl"), profile)

        val oldHost = profile.cookieHost ?: ServerUrl.parse(profile.baseUrl)?.host
        if (oldHost != null && oldHost != newServer.host) {
            cookies.purgeHost(oldHost)
            Log.i(TAG, "URL host changed ${oldHost} -> ${newServer.host}; purged old jar")
        }

        val updated = profile.copy(
            baseUrl = newUrl,
            instanceUuid = newServer.instanceUuid ?: profile.instanceUuid,
            cookieHost = null,     // invalidated until the fresh login re-stamps it
        )
        dao.update(updated)
        return ReauthResult(login(updated), updated)
    }

    /**
     * Gap-1: GET the login page and resolve its `<form action>` to the real POST
     * target. Falls back to the origin-root `/login` on any failure or when the
     * page has no form action. Uses a redirect-FOLLOWING clone of [http] so a
     * relative 302 to the login page resolves before we parse the form.
     */
    private fun resolveLoginUrl(server: ServerUrl): String {
        return try {
            val getReq = Request.Builder().url(server.loginUrl).get().build()
            http.newBuilder().followRedirects(true).followSslRedirects(true).build()
                .newCall(getReq).execute().use { resp ->
                    val pageUrl = resp.request.url          // after any redirects
                    val body = resp.body?.string().orEmpty()
                    val action = LoginForm.resolveAction(body, pageUrl)
                    (action?.toString() ?: server.loginUrl)
                }
        } catch (e: Exception) {
            Log.w(TAG, "resolveLoginUrl fell back to origin-root: ${e.message}")
            server.loginUrl
        }
    }

    /** True when [location] points at a different origin than the code-server one → SSO IdP. */
    private fun isSsoRedirect(location: String?, server: ServerUrl): Boolean {
        if (location.isNullOrBlank()) return false
        // Relative redirects (./login, ./../../login) are code-server's own — not SSO.
        val abs = server.httpUrl.resolve(location) ?: return false
        return abs.host != server.host
    }

    private companion object {
        const val TAG = "SessionManager"
        const val SESSION_COOKIE = "code-server-session"

        fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .followRedirects(false)          // we need the 302 + Set-Cookie, not the target
            .followSslRedirects(false)
            .build()
    }
}


/** How one login attempt's outcome steers the retry loop in [SessionManager.login]. */
private enum class AttemptKind {
    /** A real answer (success, bad password, SSO handoff) — never retried. */
    DEFINITIVE,
    /** A transport failure with no usable HTTP response — short ladder. */
    TRANSIENT,
    /** The sandbox behind the tunnel is still booting — long ladder. */
    WARMING,
}

private data class Attempt(val result: LoginResult, val kind: AttemptKind)

/** Bounded retry for transient transport failures of the login POST. */
internal object LoginRetryPolicy {
    const val MAX_ATTEMPTS = 3

    /**
     * Longer ladder for a WARMING sandbox: a cold container behind the vscode
     * proxy routinely takes tens of seconds before anything listens, so the
     * short 3-attempt/3.5s ladder would hand the user back a dead page long
     * before the host is up.
     */
    const val MAX_WARMING_ATTEMPTS = 6

    fun maxAttempts(warming: Boolean): Int =
        if (warming) MAX_WARMING_ATTEMPTS else MAX_ATTEMPTS

    /** Delay before the next attempt, after [failedAttempts] failures. */
    fun backoffMs(failedAttempts: Int, warming: Boolean = false): Long = when {
        warming -> minOf(2_000L * failedAttempts, 8_000L)
        failedAttempts <= 1 -> 1_000L
        else -> 2_500L
    }

    /** The wire contract for "sandbox waking" lives in [TofuProbe]. */
    fun isWarmingStatus(code: Int): Boolean = TofuProbe.isWakingStatus(code)

    /** Socket-timeout phrasing ("timeout", "connect timed out", …). */
    fun isWarmingTransport(message: String?): Boolean {
        val m = message?.lowercase() ?: return false
        return "timed out" in m || "timeout" in m
    }

    /** User-facing text for a warming retry that eventually exhausted. */
    fun warmingMessage(cause: String): String =
        "The sandbox is still waking up ($cause) — it usually comes up within " +
            "half a minute. Give it a few more seconds and tap Open again."
}
