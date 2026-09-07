package com.tofu.client.session

import okhttp3.Cookie

/**
 * Seam over the WebView cookie jar so session logic is unit-testable without an
 * Android runtime. Production impl is [CookieBridge]; tests supply a fake.
 */
interface CookieSink {
    /** Inject [cookies] for [origin] (scheme://host), persisting them. */
    fun inject(origin: String, cookies: List<Cookie>)

    /** Hard-invalidate every cookie pinned to [host] (Domain-pinned re-provision path). */
    fun purgeHost(host: String)

    /**
     * The raw `Cookie:` header the jar holds for [origin], or null when empty.
     *
     * Needed by the INTERACTIVE_SSO path: that login happens inside the WebView,
     * so no OkHttp response ever passes through [inject] and nothing would
     * otherwise stamp `cookieHost`. Reading the jar back is the only way to
     * observe that an interactive sign-in actually succeeded.
     */
    fun cookieHeader(origin: String): String?
}
