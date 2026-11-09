package com.tofu.client.session

import android.webkit.CookieManager
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import okhttp3.CookieJar
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import java.io.Closeable
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Single health request with exact-endpoint cookies, native body bounds and no redirects.
 * The remote review form rejects reserved hosts before calling this owner. LOCAL intentionally
 * supplies its own cookieProvider and retains the synchronous probe for installed-session proof.
 */
class HealthProbe(
    http: OkHttpClient? = null,
    private val diagnostics: DiagLog? = null,
    private val cookieProvider: (String) -> String? = { endpoint ->
        if (LocalKernelCookie.isReservedUrl(endpoint)) null else CookieManager.getInstance().getCookie(endpoint)
    },
) : Closeable {
    private val ownsHttp = http == null
    private val closed = AtomicBoolean(false)
    private val http = DiagnosticHttp.attach(http ?: defaultClient(), diagnostics, "HealthHttp").newBuilder()
        .followRedirects(false).followSslRedirects(false).cookieJar(CookieJar.NO_COOKIES)
        .callTimeout(HEALTH_DEADLINE_SECONDS, TimeUnit.SECONDS).build()

    data class Outcome(val verdict: TofuProbe.Verdict, val status: Int, val detail: String)

    private fun request(serverUrl: String): Request? {
        if (closed.get() || ServerUrl.storageError(serverUrl) != null) return null
        val server = ServerUrl.parse(serverUrl) ?: return null
        val endpoint = server.endpoint("api/health")
        return Request.Builder().url(endpoint).get().apply {
            header("Accept", "application/json")
            cookieProvider(endpoint)?.takeIf { it.isNotBlank() }?.let { header("Cookie", it) }
        }.build()
    }

    /** The local adapter's synchronous seam keeps its explicitly injected credential path. */
    fun probe(serverUrl: String): Outcome = try {
        val request = request(serverUrl)
        if (request == null) unavailable() else http.newCall(request).execute().use(::outcome)
    } catch (error: CancellationException) { throw error }
      catch (error: Exception) { failed(error) }

    /** Form cancellation owns the actual socket throughout the bounded body read. */
    suspend fun probeCancellable(serverUrl: String): Outcome = try {
        currentCoroutineContext().ensureActive()
        val request = request(serverUrl)
        if (request == null) unavailable() else http.newCall(request).awaitBounded(MAX_RESPONSE_BYTES).use(::outcome)
    } catch (error: CancellationException) { throw error }
      catch (error: Exception) { failed(error) }

    private fun outcome(response: Response): Outcome {
        val body = response.readBoundedBody(MAX_RESPONSE_BYTES)
        if (body == null) return unavailable(response.code)
        // Server-controlled bodies and exception messages can themselves contain credentials.
        // Classification consumes the body; diagnostic/UI details expose only status/verdict.
        val verdict = TofuProbe.classify(response.code, body)
        return Outcome(verdict, response.code, "HTTP ${response.code}: ${verdict.name}")
    }

    private fun failed(error: Exception): Outcome {
        diagnostics?.record("W", "Health", "health probe failed: ${error.javaClass.simpleName}")
        return unavailable()
    }

    private fun unavailable(status: Int = 0) = Outcome(TofuProbe.Verdict.UNREACHABLE, status, "Health could not be verified")

    /** A supplied client's dispatcher/pool remains caller-owned. */
    override fun close() {
        if (closed.compareAndSet(false, true) && ownsHttp) {
            http.dispatcher.cancelAll()
            http.connectionPool.evictAll()
            http.dispatcher.executorService.shutdown()
        }
    }

    private companion object {
        // Native authentication/health correctness contract: 256 KiB and the existing 8s health deadline.
        const val MAX_RESPONSE_BYTES = NATIVE_HTTP_RESPONSE_MAX_BYTES
        const val HEALTH_DEADLINE_SECONDS = 8L
        fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(HEALTH_DEADLINE_SECONDS, TimeUnit.SECONDS)
            .readTimeout(HEALTH_DEADLINE_SECONDS, TimeUnit.SECONDS)
            .callTimeout(HEALTH_DEADLINE_SECONDS, TimeUnit.SECONDS)
            .followRedirects(false).followSslRedirects(false).cookieJar(CookieJar.NO_COOKIES).build()
    }
}
