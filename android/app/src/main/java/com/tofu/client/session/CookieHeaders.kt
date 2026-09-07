package com.tofu.client.session

import okhttp3.Cookie

/**
 * Pure (Android-free) Set-Cookie header formatting. Extracted from
 * [CookieBridge] so the cold-start-survival upgrade is unit-testable on the JVM.
 */
object CookieHeaders {

    /** One week; long enough to avoid re-login churn, short enough to bound staleness. */
    const val PERSIST_SECONDS = 7 * 24 * 60 * 60

    /**
     * Serialise an OkHttp [Cookie] to a Set-Cookie header string.
     *
     * The load-bearing behaviour: a cookie WITHOUT a real expiry (a session
     * cookie — exactly what the gateway issues for `code-server-session`) gets a
     * `Max-Age` appended so the WebView keeps it across a cold start. A cookie
     * that already carries a far-future expiry keeps its own remaining lifetime.
     */
    fun toPersistentHeader(c: Cookie, nowMillis: Long = System.currentTimeMillis()): String {
        val sb = StringBuilder()
        sb.append(c.name).append('=').append(c.value)
        sb.append("; Path=").append(c.path)
        val hasExpiry = c.persistent && c.expiresAt < Long.MAX_VALUE
        if (hasExpiry) {
            sb.append("; Max-Age=").append((c.expiresAt - nowMillis) / 1000)
        } else {
            sb.append("; Max-Age=").append(PERSIST_SECONDS)
        }
        if (c.secure) sb.append("; Secure")
        if (c.httpOnly) sb.append("; HttpOnly")
        sb.append("; SameSite=Lax")   // top-level navigation → Lax is correct
        return sb.toString()
    }
}
