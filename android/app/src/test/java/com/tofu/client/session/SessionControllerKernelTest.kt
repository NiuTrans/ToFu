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
        override fun secretFor(alias: String) = map[alias]
        override fun putSecret(alias: String, secret: String) { map[alias] = secret }
        override fun removeSecret(alias: String) { map.remove(alias) }
    }

    private class FakeDao(vararg seed: Profile) : ProfileDao {
        val rows = seed.associateBy { it.id }.toMutableMap()
        var nextId = 100L
        val cookieWrites = mutableListOf<Long>()
        override fun observeAll(): Flow<List<Profile>> = flowOf(rows.values.toList())
        override suspend fun getById(id: Long) = rows[id]
        override suspend fun getAllOnce(): List<Profile> = rows.values.toList()
        override suspend fun getByAlias(alias: String) = rows.values.firstOrNull { it.alias == alias }
        override suspend fun insert(profile: Profile): Long {
            val id = nextId++; rows[id] = profile.copy(id = id); return id
        }
        override suspend fun update(profile: Profile): Int { rows[profile.id] = profile; return 1 }
        override suspend fun deleteById(id: Long) { rows.remove(id) }
        override suspend fun setCookieHost(id: Long, host: String?): Int {
            cookieWrites += id
            val row = rows[id] ?: return 0
            rows[id] = row.copy(cookieHost = host)
            return 1
        }
        override suspend fun setCookieHostIfCurrent(id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType, host: String): Int {
            val row = rows[id] ?: return 0
            return if (row.baseUrl == expectedUrl && row.alias == expectedAlias && row.authType == expectedAuthType) setCookieHost(id, host) else 0
        }
        override suspend fun touchLastUsed(id: Long, timestamp: Long): Int {
            val row = rows[id] ?: return 0
            rows[id] = row.copy(lastUsedAt = timestamp)
            return 1
        }
        override suspend fun setAuthType(id: Long, authType: AuthType): Int = 0
        override suspend fun pinSshHostKey(id: Long, host: String, port: Int, sha256: String): Int = 0
    }

    /** A kernel that comes up at [endpoint], recording what it was asked. */
    private class FakeKernel(private val endpoint: KernelEndpoint) : KernelController {
        val started = mutableListOf<Profile>()
        val stopped = mutableListOf<Profile>()
        val fronts = mutableListOf<Profile>()
        override fun frontChanged(front: Profile) { fronts += front }
        private val state = MutableStateFlow<KernelState>(KernelState.Stopped)
        override suspend fun ensureRunning(profile: Profile): KernelEndpoint {
            started += profile; state.value = KernelState.Ready; return endpoint
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
        override fun intercept(chain: okhttp3.Interceptor.Chain): Response {
            val req = chain.request()
            requests += req
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
    ) = SessionController(
        dao, vault,
        SessionManager(dao, vault, FakeCookieSink(), OkHttpClient.Builder().addInterceptor(server).build(), appVersionCode = 17),
        KernelControllers(remoteUrl = NoUrlKernel, local = local, ssh = ssh),
        clock = { 5L }, report = {},
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
        val dao = FakeDao(); val vault = FakeVault().apply { putSecret("box", "pw") }; val server = KernelServer()
        val odd = box.copy(authType = AuthType.CODE_SERVER_PASSWORD).boundTo(sshEndpoint)
        val manager = SessionManager(dao, vault, FakeCookieSink(), OkHttpClient.Builder().addInterceptor(server).build())

        assertEquals(LoginResult.Success("127.0.0.2"), manager.login(odd))
        assertEquals(listOf("/api/v4/meta"), server.requests.map { it.url.encodedPath })
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
}
