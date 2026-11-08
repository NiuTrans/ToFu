package com.tofu.client.ui

import com.tofu.client.api.ApiV4Contract
import com.tofu.client.data.AuthType
import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import com.tofu.client.data.SshEndpoint
import com.tofu.client.session.CookieSink
import com.tofu.client.session.KernelController
import com.tofu.client.session.KernelControllers
import com.tofu.client.session.KernelEndpoint
import com.tofu.client.session.KernelList
import com.tofu.client.session.KernelState
import com.tofu.client.session.KernelUnavailableException
import com.tofu.client.session.SecretVault
import com.tofu.client.session.SessionController
import com.tofu.client.session.SessionManager
import com.tofu.client.session.UnavailableKernelController
import com.tofu.client.ssh.SshSetup
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.withTimeout
import okhttp3.Cookie
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * The Kernels list as the ViewModel serves it: "This phone" pinned first,
 * placeholder kernels reporting their state instead of navigating, SSH kernels
 * saved without connecting, and the kernel in front tracked until deleted.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class ProfilesViewModelTest {

    private class FakeDao(vararg seed: Profile) : ProfileDao {
        val rows = MutableStateFlow(seed.toList())
        private var nextId = 100L
        private fun put(p: Profile) { rows.value = rows.value.filter { it.id != p.id } + p }
        override fun observeAll(): Flow<List<Profile>> = rows
        override suspend fun getById(id: Long) = rows.value.firstOrNull { it.id == id }
        override suspend fun getByAlias(alias: String) = rows.value.firstOrNull { it.alias == alias }
        override suspend fun getAllOnce(): List<Profile> = rows.value
        override suspend fun insert(profile: Profile): Long { val id = nextId++; put(profile.copy(id = id)); return id }
        override suspend fun update(profile: Profile): Int { put(profile); return 1 }
        override suspend fun deleteById(id: Long) { rows.value = rows.value.filter { it.id != id } }
        override suspend fun setCookieHost(id: Long, host: String?): Int = 0
        override suspend fun setCookieHostIfCurrent(id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType, host: String): Int = 0
        override suspend fun touchLastUsed(id: Long, timestamp: Long): Int {
            val row = getById(id) ?: return 0
            put(row.copy(lastUsedAt = timestamp)); return 1
        }
        override suspend fun setAuthType(id: Long, authType: AuthType): Int = 0
        override suspend fun pinSshHostKey(id: Long, host: String, port: Int, sha256: String): Int {
            val row = getById(id) ?: return 0
            val ssh = row.ssh ?: return 0
            if (ssh.host != host || ssh.port != port || ssh.hostKeySha256 != null) return 0
            put(row.copy(ssh = ssh.copy(hostKeySha256 = sha256))); return 1
        }
    }

    private class FakeVault : SecretVault {
        val map = mutableMapOf<String, String>()
        override fun secretFor(alias: String) = map[alias]
        override fun putSecret(alias: String, secret: String) { map[alias] = secret }
        override fun removeSecret(alias: String) { map.remove(alias) }
    }

    private class NoCookies : CookieSink {
        override fun inject(origin: String, cookies: List<Cookie>) {}
        override fun purgeHost(host: String) {}
        override fun cookieHeader(origin: String): String? = null
    }

    /** URL kernels in these tests: attached, so always Ready. */
    private object ReadyUrlKernel : KernelController {
        private val ready = MutableStateFlow<KernelState>(KernelState.Ready)
        override suspend fun ensureRunning(profile: Profile) = KernelEndpoint(profile.baseUrl, "u.example")
        override suspend fun stop(profile: Profile) {}
        override fun state(profile: Profile): StateFlow<KernelState> = ready
    }

    private val url = Profile(id = 1, alias = "url", baseUrl = "https://u.example/", lastUsedAt = 3)
    private val box = Profile(id = 2, alias = "box", baseUrl = "", kind = KernelKind.SSH,
        ssh = SshEndpoint(host = "dev.example", user = "me"), lastUsedAt = 2)

    /**
     * An SSH carrier whose host presents [presented]: it refuses until that key
     * is pinned, and pins it through the DAO's first-trust write.
     */
    private class FirstTrustSsh(private val dao: ProfileDao, private val presented: String) : KernelController, SshSetup {
        private val state = MutableStateFlow<KernelState>(KernelState.Stopped)
        override suspend fun ensureRunning(profile: Profile): KernelEndpoint {
            if (profile.ssh?.hostKeySha256 != presented) throw KernelUnavailableException("confirm $presented")
            return KernelEndpoint("http://127.0.0.2:41234", "127.0.0.2")
        }
        override suspend fun stop(profile: Profile) {}
        override fun state(profile: Profile): StateFlow<KernelState> = state
        override suspend fun publicKeyLine() = "ssh-ed25519 AAAAkey tofu-android"
        override fun hostKeyToTrust(profile: Profile) = presented.takeIf { profile.ssh?.hostKeySha256 == null }
        override suspend fun trustHostKey(profile: Profile, fingerprint: String): Boolean {
            val ssh = profile.ssh ?: return false
            return fingerprint == hostKeyToTrust(profile) && dao.pinSshHostKey(profile.id, ssh.host, ssh.port, fingerprint) == 1
        }
    }

    private fun viewModel(
        dao: FakeDao,
        vault: FakeVault = FakeVault(),
        ssh: KernelController = UnavailableKernelController(),
        answerMeta: Boolean = false,
    ): ProfilesViewModel {
        val kernels = KernelControllers(ReadyUrlKernel, UnavailableKernelController(), ssh)
        // Only the API gate of a kernel the test opens is answered; nothing else may need the network.
        val session = SessionManager(dao, vault, NoCookies(), OkHttpClient.Builder().addInterceptor {
            val request = it.request()
            if (!answerMeta || !request.url.encodedPath.endsWith("/api/v4/meta")) throw AssertionError("unexpected request ${request.url}")
            val meta = """{"data":{"apiMajor":${ApiV4Contract.API_MAJOR},"minAndroidBuild":1,"schemaVersion":1,""" +
                """"serverBuild":"x","minDesktopBuild":"0.0.1"},"meta":{"requestId":"r","serverTimeMs":0}}"""
            Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(200).message("OK")
                .body(meta.toResponseBody("application/json".toMediaType())).build()
        }.build())
        return ProfilesViewModel(dao, vault, SessionController(dao, vault, session, kernels, report = {}), kernels)
    }

    @Before fun mainDispatcher() = Dispatchers.setMain(UnconfinedTestDispatcher())

    @After fun resetDispatcher() = Dispatchers.resetMain()

    @Test fun this_phone_is_pinned_above_every_stored_kernel() {
        val dao = FakeDao(url, box)
        val vm = viewModel(dao)
        assertEquals(listOf(KernelList.THIS_PHONE, url, box), vm.kernelList.value)
        assertEquals("onboarding still sees only stored rows", listOf(url, box), vm.profiles.value)

        val stored = Profile(id = 9, alias = "This phone", baseUrl = "", kind = KernelKind.LOCAL)
        dao.rows.value = listOf(url, stored, box)
        assertEquals(listOf(stored, url, box), vm.kernelList.value)
    }

    @Test fun an_unavailable_kernel_shows_its_reason_and_does_not_navigate() {
        val vm = viewModel(FakeDao(url, box))
        val reason = "not available in this build yet"
        assertEquals(KernelState.Failed(reason), vm.kernelState(KernelList.THIS_PHONE).value)
        assertEquals(KernelState.Failed(reason), vm.kernelState(box).value)

        vm.activate(KernelList.THIS_PHONE)
        assertEquals(UiStatus.Error(reason), vm.status.value)
        assertEquals(Screen.List, vm.screen.value)
        vm.activate(box)
        assertEquals(UiStatus.Error(reason), vm.status.value)
        assertNull(vm.activeKernel.value)
    }

    @Test fun opening_a_url_kernel_puts_it_in_front_until_it_is_deleted() {
        val dao = FakeDao(url, box)
        val vm = viewModel(dao)
        vm.activate(url)
        // Sign-in hops to Dispatchers.IO; wait for the navigation it ends in.
        runBlocking { withTimeout(10_000) { vm.screen.first { it is Screen.Web } } }
        val opened = dao.rows.value.single { it.id == url.id }
        assertEquals(Screen.Web(opened), vm.screen.value)
        assertEquals(opened, vm.activeKernel.value)
        assertEquals(KernelState.Ready, vm.kernelState(opened).value)

        vm.backToList()
        assertEquals("still in front after returning to the list", opened, vm.activeKernel.value)
        vm.deleteProfile(box)
        assertEquals(opened, vm.activeKernel.value)
        vm.deleteProfile(opened)
        assertNull(vm.activeKernel.value)
    }

    @Test fun adding_an_ssh_kernel_saves_it_and_returns_to_the_list() {
        val dao = FakeDao(url)
        val vm = viewModel(dao)
        vm.startAddSsh()
        assertEquals(Screen.AddEditSsh, vm.screen.value)
        assertNull(vm.editing)

        val endpoint = SshEndpoint(host = "dev.example", port = 2222, user = "me")
        vm.submitSsh("box", endpoint)
        val saved = dao.rows.value.single { it.alias == "box" }
        assertEquals(KernelKind.SSH, saved.kind)
        assertEquals(endpoint, saved.ssh)
        assertEquals(Screen.List, vm.screen.value)
        assertEquals(UiStatus.Idle, vm.status.value)
        assertEquals(listOf(KernelList.THIS_PHONE, url, saved), vm.kernelList.value)

        vm.startAddSsh()
        vm.submitSsh("url", endpoint)
        assertTrue("a taken name is refused", vm.status.value is UiStatus.Error)
        assertEquals(Screen.AddEditSsh, vm.screen.value)
    }

    @Test fun an_unpinned_ssh_host_opens_its_form_to_trust_the_presented_key_then_connects() {
        val dao = FakeDao(url, box)
        val vm = viewModel(dao, ssh = FirstTrustSsh(dao, "SHA256:presented"), answerMeta = true)

        vm.activate(box)
        assertEquals(Screen.AddEditSsh, vm.screen.value)
        assertEquals(box.id, vm.editing?.id)
        assertEquals("SHA256:presented", vm.hostKeyToTrust)
        assertEquals("ssh-ed25519 AAAAkey tofu-android", vm.sshPublicKey.value)
        assertEquals(UiStatus.Idle, vm.status.value)

        vm.trustHostKey()
        // Sign-in hops to Dispatchers.IO; wait for the navigation it ends in.
        runBlocking { withTimeout(10_000) { vm.screen.first { it is Screen.Web } } }
        assertEquals("SHA256:presented", dao.rows.value.single { it.id == box.id }.ssh?.hostKeySha256)
        assertNull(vm.hostKeyToTrust)
        assertEquals("http://127.0.0.2:41234", (vm.screen.value as Screen.Web).profile.baseUrl)
    }

    @Test fun leaving_the_ssh_form_forgets_the_key_it_offered() {
        val dao = FakeDao(url, box)
        val vm = viewModel(dao, ssh = FirstTrustSsh(dao, "SHA256:presented"))
        vm.activate(box)
        assertEquals("SHA256:presented", vm.hostKeyToTrust)

        vm.backToList()
        assertNull(vm.hostKeyToTrust)
        vm.trustHostKey()
        assertNull("nothing is pinned without the prompt", dao.rows.value.single { it.id == box.id }.ssh?.hostKeySha256)
    }

    @Test fun editing_routes_by_kind_and_this_phone_has_no_form() {
        val dao = FakeDao(url, box)
        val vm = viewModel(dao)
        vm.startEdit(url)
        assertEquals(Screen.AddEdit, vm.screen.value)
        vm.backToList()
        vm.startEdit(KernelList.THIS_PHONE)
        assertEquals(Screen.List, vm.screen.value)

        vm.startEdit(box)
        assertEquals(Screen.AddEditSsh, vm.screen.value)
        vm.submitSsh("box", box.ssh!!.copy(user = "other"))
        assertEquals("other", dao.rows.value.single { it.id == box.id }.ssh?.user)
        assertEquals(Screen.List, vm.screen.value)
    }
}
