package com.tofu.client.session

import android.webkit.CookieManager
import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Robolectric tier: exercises the Android-runtime files on the JVM (no device)
 * against Robolectric's shadow framework.
 *
 *  - [CookieBridge] against the shadow [CookieManager]: inject() writes a
 *    persistent (Max-Age-bearing) cookie; purgeHost() clears it.
 *  - [ReauthWebViewClient] Gap-2: the in-flight latch clears on the observed
 *    outcome ([ReauthWebViewClient.reauthSettled]), not a timer.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class CookieBridgeRobolectricTest {

    private val host = "abc12345-vscode-dc1.codelab.example.com"
    private val origin = "https://$host"

    private fun sessionCookie() =
        okhttp3.Cookie.parse("$origin/".toHttpUrl(), "code-server-session=abc123; Path=/")!!

    @Test
    fun inject_writes_persistent_cookie_into_jar() {
        CookieBridge.inject(origin, listOf(sessionCookie()))

        val stored = CookieManager.getInstance().getCookie(origin)
        assertTrue("cookie must be present after inject: $stored",
            stored != null && stored.contains("code-server-session=abc123"))
    }

    @Test
    fun purgeHost_clears_the_jar() {
        CookieBridge.inject(origin, listOf(sessionCookie()))
        CookieBridge.purgeHost(host)

        val stored = CookieManager.getInstance().getCookie(origin)
        // After expiry the shadow jar returns null or an empty string for the host.
        assertTrue("cookie must be gone after purge: $stored",
            stored == null || !stored.contains("code-server-session=abc123"))
    }

    @Test
    fun reauth_latch_clears_only_on_settled_not_by_time() {
        val profile = Profile(
            id = 1, alias = "dc1", baseUrl = "$origin/proxy/15000/",
            authType = AuthType.CODE_SERVER_PASSWORD,
        )
        var reauthCalls = 0
        val client = ReauthWebViewClient(profile, onReauth = { reauthCalls++ })
        val webView = android.webkit.WebView(
            org.robolectric.RuntimeEnvironment.getApplication()
        )

        // First login-redirect navigation triggers exactly one re-auth and latches.
        val req = fakeMainFrameRequest("$origin/login")
        client.shouldOverrideUrlLoading(webView, req)
        assertTrue("must latch in-flight after trigger", client.isReauthInFlight())

        // A SECOND redirect while still in-flight must NOT trigger again (storm guard).
        client.shouldOverrideUrlLoading(webView, req)
        assertTrue("still latched", client.isReauthInFlight())

        // Only the observed OUTCOME clears the latch — NEUTER: if reauthSettled()
        // were a no-op (or a timer cleared it), this would stay latched / or the
        // second trigger above would have re-fired.
        client.reauthSettled(true)
        assertFalse("latch must clear on settled", client.isReauthInFlight())
        assertTrue("exactly one re-auth fired despite two redirects", reauthCalls == 1)
    }

    @Test
    fun reauth_failures_trip_the_exhaustion_cap() {
        val profile = Profile(
            id = 1, alias = "dc1", baseUrl = "$origin/proxy/15000/",
            authType = AuthType.CODE_SERVER_PASSWORD,
        )
        var exhausted = 0
        val client = ReauthWebViewClient(
            profile,
            onReauth = {},
            onReauthExhausted = { exhausted++ },
        )

        client.reauthSettled(false)
        client.reauthSettled(false)
        assertEquals("below the cap: no exhaustion", 0, exhausted)
        client.reauthSettled(false)
        assertEquals("3 consecutive failures must trip the cap", 1, exhausted)
        // Tripping resets the streak: the next TWO failures stay below it.
        client.reauthSettled(false)
        client.reauthSettled(false)
        assertEquals("streak must reset after tripping", 1, exhausted)
        client.reauthSettled(false)
        assertEquals(2, exhausted)
    }

    @Test
    fun reauth_success_resets_the_failure_streak() {
        val profile = Profile(
            id = 1, alias = "dc1", baseUrl = "$origin/proxy/15000/",
            authType = AuthType.CODE_SERVER_PASSWORD,
        )
        var exhausted = 0
        val client = ReauthWebViewClient(
            profile,
            onReauth = {},
            onReauthExhausted = { exhausted++ },
        )

        client.reauthSettled(false)
        client.reauthSettled(false)
        client.reauthSettled(true)   // a win clears the streak
        client.reauthSettled(false)
        client.reauthSettled(false)
        assertEquals("success mid-streak must reset the cap countdown", 0, exhausted)
    }

    @Test
    fun external_navigation_leaves_the_webview() {
        val profile = Profile(
            id = 1, alias = "dc1", baseUrl = "$origin/proxy/15000/",
            authType = AuthType.CODE_SERVER_PASSWORD,
        )
        val opened = mutableListOf<android.net.Uri>()
        val client = ReauthWebViewClient(
            profile,
            onReauth = {},
            onExternalUrl = { opened += it },
        )
        val webView = android.webkit.WebView(
            org.robolectric.RuntimeEnvironment.getApplication()
        )

        // A user-TAPPED link to a foreign host: the shell has no chrome (no
        // address bar, no way back), so it goes to the real browser.
        assertTrue(client.shouldOverrideUrlLoading(
            webView, fakeMainFrameRequest("https://example.com/x", gesture = true)))
        assertEquals(listOf(android.net.Uri.parse("https://example.com/x")), opened)

        // Same-host tap stays in place.
        assertFalse(client.shouldOverrideUrlLoading(
            webView, fakeMainFrameRequest("$origin/proxy/15000/page", gesture = true)))

        // A foreign-host navigation WITHOUT a gesture is a redirect (SSO hop
        // etc.) and must not be yanked out from under the page.
        assertFalse(client.shouldOverrideUrlLoading(
            webView, fakeMainFrameRequest("https://example.com/x", gesture = false)))

        // Non-http schemes (mailto:/tel:) can only be handled by another app.
        assertTrue(client.shouldOverrideUrlLoading(
            webView, fakeMainFrameRequest("mailto:a@b.c", gesture = false)))
        assertEquals(2, opened.size)
    }

    private fun fakeMainFrameRequest(
        url: String,
        gesture: Boolean = false,
    ): android.webkit.WebResourceRequest {
        return object : android.webkit.WebResourceRequest {
            override fun getUrl() = android.net.Uri.parse(url)
            override fun isForMainFrame() = true
            override fun isRedirect() = !gesture
            override fun hasGesture() = gesture
            override fun getMethod() = "GET"
            override fun getRequestHeaders() = emptyMap<String, String>()
        }
    }
}
