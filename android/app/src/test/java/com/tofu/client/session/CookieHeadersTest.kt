package com.tofu.client.session

import okhttp3.Cookie
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Guards the cold-start-survival fix: an expiry-less SESSION cookie (exactly
 * what the gateway issues for `code-server-session`) must gain a `Max-Age` so a
 * plain WebView keeps it across process death.
 */
class CookieHeadersTest {

    private val host =
        "https://00000000-0000-4000-8000-000000000001-vscode-dc1.codelab.example.com/".toHttpUrl()

    /** The poisoned input: a session cookie with NO Max-Age/Expires. */
    private fun sessionCookie(): Cookie =
        Cookie.parse(host, "code-server-session=abc123; Path=/; SameSite=Lax")!!

    @Test
    fun sessionCookie_gets_maxAge_appended() {
        val c = sessionCookie()
        // Sanity: the parsed cookie really is non-persistent (no expiry).
        assertTrue("fixture must be a session cookie", !c.persistent)

        val header = CookieHeaders.toPersistentHeader(c)

        // NEUTER CHECK: if the `else` upgrade branch in toPersistentHeader is
        // removed (only emit Max-Age when hasExpiry), this assertion fails —
        // the session cookie would ship with no Max-Age and die on cold start.
        assertTrue("must append Max-Age: $header", header.contains("Max-Age="))
        assertTrue(header.contains("Max-Age=${CookieHeaders.PERSIST_SECONDS}"))
        assertTrue(header.startsWith("code-server-session=abc123"))
        assertTrue(header.contains("Path=/"))
    }

    @Test
    fun persistentCookie_keeps_its_own_remaining_lifetime() {
        val now = 1_000_000_000_000L
        val expires = now + 3_600_000L // +1h
        val c = Cookie.Builder()
            .name("code-server-session").value("v")
            .domain("00000000-0000-4000-8000-000000000001-vscode-dc1.codelab.example.com")
            .path("/")
            .expiresAt(expires)
            .build()

        val header = CookieHeaders.toPersistentHeader(c, nowMillis = now)
        // ~3600s remaining, NOT the 1-week default → proves we honour a real expiry.
        assertTrue("expected ~3600s: $header", header.contains("Max-Age=3600"))
    }

    @Test fun raw_cookie_preserves_scope_samesite_and_server_expiry() {
        val raw = "code-server-session=tok; Domain=.example.com; Path=/dev/; Secure; HttpOnly; SameSite=None"
        val saved = CookieHeaders.persistentResponseHeader(raw)
        org.junit.Assert.assertEquals("$raw; Max-Age=${CookieHeaders.PERSIST_SECONDS}", saved)
        for (expiry in listOf("Max-Age=0", "Expires=Wed, 21 Oct 2030 07:28:00 GMT")) {
            org.junit.Assert.assertEquals("$raw; $expiry", CookieHeaders.persistentResponseHeader("$raw; $expiry"))
        }
        val idp = "sso_session=secret; Path=/; HttpOnly; SameSite=None; Secure"
        org.junit.Assert.assertEquals(idp, CookieHeaders.persistentResponseHeader(idp))
    }

    @Test fun parsed_parent_domain_cookie_keeps_its_domain() {
        val c = Cookie.Builder().name("code-server-session").value("x").domain("example.com").path("/dev/").build()
        assertTrue(CookieHeaders.toPersistentHeader(c).contains("Domain=example.com"))
    }
}
