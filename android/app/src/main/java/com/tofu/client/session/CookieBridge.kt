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

    override fun injectResponseCookies(url: String, headers: List<String>) {
        if (headers.isEmpty()) return
        val parsed = ServerUrl.parse(url) ?: return
        val cm = manager()
        for (raw in headers) {
            if (Cookie.parse(parsed.httpUrl, raw) != null) {
                cm.setCookie(url, CookieHeaders.persistentResponseHeader(raw))
            }
        }
        cm.flush()
    }

    override fun flush() = manager().flush()

    override fun purgeHost(host: String) = purgeOrigin("https://$host", "https://$host/")

    override fun purgeOrigin(origin: String, baseUrl: String) {
        val server = ServerUrl.parse(baseUrl) ?: return
        val cm = manager()
        // CookieManager has no cookie enumeration API. Expire host-scoped
        // cookies on every ancestor path of the old profile, including login.
        // Parent-domain IdP cookies are shared and remain owned by the IdP.
        val paths = mutableSetOf("/")
        var path = server.httpUrl.encodedPath.trimEnd('/')
        while (path.isNotEmpty()) {
            paths += path; paths += "$path/"
            path = path.substringBeforeLast('/', "")
        }
        val urls = paths.map { server.httpUrl.newBuilder().encodedPath(it).query(null).fragment(null).build().toString() }
        val names = urls.flatMap { cm.getCookie(it).orEmpty().split(';') }
            .mapNotNull { it.substringBefore('=', "").trim().ifEmpty { null } }.toSet()
        for (name in names) for (cookiePath in paths) {
            cm.setCookie(origin, "$name=; Max-Age=0; Path=$cookiePath")
            cm.setCookie(origin, "$name=; Max-Age=0; Path=$cookiePath; Domain=${server.host}")
        }
        cm.flush()
        WebStorage.getInstance().deleteOrigin(origin)
    }
}
