package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile

/**
 * Pure handoff and completion rules. Page URL + cookies grant only candidate
 * status; SessionManager must verify a real endpoint before stamping the DAO.
 * Entry points: profileForWebView, completedSignIn. No Android dependencies.
 */
object InteractiveSso {

    /**
     * Whether a login outcome must hand the user into the WebView.
     *
     * True for [LoginResult.Success] (session replayed headlessly — go straight
     * to the page) AND for [LoginResult.NeedsInteractiveSso], whose WHOLE POINT
     * is that the sign-in can only be completed interactively. Treating the
     * latter as a mere status message is what stranded SSO users on the list
     * behind an "opening…" label that never opened anything.
     */
    fun shouldOpenWebView(result: LoginResult): Boolean = when (result) {
        is LoginResult.Success -> true
        is LoginResult.NeedsInteractiveSso -> true
        is LoginResult.BadCredentials -> false
        is LoginResult.Error -> false
        is LoginResult.Incompatible -> false
    }

    /** Candidate detection grants no authenticated status; SessionManager probes the gate. */
    fun isCompletionCandidate(profile: Profile, finishedUrl: String, cookieHeader: String?): Boolean {
        if (profile.authType != AuthType.INTERACTIVE_SSO || cookieHeader.isNullOrBlank()) return false
        val own = ServerUrl.parse(profile.baseUrl) ?: return false
        val landed = ServerUrl.parse(finishedUrl) ?: return false
        if (!own.sameOrigin(landed.httpUrl)) return false
        val basePath = own.httpUrl.encodedPath.trimEnd('/')
        val landedPath = landed.httpUrl.encodedPath.trimEnd('/')
        return (landedPath == basePath || landedPath.startsWith("$basePath/")) && !isLoginPage(finishedUrl)
    }

    fun completedSignIn(profile: Profile, finishedUrl: String, cookieHeader: String?, gatewayVerified: Boolean = false): Boolean =
        gatewayVerified && isCompletionCandidate(profile, finishedUrl, cookieHeader)

    /** The browser uses a temporary mode; the stored password profile remains intact. */
    fun profileForWebView(profile: Profile, result: LoginResult): Profile =
        if (result is LoginResult.NeedsInteractiveSso) profile.copy(authType = AuthType.INTERACTIVE_SSO) else profile

    /**
     * The host to stamp on the profile once [completedSignIn] holds, or null
     * when there is nothing to stamp. Keeps the caller from re-deriving (and
     * re-fumbling) the host.
     */
    fun hostToStamp(profile: Profile): String? =
        ServerUrl.parse(profile.baseUrl)?.host

    private fun isLoginPage(url: String): Boolean =
        ServerUrl.parse(url)?.httpUrl?.encodedPath?.trimEnd('/')?.substringAfterLast('/') == "login"
}
