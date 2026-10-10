package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Exercises the REAL start/stop execution flow, not just the pure decision
 * helpers it calls.
 *
 * This layer exists because the previous version wired the staleness guard as
 * `startedFor == stateKey` inside a Composable, comparing a captured local
 * against itself — structurally always true. The pure `completionFor` test
 * passed while the production guard was dead code. Testing the flow with a
 * mutable `isCurrent` is the only way that class of bug shows up.
 */
class SupervisorRunnerTest {

    private val host = "abc-vscode-dc1.codelab.example.com"

    private fun profile(cookieHost: String? = host) = Profile(
        id = 1, alias = "s",
        baseUrl = "https://$host/proxy/15000/",
        authType = AuthType.CODE_SERVER_PASSWORD,
        cookieHost = cookieHost, projectPath = "/home/dev/tofu",
    )

    // NOTE: `JSONObject.put()` returns null under the unit-test android.jar
    // stub (isReturnDefaultValues), which would NPE on the non-null `body`
    // parameter. The body is irrelevant here — only `running` is asserted — so
    // construct it bare.
    private fun ok(running: Boolean) =
        SupervisorClient.Result.Ok(running, JSONObject())

    private val userPlan = ServerLifecycle.probePlan(ProbeTrigger.USER, signedIn = true)
    private val autoPlan = ServerLifecycle.probePlan(ProbeTrigger.AUTO, signedIn = true)

    /** Records every login attempt so "did it authenticate?" is assertable. */
    private class LoginSpy(private val result: LoginResult) {
        var calls = 0
        suspend fun login(@Suppress("UNUSED_PARAMETER") p: Profile): LoginResult {
            calls++
            return result
        }
    }

    // ── the staleness guard, at the WIRING level ───────────────────────────

    /**
     * THE DEAD-GUARD REGRESSION. The card's identity changes WHILE the start
     * poll runs (user edited the profile / navigated away). `isCurrent` must be
     * re-evaluated after the call, so the hand-off is suppressed.
     *
     * NEUTER CHECK: capture isCurrent's value up-front instead of calling it
     * after the work, and this fails — which is exactly the bug that shipped.
     */
    @Test
    fun `a card that changed during the call does not hand off`() = runTest {
        var current = true
        val outcome = executeSupervisorCall(
            profile = profile(),
            action = SupervisorAction.START,
            plan = userPlan,
            signedIn = true,
            login = { LoginResult.Success(host) },
            call = { _, _ ->
                // The card is replaced while the request is in flight.
                current = false
                ok(true)
            },
            isCurrent = { current },
        )
        assertFalse("must not yank the user into a WebView they left", outcome.handOff)
    }

    @Test
    fun `a card still current when the start completes hands off`() = runTest {
        val outcome = executeSupervisorCall(
            profile = profile(),
            action = SupervisorAction.START,
            plan = userPlan,
            signedIn = true,
            login = { LoginResult.Success(host) },
            call = { _, _ -> ok(true) },
            isCurrent = { true },
        )
        assertTrue(outcome.handOff)
        assertEquals(true, outcome.running)
    }

    // ── login-then-act, end to end ─────────────────────────────────────────

    /** A USER tap with no session must authenticate before calling. */
    @Test
    fun `user action without a session logs in first`() = runTest {
        val spy = LoginSpy(LoginResult.Success(host))
        val plan = ServerLifecycle.probePlan(ProbeTrigger.USER, signedIn = false)
        val outcome = executeSupervisorCall(
            profile = profile(cookieHost = null),
            action = SupervisorAction.START,
            plan = plan,
            signedIn = false,
            login = spy::login,
            call = { _, _ -> ok(true) },
            isCurrent = { true },
        )
        assertEquals("login-then-act must authenticate", 1, spy.calls)
        assertTrue(outcome.handOff)
    }

    /**
     * The complement: an AUTO probe must never spend a login. Opening the home
     * screen with N un-signed-in servers must not fire N logins.
     */
    @Test
    fun `auto probe never logs in`() = runTest {
        val spy = LoginSpy(LoginResult.Success(host))
        val plan = ServerLifecycle.probePlan(ProbeTrigger.AUTO, signedIn = false)
        executeSupervisorCall(
            profile = profile(cookieHost = null),
            action = SupervisorAction.STATUS,
            plan = plan,
            signedIn = false,
            login = spy::login,
            call = { _, _ -> ok(true) },
            isCurrent = { true },
        )
        assertEquals("an unrequested probe must not authenticate", 0, spy.calls)
    }

    /** SSO yields no cookie, so the supervisor call must not even be attempted. */
    @Test
    fun `sso blocks before the supervisor is called`() = runTest {
        var called = false
        val plan = ServerLifecycle.probePlan(ProbeTrigger.USER, signedIn = false)
        val outcome = executeSupervisorCall(
            profile = profile(cookieHost = null),
            action = SupervisorAction.START,
            plan = plan,
            signedIn = false,
            login = { LoginResult.NeedsInteractiveSso("https://h/") },
            call = { _, _ -> called = true; ok(true) },
            isCurrent = { true },
        )
        assertFalse("must not 401 the supervisor and blame the host", called)
        assertTrue(outcome.failed)
        assertTrue(outcome.message.orEmpty().contains("Open"))
    }

    // ── transport failure must surface, not vanish ─────────────────────────

    /**
     * A thrown IOException previously escaped to the coroutine scope's handler:
     * the card silently reverted and the user saw NOTHING happen after tapping
     * Start.
     *
     * NEUTER CHECK: drop the `catch (t: Throwable)` and this test fails with
     * the raw exception instead of a reported outcome.
     */
    @Test
    fun `a thrown transport error is reported not swallowed`() = runTest {
        val outcome = executeSupervisorCall(
            profile = profile(),
            action = SupervisorAction.START,
            plan = userPlan,
            signedIn = true,
            login = { LoginResult.Success(host) },
            call = { _, _ -> throw java.io.IOException("socket closed") },
            isCurrent = { true },
        )
        assertTrue("the user must see why nothing happened", outcome.failed)
        assertTrue(outcome.message.orEmpty(), outcome.message.orEmpty().isNotBlank())
    }

    /** …but an AUTO probe still stays silent about it. */
    @Test
    fun `a thrown error during an auto probe stays silent`() = runTest {
        val outcome = executeSupervisorCall(
            profile = profile(),
            action = SupervisorAction.STATUS,
            plan = autoPlan,
            signedIn = true,
            login = { LoginResult.Success(host) },
            call = { _, _ -> throw java.io.IOException("socket closed") },
            isCurrent = { true },
        )
        assertFalse("an unrequested probe must not paint the card red", outcome.failed)
    }

    // ── post-start polling ─────────────────────────────────────────────────

    /** /start returns before the port binds, so the poll must pick it up. */
    @Test
    fun `start polls until the port comes up`() = runTest {
        var statusCalls = 0
        val outcome = executeSupervisorCall(
            profile = profile(),
            action = SupervisorAction.START,
            plan = userPlan,
            signedIn = true,
            login = { LoginResult.Success(host) },
            call = { a, _ ->
                if (a == SupervisorAction.START) ok(false)
                else { statusCalls++; ok(statusCalls >= 3) }
            },
            isCurrent = { true },
            pollAttempts = 10,
            pollIntervalMs = 1,
        )
        assertEquals(true, outcome.running)
        assertTrue(outcome.handOff)
        assertEquals("must stop polling once up", 3, statusCalls)
    }

    /** The window can expire — that is not an error, but it must be explained. */
    @Test
    fun `start that never comes up explains itself instead of failing`() = runTest {
        val outcome = executeSupervisorCall(
            profile = profile(),
            action = SupervisorAction.START,
            plan = userPlan,
            signedIn = true,
            login = { LoginResult.Success(host) },
            call = { _, _ -> ok(false) },
            isCurrent = { true },
            pollAttempts = 3,
            pollIntervalMs = 1,
        )
        assertFalse("a slow boot is not a failure", outcome.failed)
        assertFalse(outcome.handOff)
        assertTrue(outcome.message.orEmpty().isNotBlank())
    }

    // ── CardKey identity: what makes a result "still mine" ─────────────────

    /**
     * Editing the URL or the project path changes what a supervisor call MEANS,
     * while leaving `id` untouched. If those fields were left out of the key,
     * an in-flight result would be applied to the edited card.
     *
     * NEUTER CHECK: drop cookieHost/projectPath from CardKey and this fails.
     */
    @Test
    fun `card identity covers every field that changes a call's meaning`() {
        val base = CardKey.of(profile())
        assertEquals("same profile must be the same card", base, CardKey.of(profile()))

        val p = profile()
        assertTrue(base != CardKey.of(p.copy(baseUrl = "https://other/proxy/15000/")))
        assertTrue(base != CardKey.of(p.copy(cookieHost = null)))
        assertTrue(base != CardKey.of(p.copy(projectPath = "/other")))
        assertTrue(base != CardKey.of(p.copy(id = 2)))
    }

    /**
     * THE RECYCLED-CARD CASE. A LazyColumn reuses a row's slot for a DIFFERENT
     * profile. Work started under the old identity must not be applied to the
     * new occupant — otherwise scrolling during a start could hand the user
     * into the wrong server's WebView.
     */
    @Test
    fun `work started before a slot was recycled is not current`() {
        val started = CardKey.of(profile())
        val recycledInto = CardKey.of(
            profile().copy(id = 7, baseUrl = "https://zzz-vscode-dc1.codelab.example.com/proxy/15000/"),
        )
        assertFalse(isStillCurrent(started, recycledInto))
        assertTrue(isStillCurrent(started, started))
    }

    /**
     * The wiring contract the Composable must honour: the ref holding "who am I
     * now" is keyed on the SAME CardKey as every other piece of card state, so
     * a key change resets them together. Modelled here because the asymmetry —
     * some state keyed, the ref not — is precisely how the last two versions of
     * this guard became unable to fire.
     */
    @Test
    fun `a profile edit mid-call flips the guard to false`() = runTest {
        val original = profile()
        val started = CardKey.of(original)
        // Stands in for the remembered ref that each composition refreshes.
        var liveKey = started

        val outcome = executeSupervisorCall(
            profile = original,
            action = SupervisorAction.START,
            plan = userPlan,
            signedIn = true,
            login = { LoginResult.Success(host) },
            call = { a, _ ->
                if (a == SupervisorAction.START) {
                    ok(false)
                } else {
                    // The user edits the profile while we poll: a new
                    // composition writes a NEW key into the ref.
                    liveKey = CardKey.of(original.copy(projectPath = "/edited"))
                    ok(true)
                }
            },
            isCurrent = { isStillCurrent(started, liveKey) },
            pollAttempts = 3,
            pollIntervalMs = 1,
        )
        assertEquals("the server did come up", true, outcome.running)
        assertFalse("but this card is no longer the one that asked", outcome.handOff)
    }

    /** A Stop must never navigate the user anywhere. */
    @Test
    fun `stop never hands off`() = runTest {
        val outcome = executeSupervisorCall(
            profile = profile(),
            action = SupervisorAction.STOP,
            plan = userPlan,
            signedIn = true,
            login = { LoginResult.Success(host) },
            call = { _, _ -> ok(false) },
            isCurrent = { true },
        )
        assertFalse(outcome.handOff)
        assertEquals(false, outcome.running)
    }
}
