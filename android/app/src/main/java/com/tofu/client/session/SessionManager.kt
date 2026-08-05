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
) {

    /**
     * Establish a session for [profile]. Does NOT follow the 302 (we only need
     * the Set-Cookie), so redirects are disabled on [http].
     */
    suspend fun login(profile: Profile): LoginResult = withContext(Dispatchers.IO) {
        val server = ServerUrl.parse(profile.baseUrl)
            ?: return@withContext LoginResult.Error("Invalid URL: ${profile.baseUrl}")

        if (profile.authType == AuthType.NONE) {
            return@withContext LoginResult.Success(server.host)
        }
        if (profile.authType == AuthType.INTERACTIVE_SSO) {
            // Layer-1 SSO can't be replayed headlessly; hand off to the WebView.
            return@withContext LoginResult.NeedsInteractiveSso(profile.baseUrl)
        }

        val secret = secrets.secretFor(profile.alias)
            ?: return@withContext LoginResult.NoCredential

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
                    return@withContext LoginResult.NeedsInteractiveSso(profile.baseUrl)
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
                        return@withContext LoginResult.Success(server.host)
                    }
                    cookies.inject(server.origin, sessionCookies)
                    dao.setCookieHost(profile.id, server.host)
                    Log.i(TAG, "login ok alias=${profile.alias} host=${server.host}")
                    return@withContext LoginResult.Success(server.host)
                }

                // code-server re-serves the login page (200) on a bad password.
                // Keep that as the confirmed BadCredentials signal so the user
                // sees "wrong password" rather than being silently dropped into
                // the WebView.
                if (resp.code == 200) return@withContext LoginResult.BadCredentials
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
                return@withContext LoginResult.Success(server.host)
            }
        } catch (e: Exception) {
            Log.w(TAG, "login failed alias=${profile.alias}: ${e.message}")
            return@withContext LoginResult.Error(e.message ?: "network error")
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
