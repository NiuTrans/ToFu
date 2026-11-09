package com.tofu.client.session

import com.tofu.client.api.ApiV4Contract
import com.tofu.client.data.AuthType
import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import com.tofu.client.data.SshEndpoint
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import okhttp3.Cookie
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Kind dispatch: LOCAL and SSH kernels are brought up by their
 * [KernelController] and signed in at the endpoint it returns, through the
 * API gate only; REMOTE_URL never reaches a controller (the URL path itself is
 * pinned by [SessionControllerTest]).
 */
class SessionControllerKernelTest {

    private class FakeCookieSink : CookieSink {
        override fun inject(origin: String, cookies: List<Cookie>) {}
        override fun purgeHost(host: String) {}
        override fun cookieHeader(origin: String): String? = null
    }

    private class FakeVault : SecretVault {
        val map = mutableMapOf<String, String>()
        val reads = mutableListOf<String>()
        override fun secretFor(alias: String): String? { reads += alias; return map[alias] }
        override fun putSecret(alias: String, secret: String) { map[alias] = secret }
        override fun removeSecret(alias: String) { map.remove(alias) }
    }

    private class FakeDao(vararg seed: Profile) : ProfileDaoFixture {
        val rows = seed.associateBy { it.id }.toMutableMap()
        var nextId = 100L
        var fullRowUpdates = 0
        val cookieWrites = mutableListOf<Long>()
        var beforeIdRead: (suspend (Long) -> Unit)? = null
        var beforeInsert: (suspend (Profile) -> Unit)? = null
        var afterInsert: (suspend (Profile) -> Unit)? = null
        var beforeSshSave: (suspend () -> Unit)? = null
        var afterSshSave: (suspend (Profile) -> Unit)? = null
        override fun observeAll(): Flow<List<Profile>> = flowOf(rows.values.toList())
        override suspend fun getById(id: Long): Profile? { beforeIdRead?.invoke(id); return rows[id] }
        override suspend fun getAllOnce(): List<Profile> = rows.values.toList()
        override suspend fun getByAlias(alias: String) = rows.values.firstOrNull { it.alias == alias }
        override suspend fun insert(profile: Profile): Long {
            beforeInsert?.invoke(profile)
            val id = nextId++; rows[id] = profile.copy(id = id)
            afterInsert?.invoke(rows.getValue(id))
            return id
        }
        override suspend fun update(profile: Profile): Int {
            fullRowUpdates++
            if (profile.id !in rows) return 0
            rows[profile.id] = profile
            return 1
        }
        override suspend fun saveSshConnectionIfCurrent(
            id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType,
            expectedProjectPath: String?, expectedHost: String, expectedPort: Int, expectedUser: String,
            expectedRemotePort: Int, alias: String, host: String, port: Int, user: String, remotePort: Int,
        ): Int {
            // The hook lands BEFORE the atomic decision, so a full-row update
            // or snapshot-supplied trust pin cannot hide a real interleaving.
            beforeSshSave?.invoke()
            val current = rows[id] ?: return 0
            val ssh = current.ssh ?: return 0
            if (current.kind != KernelKind.SSH || current.baseUrl.isNotEmpty() || current.baseUrl != expectedUrl ||
                current.alias != expectedAlias || current.authType != expectedAuthType || current.projectPath != expectedProjectPath ||
                ssh.host != expectedHost || ssh.port != expectedPort || ssh.user != expectedUser || ssh.remotePort != expectedRemotePort) return 0
            val saved = current.copy(alias = alias, ssh = ssh.copy(
                host = host, port = port, user = user, remotePort = remotePort,
                hostKeySha256 = ssh.hostKeySha256.takeIf { ssh.host == host && ssh.port == port },
            ))
            rows[id] = saved
            afterSshSave?.invoke(saved)
            return 1
        }
        override suspend fun deleteById(id: Long) { rows.remove(id) }
        override suspend fun setCookieHost(id: Long, host: String?): Int {
            cookieWrites += id
            val row = rows[id] ?: return 0
            rows[id] = row.copy(cookieHost = host)
            return 1
        }
        override suspend fun setCookieHostIfCurrent(id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType, host: String): Int {
            val row = rows[id] ?: return 0
            return if (row.kind == KernelKind.REMOTE_URL && row.baseUrl == expectedUrl && row.alias == expectedAlias && row.authType == expectedAuthType) setCookieHost(id, host) else 0
        }
        override suspend fun touchLastUsed(id: Long, timestamp: Long): Int {
            val row = rows[id] ?: return 0
            rows[id] = row.copy(lastUsedAt = timestamp)
            return 1
        }
        override suspend fun setAuthType(id: Long, authType: AuthType): Int = 0
        override suspend fun pinSshHostKey(id: Long, host: String, port: Int, sha256: String): Int {
            val row = rows[id] ?: return 0
            val ssh = row.ssh ?: return 0
            if (row.kind != KernelKind.SSH || ssh.host != host || ssh.port != port || ssh.hostKeySha256 != null) return 0
            rows[id] = row.copy(ssh = ssh.copy(hostKeySha256 = sha256))
            return 1
        }
    }

    /** A kernel that comes up at [endpoint], recording what it was asked. */
    private class FakeKernel(private val endpoint: KernelEndpoint) : KernelController {
        val started = mutableListOf<Profile>()
        val stopped = mutableListOf<Profile>()
        val fronts = mutableListOf<Profile>()
        var afterStart: (suspend (Profile) -> Unit)? = null
        override fun frontChanged(front: Profile) { fronts += front }
        private val state = MutableStateFlow<KernelState>(KernelState.Stopped)
        override suspend fun ensureRunning(profile: Profile): KernelEndpoint {
            started += profile
            afterStart?.invoke(profile)
            state.value = KernelState.Ready
            return endpoint
        }
        override suspend fun stop(profile: Profile) { stopped += profile; state.value = KernelState.Stopped }
        override fun state(profile: Profile): StateFlow<KernelState> = state
    }

    /** Never consulted: REMOTE_URL profiles keep the pre-kernel path. */
    private object NoUrlKernel : KernelController {
        override suspend fun ensureRunning(profile: Profile): KernelEndpoint = throw AssertionError("URL kernel consulted")
        override suspend fun stop(profile: Profile) = throw AssertionError("URL kernel stopped")
        override fun state(profile: Profile): StateFlow<KernelState> = throw AssertionError("URL kernel state read")
    }

    /** Answers `/api/v4/meta` and records every request; nothing else is expected. */
    private class KernelServer(private val minAndroidBuild: Int = 1) : okhttp3.Interceptor {
        val requests = mutableListOf<Request>()
        var afterRequest: ((Request) -> Unit)? = null
        override fun intercept(chain: okhttp3.Interceptor.Chain): Response {
            val req = chain.request()
            requests += req
            afterRequest?.invoke(req)
            val meta = req.url.encodedPath.endsWith("/api/v4/meta")
            val body = if (meta) {
                """{"data":{"apiMajor":${ApiV4Contract.API_MAJOR},"minAndroidBuild":$minAndroidBuild,""" +
                    """"schemaVersion":1,"serverBuild":"x","minDesktopBuild":"0.0.1"},""" +
                    """"meta":{"requestId":"r","serverTimeMs":0}}"""
            } else "{}"
            return Response.Builder().request(req).protocol(Protocol.HTTP_1_1)
                .code(if (meta) 200 else 500).message("stub")
                .body(body.toResponseBody("application/json".toMediaType())).build()
        }
    }

    private val localEndpoint = KernelEndpoint("http://127.0.0.1:43123/", "127.0.0.1")
    private val sshEndpoint = KernelEndpoint("http://127.0.0.2:41234/", "127.0.0.2")
    private val box = Profile(
        id = 7, alias = "box", baseUrl = "", kind = KernelKind.SSH,
        ssh = SshEndpoint(host = "dev.example", user = "me", hostKeySha256 = "SHA256:pin"),
    )

    private fun controller(
        dao: FakeDao,
        vault: FakeVault = FakeVault(),
        server: KernelServer = KernelServer(),
        local: KernelController = UnavailableKernelController(),
        ssh: KernelController = UnavailableKernelController(),
        report: (String) -> Unit = {},
    ) = SessionController(
        dao, vault,
        SessionManager(dao, vault, FakeCookieSink(), OkHttpClient.Builder().addInterceptor(server).build(), appVersionCode = 17),
        KernelControllers(remoteUrl = NoUrlKernel, local = local, ssh = ssh),
        clock = { 5L }, report = report,
    )

    @Test fun this_phone_signs_in_at_the_endpoint_its_controller_returns() = runTest {
        val dao = FakeDao(); val server = KernelServer(); val local = FakeKernel(localEndpoint)
        val result = controller(dao, server = server, local = local).activate(KernelList.THIS_PHONE)

        assertEquals(listOf(KernelList.THIS_PHONE), local.started)
        assertEquals(LoginResult.Success("127.0.0.1"), result.login)
        assertEquals(KernelList.THIS_PHONE.boundTo(localEndpoint), result.persisted)
        // Only the API gate, at the kernel's own address — no gateway handshake.
        assertEquals(listOf("http://127.0.0.1:43123/api/v4/meta"), server.requests.map { it.url.toString() })
        assertTrue("the placeholder row is never persisted", dao.rows.isEmpty())
    }

    @Test fun a_kernel_whose_webview_opens_is_put_in_front_and_a_refused_one_is_not() = runTest {
        val url = Profile(id = 1, alias = "url", baseUrl = "https://u.example/")
        val dao = FakeDao(box, url); val local = FakeKernel(localEndpoint)
        val ctl = controller(dao, local = local)

        val opened = ctl.activate(KernelList.THIS_PHONE).persisted
        assertEquals(listOf(opened), local.fronts)

        ctl.activate(box) // SSH unavailable: the WebView stays where it was
        assertEquals(1, local.fronts.size)

        ctl.activate(url) // AuthType.NONE opens without a request
        assertEquals(listOf(opened, url.copy(lastUsedAt = 5L)), local.fronts)
    }

    @Test fun a_url_kernel_on_this_phones_host_is_refused_before_any_request() = runTest {
        val server = KernelServer()
        for (address in listOf("http://127.0.0.1:15000/", "http://127.1:15000/")) {
            val termux = Profile(id = 2, alias = "termux", baseUrl = address)
            val manager = SessionManager(FakeDao(termux), FakeVault(), FakeCookieSink(), OkHttpClient.Builder().addInterceptor(server).build())
            assertEquals(LoginResult.Error(LocalKernelCookie.RESERVED_HOST_MESSAGE), manager.login(termux))
        }
        assertTrue(server.requests.isEmpty())
    }

    @Test fun the_api_gate_still_refuses_an_incompatible_kernel() = runTest {
        val dao = FakeDao(box)
        val result = controller(dao, server = KernelServer(minAndroidBuild = 99), ssh = FakeKernel(sshEndpoint)).activate(box)
        assertTrue(result.login is LoginResult.Incompatible)
    }

    @Test fun an_unavailable_kernel_reports_its_reason_without_any_request() = runTest {
        val dao = FakeDao(box); val server = KernelServer()
        val result = controller(dao, server = server).activate(box)

        assertEquals(LoginResult.Error("not available in this build yet"), result.login)
        assertEquals("the unbound row comes back", box.copy(lastUsedAt = 5L), result.persisted)
        assertTrue(server.requests.isEmpty())
    }

    @Test fun the_live_endpoint_is_never_written_back_to_the_row() = runTest {
        val dao = FakeDao(box); val ssh = FakeKernel(sshEndpoint)
        val result = controller(dao, ssh = ssh).activate(box.copy(alias = "stale render"))

        assertEquals("the controller gets the re-read row", "box", ssh.started.single().alias)
        assertEquals("http://127.0.0.2:41234/", result.persisted.baseUrl)
        assertEquals("127.0.0.2", result.persisted.cookieHost)
        assertEquals("", dao.rows.getValue(7).baseUrl)
        assertNull(dao.rows.getValue(7).cookieHost)
        assertTrue("no session marker is stamped for a kernel", dao.cookieWrites.isEmpty())
        assertEquals("recency still bumps", 5L, dao.rows.getValue(7).lastUsedAt)
    }

    @Test fun a_kernel_ignores_gateway_auth_fields() = runTest {
        val source = box.copy(authType = AuthType.CODE_SERVER_PASSWORD)
        val dao = FakeDao(source); val vault = FakeVault().apply { putSecret("box", "pw") }; val server = KernelServer()
        val odd = source.boundTo(sshEndpoint)
        val manager = SessionManager(dao, vault, FakeCookieSink(), OkHttpClient.Builder().addInterceptor(server).build())

        assertEquals(LoginResult.Success("127.0.0.2"), manager.login(odd))
        assertEquals(listOf("/api/v4/meta"), server.requests.map { it.url.encodedPath })
        assertTrue("SSH account passwords never reach gateway auth", vault.reads.isEmpty())
        assertTrue(dao.cookieWrites.isEmpty())
    }

    @Test fun deleting_a_kernel_stops_it_first_but_a_url_server_keeps_running() = runTest {
        val url = Profile(id = 1, alias = "url", baseUrl = "https://u.example/")
        val dao = FakeDao(box, url); val vault = FakeVault().apply { putSecret("box", "k"); putSecret("url", "pw") }
        val ssh = FakeKernel(sshEndpoint)
        val ctl = controller(dao, vault, ssh = ssh)

        ctl.deleteProfile(box)
        assertEquals(listOf(box), ssh.stopped)
        assertNull(vault.map["box"])
        assertNull(dao.rows[7])

        ctl.deleteProfile(url) // NoUrlKernel fails the test if consulted
        assertNull(dao.rows[1])
    }

    @Test fun adding_an_ssh_kernel_saves_it_without_connecting() = runTest {
        val dao = FakeDao(); val server = KernelServer(); val ssh = FakeKernel(sshEndpoint)
        val ctl = controller(dao, server = server, ssh = ssh)
        val endpoint = SshEndpoint(host = "dev.example", port = 2222, user = "me", remotePort = 15001)

        val saved = ctl.addSshKernel(" box ", endpoint) as SessionController.AddResult.Saved
        assertEquals(Profile(id = saved.profile.id, alias = "box", baseUrl = "", lastUsedAt = 5L, kind = KernelKind.SSH, ssh = endpoint), saved.profile)
        assertEquals(saved.profile, dao.rows[saved.profile.id])
        assertTrue(ssh.started.isEmpty())
        assertTrue(server.requests.isEmpty())
        assertEquals(SessionController.AddResult.DuplicateAlias, ctl.addSshKernel("box", endpoint))
    }

    @Test fun an_ssh_password_goes_to_the_vault_under_the_alias_never_to_room() = runTest {
        val dao = FakeDao(); val vault = FakeVault()
        val ctl = controller(dao, vault)
        val endpoint = SshEndpoint(host = "dev.example", user = "me")

        val saved = (ctl.addSshKernel("box", endpoint, password = "hunter2") as SessionController.AddResult.Saved).profile
        assertEquals("hunter2", vault.map["box"])
        assertEquals(endpoint, dao.rows.getValue(saved.id).ssh)
        ctl.addSshKernel("keyonly", endpoint)
        assertNull("a key-only kernel stores no secret", vault.map["keyonly"])

        ctl.editSshKernel(saved, "box", endpoint)
        assertEquals("a blank password keeps the saved one", "hunter2", vault.map["box"])
        ctl.editSshKernel(saved, "box2", endpoint, newPassword = "changed")
        assertEquals("changed", vault.map["box2"])
        assertNull(vault.map["box"])
    }

    @Test fun editing_an_ssh_kernel_moves_its_secret_and_drops_the_pin_only_for_a_new_machine() = runTest {
        val dao = FakeDao(box); val vault = FakeVault().apply { putSecret("box", "key") }
        val ctl = controller(dao, vault)
        val sameMachine = box.ssh!!.copy(user = "other", remotePort = 16000, hostKeySha256 = null)

        val renamed = ctl.editSshKernel(box, "box2", sameMachine)
        assertEquals("key", vault.map["box2"])
        assertNull(vault.map["box"])
        assertEquals("user and remote port keep the pinned key", "SHA256:pin", renamed.ssh?.hostKeySha256)
        assertEquals(renamed, dao.rows[7])

        val moved = ctl.editSshKernel(renamed, "box2", sameMachine.copy(port = 2200, hostKeySha256 = "SHA256:forged"))
        assertNull("a new port is a new trust decision; the form cannot set a pin", moved.ssh?.hostKeySha256)
        val pinnedAgain = dao.rows.getValue(7).copy(ssh = moved.ssh!!.copy(hostKeySha256 = "SHA256:new"))
        dao.update(pinnedAgain)
        assertNull(ctl.editSshKernel(pinnedAgain, "box2", moved.ssh!!.copy(host = "elsewhere.example")).ssh?.hostKeySha256)
    }

    @Test fun a_deleted_saved_kernel_never_starts_or_uses_the_local_placeholder() = runTest {
        val dao = FakeDao(); val server = KernelServer()
        val local = FakeKernel(localEndpoint); val ssh = FakeKernel(sshEndpoint)
        val ctl = controller(dao, server = server, local = local, ssh = ssh)
        for (deleted in listOf(box, Profile(id = 9, alias = "stored phone", baseUrl = "", kind = KernelKind.LOCAL))) {
            assertTrue(ctl.activate(deleted).login is LoginResult.Error)
        }
        assertTrue(local.started.isEmpty())
        assertTrue(ssh.started.isEmpty())
        assertTrue(server.requests.isEmpty())
        assertTrue(dao.rows.isEmpty())
    }

    @Test fun the_placeholder_uses_a_fresh_stored_local_row_when_one_exists() = runTest {
        val stored = Profile(id = 9, alias = "stored phone", baseUrl = "", kind = KernelKind.LOCAL)
        val dao = FakeDao(stored); val local = FakeKernel(localEndpoint)
        val result = controller(dao, local = local).activate(KernelList.THIS_PHONE)
        assertEquals(listOf(stored.copy(lastUsedAt = 5L)), local.started)
        assertEquals(stored.copy(lastUsedAt = 5L).boundTo(localEndpoint), result.persisted)
        assertEquals("", dao.rows.getValue(stored.id).baseUrl)
    }

    @Test fun activation_dispatches_on_the_fresh_kind_and_configuration_not_the_rendered_row() = runTest {
        val dao = FakeDao(box); val ssh = FakeKernel(sshEndpoint)
        val staleUrl = box.copy(kind = KernelKind.REMOTE_URL, baseUrl = "https://stale.example/", alias = "stale")
        val opened = controller(dao, ssh = ssh).activate(staleUrl)
        assertEquals(listOf(box.copy(lastUsedAt = 5L)), ssh.started)
        assertEquals("http://127.0.0.2:41234/", opened.persisted.baseUrl)
        val url = Profile(id = box.id, alias = "fresh URL", baseUrl = "https://fresh.example/")
        dao.rows[box.id] = url
        val result = controller(dao, ssh = ssh).activate(box)
        assertEquals(url.copy(lastUsedAt = 5L), result.persisted)
        assertEquals(1, ssh.started.size)
    }

    private fun sourceChanges(): List<(Profile) -> Profile?> = listOf(
        { null }, { it.copy(alias = "foreign") }, { it.copy(kind = KernelKind.LOCAL) },
        { it.copy(baseUrl = "http://127.0.0.2:45000/") }, { it.copy(authType = AuthType.CODE_SERVER_PASSWORD) },
        { it.copy(projectPath = "/foreign/project") }, { it.copy(ssh = it.ssh!!.copy(host = "foreign.example")) },
        { it.copy(ssh = it.ssh!!.copy(port = 2222)) }, { it.copy(ssh = it.ssh!!.copy(user = "foreign")) },
        { it.copy(ssh = it.ssh!!.copy(remotePort = 18000)) },
    )

    private fun changeSource(dao: FakeDao, change: (Profile) -> Profile?) {
        val replacement = change(dao.rows.getValue(box.id))
        if (replacement == null) dao.rows.remove(box.id) else dao.rows[box.id] = replacement
    }

    @Test fun a_source_deleted_or_edited_during_startup_never_preflights_or_moves_the_front() = runTest {
        for (change in sourceChanges()) {
            val dao = FakeDao(box); val server = KernelServer(); val ssh = FakeKernel(sshEndpoint)
            ssh.afterStart = { changeSource(dao, change) }
            val result = controller(dao, server = server, ssh = ssh).activate(box)
            assertTrue(result.login is LoginResult.Error)
            assertTrue(server.requests.isEmpty())
            assertTrue(ssh.fronts.isEmpty())
            assertTrue(dao.cookieWrites.isEmpty())
        }
    }

    @Test fun a_source_deleted_or_edited_during_preflight_never_opens_or_stamps() = runTest {
        for (change in sourceChanges()) {
            val dao = FakeDao(box); val server = KernelServer(); val ssh = FakeKernel(sshEndpoint)
            server.afterRequest = { changeSource(dao, change) }
            val result = controller(dao, server = server, ssh = ssh).activate(box)
            assertTrue(result.login is LoginResult.Error)
            assertEquals(listOf("/api/v4/meta"), server.requests.map { it.url.encodedPath })
            assertTrue(ssh.fronts.isEmpty())
            assertTrue(dao.cookieWrites.isEmpty())
        }
    }

    @Test fun internal_reauth_is_api_only_and_no_url_helper_persists_its_runtime_address() = runTest {
        val source = box.copy(authType = AuthType.CODE_SERVER_PASSWORD)
        val dao = FakeDao(source); val vault = FakeVault().apply { putSecret(source.alias, "fixture-ssh-account") }
        val server = KernelServer()
        val manager = SessionManager(dao, vault, FakeCookieSink(), OkHttpClient.Builder().addInterceptor(server).build())
        val live = source.boundTo(sshEndpoint)
        assertNull(manager.persistUrlChange(live, live.baseUrl))
        val reauthed = manager.updateUrlAndReauth(live, live.baseUrl)
        assertEquals(LoginResult.Success("127.0.0.2"), reauthed.login)
        assertEquals(live, reauthed.persisted)
        assertTrue(manager.updateUrlAndReauth(live, "http://127.0.0.2:45000/").login is LoginResult.Error)
        assertNull(manager.resumePasswordSignIn(live, live.baseUrl + "login"))
        assertTrue(!manager.noteInteractiveSignIn(live, live.baseUrl))
        assertEquals(listOf("/api/v4/meta"), server.requests.map { it.url.encodedPath })
        assertTrue(vault.reads.isEmpty())
        assertTrue(dao.cookieWrites.isEmpty())
        assertEquals(source, dao.rows[source.id])
        assertEquals(0, dao.fullRowUpdates)
    }

    @Test fun an_internal_kind_cannot_preflight_an_arbitrary_or_cross_kind_endpoint() = runTest {
        val server = KernelServer()
        val dao = FakeDao(box)
        val manager = SessionManager(dao, FakeVault(), FakeCookieSink(), OkHttpClient.Builder().addInterceptor(server).build())
        for (endpoint in listOf(
            KernelEndpoint("https://foreign.example/", "foreign.example"),
            localEndpoint, KernelEndpoint("http://127.0.0.2:41234/proxy/15000/", "127.0.0.2"),
            KernelEndpoint("http://127.0.0.2:41234/?token=fixture-value", "127.0.0.2"),
            KernelEndpoint(sshEndpoint.baseUrl, "127.0.0.1"),
        )) {
            assertTrue(manager.login(box.boundTo(endpoint)) is LoginResult.Error)
        }
        assertTrue(server.requests.isEmpty())
        assertEquals(box, dao.rows[box.id])
    }

    @Test fun an_ssh_editor_preserves_first_trust_acquired_between_read_and_atomic_save() = runTest {
        val source = box.copy(ssh = box.ssh!!.copy(hostKeySha256 = null))
        val dao = FakeDao(source)
        dao.beforeSshSave = {
            assertEquals(1, dao.pinSshHostKey(source.id, source.ssh!!.host, source.ssh.port, "SHA256:acquired"))
            dao.rows[source.id] = dao.rows.getValue(source.id).copy(lastUsedAt = 999, cookieHost = "owner-marker")
        }
        val result = controller(dao).editSshKernel(source, "reviewed", source.ssh!!.copy(user = "other", remotePort = 18000))
        assertEquals("SHA256:acquired", result.ssh?.hostKeySha256)
        assertEquals(999L, result.lastUsedAt)
        assertEquals("owner-marker", result.cookieHost)
        assertEquals("", result.baseUrl)
        assertEquals("other", result.ssh?.user)
        assertEquals(18000, result.ssh?.remotePort)
        assertEquals(result, dao.rows[source.id])
        assertEquals(0, dao.fullRowUpdates)
    }

    @Test fun a_reviewed_ssh_host_or_port_change_clears_a_concurrently_acquired_pin() = runTest {
        for (newEndpoint in listOf(box.ssh!!.copy(host = "new.example"), box.ssh!!.copy(port = 2222))) {
            val source = box.copy(ssh = box.ssh!!.copy(hostKeySha256 = null))
            val dao = FakeDao(source)
            dao.beforeSshSave = { assertEquals(1, dao.pinSshHostKey(source.id, source.ssh!!.host, source.ssh.port, "SHA256:acquired")) }
            val result = controller(dao).editSshKernel(source, source.alias, newEndpoint.copy(hostKeySha256 = "SHA256:forged"))
            assertNull(result.ssh?.hostKeySha256)
            assertEquals(0, dao.fullRowUpdates)
        }
    }

    @Test fun editing_a_bound_ssh_snapshot_saves_only_its_endpoint_free_source_fields() = runTest {
        val dao = FakeDao(box)
        val saved = controller(dao).editSshKernel(box.boundTo(sshEndpoint), "reviewed", box.ssh!!)
        assertEquals("", saved.baseUrl)
        assertNull(saved.cookieHost)
        assertEquals(saved, dao.rows[box.id])
        assertEquals(0, dao.fullRowUpdates)
    }

    @Test fun changed_or_deleted_ssh_config_at_the_query_cannot_move_or_stage_credentials() = runTest {
        for (change in sourceChanges()) {
            val dao = FakeDao(box); val vault = FakeVault().apply { putSecret(box.alias, "fixture-old") }
            dao.beforeSshSave = { changeSource(dao, change) }
            val ctl = controller(dao, vault)
            try {
                ctl.editSshKernel(box, "reviewed", box.ssh!!.copy(user = "reviewed"), "fixture-new")
                org.junit.Assert.fail("a stale query must refuse the save")
            } catch (_: IllegalStateException) { }
            assertEquals("fixture-old", vault.map[box.alias])
            assertNull(vault.map["reviewed"])
            assertEquals(0, dao.fullRowUpdates)
        }
    }

    @Test fun failed_ssh_insert_or_edit_never_orphans_or_moves_a_password() = runTest {
        val dao = FakeDao(box); val vault = FakeVault().apply { putSecret(box.alias, "fixture-old") }
        val ctl = controller(dao, vault)
        dao.beforeInsert = { assertNull(vault.map["new"]); error("injected insert failure") }
        try {
            ctl.addSshKernel("new", box.ssh!!, "fixture-new")
            org.junit.Assert.fail("the original insert failure must propagate")
        } catch (_: IllegalStateException) { }
        dao.beforeSshSave = { error("injected pre-commit failure") }
        try {
            ctl.editSshKernel(box, "reviewed", box.ssh!!, "fixture-new")
            org.junit.Assert.fail("the original save failure must propagate")
        } catch (_: IllegalStateException) { }
        assertEquals(box, dao.rows[box.id])
        assertEquals(mapOf(box.alias to "fixture-old"), vault.map)
        assertEquals(0, dao.fullRowUpdates)
    }

    @Test fun cancelled_ssh_save_reconciles_only_a_committed_row_and_never_rolls_it_back() = runTest {
        for (committed in listOf(false, true)) {
            val dao = FakeDao(box); val vault = FakeVault().apply { putSecret(box.alias, "fixture-old") }
            val ctl = controller(dao, vault)
            val entered = CompletableDeferred<Unit>()
            val wait: suspend () -> Unit = { entered.complete(Unit); CompletableDeferred<Unit>().await() }
            if (committed) dao.afterSshSave = { wait() } else dao.beforeSshSave = wait
            val save = launch { ctl.editSshKernel(box, "reviewed", box.ssh!!.copy(user = "reviewed"), "fixture-new") }
            entered.await(); save.cancelAndJoin()
            assertEquals(if (committed) "reviewed" else box.alias, dao.rows.getValue(box.id).alias)
            assertEquals(if (committed) null else "fixture-old", vault.map[box.alias])
            assertEquals(if (committed) "fixture-new" else null, vault.map["reviewed"])
            assertEquals(0, dao.fullRowUpdates)
        }
    }

    @Test fun a_foreign_vault_value_after_ssh_commit_is_preserved_and_reported_without_row_rollback() = runTest {
        val dao = FakeDao(box); val vault = FakeVault().apply { putSecret(box.alias, "fixture-old") }
        val reports = mutableListOf<String>()
        dao.afterSshSave = { vault.putSecret("reviewed", "fixture-foreign") }
        val saved = controller(dao, vault, report = { reports += it }).editSshKernel(box, "reviewed", box.ssh!!, "fixture-new")
        assertEquals("reviewed", saved.alias)
        assertEquals(saved, dao.rows[box.id])
        assertEquals("fixture-foreign", vault.map["reviewed"])
        assertEquals("fixture-old", vault.map[box.alias])
        assertTrue(reports.any { "reconciliation incomplete" in it })
        assertTrue(reports.none { "fixture-new" in it || "fixture-old" in it || "fixture-foreign" in it })
    }

    @OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
    @Test fun ssh_post_commit_readback_is_bounded_to_five_seconds_without_row_rollback() = runTest {
        val dao = FakeDao(box); val vault = FakeVault().apply { putSecret(box.alias, "fixture-old") }
        val reports = mutableListOf<String>()
        dao.afterSshSave = {
            dao.beforeIdRead = { CompletableDeferred<Unit>().await() }
            error("injected lost committed result")
        }
        try {
            controller(dao, vault, report = { reports += it }).editSshKernel(box, "reviewed", box.ssh!!, "fixture-new")
            org.junit.Assert.fail("the original save failure must propagate")
        } catch (_: IllegalStateException) { }
        assertEquals(SessionManager.SAVE_RECONCILIATION_MS, testScheduler.currentTime)
        assertEquals("reviewed", dao.rows.getValue(box.id).alias)
        assertEquals("fixture-old", vault.map[box.alias])
        assertNull(vault.map["reviewed"])
        assertTrue(reports.any { "reconciliation incomplete" in it })
    }
}
