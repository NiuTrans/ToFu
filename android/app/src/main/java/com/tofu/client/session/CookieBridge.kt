package com.tofu.client.session

import android.webkit.CookieManager
import android.webkit.WebStorage
import okhttp3.Cookie

/**
 * Production [CookieSink]: bridges an OkHttp-obtained session cookie into the
 * WebView [CookieManager].
 *
 * Two spike-driven behaviours are load-bearing:
 *  1. **Persistence upgrade** — the gateway issues `code-server-session` with NO
 *     `Max-Age`/`Expires` (a session cookie the WebView drops on cold start), so
 *     [CookieHeaders.toPersistentHeader] appends a `Max-Age`.
 *  2. **flush() is mandatory** — [CookieManager] persists lazily; without an
 *     explicit flush a cold kill loses even a persistent cookie.
 *
 * [purgeHost] implements the URL-change hard-invalidation: the cookie is
 * `Domain`-pinned to the full host, so a re-provisioned sandbox's new host must
 * NOT inherit the dead host's jar.
 */
object CookieBridge : CookieSink {

    private fun manager(): CookieManager = CookieManager.getInstance().apply {
        setAcceptCookie(true)
    }

    override fun inject(origin: String, cookies: List<Cookie>) {
        val cm = manager()
        for (c in cookies) {
            cm.setCookie(origin, CookieHeaders.toPersistentHeader(c))
        }
        cm.flush()
    }

    override fun cookieHeader(origin: String): String? =
        manager().getCookie(origin)?.takeIf { it.isNotBlank() }

    override fun purgeHost(host: String) {
        val cm = manager()
        val origin = "https://$host"
        val existing = cm.getCookie(origin)
        if (existing != null) {
            existing.split(';')
                .mapNotNull { it.substringBefore('=', "").trim().ifEmpty { null } }
                .forEach { name ->
                    cm.setCookie(origin, "$name=; Max-Age=0; Path=/")
                }
        }
        cm.flush()
        // Gap-4: also drop per-host web storage. Tofu caches conversations in
        // IndexedDB/localStorage keyed by origin; a dead re-provisioned host's
        // stale storage must not linger. deleteOrigin covers localStorage,
        // IndexedDB, WebSQL, and AppCache for the origin.
        WebStorage.getInstance().deleteOrigin(origin)
    }
}
