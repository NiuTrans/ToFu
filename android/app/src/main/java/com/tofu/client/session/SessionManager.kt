package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import com.tofu.client.data.sameConnectionConfiguration
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Cookie
import okhttp3.CookieJar
import java.io.IOException
import okhttp3.HttpUrl
import okhttp3.Response
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withTimeout
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.util.concurrent.atomic.AtomicBoolean
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
    http: OkHttpClient? = null,
    private val appVersionCode: Int = Int.MAX_VALUE,
    private val sleeper: suspend (Long) -> Unit = { kotlinx.coroutines.delay(it) },
    val diagnostics: DiagLog? = null,
) : java.io.Closeable {
    private val ownsHttp = http == null
    private val closed = AtomicBoolean(false)
    // Disable automatic redirect/cookie machinery even on injected clients:
    // every hop must re-evaluate origin and cookie path before another request.
    private val http = DiagnosticHttp.attach(http ?: defaultClient(), diagnostics, "AuthHttp").newBuilder().followRedirects(false)
        .followSslRedirects(false).cookieJar(CookieJar.NO_COOKIES).build()

    /** One repository-graph commit owner; network sign-in never holds this lock. */
    internal val profileMutationMutex = Mutex()

    /** Called only after the retained graph's owned producers have completed. */
    override fun close() {
        if (ownsHttp && closed.compareAndSet(false, true)) {
            http.dispatcher.cancelAll()
            http.connectionPool.evictAll()
            http.dispatcher.executorService.shutdown()
        }
    }

    suspend fun login(profile: Profile): LoginResult {
        diagnostics?.record("I", "Auth", "begin profile=${profile.alias} kind=${profile.kind} mode=${profile.authType} url=${ClientReport.safeUrl(profile.baseUrl)}")
        try {
            val attempted = loginInternal(profile)
            currentCoroutineContext().ensureActive()
            val result = if (InteractiveSso.shouldOpenWebView(attempted) && !profileIsCurrent(profile)) changedProfile() else attempted
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
        currentCoroutineContext().ensureActive()
        // Kind decides the protocol before gateway auth fields or vault access.
        // Internal endpoints are runtime copies of endpoint-free source rows.
        if (profile.kind != KernelKind.REMOTE_URL) {
            val runtime = runtimeServer(profile)
                ?: return@withContext LoginResult.Error("Kernel endpoint is unavailable. Return to the list and reconnect.")
            if (!profileIsCurrent(profile)) return@withContext changedProfile()
            return@withContext withApiPreflight(runtime, LoginResult.Success(runtime.host), profile)
        }
        urlAdmissionError(profile.baseUrl)?.let { return@withContext LoginResult.Error(it) }
        val server = ServerUrl.parse(profile.baseUrl)
            ?: return@withContext LoginResult.Error("Invalid server URL")
        if (!profileIsCurrent(profile)) return@withContext changedProfile()
        if (profile.authType == AuthType.NONE) return@withContext LoginResult.Success(server.host)
        if (!cookies.cookieHeader(sessionProbeUrl(server)).isNullOrBlank() && verifyGatewaySession(profile)) {
            return@withContext if (stamp(profile, server.host)) withApiPreflight(server, LoginResult.Success(server.host), profile) else changedProfile()
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
        withApiPreflight(server, result, profile)
    }

    /** Match saved authority without confusing an internal runtime URL with a Room field. */
    private suspend fun profileIsCurrent(profile: Profile): Boolean {
        currentCoroutineContext().ensureActive()
        if (profile.id == 0L && profile.copy(baseUrl = "", cookieHost = null) == KernelList.THIS_PHONE) {
            val storedLocal = dao.getAllOnce().any { it.kind == KernelKind.LOCAL }
            currentCoroutineContext().ensureActive()
            return !storedLocal
        }
        val current = dao.getById(profile.id) ?: return false
        currentCoroutineContext().ensureActive()
        return if (profile.kind == KernelKind.REMOTE_URL) current.sameGatewayIdentity(profile)
        else current.baseUrl.isEmpty() && current.sameConnectionConfiguration(profile.copy(baseUrl = ""))
    }

    /**
     * A URL gateway session belongs to id/kind/alias/URL/auth, not the
     * supervisor's project selector. Reviewed saves and internal kernels still
     * compare the full source configuration; a cookie stamp preserves a newer
     * project path instead of rejecting or overwriting that unrelated field.
     */
    private fun Profile.sameGatewayIdentity(snapshot: Profile): Boolean =
        kind == KernelKind.REMOTE_URL && snapshot.kind == KernelKind.REMOTE_URL &&
            sameConnectionConfiguration(snapshot.copy(projectPath = projectPath))

    private fun runtimeServer(profile: Profile): ServerUrl? {
        if (profile.kind == KernelKind.REMOTE_URL || ServerUrl.storageError(profile.baseUrl) != null) return null
        val server = ServerUrl.parse(profile.baseUrl) ?: return null
        val expectedHost = if (profile.kind == KernelKind.LOCAL) "127.0.0.1" else "127.0.0.2"
        return server.takeIf {
            it.httpUrl.scheme == "http" && it.host == expectedHost && it.httpUrl.encodedPath == "/" &&
                it.httpUrl.encodedQuery == null && it.httpUrl.encodedFragment == null &&
                (profile.cookieHost == null || profile.cookieHost == expectedHost)
        }
    }

    private fun urlAdmissionError(url: String): String? = ServerUrl.storageError(url)
        ?: LocalKernelCookie.RESERVED_HOST_MESSAGE.takeIf { LocalKernelCookie.isReservedUrl(url) }

    private fun changedProfile() = LoginResult.Error(PROFILE_CHANGED_MESSAGE)

    private fun interactive(profile: Profile) = LoginResult.NeedsInteractiveSso(profile.baseUrl)

    private suspend fun attemptLogin(profile: Profile, server: ServerUrl, secret: String): Attempt {
        try {
            // Start at the configured application URL. Its real redirects carry
            // the reverse-proxy prefix and return target; root /login guesses do not.
            var target = server.httpUrl
            var passwordForm: LoginForm.PasswordForm? = null
            for (hop in 0..MAX_GATEWAY_REDIRECTS) {
                var next: HttpUrl? = null
                if (!profileIsCurrent(profile)) return Attempt(changedProfile(), AttemptKind.DEFINITIVE)
                execute(cookieRequest(target.toString()).get().build()).use { response ->
                    currentCoroutineContext().ensureActive()
                    if (!profileIsCurrent(profile)) return Attempt(changedProfile(), AttemptKind.DEFINITIVE)
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
                val result = if (verifyGatewaySession(profile) && stamp(profile, server.host)) {
                    LoginResult.Success(server.host)
                } else interactive(profile)
                return Attempt(result, AttemptKind.DEFINITIVE)
            }
            currentCoroutineContext().ensureActive()
            val current = dao.getById(profile.id)
            currentCoroutineContext().ensureActive()
            if (current == null || current.kind != KernelKind.REMOTE_URL ||
                !current.sameGatewayIdentity(profile) || secrets.secretFor(current.alias) != secret) {
                return Attempt(changedProfile(), AttemptKind.DEFINITIVE)
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
                currentCoroutineContext().ensureActive()
                if (!profileIsCurrent(profile) || secrets.secretFor(profile.alias) != secret)
                    return Attempt(changedProfile(), AttemptKind.DEFINITIVE)
                cookies.injectResponseCookies(loginForm.target.toString(), rawCookies)
                val issuedSession = rawCookies.mapNotNull { Cookie.parse(loginForm.target, it) }
                    .any { it.name == SESSION_COOKIE && it.expiresAt > System.currentTimeMillis() && it.value.isNotEmpty() }
                val redirected = response.header("Location")?.let { loginForm.target.resolve(it) }
                if (redirected != null && !server.sameOrigin(redirected)) {
                    return Attempt(interactive(profile), AttemptKind.DEFINITIVE)
                }
                if (issuedSession && response.code in setOf(200, 204, 302, 303)) {
                    return Attempt(if (stamp(profile, server.host)) LoginResult.Success(server.host) else interactive(profile), AttemptKind.DEFINITIVE)
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

    private suspend fun execute(request: Request): Response =
        http.newCall(request).awaitBounded(MAX_RESPONSE_BYTES)

    private fun readBounded(response: Response): String = response.readBoundedBody(MAX_RESPONSE_BYTES).orEmpty()

    private fun isJsonResponse(response: Response, body: String): Boolean {
        val type = response.body?.contentType()
        val value = body.trim()
        return type?.subtype?.let { it == "json" || it.endsWith("+json") } == true &&
            value.startsWith("{") && value.endsWith("}")
    }

    private suspend fun stamp(profile: Profile, host: String): Boolean {
        currentCoroutineContext().ensureActive()
        if (profile.kind != KernelKind.REMOTE_URL) return false
        val current = dao.getById(profile.id)
        currentCoroutineContext().ensureActive()
        if (current == null || !current.sameGatewayIdentity(profile)) return false
        if (current.cookieHost == host) return true
        val stamped = dao.setCookieHostIfCurrent(profile.id, profile.baseUrl, profile.alias, profile.authType, host) > 0
        diagnostics?.record(if (stamped) "I" else "W", "Auth", "session marker saved=$stamped profile=${profile.alias} (conditional profile identity match)")
        return stamped
    }

    private suspend fun withApiPreflight(server: ServerUrl, result: LoginResult, profile: Profile): LoginResult {
        if (result !is LoginResult.Success) return result
        if (!profileIsCurrent(profile)) return changedProfile()
        val verdict = try {
            execute(cookieRequest(server.endpoint("api/v4/meta")).get().build()).use { response ->
                val reason = ApiMetaGate.incompatibilityReason(response.code, readBounded(response), appVersionCode)
                if (reason == null) result else LoginResult.Incompatible(reason)
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // Unknown compatibility remains fail-open, not unknown source identity.
            result
        }
        currentCoroutineContext().ensureActive()
        return if (profileIsCurrent(profile)) verdict else changedProfile()
    }

    /** Proves the outer gate is passed, even if Tofu's own account gate refuses. */
    private suspend fun verifyGatewaySession(profile: Profile): Boolean {
        if (profile.kind != KernelKind.REMOTE_URL || !profileIsCurrent(profile)) return false
        val server = ServerUrl.parse(profile.baseUrl) ?: return false
        suspend fun tofuSession(): Boolean = execute(cookieRequest(sessionProbeUrl(server)).get().build()).use { response ->
            if (!profileIsCurrent(profile)) return@use false
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
                if (profile.projectPath.isNullOrBlank() || !profileIsCurrent(profile)) return false
                val supervisor = SupervisorUrl.fromServerUrl(profile.baseUrl) ?: return false
                val statusUrl = SupervisorUrl.endpoint(supervisor, SupervisorUrl.STATUS, profile.projectPath)
                execute(cookieRequest(statusUrl).get().build()).use { response ->
                    if (!profileIsCurrent(profile)) return@use false
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
        currentCoroutineContext().ensureActive()
        if (profile.kind != KernelKind.REMOTE_URL || profile.authType == AuthType.NONE ||
            urlAdmissionError(profile.baseUrl) != null) return null
        val server = ServerUrl.parse(profile.baseUrl) ?: return null
        val finished = ServerUrl.parse(finishedUrl) ?: return null
        val login = ServerUrl.parse(server.loginUrl) ?: return null
        if (!server.sameOrigin(finished.httpUrl) || finished.httpUrl.encodedPath.trimEnd('/') != login.httpUrl.encodedPath.trimEnd('/')) return null
        val current = dao.getById(profile.id) ?: return null
        currentCoroutineContext().ensureActive()
        // WebView handoff uses a temporary SSO display mode. Only the fresh
        // saved password choice can authorize replay, never that display copy.
        if (current.authType != AuthType.CODE_SERVER_PASSWORD ||
            !current.sameGatewayIdentity(profile.copy(authType = current.authType)) ||
            secrets.secretFor(current.alias).isNullOrEmpty()) return null
        return login(current)
    }

    /** A browser page load is a candidate; only a real endpoint proves sign-in. */
    suspend fun noteInteractiveSignIn(profile: Profile, finishedUrl: String): Boolean = withContext(Dispatchers.IO) {
        currentCoroutineContext().ensureActive()
        if (profile.kind != KernelKind.REMOTE_URL || profile.authType != AuthType.INTERACTIVE_SSO ||
            urlAdmissionError(profile.baseUrl) != null) return@withContext false
        val source = dao.getById(profile.id) ?: return@withContext false
        currentCoroutineContext().ensureActive()
        if (source.authType == AuthType.NONE ||
            !source.sameGatewayIdentity(profile.copy(authType = source.authType))) return@withContext false
        val server = ServerUrl.parse(source.baseUrl) ?: return@withContext false
        // The browser mode is transient; verification and the conditional
        // stamp use the saved auth choice and recheck it after the request.
        val browser = source.copy(authType = AuthType.INTERACTIVE_SSO)
        val header = cookies.cookieHeader(sessionProbeUrl(server))
        if (!InteractiveSso.isCompletionCandidate(browser, finishedUrl, header)) return@withContext false
        val verified = verifyGatewaySession(source)
        diagnostics?.record(if (verified) "I" else "W", "SSO", "completion profile=${profile.alias} endpointVerified=$verified page=${ClientReport.safeUrl(finishedUrl)}")
        if (!InteractiveSso.completedSignIn(browser, finishedUrl, header, verified)) return@withContext false
        val current = dao.getById(profile.id) ?: return@withContext false
        currentCoroutineContext().ensureActive()
        if (!current.sameGatewayIdentity(source)) return@withContext false
        val stamped = stamp(current, server.host)
        currentCoroutineContext().ensureActive()
        if (stamped) cookies.flush()
        stamped
    }

    suspend fun submitReport(profile: Profile, reportJson: String): String = withContext(Dispatchers.IO) {
        currentCoroutineContext().ensureActive()
        val urlError = if (profile.kind == KernelKind.REMOTE_URL) urlAdmissionError(profile.baseUrl) else null
        if (urlError != null) return@withContext if (urlError == ServerUrl.INVALID_URL_ERROR)
            "Can't send: invalid server URL"
        else "Can't send: edit the saved server URL to remove credentials first."
        val server = if (profile.kind == KernelKind.REMOTE_URL) ServerUrl.parse(profile.baseUrl) else runtimeServer(profile)
        if (server == null) return@withContext "Can't send: invalid server URL"
        if (!profileIsCurrent(profile)) return@withContext "Can't send: this server changed or was deleted. Return to the list."
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

    /** Candidate-compatible Room-first helper; only URL rows have saved URLs. */
    internal suspend fun persistUrlChange(profile: Profile, newUrl: String, expected: Profile = profile): Profile? =
        persistUrlChangeAndReconcile(
            profile, newUrl, expected, onCommitted = {},
            onIncomplete = { diagnostics?.record("E", "Auth", it) },
        )

    /**
     * The controller adds vault reconciliation to the SAME five-second budget
     * as readback and old-origin invalidation. A lost result can prove only a
     * changed save intent; unknown/newer state is preserved, never rolled back.
     * Callers serialize reviewed saves with [profileMutationMutex].
     */
    internal suspend fun persistUrlChangeAndReconcile(
        profile: Profile,
        newUrl: String,
        expected: Profile = profile,
        onCommitted: suspend (Profile) -> Unit,
        onIncomplete: (String) -> Unit,
    ): Profile? {
        currentCoroutineContext().ensureActive()
        if (profile.kind != KernelKind.REMOTE_URL || expected.kind != KernelKind.REMOTE_URL) return null
        val error = urlAdmissionError(newUrl)
        require(error == null) { error.orEmpty() }
        val newServer = checkNotNull(ServerUrl.parse(newUrl))
        val oldServer = ServerUrl.parse(expected.baseUrl)
            ?: ServerUrl.sanitizeForStorage(expected.baseUrl)?.let(ServerUrl::parse)
        val updated = profile.copy(
            baseUrl = newUrl.trim(), instanceUuid = newServer.instanceUuid ?: expected.instanceUuid,
            cookieHost = expected.cookieHost.takeIf { expected.baseUrl == newUrl.trim() && expected.authType == profile.authType },
        )
        var saved: Profile? = null
        var committed = false
        var returned = false
        try {
            currentCoroutineContext().ensureActive()
            committed = dao.saveConnectionIfCurrent(
                expected.id, expected.baseUrl, expected.alias, expected.authType, expected.projectPath,
                updated.alias, updated.baseUrl, updated.authType, updated.instanceUuid, updated.projectPath,
            ) > 0
            returned = true
        } finally {
            val complete = reconcileProfileSave(onIncomplete) {
                val row = dao.getById(expected.id)
                val confirmed = if (returned) committed else !expected.sameSaveIntent(updated)
                if (confirmed && row != null && row.sameSaveIntent(updated)) {
                    saved = row
                    if (oldServer != null && oldServer.origin != newServer.origin)
                        cookies.purgeOrigin(oldServer.origin, oldServer.raw)
                    onCommitted(row)
                }
            }
            if (returned && committed && saved == null && !complete)
                error("Connection saved; reconciliation incomplete. Return to the list and retry the edit.")
        }
        currentCoroutineContext().ensureActive()
        return saved.takeIf { committed }
    }

    private suspend fun reconcileProfileSave(onIncomplete: (String) -> Unit, action: suspend () -> Unit): Boolean =
        withContext(NonCancellable) {
            try {
                withTimeout(SAVE_RECONCILIATION_MS) { action() }
                true
            } catch (error: Exception) {
                onIncomplete("credential save reconciliation incomplete (${error.javaClass.simpleName}); retry the server edit")
                false
            }
        }

    private fun Profile.sameSaveIntent(other: Profile): Boolean = sameConnectionConfiguration(other)

    suspend fun updateUrlAndReauth(profile: Profile, newUrl: String): ReauthResult {
        currentCoroutineContext().ensureActive()
        if (profile.kind != KernelKind.REMOTE_URL) {
            if (newUrl.trim() != profile.baseUrl.trim()) return ReauthResult(
                LoginResult.Error("Kernel addresses are runtime state. Return to the list and reconnect."), profile,
            )
            return ReauthResult(login(profile), profile)
        }
        urlAdmissionError(newUrl)?.let { return ReauthResult(LoginResult.Error(it), profile) }
        val updated = profileMutationMutex.withLock {
            currentCoroutineContext().ensureActive()
            val current = dao.getById(profile.id) ?: return@withLock null
            currentCoroutineContext().ensureActive()
            if (!current.sameSaveIntent(profile)) return@withLock null
            persistUrlChange(current, newUrl)
        } ?: return ReauthResult(changedProfile(), profile)
        currentCoroutineContext().ensureActive()
        return ReauthResult(login(updated), updated)
    }

    internal companion object {
        const val SAVE_RECONCILIATION_MS = 5_000L
        private const val PROFILE_CHANGED_MESSAGE = "This server changed or was deleted. Return to the list and review it again."
        private const val SESSION_PROBE_PATH = "api/v4/conversations"
        private const val SESSION_COOKIE = "code-server-session"
        private const val MAX_GATEWAY_REDIRECTS = 8
        private const val MAX_RESPONSE_BYTES = NATIVE_HTTP_RESPONSE_MAX_BYTES
        private val REDIRECT_STATUSES = setOf(301, 302, 303, 307, 308)
        private val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()
        private fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
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
