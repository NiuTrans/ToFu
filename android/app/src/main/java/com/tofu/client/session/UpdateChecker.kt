package com.tofu.client.session

import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import okhttp3.CookieJar
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Health is public inside Tofu, not necessarily outside the selected gateway.
 * Select cookies for the full endpoint, disable redirects/automatic cookie jars
 * and reuse authentication's bounded decoded-body/cancellable socket boundary.
 */
class UpdateProbe(
    http: OkHttpClient? = null,
    private val diagnostics: DiagLog? = null,
    private val cookies: CookieSink = CookieBridge,
) : java.io.Closeable {
    private val ownsHttp = http == null
    private val closed = AtomicBoolean(false)
    private val http = DiagnosticHttp.attach(http ?: defaultClient(), diagnostics, "UpdateHttp").newBuilder()
        .followRedirects(false).followSslRedirects(false).cookieJar(CookieJar.NO_COOKIES)
        .callTimeout(PROBE_DEADLINE_SECONDS, TimeUnit.SECONDS).build()

    private fun request(baseUrl: String): Request? {
        // Refuse legacy URL rows on either credential host before reading the
        // WebView jar or creating a call. Cookies ignore ports and the WebView
        // canonicalizes alternative IPv4 spellings, unlike OkHttp.
        if (!acceptsRemoteUrl(baseUrl)) return null
        val server = ServerUrl.parse(baseUrl) ?: return null
        val endpoint = server.endpoint("api/health")
        return Request.Builder().url(endpoint).get().apply {
            header("Accept", "application/json")
            cookies.cookieHeader(endpoint)?.takeIf { it.isNotBlank() }?.let { header("Cookie", it) }
        }.build()
    }

    /** Synchronous test seam; production uses the cancellable variant. */
    fun advertised(baseUrl: String): AdvertisedClient? = try {
        val request = request(baseUrl)
        if (request == null) null else http.newCall(request).execute().use(::advertisement)
    } catch (error: CancellationException) { throw error }
      catch (error: Exception) { failed(error); null }

    suspend fun advertisedCancellable(baseUrl: String): AdvertisedClient? = try {
        currentCoroutineContext().ensureActive()
        val request = request(baseUrl)
        if (request == null) null else http.newCall(request).awaitBounded(MAX_RESPONSE_BYTES).use(::advertisement)
    } catch (error: CancellationException) { throw error }
      catch (error: Exception) { failed(error); null }

    private fun advertisement(response: Response): AdvertisedClient? {
        if (!response.isSuccessful) return null
        val type = response.body?.contentType()?.subtype ?: return null
        if (type != "json" && !type.endsWith("+json")) return null
        val body = response.readBoundedBody(MAX_RESPONSE_BYTES)?.trim() ?: return null
        if (!body.startsWith('{') || !body.endsWith('}')) return null
        return UpdateCheck.parseAdvertised(body)
    }

    private fun failed(error: Exception) {
        diagnostics?.record("W", "Update", "health probe failed: ${error.javaClass.simpleName}")
    }

    /** Injected clients remain their caller's responsibility. */
    override fun close() {
        if (ownsHttp && closed.compareAndSet(false, true)) {
            http.dispatcher.cancelAll()
            http.connectionPool.evictAll()
            http.dispatcher.executorService.shutdown()
        }
    }

    companion object {
        private const val MAX_RESPONSE_BYTES = NATIVE_HTTP_RESPONSE_MAX_BYTES
        private const val PROBE_DEADLINE_SECONDS = 8L
        private const val SSH_FORWARD_COOKIE_ADDRESS = 0x7F00_0002L

        internal fun acceptsRemoteUrl(baseUrl: String): Boolean {
            if (ServerUrl.storageError(baseUrl) != null) return false
            val server = ServerUrl.parse(baseUrl) ?: return false
            return !LocalKernelCookie.isReservedHost(server.host) &&
                LocalKernelCookie.ipv4(server.host.lowercase()) != SSH_FORWARD_COOKIE_ADDRESS
        }

        fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(4, TimeUnit.SECONDS).readTimeout(4, TimeUnit.SECONDS)
            .callTimeout(PROBE_DEADLINE_SECONDS, TimeUnit.SECONDS)
            .followRedirects(false).followSslRedirects(false).cookieJar(CookieJar.NO_COOKIES).build()
    }
}

/** Only saved REMOTE_URL servers advertise updates; failed/unsafe rows remain unknown. */
class UpdateChecker(
    private val installedName: String,
    private val probe: suspend (String) -> AdvertisedClient? = UpdateProbe()::advertisedCancellable,
) {
    suspend fun check(profiles: List<Profile>): UpdateStatus? = withContext(Dispatchers.IO) {
        currentCoroutineContext().ensureActive()
        val remote = profiles.filter { it.kind == KernelKind.REMOTE_URL }
        if (remote.isEmpty()) return@withContext null
        val advertised = coroutineScope {
            remote.map { profile ->
                async {
                    try { if (UpdateProbe.acceptsRemoteUrl(profile.baseUrl)) probe(profile.baseUrl) else null }
                    catch (error: CancellationException) { throw error }
                    catch (_: Exception) { null }
                }
            }.awaitAll()
        }
        currentCoroutineContext().ensureActive()
        val best = advertised.filterNotNull().maxWithOrNull { a, b ->
            UpdateCheck.compareVersions(a.version, b.version)
        } ?: return@withContext null
        if (advertised.any { it == null } && UpdateCheck.compareVersions(best.version, installedName) <= 0)
            return@withContext null // Partial knowledge cannot prove the whole set is up to date.
        UpdateCheck.evaluate(installedName, best)
    }
}
