package com.tofu.client.session

import android.net.Uri
import android.net.http.SslError
import android.os.Build
import android.util.Log
import android.webkit.RenderProcessGoneDetail
import android.webkit.SslErrorHandler
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
    /**
     * Invoked when [MAX_CONSECUTIVE_REAUTH_FAILURES] headless re-logins fail
     * in a row — the session can no longer be re-established without the user
     * (password changed, gateway down). The host drops back to the profile
     * list instead of leaving a page that 401s forever.
     */
    private val onReauthExhausted: (() -> Unit)? = null,
    /**
     * TLS validation failed on a load. The host shows the user a proceed-once
     * dialog; this client NEVER proceeds on its own. Null = always cancel.
     */
    private val onSslError: ((handler: SslErrorHandler, error: SslError) -> Unit)? = null,
    /**
     * A URL the WebView should not load itself (external host, or a non-http
     * scheme like mailto:/tel:/intent:). The host opens it in the system
     * browser / handler app.
     */
    private val onExternalUrl: ((Uri) -> Unit)? = null,
    /**
     * The MAIN FRAME failed to load — a transport error, or the proxy edge's
     * own 5xx page (the vscode tunnel answers 502/503/504 while the sandbox
     * behind it is still booting). The WebView's built-in error page has no
     * retry affordance and the shell has no address bar, so without a native
     * recovery surface the user is stranded on a dead page. The host shows an
     * overlay with the reason + a Retry button. Sub-resource failures never
     * reach this — a failed avatar fetch must not blanket the page.
     */
    private val onMainFrameFailure: ((reason: String) -> Unit)? = null,
) : WebViewClient() {

    @Volatile private var reauthInFlight = false
    @Volatile private var consecutiveReauthFailures = 0

    override fun onPageFinished(view: WebView, url: String) {
        onPageDone?.invoke(view, url)
    }

    override fun onReceivedHttpError(
        view: WebView,
        request: WebResourceRequest,
        errorResponse: WebResourceResponse,
    ) {
        if (!request.isForMainFrame) return
        if (errorResponse.statusCode == 401) {
            // Same reasoning as shouldOverrideUrlLoading: a headless re-login
            // cannot resolve an SSO gate, so triggering here would just latch
            // reauthInFlight and log noise while the user signs in.
            if (profile.authType == AuthType.INTERACTIVE_SSO) return
            trigger(view, "401 on main frame")
            return
        }
        if (errorResponse.statusCode >= 500) {
            onMainFrameFailure?.invoke(describeHttpFailure(errorResponse.statusCode))
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

    /** Surface main-frame load failures to the host's recovery overlay. */
    override fun onReceivedError(
        view: WebView,
        request: WebResourceRequest,
        error: WebResourceError,
    ) {
        routeMainFrameTransportError(
            request.isForMainFrame,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
                error.errorCode else -1,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
                error.description?.toString() else null,
            request.url,
        )
    }

    /**
     * WebResourceError cannot be instantiated or subclassed in a unit test
     * (its constructor is package-private), so the routing decision lives
     * here on plain values; the override above is only the framework adapter.
     */
    internal fun routeMainFrameTransportError(
        isMainFrame: Boolean,
        code: Int,
        desc: String?,
        url: Uri?,
    ) {
        if (!isMainFrame) return
        Log.e(TAG, "main-frame load error (${profile.alias}) code=$code " +
            "desc=$desc url=$url")
        // ERR_ABORTED fires when a NEWER load interrupts this one (reload,
        // redirect chain) — it is not a failure the user must act on, and
        // surfacing it would flash the recovery overlay over the page that
        // is actively loading.
        if (desc?.contains("ERR_ABORTED") == true) return
        onMainFrameFailure?.invoke(describeTransportFailure(desc))
    }

    override fun shouldOverrideUrlLoading(
        view: WebView,
        request: WebResourceRequest,
    ): Boolean {
        val uri = request.url
        val url = uri.toString()
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
        val scheme = uri.scheme?.lowercase()
        if (scheme != null && scheme != "http" && scheme != "https") {
            // mailto:/tel:/intent: can only be handled by another app; loading
            // them in the WebView is a dead link with an error toast.
            onExternalUrl?.invoke(uri)
            return true
        }
        // A user-tapped link that leaves the server's own host belongs in the
        // real browser: the shell has no chrome (no address bar, no way back),
        // so loading it in place strands the user. Redirects (no gesture) and
        // INTERACTIVE_SSO IdP hops stay in place.
        if (request.isForMainFrame && request.hasGesture()
            && profile.authType != AuthType.INTERACTIVE_SSO
            && isExternalHost(uri)
        ) {
            onExternalUrl?.invoke(uri)
            return true
        }
        return false
    }

    /**
     * TLS validation failed. NEVER proceed silently — this app holds
     * credentials, and auto-proceed would make a hostile network indistinguish-
     * able from the self-hosted server it protects. Default is cancel(); the
     * host may surface a dialog and let the USER decide, once.
     */
    override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
        val callback = onSslError
        if (callback == null) handler.cancel() else callback(handler, error)
    }

    private fun isExternalHost(uri: Uri): Boolean {
        val own = ServerUrl.parse(profile.baseUrl)?.host ?: return false
        return uri.host != null && uri.host != own
    }

    private fun looksLikeLogin(url: String): Boolean =
        url.endsWith("/login") || url.contains("/login?") || url.contains("/login#")

    /**
     * Honest, actionable text for a main-frame HTTP error page. The vscode
     * proxy's 502/503/504 means "the sandbox behind the tunnel is still
     * waking up" — the fix is to wait and retry, not to edit anything.
     */
    private fun describeHttpFailure(status: Int): String =
        if (TofuProbe.isWakingStatus(status)) {
            "The proxy answered HTTP $status — the sandbox behind the tunnel is " +
                "still waking up. It usually comes up within half a minute; " +
                "retry in a few seconds."
        } else {
            "The server answered HTTP $status instead of the page."
        }

    private fun describeTransportFailure(desc: String?): String =
        if (desc != null) {
            "The page couldn't be loaded ($desc). Check the tunnel is still " +
                "forwarding, then retry."
        } else {
            "The page couldn't be loaded. Check the tunnel is still forwarding, " +
                "then retry."
        }

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
     * The page itself detected the gateway session died (its API calls came
     * back with the edge's bare 401) and asked for a re-login through the
     * TofuNative bridge. Routed through the same latch + headless login as an
     * in-WebView trigger; the JS side rate-limits bursts.
     */
    fun requestReauth(view: WebView, reason: String) {
        if (profile.authType == AuthType.INTERACTIVE_SSO) return
        trigger(view, "page-requested re-auth: $reason")
    }

    /**
     * Host signals that the re-auth attempt has finished (success OR failure).
     * Only then is the trigger re-armed. Called from the host after
     * [SessionManager.login] resolves and any reload is issued.
     *
     * [succeeded] feeds the consecutive-failure cap: an expired password (or
     * a dead gateway) would otherwise retry forever behind the latch, burning
     * the tunnel and, on some gates, marching toward account lockout. After
     * [MAX_CONSECUTIVE_REAUTH_FAILURES] the host is told to give up.
     */
    fun reauthSettled(succeeded: Boolean) {
        reauthInFlight = false
        if (succeeded) {
            consecutiveReauthFailures = 0
            return
        }
        consecutiveReauthFailures += 1
        if (consecutiveReauthFailures >= MAX_CONSECUTIVE_REAUTH_FAILURES) {
            Log.e(TAG, "re-auth failed $consecutiveReauthFailures times in a row " +
                "(${profile.alias}); giving up instead of storming the login endpoint")
            consecutiveReauthFailures = 0
            onReauthExhausted?.invoke()
        }
    }

    /** Test/inspection hook: whether a re-auth is currently latched in-flight. */
    fun isReauthInFlight(): Boolean = reauthInFlight

    private companion object {
        const val TAG = "ReauthWebViewClient"
        const val MAX_CONSECUTIVE_REAUTH_FAILURES = 3
    }
}
