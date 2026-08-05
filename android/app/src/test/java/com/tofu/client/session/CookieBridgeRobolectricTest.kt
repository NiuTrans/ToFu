package com.tofu.client.session

import android.webkit.CookieManager
import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import okhttp3.HttpUrl.Companion.toHttpUrl
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
        client.reauthSettled()
        assertFalse("latch must clear on settled", client.isReauthInFlight())
        assertTrue("exactly one re-auth fired despite two redirects", reauthCalls == 1)
    }

    private fun fakeMainFrameRequest(url: String): android.webkit.WebResourceRequest {
        return object : android.webkit.WebResourceRequest {
            override fun getUrl() = android.net.Uri.parse(url)
            override fun isForMainFrame() = true
            override fun isRedirect() = true
            override fun hasGesture() = false
            override fun getMethod() = "GET"
            override fun getRequestHeaders() = emptyMap<String, String>()
        }
    }
}
