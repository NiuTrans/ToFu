package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.runTest
import okhttp3.Cookie
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Guards the orchestration invariants that would silently break the
 * "authenticate once / survive re-provision / no orphaned credential" promise.
 */
class SessionControllerTest {

    private val oldHost = "abc12345-vscode-dc1.codelab.example.com"
    private val newHost = "aaaa1111-vscode-dc1.codelab.example.com"

    private class FakeCookieSink : CookieSink {
        val purged = mutableListOf<String>()
        override fun inject(origin: String, cookies: List<Cookie>) {}
        override fun purgeHost(host: String) { purged += host }
        override fun cookieHeader(origin: String): String? = null
    }

    private class FakeVault : SecretVault {
        val map = mutableMapOf<String, String>()
        override fun secretFor(alias: String) = map[alias]
        override fun putSecret(alias: String, secret: String) { map[alias] = secret }
        override fun removeSecret(alias: String) { map.remove(alias) }
    }

    private class FakeDao(seed: Profile? = null) : ProfileDao {
        val rows = mutableMapOf<Long, Profile>()
        val deleted = mutableListOf<Long>()
        var nextId = 1L
        init { if (seed != null) rows[seed.id] = seed }
        override fun observeAll(): Flow<List<Profile>> = flowOf(rows.values.toList())
        override suspend fun getById(id: Long) = rows[id]
        /**
         * One-shot hook fired AFTER the snapshot is taken but BEFORE the caller
         * acts on it — the only way to reproduce a write interleaving into a
         * read-then-write loop.
         */
        var afterSnapshot: (suspend () -> Unit)? = null
        override suspend fun getAllOnce(): List<Profile> {
            val snapshot = rows.values.toList()
            val hook = afterSnapshot
            afterSnapshot = null
            hook?.invoke()
            return snapshot
        }
        override suspend fun getByAlias(alias: String) = rows.values.firstOrNull { it.alias == alias }
        override suspend fun insert(profile: Profile): Long {
            val id = nextId++; rows[id] = profile.copy(id = id); return id
        }
        override suspend fun update(profile: Profile): Int { rows[profile.id] = profile; return 1 }
        override suspend fun deleteById(id: Long) { rows.remove(id); deleted += id }
        override suspend fun setCookieHost(id: Long, host: String?): Int {
            val row = rows[id] ?: return 0
            rows[id] = row.copy(cookieHost = host)
            return 1
        }
        override suspend fun touchLastUsed(id: Long, timestamp: Long): Int {
            val row = rows[id] ?: return 0
            rows[id] = row.copy(lastUsedAt = timestamp)
            return 1
        }
        override suspend fun setAuthType(id: Long, authType: AuthType): Int {
            val row = rows[id] ?: return 0
            rows[id] = row.copy(authType = authType)
            return 1
        }
    }

    private fun mgr(dao: ProfileDao, vault: SecretVault, sink: FakeCookieSink) =
        // No secret path exercised through the network: login short-circuits on
        // NONE / NoCredential. We assert side-effects (purge, secret moves), not HTTP.
        SessionManager(dao, vault, sink)

    private fun profile(alias: String, host: String, id: Long = 1) = Profile(
        id = id, alias = alias, baseUrl = "https://$host/proxy/15000/",
        authType = AuthType.NONE, instanceUuid = host.substringBefore("-vscode"),
        cookieHost = host,
    )

    @Test
    fun add_stores_secret_before_insert_and_logs_in() = runTest {
        val dao = FakeDao(); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), clock = { 42L })

        val res = ctl.addProfile("dc1", "https://$oldHost/proxy/15000/",
            AuthType.CODE_SERVER_PASSWORD, "sekret")

        assertTrue(res is SessionController.AddResult.Added)
        // Secret stored under the alias, row inserted.
        assertEquals("sekret", vault.map["dc1"])
        assertEquals(1, dao.rows.size)
    }

    @Test
    fun add_rejects_duplicate_alias() = runTest {
        val dao = FakeDao(profile("dc1", oldHost)); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))
        val res = ctl.addProfile("dc1", "https://$oldHost/proxy/15000/", AuthType.NONE, "")
        assertTrue(res is SessionController.AddResult.DuplicateAlias)
    }

    @Test
    fun edit_url_host_change_purges_old_jar() = runTest {
        val seed = profile("dc1", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        ctl.editProfile(seed, "dc1", "https://$newHost/proxy/15000/", AuthType.NONE, "")

        // NEUTER: if editProfile didn't route host-changes through
        // updateUrlAndReauth, the old Domain-pinned jar would survive.
        assertEquals(listOf(oldHost), sink.purged)
    }

    @Test
    fun edit_same_host_does_not_purge() = runTest {
        val seed = profile("dc1", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))
        ctl.editProfile(seed, "dc1", "https://$oldHost/proxy/16000/", AuthType.NONE, "")
        assertTrue(sink.purged.isEmpty())
    }

    @Test
    fun rename_moves_alias_keyed_secret() = runTest {
        val seed = profile("old", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("old", "sekret")
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        ctl.editProfile(seed, "new", seed.baseUrl, AuthType.CODE_SERVER_PASSWORD, "")

        // NEUTER: if rename didn't move the secret, login under "new" would find
        // no credential — the credential would be orphaned under "old".
        assertEquals("sekret", vault.map["new"])
        assertNull(vault.map["old"])
    }

    @Test
    fun add_with_blank_secret_reuses_same_host_password() = runTest {
        // Existing profile on oldHost:15000 with a stored password.
        val seed = profile("port15000", oldHost).copy(
            baseUrl = "https://$oldHost/proxy/15000/", authType = AuthType.CODE_SERVER_PASSWORD)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("port15000", "sharedpw")
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        // Add a SECOND profile on the SAME host, different port, BLANK password.
        ctl.addProfile("port8080", "https://$oldHost/proxy/8080/",
            AuthType.CODE_SERVER_PASSWORD, secret = "")

        // NEUTER: without host-based reuse the new alias would have NO secret.
        assertEquals("sharedpw", vault.map["port8080"])
    }

    @Test
    fun add_with_blank_secret_does_NOT_borrow_from_a_different_host() = runTest {
        val seed = profile("other", oldHost).copy(authType = AuthType.CODE_SERVER_PASSWORD)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("other", "sharedpw")
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        // New profile on a DIFFERENT host, blank password → must NOT borrow.
        ctl.addProfile("newone", "https://$newHost/proxy/15000/",
            AuthType.CODE_SERVER_PASSWORD, secret = "")

        assertNull(vault.map["newone"])
    }

    @Test
    fun findSharedSecret_matches_host_ignoring_port() = runTest {
        val seed = profile("p15000", oldHost).copy(
            baseUrl = "https://$oldHost/proxy/15000/")
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("p15000", "sharedpw")
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        assertEquals("sharedpw", ctl.findSharedSecret("https://$oldHost/proxy/9999/"))
        assertNull(ctl.findSharedSecret("https://$newHost/proxy/15000/"))
        // excludeAlias skips the profile itself (edit case).
        assertNull(ctl.findSharedSecret("https://$oldHost/proxy/15000/", excludeAlias = "p15000"))
    }

    /**
     * THE HOTTEST STALE-SNAPSHOT PATH. `activate` runs on EVERY card tap, and
     * its argument is a row rendered from the `observeAll()` Flow — a snapshot
     * that lags any write not yet re-emitted and recomposed.
     *
     * The damaging sequence: a previous session stamped `cookieHost`; the Flow
     * has not yet pushed that value into the frame the user is looking at; the
     * user taps the card; a full-row `dao.update(profile.copy(lastUsedAt = …))`
     * writes the STALE row back and erases the stamp — silently re-locking the
     * supervisor controls the user had already unlocked. That is the exact
     * symptom chased across the last several rounds, entered by a card tap.
     *
     * NEUTER CHECK: restore `dao.update(profile.copy(lastUsedAt = clock()))`
     * and this fails — cookieHost comes back null.
     */
    @Test
    fun activate_does_not_roll_back_a_freshly_stamped_cookie_host() = runTest {
        // The row as it truly is: a login already stamped cookieHost.
        val seed = profile("dc1", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), clock = { 999L })

        // What the list is still rendering: the pre-stamp row.
        val staleFromFlow = seed.copy(cookieHost = null, lastUsedAt = 1L)

        val result = ctl.activate(staleFromFlow)

        assertEquals("the stamp must survive a card tap", oldHost, dao.rows[1]!!.cookieHost)
        assertEquals("recency must still be bumped", 999L, dao.rows[1]!!.lastUsedAt)
        // …and the caller must be handed the authoritative row, not its snapshot.
        assertEquals(oldHost, result.persisted.cookieHost)
    }

    /** A row deleted between render and tap must not crash or resurrect itself. */
    @Test
    fun activate_falls_back_to_the_given_row_when_it_no_longer_exists() = runTest {
        val dao = FakeDao(); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))
        val ghost = profile("gone", oldHost, id = 42)

        val result = ctl.activate(ghost)

        assertEquals("gone", result.persisted.alias)
        assertTrue("must not resurrect a deleted row", dao.rows.isEmpty())
    }

    @Test
    fun delete_removes_secret_and_row() = runTest {
        val seed = profile("dc1", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("dc1", "sekret")
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        ctl.deleteProfile(seed)

        assertNull("secret must be removed", vault.map["dc1"])
        assertTrue("row must be deleted", dao.deleted.contains(seed.id))
    }

    @Test
    fun migrate_flips_stale_proxy_none_leaves_others_untouched() = runTest {
        // Three rows: a stale /proxy/ profile on NONE (must flip), a bare-host
        // NONE (leave), and a proxy profile explicitly on INTERACTIVE_SSO (leave).
        val dao = FakeDao(); val vault = FakeVault(); val sink = FakeCookieSink()
        val proxyNone = Profile(id = 1, alias = "shanghai",
            baseUrl = "https://abc-vscode-dc2.codelab.example.com/proxy/15000/",
            authType = AuthType.NONE)
        val bareNone = Profile(id = 2, alias = "bare",
            baseUrl = "https://tofu.example.com/", authType = AuthType.NONE)
        val proxySso = Profile(id = 3, alias = "sso",
            baseUrl = "https://h/proxy/8080/", authType = AuthType.INTERACTIVE_SSO)
        dao.rows[1] = proxyNone; dao.rows[2] = bareNone; dao.rows[3] = proxySso
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        val fixed = ctl.migrateProxyAuthDefaults()

        // NEUTER: make ServerUrl.needsProxyAuthFix return false and this fails
        // — the stale Shanghai row stays on NONE and can't headless-login.
        assertEquals("only the stale proxy row is fixed", 1, fixed)
        assertEquals(AuthType.CODE_SERVER_PASSWORD, dao.rows[1]!!.authType)
        assertEquals("bare host untouched", AuthType.NONE, dao.rows[2]!!.authType)
        assertEquals("explicit SSO untouched", AuthType.INTERACTIVE_SSO, dao.rows[3]!!.authType)

        // Idempotent: a second run fixes nothing.
        assertEquals(0, ctl.migrateProxyAuthDefaults())
    }

    /**
     * The migration iterates a `getAllOnce()` SNAPSHOT and suspends on each
     * write, running on `viewModelScope` concurrently with the UI that
     * `setContent` builds moments later — so a card tap can land between the
     * read and the write. A full-row update would then reinstate that row's
     * pre-tap `cookieHost` / `lastUsedAt` from the snapshot.
     *
     * NEUTER CHECK: restore `dao.update(p.copy(authType = …))` and this fails —
     * the concurrently-stamped cookieHost is wiped.
     */
    @Test
    fun migration_does_not_roll_back_a_concurrent_write() = runTest {
        val dao = FakeDao(); val vault = FakeVault(); val sink = FakeCookieSink()
        val staleRow = Profile(
            id = 1, alias = "shanghai",
            baseUrl = "https://abc-vscode-dc2.codelab.example.com/proxy/15000/",
            authType = AuthType.NONE, cookieHost = null, lastUsedAt = 0L,
        )
        dao.rows[1] = staleRow
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        // The interleaving: a login stamps this row AFTER the migration has
        // taken its snapshot but BEFORE it writes. Doing it earlier would prove
        // nothing — the snapshot would already contain the new values.
        dao.afterSnapshot = {
            dao.setCookieHost(1, "abc-vscode-dc2.codelab.example.com")
            dao.touchLastUsed(1, 777L)
        }

        ctl.migrateProxyAuthDefaults()

        val row = dao.rows[1]!!
        assertEquals("the migration must still do its job",
            AuthType.CODE_SERVER_PASSWORD, row.authType)
        assertEquals("a concurrent stamp must survive",
            "abc-vscode-dc2.codelab.example.com", row.cookieHost)
        assertEquals("a concurrent recency bump must survive", 777L, row.lastUsedAt)
    }
}
