package com.tofu.client.session

import android.util.Log
import android.webkit.CookieManager
import com.tofu.client.data.Profile
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Client for the host-side supervisor daemon (see supervisor.py), which the app
 * uses to START and STOP the Tofu server for a profile's [Profile.projectPath].
 *
 * Reachability: the supervisor sits behind the SAME code-server that proxies
 * Tofu, on a sibling proxied port (15000 → 15001), so it inherits the
 * `code-server-session` cookie the profile login already established. No extra
 * auth token — Tofu is a personal app and the code-server password already
 * gates the whole proxy; this class just reuses that cookie.
 *
 * The HTTP-free URL/endpoint logic lives in [SupervisorUrl] so it is
 * unit-testable without a device.
 */
class SupervisorClient(
    private val http: OkHttpClient = defaultClient(),
    private val cookieProvider: (String) -> String? = { origin ->
        CookieManager.getInstance().getCookie(origin)
    },
) {

    sealed interface Result {
        /** running state after the call (start/stop/status all report it). */
        data class Ok(val running: Boolean, val body: JSONObject) : Result
        data class Failed(val code: Int, val message: String) : Result
    }

    /** GET /status — authoritative running state (used for polling after start). */
    fun status(profile: Profile): Result =
        call(profile, SupervisorUrl.STATUS, method = "GET")

    /** POST /start — idempotent; returns immediately, caller polls [status]. */
    fun start(profile: Profile): Result =
        call(profile, SupervisorUrl.START, method = "POST")

    /** POST /stop — runs the project's stop.sh via the supervisor. */
    fun stop(profile: Profile): Result =
        call(profile, SupervisorUrl.STOP, method = "POST")

    private fun call(profile: Profile, endpoint: String, method: String): Result {
        val projectPath = profile.projectPath
        if (projectPath.isNullOrBlank()) {
            return Result.Failed(0, "no project path configured")
        }
        val base = SupervisorUrl.fromServerUrl(profile.baseUrl)
            ?: return Result.Failed(0, "cannot derive supervisor URL from ${profile.baseUrl}")
        val url = SupervisorUrl.endpoint(base, endpoint,
            if (method == "GET") projectPath else null)

        val builder = Request.Builder().url(url)
        // Reuse the profile's live code-server session cookie (same host) —
        // the only gate the supervisor relies on.
        cookieProvider(base.origin)?.let { builder.header("Cookie", it) }

        if (method == "POST") {
            val payload = JSONObject().put("projectPath", projectPath).toString()
            builder.post(payload.toRequestBody("application/json".toMediaType()))
        } else {
            builder.get()
        }

        return try {
            http.newCall(builder.build()).execute().use { resp ->
                val text = resp.body?.string().orEmpty()
                val json = try {
                    if (text.isNotBlank()) JSONObject(text) else JSONObject()
                } catch (e: Exception) {
                    Log.w(TAG, "non-JSON supervisor response: $e")
                    JSONObject()
                }
                if (resp.isSuccessful && json.optBoolean("ok", false)) {
                    Result.Ok(running = json.optBoolean("running", false), body = json)
                } else {
                    Result.Failed(resp.code, json.optString("error", "HTTP ${resp.code}"))
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "supervisor $endpoint failed: $e")
            Result.Failed(0, e.message ?: "network error")
        }
    }

    companion object {
        private const val TAG = "SupervisorClient"

        fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(40, TimeUnit.SECONDS)   // stop.sh graceful window can be ~12s+
            .build()
    }
}
