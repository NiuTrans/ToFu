package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Locks the INTERACTIVE_SSO contract.
 *
 * Both halves of this flow existed only as intentions before: `login()`
 * returned [LoginResult.NeedsInteractiveSso] and the card copy told the user to
 * "tap Open, sign in once", but nothing navigated into the WebView and nothing
 * stamped `cookieHost` afterwards. Open did nothing visible, and Start/Stop
 * stayed unavailable no matter how often the user signed in.
 */
class InteractiveSsoTest {

    private val host = "abc-vscode-dc1.codelab.example.com"
    private val base = "https://$host/proxy/15000/"

    private fun profile(
        auth: AuthType = AuthType.INTERACTIVE_SSO,
        baseUrl: String = base,
        cookieHost: String? = null,
    ) = Profile(
        id = 1, alias = "s", baseUrl = baseUrl, authType = auth,
        cookieHost = cookieHost, projectPath = "/home/dev/tofu",
    )

    // ── navigation ────────────────────────────────────────────────────────

    /**
     * THE STRANDED-USER GUARD. SSO's entire design is "the WebView completes
     * the sign-in once, then we persist the jar" — so this outcome MUST
     * navigate. Treating it as a status message left the user on the list
     * behind an "opening…" label that never opened anything, while the card
     * simultaneously told them Open was the fix.
     *
     * NEUTER CHECK: return false for NeedsInteractiveSso and this fails.
     */
    @Test
    fun `interactive sso must open the webview`() {
        assertTrue(
            "SSO can ONLY be completed in the WebView — it must navigate",
            InteractiveSso.shouldOpenWebView(
                LoginResult.NeedsInteractiveSso(base),
            ),
        )
    }

    @Test
    fun `success also opens the webview`() {
        assertTrue(InteractiveSso.shouldOpenWebView(LoginResult.Success(host)))
    }

    /** A real failure must stay on the list so the user can fix the profile. */
    @Test
    fun `credential and transport failures do not navigate`() {
        assertFalse(InteractiveSso.shouldOpenWebView(LoginResult.BadCredentials))
        assertFalse(InteractiveSso.shouldOpenWebView(LoginResult.NoCredential))
        assertFalse(InteractiveSso.shouldOpenWebView(LoginResult.Error("boom")))
    }

    // ── recording the completed sign-in ───────────────────────────────────

    /**
     * THE START/STOP-FOREVER-LOCKED GUARD. The headless path stamps cookieHost
     * when it injects a cookie it fetched itself; an interactive sign-in never
     * passes through it. Without this the supervisor controls stay unusable.
     *
     * NEUTER CHECK: return false unconditionally and this fails.
     */
    @Test
    fun `landing back on our own host with a cookie records the sign-in`() {
        assertTrue(
            InteractiveSso.completedSignIn(
                profile(),
                finishedUrl = base,
                cookieHeader = "code-server-session=abc123",
            ),
        )
    }

    /** Still on the IdP → the user has not finished signing in. */
    @Test
    fun `a page on a foreign host does not count`() {
        assertFalse(
            InteractiveSso.completedSignIn(
                profile(),
                finishedUrl = "https://sso.example.com/authorize?client_id=x",
                cookieHeader = "session=idp",
            ),
        )
    }

    /**
     * Landing back on our host but still on /login means the gate was NOT
     * passed — a CSRF/state cookie is often present, so the cookie check alone
     * would produce a false positive.
     */
    @Test
    fun `the login page itself does not count even with a cookie`() {
        assertFalse(
            InteractiveSso.completedSignIn(
                profile(),
                finishedUrl = "https://$host/login",
                cookieHeader = "code-server-session=partial",
            ),
        )
        assertFalse(
            InteractiveSso.completedSignIn(
                profile(),
                finishedUrl = "https://$host/login?to=%2Fproxy%2F15000%2F",
                cookieHeader = "code-server-session=partial",
            ),
        )
    }

    @Test
    fun `no cookie means nothing to record`() {
        assertFalse(InteractiveSso.completedSignIn(profile(), base, null))
        assertFalse(InteractiveSso.completedSignIn(profile(), base, "   "))
    }

    /**
     * Only SSO profiles use this path. A password profile is stamped by the
     * headless login, and stamping it from a page load would paper over a
     * genuine login failure.
     */
    @Test
    fun `non sso profiles are never recorded this way`() {
        assertFalse(
            InteractiveSso.completedSignIn(
                profile(auth = AuthType.CODE_SERVER_PASSWORD),
                base,
                "code-server-session=abc",
            ),
        )
        assertFalse(
            InteractiveSso.completedSignIn(profile(auth = AuthType.NONE), base, "x=y"),
        )
    }

    @Test
    fun `an unparseable profile url yields no host to stamp`() {
        assertFalse(
            InteractiveSso.completedSignIn(
                profile(baseUrl = "not a url"), base, "code-server-session=abc",
            ),
        )
    }

    @Test
    fun `host to stamp is the profile's own host`() {
        org.junit.Assert.assertEquals(host, InteractiveSso.hostToStamp(profile()))
        org.junit.Assert.assertEquals(null, InteractiveSso.hostToStamp(profile(baseUrl = "nope")))
    }
}
