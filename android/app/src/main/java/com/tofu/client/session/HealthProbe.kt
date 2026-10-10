package com.tofu.client.session

import android.util.Log
import android.webkit.CookieManager
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/**
 * The network half of [TofuProbe]: a single ``GET {base}/api/health`` carrying
 * the profile's session cookie when one is in the jar.
 *
 * The cookie is not optional decoration: behind a VS Code port-forwarding
 * proxy the edge 401s EVERY cookie-less request (the desktop agent's
 * never-reached-Tofu incident), so a probe without the session measures the
 * GATE, not the server. With the cookie attached, the verdicts read true:
 * 200+bootId is Tofu up, a Tofu-envelope 401 is Tofu's own auth, anything
 * else refused is the proxy edge.
 */
class HealthProbe(
    http: OkHttpClient = defaultClient(),
    private val diagnostics: DiagLog? = null,
    private val cookieProvider: (String) -> String? = { origin ->
        CookieManager.getInstance().getCookie(origin)
    },
) {
    private val http = DiagnosticHttp.attach(http, diagnostics, "HealthHttp").newBuilder()
        .followRedirects(false).followSslRedirects(false).build()

    data class Outcome(val verdict: TofuProbe.Verdict, val status: Int, val detail: String)

    /** Probe ``{serverUrl}/api/health`` once. Never throws. */
    fun probe(serverUrl: String): Outcome {
        val server = ServerUrl.parse(serverUrl)
            ?: return Outcome(TofuProbe.Verdict.UNREACHABLE, 0, "invalid URL")
        val healthUrl = server.endpoint("api/health")
        val builder = Request.Builder().url(healthUrl).get()
        cookieProvider(healthUrl)?.let { builder.header("Cookie", it) }
        return try {
            http.newCall(builder.build()).execute().use { resp ->
                val body = resp.body?.string()
                Outcome(
                    TofuProbe.classify(resp.code, body),
                    resp.code,
                    body.orEmpty().take(200),
                )
            }
        } catch (e: Exception) {
            diagnostics?.exception("Health", e)
            Log.w(TAG, "health probe failed: ${e.javaClass.simpleName}")
            Outcome(TofuProbe.Verdict.UNREACHABLE, 0, e.message ?: "network error")
        }
    }

    private companion object {
        const val TAG = "HealthProbe"

        fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(8, TimeUnit.SECONDS)
            .readTimeout(8, TimeUnit.SECONDS)
            // A login redirect is a gateway verdict. Never forward the explicit
            // Cookie header to a foreign host while probing.
            .followRedirects(false)
            .build()
    }
}
