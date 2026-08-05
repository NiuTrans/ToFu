package com.tofu.client.session

import android.os.Build
import android.util.Log
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile

/**
 * Detects session expiry inside the WebView and silently re-establishes it.
 *
 * The spike showed an unauthenticated request 302s to `…/login` (relative) and
 * sub-resources 401. We treat either as the re-auth trigger: run the headless
 * login again from the stored credential, re-inject the cookie, and reload.
 *
 * [onReauth] is invoked off the UI thread by the host; the host is responsible
 * for calling [SessionManager.login] and then [WebView.reload] on success, and
 * for calling [reauthSettled] when that attempt finishes (success OR failure).
 *
 * Gap-2: the in-flight latch clears on the observed OUTCOME ([reauthSettled]),
 * NOT on a fixed timer. A slow or failed re-auth must not silently re-open the
 * trigger and resume a redirect storm — the same observable-outcome rule the
 * frontend boot-reconnect path follows.
 */
class ReauthWebViewClient(
    private val profile: Profile,
    private val onReauth: (WebView) -> Unit,
    /**
     * Invoked when the WebView's RENDERER PROCESS dies (crash or low-memory
     * kill) — the classic "blank page after load" cause on a memory-constrained
     * device rendering a heavy page. The host decides recovery (e.g. drop back
     * to the profile list) instead of leaving a dead blank WebView on screen.
     */
    private val onRendererGone: ((crashed: Boolean) -> Unit)? = null,
    /**
     * Invoked after each main-frame load finishes. The host uses it to inject a
     * viewport-diagnostics probe (window.innerWidth / devicePixelRatio) so the
     * WebView-vs-Chrome breakpoint parity can be verified from logcat on a real
     * device. Optional — null in tests / when no probe is wanted.
     */
    private val onPageDone: ((WebView, String) -> Unit)? = null,
) : WebViewClient() {

    @Volatile private var reauthInFlight = false

    override fun onPageFinished(view: WebView, url: String) {
        onPageDone?.invoke(view, url)
    }

    override fun onReceivedHttpError(
        view: WebView,
        request: WebResourceRequest,
        errorResponse: WebResourceResponse,
    ) {
        if (request.isForMainFrame && errorResponse.statusCode == 401) {
            // Same reasoning as shouldOverrideUrlLoading: a headless re-login
            // cannot resolve an SSO gate, so triggering here would just latch
            // reauthInFlight and log noise while the user signs in.
            if (profile.authType == AuthType.INTERACTIVE_SSO) return
            trigger(view, "401 on main frame")
        }
    }

    /**
     * The renderer process died. If [detail.didCrash] is false it was killed by
     * the OS (usually low memory) — common when a WebView renders a very large
     * page on a constrained device. Returning true tells the framework we
     * HANDLED it, so the host app is NOT killed; we then hand off to recovery.
     * Without this override, a renderer death leaves a permanently blank WebView.
     */
    override fun onRenderProcessGone(
        view: WebView?,
        detail: RenderProcessGoneDetail?,
    ): Boolean {
        val crashed = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            detail?.didCrash() ?: false else false
        Log.e(TAG, "RENDERER GONE (${profile.alias}) didCrash=$crashed — " +
            "likely OOM/crash rendering a heavy page; recovering")
        onRendererGone?.invoke(crashed)
        return true
    }

    /** Log main-frame load failures (blank-page diagnostics). */
    override fun onReceivedError(
        view: WebView,
        request: WebResourceRequest,
        error: WebResourceError,
    ) {
        if (request.isForMainFrame) {
            val code = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
                error.errorCode else -1
            val desc = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
                error.description?.toString() else null
            Log.e(TAG, "main-frame load error (${profile.alias}) code=$code " +
                "desc=$desc url=${request.url}")
        }
    }

    override fun shouldOverrideUrlLoading(
        view: WebView,
        request: WebResourceRequest,
    ): Boolean {
        val url = request.url.toString()
        if (request.isForMainFrame && looksLikeLogin(url)) {
            // INTERACTIVE_SSO must NOT be intercepted. Its sign-in IS a sequence
            // of main-frame navigations through login pages, and a headless
            // re-login can never satisfy it (login() returns
            // NeedsInteractiveSso, so onReauth's `is Success` reload never
            // fires). Swallowing them leaves the WebView frozen on a blank
            // surface — the user is handed into the WebView and still cannot
            // sign in. Let the engine navigate; the user completes the flow.
            if (profile.authType == AuthType.INTERACTIVE_SSO) return false
            trigger(view, "redirect to login: $url")
            return true   // swallow the navigation; re-auth will reload
        }
        return false
    }

    private fun looksLikeLogin(url: String): Boolean =
        url.endsWith("/login") || url.contains("/login?") || url.contains("/login#")

    private fun trigger(view: WebView, reason: String) {
        if (reauthInFlight) return
        reauthInFlight = true
        Log.i(TAG, "re-auth trigger (${profile.alias}): $reason")
        // NOTE: we do NOT clear the latch here. The host clears it via
        // reauthSettled() once the login attempt resolves (success or failure),
        // so a slow/failed re-auth cannot re-open the trigger mid-flight.
        onReauth(view)
    }

    /**
     * Host signals that the re-auth attempt has finished (success OR failure).
     * Only then is the trigger re-armed. Called from the host after
     * [SessionManager.login] resolves and any reload is issued.
     */
    fun reauthSettled() {
        reauthInFlight = false
    }

    /** Test/inspection hook: whether a re-auth is currently latched in-flight. */
    fun isReauthInFlight(): Boolean = reauthInFlight

    private companion object {
        const val TAG = "ReauthWebViewClient"
    }
}
