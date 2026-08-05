package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.runTest
import okhttp3.Cookie
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Flow-level guard for the SSO stamp — driven through the DAO + cookie seams,
 * not by calling the pure rule directly.
 *
 * This distinction is the lesson from the staleness-guard bugs: a pure decision
 * function tested in isolation proves the RULE is right and says nothing about
 * whether the values wired into it are computed correctly. That gap is exactly
 * where the previous three bugs lived, so the WIRING gets its own seam.
 */
class SessionManagerInteractiveSsoTest {

    private val host = "abc12345-vscode-dc1.codelab.example.com"
    private val base = "https://$host/proxy/15000/"

    private class FakeCookieSink(private val header: String?) : CookieSink {
        val queried = mutableListOf<String>()
        override fun inject(origin: String, cookies: List<Cookie>) {}
        override fun purgeHost(host: String) {}
        override fun cookieHeader(origin: String): String? {
            queried += origin
            return header
        }
    }

    private class FakeSecrets : SecretLookup {
        override fun secretFor(alias: String): String? = null
    }

    private class FakeDao(private var current: Profile) : ProfileDao {
        val updates = mutableListOf<Profile>()
        override fun observeAll(): Flow<List<Profile>> = flowOf(listOf(current))
        override suspend fun getById(id: Long): Profile? = current
        override suspend fun getAllOnce(): List<Profile> = listOf(current)
        override suspend fun getByAlias(alias: String): Profile? = current
        override suspend fun insert(profile: Profile): Long = 1
        override suspend fun update(profile: Profile): Int {
            current = profile; updates += profile; return 1
        }
        override suspend fun deleteById(id: Long) {}
        // Mirrors the real @Query: touches ONE column, so a test can tell a
        // targeted write apart from a full-row overwrite.
        val stamps = mutableListOf<String?>()
        override suspend fun setCookieHost(id: Long, host: String?): Int {
            if (current.id != id) return 0
            current = current.copy(cookieHost = host)
            stamps += host
            return 1
        }
        override suspend fun touchLastUsed(id: Long, timestamp: Long): Int {
            current = current.copy(lastUsedAt = timestamp); return 1
        }
        override suspend fun setAuthType(id: Long, authType: AuthType): Int {
            current = current.copy(authType = authType); return 1
        }
    }

    private fun profile(cookieHost: String? = null) = Profile(
        id = 1, alias = "sso server", baseUrl = base,
        authType = AuthType.INTERACTIVE_SSO, cookieHost = cookieHost,
        projectPath = "/home/dev/chatui",
    )

    /**
     * THE CORE GUARD. Landing back on our own host with a session cookie must
     * write cookieHost — that write is the ONLY thing that makes `isSignedIn`
     * true, and therefore the only thing that ever unlocks Start/Stop for an
     * SSO profile.
     *
     * NEUTER CHECK: delete the `dao.setCookieHost(...)` in noteInteractiveSignIn
     * and this fails — the supervisor controls would stay locked forever.
     */
    @Test
    fun completing_sso_in_the_webview_stamps_cookie_host() = runTest {
        val cookies = FakeCookieSink("code-server-session=tok")
        val dao = FakeDao(profile())
        val mgr = SessionManager(dao, FakeSecrets(), cookies)

        val stamped = mgr.noteInteractiveSignIn(profile(), finishedUrl = base)

        assertTrue("must record the completed sign-in", stamped)
        assertEquals(listOf(host), dao.stamps)
        assertEquals(host, dao.getById(1)!!.cookieHost)
        // It must read the jar for OUR origin, not some other host's.
        assertEquals(listOf("https://$host"), cookies.queried)
    }

    /**
     * THE STALE-SNAPSHOT GUARD. `WebScreen` holds the profile it was opened
     * with for the ENTIRE session, and an SSO sign-in can take minutes (IdP
     * redirect, one-time code). If the stamp wrote the whole row from that
     * snapshot, any edit made meanwhile would be silently rolled back.
     *
     * Here the row is renamed and re-pathed AFTER the snapshot is taken; the
     * stamp must set cookieHost and touch nothing else.
     *
     * NEUTER CHECK: replace `dao.setCookieHost(...)` with
     * `dao.update(profile.copy(cookieHost = host))` and this fails — the alias
     * and projectPath revert to their pre-edit values.
     */
    @Test
    fun stamping_does_not_roll_back_a_concurrent_edit() = runTest {
        val stale = profile()                       // what WebScreen captured
        val dao = FakeDao(stale)
        val mgr = SessionManager(dao, FakeSecrets(), FakeCookieSink("code-server-session=tok"))

        // Meanwhile the user edits the same row from the list screen.
        dao.update(stale.copy(alias = "renamed", projectPath = "/srv/other"))

        val stamped = mgr.noteInteractiveSignIn(stale, finishedUrl = base)

        assertTrue(stamped)
        val row = dao.getById(1)!!
        assertEquals("the sign-in must be recorded", host, row.cookieHost)
        assertEquals("a concurrent rename must survive", "renamed", row.alias)
        assertEquals("a concurrent path edit must survive", "/srv/other", row.projectPath)
    }

    /** Sitting on the IdP is not a completed sign-in. */
    @Test
    fun a_page_still_on_the_idp_stamps_nothing() = runTest {
        val cookies = FakeCookieSink("state=xyz")
        val dao = FakeDao(profile())
        val mgr = SessionManager(dao, FakeSecrets(), cookies)

        val stamped = mgr.noteInteractiveSignIn(
            profile(), finishedUrl = "https://sso.example.com/authorize?x=1",
        )

        assertFalse(stamped)
        assertTrue("must not stamp: ${dao.stamps}", dao.stamps.isEmpty())
    }

    /** An empty jar means the sign-in did not produce a session. */
    @Test
    fun no_cookie_stamps_nothing() = runTest {
        val cookies = FakeCookieSink(null)
        val dao = FakeDao(profile())
        val mgr = SessionManager(dao, FakeSecrets(), cookies)

        assertFalse(mgr.noteInteractiveSignIn(profile(), base))
        assertTrue(dao.stamps.isEmpty())
    }

    /**
     * onPageDone fires on EVERY main-frame load, so a non-idempotent stamp would
     * write to Room on each navigation. Worse, cookieHost is part of the card's
     * CardKey — every write re-emits the profile and resets the card's polled
     * state, so a chatty stamp would make the supervisor status flicker.
     *
     * NEUTER CHECK: drop the `profile.cookieHost == host` early return and this
     * fails.
     */
    @Test
    fun an_already_stamped_profile_is_not_rewritten() = runTest {
        val cookies = FakeCookieSink("code-server-session=tok")
        val dao = FakeDao(profile(cookieHost = host))
        val mgr = SessionManager(dao, FakeSecrets(), cookies)

        val stamped = mgr.noteInteractiveSignIn(profile(cookieHost = host), base)

        assertFalse("already signed in → no write", stamped)
        assertTrue("must not re-write: ${dao.stamps}", dao.stamps.isEmpty())
    }

    /** A password profile is stamped by the headless login, never from here. */
    @Test
    fun a_password_profile_is_never_stamped_from_a_page_load() = runTest {
        val cookies = FakeCookieSink("code-server-session=tok")
        val pw = profile().copy(authType = AuthType.CODE_SERVER_PASSWORD)
        val dao = FakeDao(pw)
        val mgr = SessionManager(dao, FakeSecrets(), cookies)

        assertFalse(mgr.noteInteractiveSignIn(pw, base))
        assertTrue(
            "stamping here would paper over a real login failure: ${dao.stamps}",
            dao.stamps.isEmpty(),
        )
    }
}
