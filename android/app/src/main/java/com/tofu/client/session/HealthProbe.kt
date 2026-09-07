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
    private val http: OkHttpClient = defaultClient(),
    private val cookieProvider: (String) -> String? = { origin ->
        CookieManager.getInstance().getCookie(origin)
    },
) {

    data class Outcome(val verdict: TofuProbe.Verdict, val status: Int, val detail: String)

    /** Probe ``{serverUrl}/api/health`` once. Never throws. */
    fun probe(serverUrl: String): Outcome {
        val server = ServerUrl.parse(serverUrl)
            ?: return Outcome(TofuProbe.Verdict.UNREACHABLE, 0, "invalid URL")
        val base = server.httpUrl.toString().trimEnd('/')
        val builder = Request.Builder().url("$base/api/health").get()
        cookieProvider(server.origin)?.let { builder.header("Cookie", it) }
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
            Log.w(TAG, "health probe failed for $serverUrl: ${e.message}")
            Outcome(TofuProbe.Verdict.UNREACHABLE, 0, e.message ?: "network error")
        }
    }

    private companion object {
        const val TAG = "HealthProbe"

        fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(8, TimeUnit.SECONDS)
            .readTimeout(8, TimeUnit.SECONDS)
            // Follow redirects: a 302-to-login lands on the login PAGE (200
            // HTML, no bootId) and classifies NOT_TOFU — the honest reading.
            .followRedirects(true)
            .build()
    }
}
