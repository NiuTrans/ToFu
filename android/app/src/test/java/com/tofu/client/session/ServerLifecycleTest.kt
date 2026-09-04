package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Locks the server-lifecycle rules that decide which controls the UI offers.
 *
 * These matter because getting them wrong is silently bad UX rather than a
 * crash: offering Stop on a stopped server, or — the real bug this guards —
 * treating a profile whose URL changed as still signed in, which would enable
 * Start/Stop against a dead host and surface an opaque 401.
 */
class ServerLifecycleTest {

    private fun profile(
        alias: String = "s",
        baseUrl: String = "https://abc-vscode-dc1.codelab.example.com/proxy/15000/",
        cookieHost: String? = null,
        projectPath: String? = "/home/dev/tofu",
    ) = Profile(
        id = 1, alias = alias, baseUrl = baseUrl,
        authType = AuthType.CODE_SERVER_PASSWORD,
        cookieHost = cookieHost, projectPath = projectPath,
    )

    @Test
    fun `no project path means unmanaged`() {
        val p = profile(projectPath = null)
        assertFalse(ServerLifecycle.isManaged(p))
        assertEquals(ServerState.UNMANAGED, ServerLifecycle.resolve(p, null))
    }

    @Test
    fun `blank project path is also unmanaged`() {
        assertFalse(ServerLifecycle.isManaged(profile(projectPath = "   ")))
    }

    @Test
    fun `managed but no cookie is unknown not disabled`() {
        val p = profile(cookieHost = null)
        assertEquals(ServerState.UNKNOWN, ServerLifecycle.resolve(p, null))
        // …and that state is handshake-pending, NOT capability-revoked.
        assertTrue(ServerLifecycle.capabilities(ServerState.UNKNOWN).canStart)
    }

    /**
     * The guarded regression: a cookie pinned to a PREVIOUS host must not count
     * as signed in. The cookie is Domain-pinned, so after a re-provision the
     * cached jar is bound to a dead host and every supervisor call would 401.
     */
    @Test
    fun `cookie from a different host does not count as signed in`() {
        val p = profile(cookieHost = "old-vscode-dc1.codelab.example.com")
        assertFalse(ServerLifecycle.isSignedIn(p))
        assertEquals(ServerState.UNKNOWN, ServerLifecycle.resolve(p, null))
    }

    @Test
    fun `matching cookie host counts as signed in`() {
        val p = profile(cookieHost = "abc-vscode-dc1.codelab.example.com")
        assertTrue(ServerLifecycle.isSignedIn(p))
        assertEquals(ServerState.UNKNOWN, ServerLifecycle.resolve(p, null))
    }

    @Test
    fun `unparseable url is never signed in`() {
        assertFalse(ServerLifecycle.isSignedIn(profile(baseUrl = "not a url", cookieHost = "x")))
    }

    @Test
    fun `running and stopped resolve from the poll result`() {
        val p = profile(cookieHost = "abc-vscode-dc1.codelab.example.com")
        assertEquals(ServerState.RUNNING, ServerLifecycle.resolve(p, running = true))
        assertEquals(ServerState.STOPPED, ServerLifecycle.resolve(p, running = false))
    }

    @Test
    fun `busy outranks a stale poll result`() {
        val p = profile(cookieHost = "abc-vscode-dc1.codelab.example.com")
        assertEquals(
            ServerState.TRANSITIONING,
            ServerLifecycle.resolve(p, running = true, busy = true),
        )
    }

    @Test
    fun `failure surfaces as unreachable`() {
        val p = profile(cookieHost = "abc-vscode-dc1.codelab.example.com")
        assertEquals(
            ServerState.UNREACHABLE,
            ServerLifecycle.resolve(p, running = null, failed = true),
        )
    }

    @Test
    fun `running offers stop but not start`() {
        val caps = ServerLifecycle.capabilities(ServerState.RUNNING)
        assertFalse(caps.canStart)
        assertTrue(caps.canStop)
    }

    @Test
    fun `stopped offers start but not stop`() {
        val caps = ServerLifecycle.capabilities(ServerState.STOPPED)
        assertTrue(caps.canStart)
        assertFalse(caps.canStop)
    }

    /**
     * THE DEADLOCK GUARD. A managed server that is DOWN has no cookie (Open
     * cannot succeed against it), so it has no authoritative state — UNKNOWN.
     * If that state disabled Start, the user could never start it: the card
     * would say "sign in first", Start would be greyed, and Open — the only
     * suggested escape — cannot work on a stopped server. That is exactly the
     * chicken-and-egg the supervisor exists to break.
     *
     * This is legal because the supervisor rides the CODE-SERVER session, and
     * code-server is the proxy — it stays up while Tofu is down, so the login
     * handshake succeeds regardless of Tofu's state.
     *
     * NEUTER CHECK: set canStart = false on UNKNOWN and this fails.
     */
    @Test
    fun `signed out server must still be startable`() {
        val caps = ServerLifecycle.capabilities(ServerState.UNKNOWN)
        assertTrue("a signed-out managed server MUST still be startable", caps.canStart)
        assertTrue(caps.canRefresh)
        assertTrue(caps.canOpen)
    }

    /** End-to-end: a profile with no cookie at all must still offer Start. */
    @Test
    fun `managed server with no cookie resolves to unknown and can start`() {
        val p = profile(cookieHost = null)
        val state = ServerLifecycle.resolve(p, running = null)
        assertEquals(ServerState.UNKNOWN, state)
        assertTrue(ServerLifecycle.capabilities(state).canStart)
    }

    /**
     * A poll result must OUTRANK the cookie check. Otherwise a login-then-act
     * that genuinely reached the supervisor still renders as unpolled until Room
     * re-emits the profile carrying the freshly-stamped cookieHost.
     */
    @Test
    fun `poll result outranks a missing cookie`() {
        val p = profile(cookieHost = null)
        assertEquals(ServerState.RUNNING, ServerLifecycle.resolve(p, running = true))
        assertEquals(ServerState.STOPPED, ServerLifecycle.resolve(p, running = false))
    }

    /**
     * A start poll can outlast its window, so TRANSITIONING must keep ONE
     * actionable escape: Open. Taking every control away leaves the user on a
     * spinner with nothing to tap — visually identical to the deadlock this
     * class exists to prevent.
     *
     * NEUTER CHECK: set canOpen = false on TRANSITIONING and this fails.
     */
    @Test
    fun `transitioning disables mutations but keeps open available`() {
        val caps = ServerLifecycle.capabilities(ServerState.TRANSITIONING)
        assertFalse(caps.canStart)
        assertFalse(caps.canStop)
        assertFalse(caps.canRefresh)
        assertTrue("mid-start the user must still have a way out", caps.canOpen)
    }

    /**
     * No state may strand the user with nothing to do. Every state must offer
     * at least one action — this is the general form of the deadlock bug, so it
     * is asserted across the whole enum rather than case by case.
     */
    @Test
    fun `no state leaves the user with zero actions`() {
        ServerState.values().forEach { s ->
            val c = ServerLifecycle.capabilities(s)
            assertTrue(
                "$s offers no action at all — that is a dead end",
                c.canStart || c.canStop || c.canRefresh || c.canOpen,
            )
        }
    }

    /**
     * The start poll window must be generous enough for a cold boot. 12s (the
     * original 6×2s) routinely expired mid-startup, so the auto-open hand-off
     * silently never fired.
     */
    @Test
    fun `start poll window is at least 30 seconds`() {
        assertTrue(
            "window was ${ServerLifecycle.startPollWindowSeconds}s",
            ServerLifecycle.startPollWindowSeconds >= 30,
        )
    }

    /** Timing out is not an error, so the copy must offer a next step. */
    @Test
    fun `start timeout message tells the user what to do next`() {
        val msg = ServerLifecycle.startTimeoutMessage()
        assertTrue(msg, msg.contains("Check") || msg.contains("Open"))
        assertTrue("must state the window", msg.contains("30"))
    }

    // ── login-then-act blocking rules ──────────────────────────────────────

    /**
     * SSO yields NO cookie, so continuing to the supervisor call would 401 and
     * be reported as "the daemon isn't responding" — blaming the host for an
     * un-completed sign-in.
     *
     * NEUTER CHECK: return false for NeedsInteractiveSso and this fails.
     */
    @Test
    fun `interactive sso blocks the supervisor call`() {
        assertTrue(
            ServerLifecycle.isLoginBlocking(
                LoginResult.NeedsInteractiveSso("https://h/proxy/15000/"),
            ),
        )
    }

    /**
     * SSO is the ONE case where Open genuinely is the fix, so the message must
     * say so rather than falling through to a generic failure.
     */
    @Test
    fun `sso explanation points at open not at a generic failure`() {
        val msg = ServerLifecycle.explainLoginBlock(
            LoginResult.NeedsInteractiveSso("https://h/proxy/15000/"),
        )
        assertTrue(msg, msg.contains("Open"))
        assertFalse("must not be the generic fallback", msg == "Couldn't sign in to this server.")
    }

    @Test
    fun `success never blocks`() {
        assertFalse(ServerLifecycle.isLoginBlocking(LoginResult.Success("h")))
    }

    @Test
    fun `credential failures block and explain themselves`() {
        assertTrue(ServerLifecycle.isLoginBlocking(LoginResult.BadCredentials))
        assertTrue(ServerLifecycle.isLoginBlocking(LoginResult.NoCredential))
        assertTrue(ServerLifecycle.isLoginBlocking(LoginResult.Error("boom")))
        assertTrue(
            ServerLifecycle.explainLoginBlock(LoginResult.Error("boom")).contains("boom"),
        )
    }

    // ── AUTO vs USER: who is allowed to cause side effects ─────────────────

    /**
     * THE SILENT-LOGIN GUARD. The auto-probe runs on every composition with
     * nobody asking. If it were allowed to log in, merely opening the home
     * screen would fire one `POST /login` per un-signed-in server: a burst of
     * unrequested logins, which on a wrong password is an auto-retry loop
     * toward account lockout.
     *
     * NEUTER CHECK: set mayLogIn = true (or proceed = true) for AUTO and this
     * fails.
     */
    @Test
    fun `auto probe without a session never logs in`() {
        val plan = ServerLifecycle.probePlan(ProbeTrigger.AUTO, signedIn = false)
        assertFalse("opening the app must not spend a login", plan.mayLogIn)
        assertFalse("with no cookie there is nothing to ask with", plan.proceed)
    }

    /**
     * "Not signed in yet" is not "unreachable". An AUTO probe must never paint
     * the card red — an SSO server can never satisfy a headless login, so it
     * would show a permanent false error on every cold start while being
     * perfectly healthy.
     *
     * NEUTER CHECK: set reportFailure = true for AUTO and this fails.
     */
    @Test
    fun `auto probe never reports failure`() {
        assertFalse(ServerLifecycle.probePlan(ProbeTrigger.AUTO, signedIn = false).reportFailure)
        assertFalse(ServerLifecycle.probePlan(ProbeTrigger.AUTO, signedIn = true).reportFailure)
    }

    /** With a live cookie the auto-probe is free — read-only, no login. */
    @Test
    fun `auto probe with a session proceeds read only`() {
        val plan = ServerLifecycle.probePlan(ProbeTrigger.AUTO, signedIn = true)
        assertTrue(plan.proceed)
        assertFalse("a held cookie is enough; still no login", plan.mayLogIn)
    }

    /**
     * The other half of the contract: an explicit tap MUST be able to log in,
     * or the start-a-stopped-server deadlock comes straight back.
     *
     * NEUTER CHECK: set mayLogIn = false for USER and this fails.
     */
    @Test
    fun `user action without a session may log in`() {
        val plan = ServerLifecycle.probePlan(ProbeTrigger.USER, signedIn = false)
        assertTrue("login-then-act is what makes a stopped server startable", plan.mayLogIn)
        assertTrue(plan.proceed)
        assertTrue("an explicit tap that fails must say so", plan.reportFailure)
    }

    @Test
    fun `user action always proceeds regardless of session`() {
        listOf(true, false).forEach { signedIn ->
            val plan = ServerLifecycle.probePlan(ProbeTrigger.USER, signedIn)
            assertTrue("USER/$signedIn must proceed", plan.proceed)
            assertTrue("USER/$signedIn must report failures", plan.reportFailure)
        }
    }

    // ── completion: what a FINISHED call may do to the UI ──────────────────

    /**
     * THE STALE-HANDOFF GUARD. A start polls for up to 30s, during which the
     * user can navigate away or edit the profile. If the completion still fired
     * the hand-off, the app would yank them into a WebView they never asked
     * for — possibly for a server they had just left.
     *
     * NEUTER CHECK: ignore stillCurrent and this fails.
     */
    @Test
    fun `a stale start never hands off`() {
        val c = ServerLifecycle.completionFor(
            SupervisorAction.START, running = true, stillCurrent = false,
        )
        assertFalse("the card is gone — do not force-open a WebView", c.handOff)
        assertFalse("and do not write a message onto a discarded card", c.showTimeout)
    }

    @Test
    fun `a current start that reached running hands off`() {
        val c = ServerLifecycle.completionFor(
            SupervisorAction.START, running = true, stillCurrent = true,
        )
        assertTrue(c.handOff)
        assertFalse(c.showTimeout)
    }

    /** Poll window expired while still on screen → explain, don't hand off. */
    @Test
    fun `a current start that timed out shows the timeout copy`() {
        val c = ServerLifecycle.completionFor(
            SupervisorAction.START, running = false, stillCurrent = true,
        )
        assertFalse(c.handOff)
        assertTrue(c.showTimeout)
    }

    /** Only START navigates. A Stop or a Check must never move the user. */
    @Test
    fun `stop and status never hand off or time out`() {
        listOf(SupervisorAction.STOP, SupervisorAction.STATUS).forEach { a ->
            listOf(true, false).forEach { running ->
                val c = ServerLifecycle.completionFor(a, running, stillCurrent = true)
                assertFalse("$a must not navigate", c.handOff)
                assertFalse("$a must not show start-timeout copy", c.showTimeout)
            }
        }
    }

    /** Unreachable must stay actionable — retrying is the only way out. */
    @Test
    fun `unreachable can retry`() {
        val caps = ServerLifecycle.capabilities(ServerState.UNREACHABLE)
        assertTrue(caps.canStart)
        assertTrue(caps.canRefresh)
    }

    @Test
    fun `every state has a non-empty label`() {
        ServerState.values().forEach {
            assertTrue(it.name, ServerLifecycle.label(it).isNotBlank())
        }
    }
}
