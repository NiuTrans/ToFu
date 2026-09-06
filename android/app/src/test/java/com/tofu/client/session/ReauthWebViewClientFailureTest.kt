package com.tofu.client.session

import android.net.Uri
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

/**
 * Pins the main-frame failure routing that feeds the host's recovery overlay.
 * Behind the vscode tunnel the decisive cases are: the proxy edge's 5xx page
 * while the sandbox wakes MUST surface (the WebView's own error page is a dead
 * end on a phone), while sub-resource failures, aborted navigations and the
 * 401 re-auth path must NOT blanket the page with an overlay.
 *
 * Robolectric tier: only the shadow framework can hand out a real
 * [WebResourceResponse] with a working statusCode, and a [WebView] needs a
 * Context. WebResourceError cannot be faked at all (package-private
 * constructor), so the transport-error cases drive
 * [ReauthWebViewClient.routeMainFrameTransportError] directly.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class ReauthWebViewClientFailureTest {

    private class Harness(auth: AuthType = AuthType.CODE_SERVER_PASSWORD) {
        var reauthCount = 0
        var failure: String? = null
        val client = ReauthWebViewClient(
            Profile(
                id = 1L,
                alias = "dev",
                baseUrl = "https://host.example/proxy/15000/",
                authType = auth,
            ),
            onReauth = { reauthCount++ },
            onMainFrameFailure = { failure = it },
        )
    }

    private val mainUrl = "https://host.example/proxy/15000/"

    private fun webView(): WebView = WebView(RuntimeEnvironment.getApplication())

    private fun fakeRequest(url: String, mainFrame: Boolean): WebResourceRequest =
        object : WebResourceRequest {
            override fun getUrl(): Uri = Uri.parse(url)
            override fun isForMainFrame(): Boolean = mainFrame
            override fun isRedirect(): Boolean = false
            override fun hasGesture(): Boolean = false
            override fun getMethod(): String = "GET"
            override fun getRequestHeaders(): Map<String, String> = emptyMap()
        }

    private fun httpError(status: Int): WebResourceResponse = WebResourceResponse(
        "text/html", "utf-8", status, "HTTP $status", emptyMap(), null,
    )

    @Test
    fun `main frame 502 parks a waking-up failure`() {
        val h = Harness()
        h.client.onReceivedHttpError(webView(), fakeRequest(mainUrl, true), httpError(502))
        val reason = h.failure
        assertTrue(reason != null && reason.contains("waking up"))
        assertTrue(reason!!.contains("502"))
        assertEquals(0, h.reauthCount)
    }

    @Test
    fun `main frame 503 and 504 are also waking failures`() {
        for (status in listOf(503, 504)) {
            val h = Harness()
            h.client.onReceivedHttpError(webView(), fakeRequest(mainUrl, true), httpError(status))
            assertTrue(h.failure != null && h.failure!!.contains("waking up"))
        }
    }

    @Test
    fun `main frame 500 parks a generic failure`() {
        val h = Harness()
        h.client.onReceivedHttpError(webView(), fakeRequest(mainUrl, true), httpError(500))
        val reason = h.failure
        assertTrue(reason != null && reason.contains("HTTP 500"))
        assertFalse(reason!!.contains("waking up"))
    }

    @Test
    fun `sub-resource 5xx never reaches the overlay`() {
        val h = Harness()
        h.client.onReceivedHttpError(
            webView(),
            fakeRequest("https://host.example/proxy/15000/static/app.js", false),
            httpError(502),
        )
        assertNull(h.failure)
    }

    @Test
    fun `main frame 401 triggers re-auth and not the overlay`() {
        val h = Harness()
        h.client.onReceivedHttpError(webView(), fakeRequest(mainUrl, true), httpError(401))
        assertEquals(1, h.reauthCount)
        assertNull(h.failure)
    }

    @Test
    fun `interactive SSO 401 triggers neither re-auth nor the overlay`() {
        val h = Harness(AuthType.INTERACTIVE_SSO)
        h.client.onReceivedHttpError(webView(), fakeRequest(mainUrl, true), httpError(401))
        assertEquals(0, h.reauthCount)
        assertNull(h.failure)
    }

    @Test
    fun `main frame 404 renders the server's own page without the overlay`() {
        val h = Harness()
        h.client.onReceivedHttpError(webView(), fakeRequest(mainUrl, true), httpError(404))
        assertNull(h.failure)
        assertEquals(0, h.reauthCount)
    }

    @Test
    fun `main frame transport error parks the description`() {
        val h = Harness()
        h.client.routeMainFrameTransportError(
            true, -2, "net::ERR_CONNECTION_REFUSED", Uri.parse(mainUrl),
        )
        assertTrue(h.failure != null && h.failure!!.contains("ERR_CONNECTION_REFUSED"))
    }

    @Test
    fun `aborted navigation is not surfaced as a failure`() {
        val h = Harness()
        h.client.routeMainFrameTransportError(true, -1, "net::ERR_ABORTED", Uri.parse(mainUrl))
        assertNull(h.failure)
    }

    @Test
    fun `sub-resource transport error never reaches the overlay`() {
        val h = Harness()
        h.client.routeMainFrameTransportError(
            false, -2, "net::ERR_FAILED",
            Uri.parse("https://host.example/proxy/15000/static/app.js"),
        )
        assertNull(h.failure)
    }
}
