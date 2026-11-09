package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.KernelKind
import com.tofu.client.data.SshEndpoint
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.launch
import kotlinx.coroutines.cancelAndJoin
import okhttp3.Cookie
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
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

    private class FakeDao(seed: Profile? = null) : ProfileDaoFixture {
        val rows = mutableMapOf<Long, Profile>()
        val deleted = mutableListOf<Long>()
        var nextId = 1L
        init { if (seed != null) rows[seed.id] = seed }
        override fun observeAll(): Flow<List<Profile>> = flowOf(rows.values.toList())
        var beforeIdRead: (suspend (Long) -> Unit)? = null
        override suspend fun getById(id: Long): Profile? {
            beforeIdRead?.invoke(id)
            return rows[id]
        }
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
        var beforeAliasRead: (suspend (String) -> Unit)? = null
        override suspend fun getByAlias(alias: String): Profile? {
            beforeAliasRead?.invoke(alias)
            return rows.values.firstOrNull { it.alias == alias }
        }
        var beforeInsert: (suspend (Profile) -> Unit)? = null
        var afterInsert: (suspend (Profile) -> Unit)? = null
        var beforeUpdate: (suspend (Profile) -> Unit)? = null
        var afterUpdate: (suspend (Profile) -> Unit)? = null
        override suspend fun insert(profile: Profile): Long {
            beforeInsert?.invoke(profile)
            val id = nextId++; rows[id] = profile.copy(id = id)
            afterInsert?.invoke(rows[id]!!)
            return id
        }
        override suspend fun update(profile: Profile): Int {
            beforeUpdate?.invoke(profile)
            if (profile.id !in rows) return 0
            rows[profile.id] = profile
            afterUpdate?.invoke(profile)
            return 1
        }
        override suspend fun saveConnectionIfCurrent(
            id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType,
            expectedProjectPath: String?, alias: String, baseUrl: String, authType: AuthType,
            instanceUuid: String?, projectPath: String?,
        ): Int {
            val before = rows[id] ?: return 0
            beforeUpdate?.invoke(before.copy(alias = alias, baseUrl = baseUrl, authType = authType, projectPath = projectPath))
            // Inject BEFORE the atomic query decision, not after a stale read.
            val row = rows[id] ?: return 0
            if (row.kind != KernelKind.REMOTE_URL || row.baseUrl != expectedUrl || row.alias != expectedAlias ||
                row.authType != expectedAuthType || row.projectPath != expectedProjectPath) return 0
            val saved = row.copy(
                alias = alias, baseUrl = baseUrl, authType = authType, instanceUuid = instanceUuid, projectPath = projectPath,
                cookieHost = row.cookieHost.takeIf { row.baseUrl == baseUrl && row.authType == authType },
            )
            rows[id] = saved
            afterUpdate?.invoke(saved)
            return 1
        }
        var beforeDelete: (suspend () -> Unit)? = null
        var afterDelete: (suspend () -> Unit)? = null
        override suspend fun deleteById(id: Long) {
            beforeDelete?.invoke(); rows.remove(id); deleted += id; afterDelete?.invoke()
        }
        override suspend fun setCookieHost(id: Long, host: String?): Int {
            val row = rows[id] ?: return 0
            rows[id] = row.copy(cookieHost = host)
            return 1
        }
        override suspend fun setCookieHostIfCurrent(id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType, host: String): Int {
            val row = getById(id) ?: return 0
            return if (row.kind == KernelKind.REMOTE_URL && row.baseUrl == expectedUrl && row.alias == expectedAlias && row.authType == expectedAuthType) setCookieHost(id, host) else 0
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
        override suspend fun pinSshHostKey(id: Long, host: String, port: Int, sha256: String): Int = 0
    }

    /**
     * Every profile here is a REMOTE_URL server, whose path predates kernels:
     * consulting any kernel controller fails the test.
     */
    private object NoKernelCalls : KernelController {
        override suspend fun ensureRunning(profile: Profile): KernelEndpoint =
            throw AssertionError("a URL profile consulted a kernel controller")
        override suspend fun stop(profile: Profile) = throw AssertionError("a URL profile stopped a kernel")
        override fun state(profile: Profile): StateFlow<KernelState> =
            throw AssertionError("a URL profile read kernel state")
    }

    private val urlOnly = KernelControllers(NoKernelCalls, NoKernelCalls, NoKernelCalls)

    private fun mgr(dao: ProfileDao, vault: SecretVault, sink: FakeCookieSink) =
        // All HTTP is intercepted: even a saved password must never trigger external DNS.
        SessionManager(dao, vault, sink, OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
                .code(200).message("OK").body("<html>interactive sign-in</html>".toResponseBody("text/html".toMediaType())).build()
        }.build())

    private fun profile(alias: String, host: String, id: Long = 1) = Profile(
        id = id, alias = alias, baseUrl = "https://$host/proxy/15000/",
        authType = AuthType.NONE, instanceUuid = host.substringBefore("-vscode"),
        cookieHost = host,
    )

    @Test
    fun add_stores_secret_after_insert_before_login() = runTest {
        val dao = FakeDao(); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), urlOnly, clock = { 42L }, report = {})

        dao.beforeInsert = { assertNull("Room must commit before a vault write", vault.secretFor(it.alias)) }
        val res = ctl.addProfile("dc1", "https://$oldHost/proxy/15000/",
            AuthType.CODE_SERVER_PASSWORD, "sekret")

        assertTrue(res is SessionController.AddResult.Added)
        // Secret stored under the alias, row inserted.
        assertEquals("sekret", vault.map["dc1"])
        assertEquals(1, dao.rows.size)
    }

    @Test fun scoped_edit_preserves_concurrent_recency_and_session_stamps() = runTest {
        val seed = profile("same", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
        dao.beforeUpdate = { dao.rows[seed.id] = seed.copy(cookieHost = "fresh-cookie-host", lastUsedAt = 999) }
        controller.editProfile(seed, seed.alias, seed.baseUrl, seed.authType, "")
        assertEquals("fresh-cookie-host", dao.rows[seed.id]!!.cookieHost)
        assertEquals(999L, dao.rows[seed.id]!!.lastUsedAt)
    }

    @Test fun a_connection_changed_before_the_atomic_save_is_not_rolled_back_or_given_a_secret() = runTest {
        val seed = profile("old", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        val foreign = seed.copy(authType = AuthType.INTERACTIVE_SSO, projectPath = "/foreign/project")
        val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
        dao.beforeUpdate = { dao.rows[seed.id] = foreign }
        val result = controller.editProfile(seed, "renamed", seed.baseUrl, seed.authType, "fixture-new")
        assertTrue(result.login is LoginResult.Error)
        assertEquals(foreign, dao.rows[seed.id])
        assertNull(vault.secretFor("renamed"))
    }

    @Test fun a_kind_changed_before_the_atomic_url_save_is_not_given_a_secret() = runTest {
        for (kind in listOf(KernelKind.LOCAL, KernelKind.SSH)) {
            val seed = profile("old", oldHost)
            val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
            vault.putSecret(seed.alias, "fixture-old")
            val foreign = seed.copy(kind = kind, ssh = SshEndpoint(host = "ssh.example", user = "me"))
            dao.beforeUpdate = { dao.rows[seed.id] = foreign }
            val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
            val result = controller.editProfile(seed, "renamed", seed.baseUrl, seed.authType, "fixture-new")
            assertTrue(result.login is LoginResult.Error)
            assertEquals(foreign, dao.rows[seed.id])
            assertEquals("fixture-old", vault.secretFor(seed.alias))
            assertNull(vault.secretFor("renamed"))
        }
    }

    @Test fun url_edit_helpers_cannot_persist_an_internal_runtime_endpoint_or_borrow_its_account_password() = runTest {
        val source = profile("ssh", oldHost).copy(kind = KernelKind.SSH, ssh = SshEndpoint(host = oldHost, user = "me"))
        val dao = FakeDao(source); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret(source.alias, "fixture-ssh-account")
        val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
        assertNull(controller.findSharedSecret(source.baseUrl))
        val bound = source.boundTo(KernelEndpoint("http://127.0.0.2:40000/", "127.0.0.2"))
        val result = controller.editProfile(bound, "renamed", bound.baseUrl, AuthType.CODE_SERVER_PASSWORD, "fixture-new")
        assertTrue(result.login is LoginResult.Error)
        assertEquals(source, dao.rows[source.id])
        assertNull(vault.secretFor("renamed"))
        assertEquals("fixture-ssh-account", vault.secretFor(source.alias))
    }

    @OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
    @Test fun url_readback_and_vault_reconciliation_share_one_five_second_budget() = runTest {
        val seed = profile("old", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret(seed.alias, "fixture-old")
        val reports = mutableListOf<String>()
        val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = { reports += it })
        dao.afterUpdate = {
            // Both the URL readback and the vault callback need a fresh row.
            // Separate five-second budgets would permit eight seconds here.
            dao.beforeIdRead = { kotlinx.coroutines.delay(4_000L) }
            error("injected lost committed result")
        }
        try {
            controller.editProfile(seed, "renamed", "https://$newHost/proxy/15000/", AuthType.NONE, "fixture-new")
            org.junit.Assert.fail("the original commit-result failure must propagate")
        } catch (_: IllegalStateException) { }
        assertEquals(SessionManager.SAVE_RECONCILIATION_MS, testScheduler.currentTime)
        assertEquals("renamed", dao.rows[seed.id]!!.alias)
        assertEquals("https://$newHost/proxy/15000/", dao.rows[seed.id]!!.baseUrl)
        assertEquals("fixture-old", vault.secretFor(seed.alias))
        assertNull(vault.secretFor("renamed"))
        assertEquals(listOf(oldHost), sink.purged)
        assertTrue(reports.any { "reconciliation incomplete" in it })
    }

    @Test fun a_lost_url_commit_result_does_not_reconcile_a_row_whose_kind_changed() = runTest {
        val seed = profile("old", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret(seed.alias, "fixture-old")
        val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
        dao.afterUpdate = {
            dao.rows[seed.id] = it.copy(kind = KernelKind.SSH)
            error("injected lost committed result")
        }
        try {
            controller.editProfile(seed, "renamed", "https://$newHost/proxy/15000/", AuthType.NONE, "fixture-new")
            org.junit.Assert.fail("the original save failure must propagate")
        } catch (_: IllegalStateException) { }
        assertEquals(KernelKind.SSH, dao.rows[seed.id]!!.kind)
        assertEquals("fixture-old", vault.secretFor(seed.alias))
        assertNull(vault.secretFor("renamed"))
        assertTrue(sink.purged.isEmpty())
    }

    @OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
    @Test fun reconciliation_readback_is_bounded_and_never_rolls_back_a_committed_insert() = runTest {
        val dao = FakeDao(); val vault = FakeVault(); val sink = FakeCookieSink()
        var reads = 0
        dao.beforeAliasRead = { if (++reads > 1) CompletableDeferred<Unit>().await() }
        dao.afterInsert = { error("injected lost committed result") }
        val reports = mutableListOf<String>()
        val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = { reports += it })
        try {
            controller.addProfile("new", "https://$oldHost/proxy/15000/", AuthType.CODE_SERVER_PASSWORD, "fixture-new")
            org.junit.Assert.fail("the original save failure must propagate")
        } catch (_: IllegalStateException) { }
        assertEquals(SessionManager.SAVE_RECONCILIATION_MS, testScheduler.currentTime)
        assertEquals(1, dao.rows.size)
        assertNull(vault.secretFor("new"))
        assertTrue(reports.any { "reconciliation incomplete" in it })
    }

    @Test fun failed_room_insert_or_edit_never_stages_or_moves_a_credential() = runTest {
        val seed = profile("old", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("old", "fixture-old")
        val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
        dao.beforeInsert = { error("injected Room insert failure") }
        try {
            controller.addProfile("new", seed.baseUrl, AuthType.CODE_SERVER_PASSWORD, "fixture-new")
            org.junit.Assert.fail("Room failure must be surfaced")
        } catch (_: IllegalStateException) { }
        assertNull(vault.secretFor("new"))
        dao.beforeUpdate = { error("injected Room update failure") }
        try {
            controller.editProfile(seed, "renamed", "https://$newHost/proxy/15000/", AuthType.NONE, "fixture-new")
            org.junit.Assert.fail("Room failure must be surfaced")
        } catch (_: IllegalStateException) { }
        assertEquals(seed, dao.rows[seed.id])
        assertEquals("fixture-old", vault.secretFor("old"))
        assertNull(vault.secretFor("renamed"))
        assertTrue(sink.purged.isEmpty())
    }

    @Test fun committed_edit_preserves_a_foreign_vault_value_instead_of_rolling_back_room() = runTest {
        val seed = profile("old", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("old", "fixture-old")
        val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
        dao.afterUpdate = { vault.putSecret("renamed", "fixture-foreign") }
        controller.editProfile(seed, "renamed", seed.baseUrl, AuthType.NONE, "fixture-new")
        assertEquals("renamed", dao.rows[seed.id]!!.alias)
        assertEquals("fixture-foreign", vault.secretFor("renamed"))
        assertEquals("fixture-old", vault.secretFor("old"))
    }

    @Test fun vault_failure_after_commit_keeps_the_committed_room_row_and_reports_reconciliation() = runTest {
        val seed = profile("old", oldHost)
        val dao = FakeDao(seed); val backing = FakeVault(); val sink = FakeCookieSink()
        backing.putSecret("old", "fixture-old")
        val vault = object : SecretVault {
            override fun secretFor(alias: String) = backing.secretFor(alias)
            override fun putSecret(alias: String, secret: String) { error("injected vault failure") }
            override fun removeSecret(alias: String) = backing.removeSecret(alias)
        }
        val reports = mutableListOf<String>()
        val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = { reports += it })
        controller.editProfile(seed, "renamed", seed.baseUrl, AuthType.NONE, "fixture-new")
        assertEquals("renamed", dao.rows[seed.id]!!.alias)
        assertEquals("fixture-old", backing.secretFor("old"))
        assertNull(backing.secretFor("renamed"))
        assertTrue(reports.any { "reconciliation incomplete" in it })
        assertTrue(reports.none { "fixture-new" in it || "fixture-old" in it })
    }

    @Test fun cancelled_add_at_room_stage_reconciles_only_its_own_secret() = runTest {
        for (committed in listOf(false, true)) {
            val dao = FakeDao(); val vault = FakeVault(); val sink = FakeCookieSink()
            val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
            val entered = CompletableDeferred<Unit>()
            val release = CompletableDeferred<Unit>()
            val wait: suspend (Profile) -> Unit = { entered.complete(Unit); release.await() }
            if (committed) dao.afterInsert = wait else dao.beforeInsert = wait
            val job = launch { controller.addProfile("pending", "https://$oldHost/proxy/15000/", AuthType.CODE_SERVER_PASSWORD, "fixture-new") }
            entered.await()
            job.cancelAndJoin()
            if (committed) {
                assertEquals("committed row must keep the staged credential", "fixture-new", vault.secretFor("pending"))
                assertEquals(1, dao.rows.size)
            } else {
                assertNull("cancelled insert must not orphan the staged credential", vault.secretFor("pending"))
                assertTrue(dao.rows.isEmpty())
            }
        }
    }

    @Test fun cancelled_add_reconciliation_cannot_claim_a_newer_identical_save() = runTest {
        val dao = FakeDao(); val vault = FakeVault(); val sink = FakeCookieSink()
        val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
        val oldInsert = CompletableDeferred<Unit>()
        val oldReadback = CompletableDeferred<Unit>()
        val releaseOld = CompletableDeferred<Unit>()
        val releaseNew = CompletableDeferred<Unit>()
        var reads = 0
        dao.beforeAliasRead = {
            reads++
            if (reads == 2) { oldReadback.complete(Unit); releaseOld.await() }
        }
        dao.beforeInsert = {
            dao.beforeInsert = null
            oldInsert.complete(Unit); CompletableDeferred<Unit>().await()
        }
        val old = launch { controller.addProfile("same", "https://$oldHost/proxy/15000/", AuthType.CODE_SERVER_PASSWORD, "fixture-old") }
        oldInsert.await(); old.cancel(); oldReadback.await()
        dao.afterInsert = { releaseNew.await() }
        val newer = launch { controller.addProfile("same", "https://$oldHost/proxy/15000/", AuthType.CODE_SERVER_PASSWORD, "fixture-new") }
        testScheduler.runCurrent()
        releaseOld.complete(Unit); old.join()
        releaseNew.complete(Unit); newer.join()
        assertEquals("a cancelled operation cannot attach its password to a newer row", "fixture-new", vault.secretFor("same"))
        assertEquals(1, dao.rows.size)
    }

    @Test fun cancelled_origin_change_purges_only_a_committed_old_origin() = runTest {
        for (committed in listOf(false, true)) {
            val seed = profile("same", oldHost)
            val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
            val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
            val entered = CompletableDeferred<Unit>()
            val wait: suspend (Profile) -> Unit = { entered.complete(Unit); CompletableDeferred<Unit>().await() }
            if (committed) dao.afterUpdate = wait else dao.beforeUpdate = wait
            val save = launch { controller.editProfile(seed, seed.alias, "https://$newHost/proxy/15000/", AuthType.NONE, "") }
            entered.await(); save.cancelAndJoin()
            assertEquals(if (committed) listOf(oldHost) else emptyList<String>(), sink.purged)
            assertEquals(if (committed) "https://$newHost/proxy/15000/" else seed.baseUrl, dao.rows[seed.id]!!.baseUrl)
        }
    }

    @Test fun cancelled_delete_keeps_the_secret_unless_the_row_was_removed() = runTest {
        for (committed in listOf(false, true)) {
            val seed = profile("same", oldHost)
            val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
            vault.putSecret(seed.alias, "fixture-old")
            val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
            val entered = CompletableDeferred<Unit>()
            val wait: suspend () -> Unit = { entered.complete(Unit); CompletableDeferred<Unit>().await() }
            if (committed) dao.afterDelete = wait else dao.beforeDelete = wait
            val deletion = launch { controller.deleteProfile(seed) }
            entered.await(); deletion.cancelAndJoin()
            assertEquals(if (committed) null else "fixture-old", vault.secretFor(seed.alias))
            assertEquals(!committed, dao.rows.containsKey(seed.id))
        }
    }

    @Test fun cancelled_rename_at_room_stage_keeps_the_credential_for_the_authoritative_row() = runTest {
        for (committed in listOf(false, true)) {
            val seed = profile("old", oldHost)
            val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
            vault.putSecret("old", "fixture-old")
            val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
            val entered = CompletableDeferred<Unit>()
            val release = CompletableDeferred<Unit>()
            val wait: suspend (Profile) -> Unit = { entered.complete(Unit); release.await() }
            if (committed) dao.afterUpdate = wait else dao.beforeUpdate = wait
            val job = launch { controller.editProfile(seed, "new", seed.baseUrl, AuthType.NONE, "fixture-new") }
            entered.await()
            job.cancelAndJoin()
            assertEquals(if (committed) "new" else "old", dao.rows[seed.id]!!.alias)
            assertEquals(if (committed) null else "fixture-old", vault.secretFor("old"))
            assertEquals(if (committed) "fixture-new" else null, vault.secretFor("new"))
        }
    }

    @Test fun cancelled_same_alias_edit_preserves_old_or_foreign_secret_without_row_rollback() = runTest {
        for (foreignSecret in listOf(false, true)) {
            val seed = profile("same", oldHost)
            val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
            vault.putSecret(seed.alias, "fixture-old")
            val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
            val entered = CompletableDeferred<Unit>()
            val release = CompletableDeferred<Unit>()
            dao.beforeUpdate = {
                if (foreignSecret) vault.putSecret(seed.alias, "fixture-foreign")
                entered.complete(Unit); release.await()
            }
            val job = launch { controller.editProfile(seed, seed.alias, "https://$oldHost/proxy/16000/", AuthType.NONE, "fixture-new") }
            entered.await()
            job.cancelAndJoin()
            assertEquals(seed, dao.rows[seed.id])
            assertEquals(if (foreignSecret) "fixture-foreign" else "fixture-old", vault.secretFor(seed.alias))
        }
    }

    @Test fun credential_urls_fail_before_legacy_or_new_profile_writes() = runTest {
        val unsafe = profile("legacy", oldHost).copy(baseUrl = "https://$oldHost/proxy/15000/?token=fixture-value")
        val dao = FakeDao(unsafe); val vault = FakeVault(); val sink = FakeCookieSink()
        val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
        val result = controller.activate(unsafe)
        assertTrue(result.login is LoginResult.Error)
        assertEquals("unsafe legacy profile must not be rewritten or touched", unsafe, dao.rows[unsafe.id])
        try {
            controller.addProfile("new", unsafe.baseUrl, AuthType.CODE_SERVER_PASSWORD, "fixture-password")
            org.junit.Assert.fail("URL credentials must fail before vault/Room writes")
        } catch (error: IllegalArgumentException) {
            assertTrue(error.message.orEmpty().contains("credential", ignoreCase = true))
            assertTrue(!error.message.orEmpty().contains("fixture-value"))
        }
        assertNull(vault.secretFor("new"))
        assertEquals(1, dao.rows.size)
    }

    @Test
    fun add_rejects_duplicate_alias() = runTest {
        val dao = FakeDao(profile("dc1", oldHost)); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), urlOnly, report = {})
        val res = ctl.addProfile("dc1", "https://$oldHost/proxy/15000/", AuthType.NONE, "")
        assertTrue(res is SessionController.AddResult.DuplicateAlias)
    }

    @Test
    fun edit_url_host_change_purges_old_jar() = runTest {
        val seed = profile("dc1", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), urlOnly, report = {})

        ctl.editProfile(seed, "dc1", "https://$newHost/proxy/15000/", AuthType.NONE, "")

        // NEUTER: if editProfile didn't route host-changes through
        // updateUrlAndReauth, the old Domain-pinned jar would survive.
        assertEquals(listOf(oldHost), sink.purged)
    }

    @Test fun changed_same_origin_url_clears_marker_without_purging_unrelated_gateway_cookies() = runTest {
        val seed = profile("test", oldHost).copy(
            baseUrl = "https://$oldHost/dev-a/proxy/15000/", authType = AuthType.CODE_SERVER_PASSWORD)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        val controller = SessionController(dao, vault, mgr(dao, vault, sink), report = {})
        val result = controller.editProfile(seed, seed.alias, "https://$oldHost/dev-b/proxy/15000/", seed.authType, "")
        assertTrue(result.login is LoginResult.NeedsInteractiveSso)
        assertNull("new gateway prefix has not authenticated", dao.rows[seed.id]!!.cookieHost)
        assertTrue("same-origin cookie jar remains shared", sink.purged.isEmpty())
    }

    @Test
    fun edit_same_host_does_not_purge() = runTest {
        val seed = profile("dc1", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), urlOnly, report = {})
        ctl.editProfile(seed, "dc1", "https://$oldHost/proxy/16000/", AuthType.NONE, "")
        assertTrue(sink.purged.isEmpty())
    }

    @Test
    fun rename_moves_alias_keyed_secret() = runTest {
        val seed = profile("old", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("old", "sekret")
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), urlOnly, report = {})

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
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), urlOnly, report = {})

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
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), urlOnly, report = {})

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
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), urlOnly, report = {})

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
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), urlOnly, clock = { 999L }, report = {})

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
    fun activate_refuses_the_given_row_when_it_no_longer_exists() = runTest {
        val dao = FakeDao(); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), urlOnly, report = {})
        val ghost = profile("gone", oldHost, id = 42)

        val result = ctl.activate(ghost)

        assertEquals("gone", result.persisted.alias)
        assertTrue("a deleted row must not open the WebView", result.login is LoginResult.Error)
        assertTrue("must not resurrect a deleted row", dao.rows.isEmpty())
    }

    @Test
    fun delete_removes_secret_and_row() = runTest {
        val seed = profile("dc1", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("dc1", "sekret")
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), urlOnly, report = {})

        ctl.deleteProfile(seed)

        assertNull("secret must be removed", vault.map["dc1"])
        assertTrue("row must be deleted", dao.deleted.contains(seed.id))
    }



    @Test fun explicit_none_on_a_proxy_url_survives_activation_and_editing() = runTest {
        val original = profile("test", oldHost).copy(authType = AuthType.NONE)
        val dao = FakeDao(original); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret(original.alias, "previously-saved-password")
        val controller = SessionController(dao, vault, mgr(dao, vault, sink), urlOnly, report = {})
        val activated = controller.activate(original)
        assertTrue(activated.login is LoginResult.Success)
        assertEquals(AuthType.NONE, dao.rows[original.id]!!.authType)
        val edited = controller.editProfile(original, original.alias, original.baseUrl, AuthType.NONE, "")
        assertTrue(edited.login is LoginResult.Success)
        assertEquals(AuthType.NONE, edited.persisted.authType)
        assertEquals("previously-saved-password", vault.secretFor(original.alias))
    }
}
