package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.Call
import okhttp3.Callback
import okhttp3.ResponseBody.Companion.toResponseBody
import java.io.IOException
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resumeWithException
import okhttp3.HttpUrl
import okhttp3.Response
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import java.util.concurrent.TimeUnit
import okhttp3.FormBody
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/** Outcome of an attempt to establish a session for a profile. */
sealed interface LoginResult {
    /** Session cookie obtained and injected into the WebView jar. */
    data class Success(val host: String) : LoginResult
    /** Credentials rejected (code-server re-served the login page). */
    data object BadCredentials : LoginResult
    /** Layer-1 SSO detected — caller must open the WebView interactively. */
    data class NeedsInteractiveSso(val url: String) : LoginResult
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
 * Owns bounded gateway discovery, trusted code-server password replay and
 * browser handoff. Cookies are selected for each request URL, never copied
 * across origins. Unknown gateways require interaction, not guessed POSTs.
 * Dependencies: profile repository, secret vault, cookie sink, OkHttp.
 */
class SessionManager(
    private val dao: ProfileDao,
    private val secrets: SecretLookup,
    private val cookies: CookieSink = CookieBridge,
    http: OkHttpClient = defaultClient(),
    private val appVersionCode: Int = Int.MAX_VALUE,
    private val sleeper: suspend (Long) -> Unit = { kotlinx.coroutines.delay(it) },
    val diagnostics: DiagLog? = null,
) {
    // Disable automatic redirect/cookie machinery even on injected clients:
    // every hop must re-evaluate origin and cookie path before another request.
    private val http = DiagnosticHttp.attach(http, diagnostics, "AuthHttp").newBuilder().followRedirects(false)
        .followSslRedirects(false).cookieJar(CookieJar.NO_COOKIES).build()

    suspend fun login(profile: Profile): LoginResult {
        diagnostics?.record("I", "Auth", "begin profile=${profile.alias} mode=${profile.authType} url=${ClientReport.safeUrl(profile.baseUrl)}")
        try {
            val result = loginInternal(profile)
            val detail = when (result) {
                is LoginResult.Error -> result.message
                is LoginResult.Incompatible -> result.message
                is LoginResult.Success -> "session-established"
                is LoginResult.BadCredentials -> "password-rejected"
                is LoginResult.NeedsInteractiveSso -> "browser-handoff"
            }
            diagnostics?.record(if (result is LoginResult.Error || result is LoginResult.BadCredentials || result is LoginResult.Incompatible) "E" else "I", "Auth", "outcome profile=${profile.alias} $detail")
            return result
        } catch (error: CancellationException) {
            diagnostics?.record("W", "Auth", "cancelled profile=${profile.alias}")
            throw error
        } catch (error: Exception) {
            diagnostics?.exception("Auth", error)
            throw error
        }
    }

    private suspend fun loginInternal(profile: Profile): LoginResult = withContext(Dispatchers.IO) {
        val server = ServerUrl.parse(profile.baseUrl)
            ?: return@withContext LoginResult.Error("Invalid server URL")
        if (profile.authType == AuthType.NONE) return@withContext LoginResult.Success(server.host)
        if (!cookies.cookieHeader(sessionProbeUrl(server)).isNullOrBlank() && verifyGatewaySession(profile)) {
            stamp(profile, server.host)
            return@withContext withApiPreflight(server, LoginResult.Success(server.host))
        }
        if (profile.authType == AuthType.INTERACTIVE_SSO) return@withContext interactive(profile)
        val secret = secrets.secretFor(profile.alias)
        if (secret.isNullOrEmpty()) return@withContext interactive(profile)

        var attempt = 0
        var kind = AttemptKind.DEFINITIVE
        var result: LoginResult
        do {
            attempt++
            if (attempt > 1) sleeper(LoginRetryPolicy.backoffMs(attempt - 1, kind == AttemptKind.WARMING))
            diagnostics?.record("I", "Auth", "attempt=$attempt previousKind=$kind")
            val outcome = attemptLogin(profile, server, secret)
            kind = outcome.kind
            result = outcome.result
        } while (kind != AttemptKind.DEFINITIVE && attempt < LoginRetryPolicy.maxAttempts(kind == AttemptKind.WARMING))
        withApiPreflight(server, result)
    }

    private fun interactive(profile: Profile) = LoginResult.NeedsInteractiveSso(profile.baseUrl)

    private suspend fun attemptLogin(profile: Profile, server: ServerUrl, secret: String): Attempt {
        try {
            // Start at the configured application URL. Its real redirects carry
            // the reverse-proxy prefix and return target; root /login guesses do not.
            var target = server.httpUrl
            var passwordForm: LoginForm.PasswordForm? = null
            for (hop in 0..MAX_GATEWAY_REDIRECTS) {
                var next: HttpUrl? = null
                execute(cookieRequest(target.toString()).get().build()).use { response ->
                    cookies.injectResponseCookies(target.toString(), response.headers("Set-Cookie"))
                    if (LoginRetryPolicy.isWarmingStatus(response.code)) return warming(response.code)
                    if (response.code == 429) return rateLimited()
                    if (response.code in REDIRECT_STATUSES) {
                        val location = response.header("Location")?.let { target.resolve(it) }
                        if (location == null || !server.sameOrigin(location) ||
                            location.username.isNotEmpty() || location.password.isNotEmpty()) {
                            return Attempt(interactive(profile), AttemptKind.DEFINITIVE)
                        }
                        next = location
                    } else if (response.code == 200) {
                        passwordForm = LoginForm.discover(readBounded(response), target, server)
                    }
                }
                if (next == null) break
                target = next!!
                if (hop == MAX_GATEWAY_REDIRECTS) return Attempt(interactive(profile), AttemptKind.DEFINITIVE)
            }
            val loginForm = passwordForm ?: run {
                val result = if (verifyGatewaySession(profile)) {
                    stamp(profile, server.host)
                    LoginResult.Success(server.host)
                } else interactive(profile)
                return Attempt(result, AttemptKind.DEFINITIVE)
            }
            diagnostics?.record("I", "Auth", "recognized code-server form target=${ClientReport.safeUrl(loginForm.target.toString())} fields=${loginForm.fields.keys}")
            val form = FormBody.Builder().apply {
                for ((name, value) in loginForm.fields) add(name, value)
                add("password", secret)
            }.build()
            execute(cookieRequest(loginForm.target.toString()).post(form).build()).use { response ->
                if (LoginRetryPolicy.isWarmingStatus(response.code)) return warming(response.code)
                if (response.code == 429) return rateLimited()
                val rawCookies = response.headers("Set-Cookie")
                cookies.injectResponseCookies(loginForm.target.toString(), rawCookies)
                val issuedSession = rawCookies.mapNotNull { Cookie.parse(loginForm.target, it) }
                    .any { it.name == SESSION_COOKIE && it.expiresAt > System.currentTimeMillis() && it.value.isNotEmpty() }
                val redirected = response.header("Location")?.let { loginForm.target.resolve(it) }
                if (redirected != null && !server.sameOrigin(redirected)) {
                    return Attempt(interactive(profile), AttemptKind.DEFINITIVE)
                }
                if (issuedSession && response.code in setOf(200, 204, 302, 303)) {
                    stamp(profile, server.host)
                    return Attempt(LoginResult.Success(server.host), AttemptKind.DEFINITIVE)
                }
                // HTTP 200 is only a wrong-password verdict when a known
                // code-server form was re-served. An IdP HTML page is not evidence.
                if (response.code == 200 && LoginForm.discover(readBounded(response), loginForm.target, server) != null) {
                    return Attempt(LoginResult.BadCredentials, AttemptKind.DEFINITIVE)
                }
                return Attempt(interactive(profile), AttemptKind.DEFINITIVE)
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            diagnostics?.exception("AuthAttempt", e)
            val message = e.message ?: "network error"
            return if (LoginRetryPolicy.isWarmingTransport(message)) Attempt(
                LoginResult.Error(LoginRetryPolicy.warmingMessage(message)), AttemptKind.WARMING,
            ) else Attempt(LoginResult.Error(message), AttemptKind.TRANSIENT)
        }
    }

    private fun warming(status: Int) = Attempt(
        LoginResult.Error(LoginRetryPolicy.warmingMessage("the proxy answered HTTP $status")), AttemptKind.WARMING,
    )

    private fun rateLimited() = Attempt(
        LoginResult.Error("The sign-in gateway is limiting attempts (HTTP 429). Wait before trying again."),
        AttemptKind.DEFINITIVE,
    )

    private fun sessionProbeUrl(server: ServerUrl): String =
        server.endpoint(SESSION_PROBE_PATH) + "?limit=1&meta=1"

    private fun cookieRequest(url: String): Request.Builder = Request.Builder().url(url).apply {
        cookies.cookieHeader(url)?.takeIf { it.isNotBlank() }?.let { header("Cookie", it) }
    }

    /** Cancellation closes the socket even while a body is arriving slowly. */
    @OptIn(ExperimentalCoroutinesApi::class)
    private suspend fun execute(request: Request): Response = suspendCancellableCoroutine { continuation ->
        val call = http.newCall(request)
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, error: IOException) {
                continuation.resumeWithException(error)
            }
            override fun onResponse(call: Call, response: Response) {
                try {
                    // Read under the cancellation hook, then release the socket.
                    // The coroutine receives a bounded in-memory response only.
                    val type = response.body?.contentType()
                    val bounded = response.newBuilder().body(readBounded(response).toResponseBody(type)).build()
                    response.close()
                    continuation.resume(bounded, onCancellation = { bounded.close() })
                } catch (error: Exception) {
                    response.close()
                    continuation.resumeWithException(error)
                }
            }
        })
    }

    private fun readBounded(response: Response): String {
        val source = response.body?.source() ?: return ""
        source.request(MAX_RESPONSE_BYTES + 1L)
        if (source.buffer.size > MAX_RESPONSE_BYTES) {
            diagnostics?.record("W", "AuthBody", "response exceeded $MAX_RESPONSE_BYTES bytes: ${ClientReport.safeUrl(response.request.url.toString())}")
            return ""
        }
        return source.readUtf8()
    }

    private fun isJsonResponse(response: Response, body: String): Boolean {
        val type = response.body?.contentType()
        val value = body.trim()
        return type?.subtype?.let { it == "json" || it.endsWith("+json") } == true &&
            value.startsWith("{") && value.endsWith("}")
    }

    private suspend fun stamp(profile: Profile, host: String): Boolean {
        currentCoroutineContext().ensureActive()
        val current = dao.getById(profile.id)
        if (current?.baseUrl == profile.baseUrl && current.cookieHost == host) return true
        val stamped = dao.setCookieHostIfUrlMatches(profile.id, profile.baseUrl, host) > 0
        diagnostics?.record(if (stamped) "I" else "W", "Auth", "session marker saved=$stamped profile=${profile.alias} (conditional URL match)")
        return stamped
    }

    private suspend fun withApiPreflight(server: ServerUrl, result: LoginResult): LoginResult {
        if (result !is LoginResult.Success) return result
        return try {
            execute(cookieRequest(server.endpoint("api/v4/meta")).get().build()).use { response ->
                val reason = ApiMetaGate.incompatibilityReason(response.code, readBounded(response), appVersionCode)
                if (reason == null) result else LoginResult.Incompatible(reason)
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // Compatibility is unknown on a transport failure; the browser
            // owns the ensuing load error. No redirected credential request.
            result
        }
    }

    /** Proves the outer gate is passed, even if Tofu's own account gate refuses. */
    private suspend fun verifyGatewaySession(profile: Profile): Boolean {
        val server = ServerUrl.parse(profile.baseUrl) ?: return false
        suspend fun tofuSession(): Boolean = execute(cookieRequest(sessionProbeUrl(server)).get().build()).use { response ->
            val body = readBounded(response)
            // Health/meta are public and may bypass an external gate. Read one
            // metadata row through the protected owner-scoped API instead.
            val ownAuthRefusal = response.code in setOf(401, 403) &&
                Regex("\"type\"\\s*:\\s*\"urn:tofu:problem:").containsMatchIn(body) &&
                Regex("\"instance\"\\s*:\\s*\"/api/v4/conversations\"").containsMatchIn(body) &&
                Regex("\"status\"\\s*:\\s*${response.code}(?:\\s*[,}])").containsMatchIn(body)
            val metadataReached = response.code == 200 &&
                Regex("\"data\"\\s*:\\s*\\{").containsMatchIn(body) &&
                Regex("\"items\"\\s*:\\s*\\[").containsMatchIn(body) &&
                Regex("\"page\"\\s*:\\s*\\{").containsMatchIn(body) &&
                Regex("\"meta\"\\s*:\\s*\\{").containsMatchIn(body) &&
                Regex("\"serverTimeMs\"\\s*:\\s*\\d+").containsMatchIn(body)
            val verified = isJsonResponse(response, body) && Regex("\"requestId\"\\s*:\\s*\"[^\"]+\"").containsMatchIn(body) &&
                (ownAuthRefusal || metadataReached)
            diagnostics?.record(if (verified) "I" else "W", "AuthProbe", "status=${response.code} appMetadata=$metadataReached appAuthRefusal=$ownAuthRefusal verified=$verified")
            verified
        }
        return try {
            val tofuVerified = try { tofuSession() } catch (e: CancellationException) { throw e } catch (e: IOException) { false }
            if (tofuVerified) true else {
                // A stopped Tofu still needs Start. Probe the actual supervisor
                // control-plane status, not an optionally public health route.
                if (profile.projectPath.isNullOrBlank()) return false
                val supervisor = SupervisorUrl.fromServerUrl(profile.baseUrl) ?: return false
                val statusUrl = SupervisorUrl.endpoint(supervisor, SupervisorUrl.STATUS, profile.projectPath)
                execute(cookieRequest(statusUrl).get().build()).use { response ->
                    val body = readBounded(response)
                    response.code == 200 && isJsonResponse(response, body) && Regex("\"supervisorProtocolVersion\"\\s*:\\s*\\d+").containsMatchIn(body) &&
                        Regex("\"supervisorSourceFingerprint\"\\s*:\\s*\"").containsMatchIn(body) &&
                        Regex("\"running\"\\s*:\\s*(true|false)").containsMatchIn(body) &&
                        Regex("\"ok\"\\s*:\\s*true").containsMatchIn(body)
                }
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            false // Session remains unverified; no repository state is promoted.
        }
    }

    /**
     * After an outer SSO hop, resume the saved code-server password through the
     * SAME trusted discovery path. The browser never provides a POST target.
     * A fresh repository row protects edits made during the SSO interaction.
     */
    suspend fun resumePasswordSignIn(profile: Profile, finishedUrl: String): LoginResult? {
        val server = ServerUrl.parse(profile.baseUrl) ?: return null
        val finished = ServerUrl.parse(finishedUrl) ?: return null
        val login = ServerUrl.parse(server.loginUrl) ?: return null
        if (!server.sameOrigin(finished.httpUrl) || finished.httpUrl.encodedPath.trimEnd('/') != login.httpUrl.encodedPath.trimEnd('/')) return null
        val current = dao.getById(profile.id) ?: return null
        if (current.baseUrl != profile.baseUrl || current.authType != AuthType.CODE_SERVER_PASSWORD || secrets.secretFor(current.alias).isNullOrEmpty()) return null
        return login(current)
    }

    /** A browser page load is a candidate; only a real endpoint proves sign-in. */
    suspend fun noteInteractiveSignIn(profile: Profile, finishedUrl: String): Boolean = withContext(Dispatchers.IO) {
        val server = ServerUrl.parse(profile.baseUrl) ?: return@withContext false
        val header = cookies.cookieHeader(sessionProbeUrl(server))
        if (!InteractiveSso.isCompletionCandidate(profile, finishedUrl, header)) return@withContext false
        val verified = verifyGatewaySession(profile)
        diagnostics?.record(if (verified) "I" else "W", "SSO", "completion profile=${profile.alias} endpointVerified=$verified page=${ClientReport.safeUrl(finishedUrl)}")
        if (!InteractiveSso.completedSignIn(profile, finishedUrl, header, verified)) return@withContext false
        cookies.flush()
        stamp(profile, server.host)
    }

    suspend fun submitReport(profile: Profile, reportJson: String): String = withContext(Dispatchers.IO) {
        val server = ServerUrl.parse(profile.baseUrl) ?: return@withContext "Can't send: invalid server URL"
        try {
            execute(cookieRequest(server.endpoint("api/v1/mobile/client-report"))
                .post(reportJson.toRequestBody(JSON_MEDIA_TYPE)).build()).use { response ->
                if (response.isSuccessful) "Report sent to ${profile.alias}"
                else "Server rejected the report (HTTP ${response.code})"
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            "Couldn't send to server: ${e.message ?: "network error"}"
        }
    }

    suspend fun updateUrlAndReauth(profile: Profile, newUrl: String): ReauthResult {
        val newServer = ServerUrl.parse(newUrl) ?: return ReauthResult(LoginResult.Error("Invalid server URL"), profile)
        val oldServer = ServerUrl.parse(profile.baseUrl)
        if (oldServer != null && oldServer.origin != newServer.origin) cookies.purgeOrigin(oldServer.origin, profile.baseUrl)
        val updated = profile.copy(baseUrl = newUrl, instanceUuid = newServer.instanceUuid ?: profile.instanceUuid, cookieHost = null)
        dao.update(updated)
        return ReauthResult(login(updated), updated)
    }

    private companion object {
        const val SESSION_PROBE_PATH = "api/v4/conversations"
        const val SESSION_COOKIE = "code-server-session"
        const val MAX_GATEWAY_REDIRECTS = 8
        const val MAX_RESPONSE_BYTES = 262_144L
        val REDIRECT_STATUSES = setOf(301, 302, 303, 307, 308)
        val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()
        fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS).readTimeout(15, TimeUnit.SECONDS)
            .callTimeout(25, TimeUnit.SECONDS).followRedirects(false).followSslRedirects(false).build()
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
