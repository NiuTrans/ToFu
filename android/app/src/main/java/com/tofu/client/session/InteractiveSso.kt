package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile

/**
 * The two rules that make INTERACTIVE_SSO actually work, as pure functions.
 *
 * Both existed only as intentions before: the login path RETURNED
 * [LoginResult.NeedsInteractiveSso] and the card copy told the user to "tap
 * Open, sign in once", but nothing navigated into the WebView and nothing ever
 * stamped `cookieHost` afterwards. So SSO was a closed loop — Open did nothing
 * visible, and Start/Stop stayed unavailable forever.
 *
 * They live here, off Android, because the previous three staleness-guard bugs
 * all shared one shape: the RULE was fine, the values wired into it were not.
 * A named pure function lets a test assert the inputs.
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
        is LoginResult.NoCredential -> false
        is LoginResult.Error -> false
        is LoginResult.Incompatible -> false
    }

    /**
     * Whether an in-WebView sign-in should now be recorded as a real session.
     *
     * The interactive flow leaves the IdP and lands back on the server's own
     * origin carrying a session cookie. Two conditions, both required:
     *  - the finished page is back on the profile's OWN host (still sitting on
     *    the IdP means the user has not finished — or has bounced off — sign-in);
     *  - the jar actually holds a cookie for that origin.
     *
     * [cookieHeader] is the raw `Cookie:` string from the jar (null/blank = none).
     */
    fun completedSignIn(
        profile: Profile,
        finishedUrl: String,
        cookieHeader: String?,
    ): Boolean {
        if (profile.authType != AuthType.INTERACTIVE_SSO) return false
        if (cookieHeader.isNullOrBlank()) return false
        val own = ServerUrl.parse(profile.baseUrl)?.host ?: return false
        val landed = ServerUrl.parse(finishedUrl)?.host ?: return false
        if (landed != own) return false
        // Still on the login page means the gate has not been passed, even
        // though a cookie (often just a CSRF/state cookie) exists.
        return !isLoginPage(finishedUrl)
    }

    /**
     * The host to stamp on the profile once [completedSignIn] holds, or null
     * when there is nothing to stamp. Keeps the caller from re-deriving (and
     * re-fumbling) the host.
     */
    fun hostToStamp(profile: Profile): String? =
        ServerUrl.parse(profile.baseUrl)?.host

    private fun isLoginPage(url: String): Boolean =
        url.endsWith("/login") || url.contains("/login?") || url.contains("/login#")
}
