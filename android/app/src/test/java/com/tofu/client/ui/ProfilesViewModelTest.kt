package com.tofu.client.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelStore
import androidx.lifecycle.viewModelScope
import com.tofu.client.api.ApiV4Contract
import com.tofu.client.data.AuthType
import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import com.tofu.client.data.SshEndpoint
import com.tofu.client.session.AdvertisedClient
import com.tofu.client.session.CookieSink
import com.tofu.client.session.KernelController
import com.tofu.client.session.KernelControllers
import com.tofu.client.session.KernelEndpoint
import com.tofu.client.session.KernelList
import com.tofu.client.session.KernelState
import com.tofu.client.session.KernelUnavailableException
import com.tofu.client.session.ProfileDaoFixture
import com.tofu.client.session.SecretVault
import com.tofu.client.session.SessionController
import com.tofu.client.session.SessionManager
import com.tofu.client.session.UnavailableKernelController
import com.tofu.client.session.UpdateChecker
import com.tofu.client.ssh.SshSetup
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.withContext
import okhttp3.Cookie
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.*
import org.junit.Test

/** Real ViewModelStore ownership: kernel wiring and obsolete UI intent share one lifetime. */
@OptIn(ExperimentalCoroutinesApi::class)
class ProfilesViewModelTest {
    private val first = Profile(1, "first", baseUrl = "https://first.test/", authType = AuthType.NONE)
    private val second = Profile(2, "second", baseUrl = "https://second.test/", authType = AuthType.NONE)
    private val url = Profile(1, "url", baseUrl = "https://u.example/", lastUsedAt = 3)
    private val box = Profile(2, "box", baseUrl = "", kind = KernelKind.SSH,
        ssh = SshEndpoint(host = "dev.example", user = "me"), lastUsedAt = 2)

    private class Vault : SecretVault {
        val stored = mutableMapOf<String, String>()
        override fun secretFor(alias: String) = stored[alias]
        override fun putSecret(alias: String, secret: String) { stored[alias] = secret }
        override fun removeSecret(alias: String) { stored.remove(alias) }
    }
    private class Cookies : CookieSink {
        override fun inject(origin: String, cookies: List<Cookie>) {}
        override fun purgeHost(host: String) {}
        override fun cookieHeader(origin: String): String? = null
    }
    private class Dao(seed: List<Profile>) : ProfileDaoFixture {
        val rows = MutableStateFlow(seed)
        var gate: CompletableDeferred<Unit>? = null
        var writes = 0
        private var nextId = 100L
        override fun observeAll() = rows
        override suspend fun getById(id: Long) = rows.value.firstOrNull { it.id == id }
        override suspend fun getAllOnce() = rows.value
        override suspend fun getByAlias(alias: String): Profile? {
            // Intentionally ignores cancellation to exercise the production fences.
            gate?.let { waiting -> withContext(NonCancellable) { waiting.await() } }
            return rows.value.firstOrNull { it.alias == alias }
        }
        override suspend fun insert(profile: Profile): Long {
            val id = nextId++
            writes++; rows.value += profile.copy(id = id); return id
        }
        override suspend fun update(profile: Profile): Int {
            if (rows.value.none { it.id == profile.id }) return 0
            writes++; rows.value = rows.value.map { if (it.id == profile.id) profile else it }; return 1
        }
        override suspend fun deleteById(id: Long) { rows.value = rows.value.filterNot { it.id == id } }
        override suspend fun setCookieHost(id: Long, host: String?) = 0
        override suspend fun setCookieHostIfCurrent(id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType, host: String) = 0
        override suspend fun touchLastUsed(id: Long, timestamp: Long): Int {
            val current = getById(id) ?: return 0
            rows.value = rows.value.map { if (it.id == id) current.copy(lastUsedAt = timestamp) else it }
            return 1
        }
        override suspend fun setAuthType(id: Long, authType: AuthType) = 0
        override suspend fun pinSshHostKey(id: Long, host: String, port: Int, sha256: String): Int {
            val current = getById(id) ?: return 0
            val ssh = current.ssh ?: return 0
            if (current.kind != KernelKind.SSH || ssh.host != host || ssh.port != port || ssh.hostKeySha256 != null) return 0
            return update(current.copy(ssh = ssh.copy(hostKeySha256 = sha256)))
        }
    }

    private object ReadyUrlKernel : KernelController {
        private val ready = MutableStateFlow<KernelState>(KernelState.Ready)
        override suspend fun ensureRunning(profile: Profile) = KernelEndpoint(profile.baseUrl, "u.example")
        override suspend fun stop(profile: Profile) {}
        override fun state(profile: Profile): StateFlow<KernelState> = ready
    }
    private class ReadyKernel(private val endpoint: KernelEndpoint, private val gate: CompletableDeferred<Unit>? = null) : KernelController {
        private val ready = MutableStateFlow<KernelState>(KernelState.Ready)
        val fronts = mutableListOf<Profile>()
        override suspend fun ensureRunning(profile: Profile): KernelEndpoint { gate?.await(); return endpoint }
        override suspend fun stop(profile: Profile) {}
        override fun state(profile: Profile): StateFlow<KernelState> = ready
        override fun frontChanged(front: Profile) { fronts += front }
    }
    private class FirstTrustSsh(
        private val dao: ProfileDao,
        private val presented: String,
        private val connectGate: CompletableDeferred<Unit>? = null,
        private val keyGate: CompletableDeferred<Unit>? = null,
        private val trustGate: CompletableDeferred<Unit>? = null,
    ) : KernelController, SshSetup {
        private val state = MutableStateFlow<KernelState>(KernelState.Stopped)
        var connects = 0
        override suspend fun ensureRunning(profile: Profile): KernelEndpoint {
            connects++
            return withContext(NonCancellable) {
                connectGate?.await()
                if (profile.ssh?.hostKeySha256 != presented) throw KernelUnavailableException("confirm $presented")
                KernelEndpoint("http://127.0.0.2:41234", "127.0.0.2")
            }
        }
        override suspend fun stop(profile: Profile) {}
        override fun state(profile: Profile): StateFlow<KernelState> = state
        override suspend fun publicKeyLine() = withContext(NonCancellable) {
            keyGate?.await(); "ssh-ed25519 AAAAkey tofu-android"
        }
        override fun hostKeyToTrust(profile: Profile) = presented.takeIf { profile.ssh?.hostKeySha256 == null && connects > 0 }
        override suspend fun trustHostKey(profile: Profile, fingerprint: String): Boolean = withContext(NonCancellable) {
            trustGate?.await()
            val ssh = profile.ssh ?: return@withContext false
            fingerprint == hostKeyToTrust(profile) && dao.pinSshHostKey(profile.id, ssh.host, ssh.port, fingerprint) == 1
        }
    }

    private val harnesses = mutableListOf<Harness>()
    private fun harness(
        seed: List<Profile>, checker: UpdateChecker? = null, clock: () -> Long = { 0L },
        release: () -> Unit = {}, local: KernelController = UnavailableKernelController(),
        sshFactory: (ProfileDao) -> KernelController = { UnavailableKernelController() }, answerMeta: Boolean = false,
    ) = Harness(seed, checker, clock, release, local, sshFactory, answerMeta).also { harnesses += it }

    private fun lifecycleTest(block: suspend TestScope.() -> Unit) = runTest {
        Dispatchers.setMain(UnconfinedTestDispatcher(testScheduler))
        try { block() }
        finally {
            withContext(NonCancellable) {
                try { for (h in harnesses) h.close() }
                finally { Dispatchers.resetMain() }
            }
        }
    }

    private class Harness(
        seed: List<Profile>, checker: UpdateChecker?, clock: () -> Long, release: () -> Unit,
        local: KernelController, sshFactory: (ProfileDao) -> KernelController, answerMeta: Boolean,
    ) {
        val dao = Dao(seed)
        val vault = Vault()
        val ssh = sshFactory(dao)
        val kernels = KernelControllers(ReadyUrlKernel, local, ssh)
        private val gates = mutableListOf<CompletableDeferred<Unit>>()
        fun releaseOnClose(gate: CompletableDeferred<Unit>) { gates += gate }
        private val http = OkHttpClient.Builder().addInterceptor { chain ->
            val request = chain.request()
            if (!answerMeta || !request.url.encodedPath.endsWith("/api/v4/meta"))
                throw AssertionError("unexpected native request ${request.url.encodedPath}")
            val meta = """{"data":{"apiMajor":${ApiV4Contract.API_MAJOR},"minAndroidBuild":1,"schemaVersion":1,""" +
                """"serverBuild":"x","minDesktopBuild":"0.0.1"},"meta":{"requestId":"r","serverTimeMs":0}}"""
            Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(200).message("OK")
                .body(meta.toResponseBody("application/json".toMediaType())).build()
        }.build()
        val session = SessionManager(dao, vault, Cookies(), http)
        val store = ViewModelStore()
        val factory = object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T = ProfilesViewModel(
                dao, vault, SessionController(dao, vault, session, kernels, report = {}), kernels,
                session = session, updateChecker = checker, clock = clock,
                release = {
                    session.close()
                    http.connectionPool.evictAll()
                    http.dispatcher.executorService.shutdown()
                    release()
                },
            ) as T
        }
        val vm = ViewModelProvider(store, factory)[ProfilesViewModel::class.java]
        private val owned = vm.viewModelScope.coroutineContext[Job]
        suspend fun close() {
            dao.gate?.complete(Unit)
            gates.forEach { it.complete(Unit) }
            store.clear()
            owned?.join()
        }
    }

    @Test fun back_edit_delete_new_selection_and_store_clear_cancel_pending_add_before_writes() = lifecycleTest {
        for (action in listOf("back", "edit", "delete", "select", "clear", "ssh")) {
            val h = harness(listOf(first, second))
            val gate = CompletableDeferred<Unit>()
            h.dao.gate = gate
            h.vm.submitAdd("pending", "https://pending.test/", AuthType.CODE_SERVER_PASSWORD, "fixture-password")
            when (action) {
                "back" -> h.vm.backToList()
                "edit" -> h.vm.startEdit(first)
                "delete" -> h.vm.deleteProfile(first)
                "select" -> h.vm.activate(second)
                "clear" -> h.store.clear()
                "ssh" -> h.vm.startAddSsh()
            }
            gate.complete(Unit)
            h.vm.awaitLogin()
            testScheduler.runCurrent()
            assertEquals("late inserts must be fenced for $action", 0, h.dao.writes)
            assertNull("late vault writes must be fenced for $action", h.vault.secretFor("pending"))
            assertFalse("cancelled add must not navigate", (h.vm.screen.value as? Screen.Web)?.profile?.alias == "pending")
            if (action == "back") assertEquals(Screen.List, h.vm.screen.value)
        }
    }

    @Test fun graph_release_waits_for_the_exact_owned_scope_to_complete() = lifecycleTest {
        val released = CompletableDeferred<Unit>()
        var releases = 0
        val h = harness(listOf(first), release = { releases++; released.complete(Unit) })
        val gate = CompletableDeferred<Unit>()
        h.dao.gate = gate
        h.vm.submitAdd("pending", "https://pending.test/", AuthType.CODE_SERVER_PASSWORD, "fixture-password")
        h.store.clear()
        assertFalse("clear must not close Room underneath an uncancellable producer", released.isCompleted)
        gate.complete(Unit)
        released.await()
        assertEquals(1, releases)
        h.store.clear()
        assertEquals("owned resources release once", 1, releases)
        assertEquals(0, h.dao.writes)
        assertNull(h.vault.secretFor("pending"))
    }

    @Test fun arriving_links_are_review_only_and_never_insert_or_authenticate() = lifecycleTest {
        val h = harness(listOf(first))
        h.vm.startAdd("https://review.test/proxy/15005/?theme=dark")
        assertEquals("review.test : 15005", h.vm.addPrefill.value!!.alias)
        assertTrue(h.vm.screen.value is Screen.AddEdit)
        assertEquals(listOf(first), h.dao.rows.value)
        assertEquals(0, h.dao.writes)
        assertTrue(h.vault.stored.isEmpty())
        h.vm.startAdd("https://replacement.test/", "Replacement")
        h.vm.acceptPrefill()
        assertEquals(0, h.dao.writes)
        assertEquals(listOf(first), h.dao.rows.value)
    }

    @Test fun legacy_edit_is_a_sanitized_draft_not_an_implicit_save_or_auth_migration() = lifecycleTest {
        val unsafe = first.copy(baseUrl = "https://first.test/proxy/15005/?token=fixture-value&theme=dark")
        val h = harness(listOf(unsafe))
        h.vm.startEdit(unsafe)
        assertEquals("https://first.test/proxy/15005/?theme=dark", h.vm.editing!!.baseUrl)
        assertEquals(AuthType.NONE, h.vm.editing!!.authType)
        assertEquals(listOf(unsafe), h.dao.rows.value)
        assertEquals(0, h.dao.writes)
        assertTrue(h.vm.status.value is UiStatus.Error)
        assertFalse(ProfilesViewModel.FormDraft::class.java.declaredFields.any {
            it.name.contains("password", true) || it.name.contains("secret", true)
        })
    }

    @Test fun viewmodel_store_reuses_screen_and_graph_instead_of_constructing_another_owner() = lifecycleTest {
        val h = harness(listOf(first))
        h.vm.startEdit(first)
        val reused = ViewModelProvider(h.store, object : ViewModelProvider.Factory {
            override fun <T : ViewModel> create(modelClass: Class<T>): T = error("must reuse stored owner")
        })[ProfilesViewModel::class.java]
        assertSame(h.vm, reused)
        assertSame(h.session, reused.session)
        assertEquals(first, reused.editing)
        assertEquals(h.vm.screen.value, reused.screen.value)
    }

    @Test fun repeated_warm_links_are_observable_without_resetting_an_existing_form() = lifecycleTest {
        val h = harness(listOf(first))
        h.vm.startAdd("https://a.test/?theme=dark", "A")
        val screen = h.vm.screen.value
        val revision = h.vm.addPrefill.value!!.revision
        h.vm.startAdd("https://a.test/?theme=dark", "A")
        assertTrue(h.vm.pendingPrefill.value!!.revision > revision)
        assertEquals("warm event must not remount a possibly dirty form", screen, h.vm.screen.value)
        h.vm.startEdit(first)
        h.vm.startAdd("https://b.test/", "B")
        assertEquals("editing requires explicit replacement", first, h.vm.editing)
        h.vm.acceptPrefill()
        assertNull(h.vm.editing)
        assertEquals("https://b.test/", h.vm.addPrefill.value!!.url)
    }

    @Test fun unaccepted_or_dismissed_warm_link_never_becomes_a_recreated_form_initializer() = lifecycleTest {
        val h = harness(listOf(first))
        h.vm.startAdd("https://accepted.test/?theme=dark", "Accepted")
        h.vm.retainFormDraft(ProfilesViewModel.FormDraft(
            "Typed", "https://typed.test/?workspace=main", AuthType.NONE,
            authTouched = true, projectPath = "/project", manageEnabled = true, dirty = true,
        ))
        h.vm.startAdd("https://unaccepted.test/", "Unaccepted")
        assertEquals("https://accepted.test/?theme=dark", h.vm.addPrefill.value!!.url)
        assertEquals("https://unaccepted.test/", h.vm.pendingPrefill.value!!.url)
        assertEquals("https://typed.test/?workspace=main", h.vm.formDraft!!.url)
        h.vm.dismissPrefill()
        val reused = ViewModelProvider(h.store, h.factory)[ProfilesViewModel::class.java]
        assertNull(reused.pendingPrefill.value)
        assertEquals("https://accepted.test/?theme=dark", reused.addPrefill.value!!.url)
        assertEquals("https://typed.test/?workspace=main", reused.formDraft!!.url)
        reused.startAdd("https://explicit.test/", "Explicit")
        reused.acceptPrefill()
        assertEquals("https://explicit.test/", reused.addPrefill.value!!.url)
        assertNull(reused.formDraft)
        assertNull(reused.pendingPrefill.value)
        reused.retainFormDraft(ProfilesViewModel.FormDraft(
            "Typed", "https://typed.test/?token=fixture-value&theme=dark", AuthType.NONE,
            authTouched = false, projectPath = "", manageEnabled = false, dirty = true,
        ))
        assertFalse(reused.formDraft!!.url.contains("fixture-value"))
    }

    @Test fun safe_partial_drafts_survive_but_disposed_forms_cannot_replace_the_current_draft() = lifecycleTest {
        val h = harness(listOf(first))
        h.vm.startAdd()
        val oldForm = (h.vm.screen.value as Screen.AddEdit).formId
        val draft = ProfilesViewModel.FormDraft("Typed", "https://", AuthType.NONE, true, "/project", true, true)
        h.vm.retainFormDraft(draft, oldForm)
        assertEquals("https://", h.vm.formDraft!!.url)
        h.vm.startAdd("https://explicit.test/", "Explicit")
        h.vm.acceptPrefill()
        h.vm.retainFormDraft(draft, oldForm)
        assertNull("late disposal/effect from a replaced form must be fenced", h.vm.formDraft)
        h.vm.startAdd("https://pending.test/", "Pending")
        val pending = h.vm.pendingPrefill.value
        h.vm.acceptPrefill(oldForm)
        h.vm.dismissPrefill(oldForm)
        assertEquals("disposed form callbacks cannot consume a newer intent", pending, h.vm.pendingPrefill.value)
    }

    @Test fun an_old_link_dialog_cannot_accept_or_dismiss_a_newer_link_on_the_same_form() = lifecycleTest {
        val h = harness(listOf(first))
        h.vm.startEdit(first)
        val form = (h.vm.screen.value as Screen.AddEdit).formId
        h.vm.startAdd("https://old.test/", "Old")
        val old = h.vm.pendingPrefill.value!!.revision
        h.vm.startAdd("https://new.test/", "New")
        val pending = h.vm.pendingPrefill.value!!
        h.vm.acceptPrefill(form, old)
        h.vm.dismissPrefill(form, old)
        assertEquals(first, h.vm.editing)
        assertEquals(pending, h.vm.pendingPrefill.value)
        h.vm.acceptPrefill(form, pending.revision)
        assertNull(h.vm.editing)
        assertEquals(pending, h.vm.addPrefill.value)
        assertNotEquals(form, (h.vm.screen.value as Screen.AddEdit).formId)
        assertEquals(0, h.dao.writes)
    }

    @Test fun a_remote_link_does_not_replace_an_ssh_form_or_import_ssh_identity() = lifecycleTest {
        val h = harness(listOf(url, box))
        h.vm.startEdit(box)
        val form = h.vm.screen.value as Screen.AddEditSsh
        h.vm.startAdd("https://review.test/", "Review")
        val offered = h.vm.pendingPrefill.value!!
        assertEquals(form, h.vm.screen.value)
        assertEquals(box, h.vm.editing)
        assertNull(h.vm.addPrefill.value)
        h.vm.dismissPrefill(form.formId, offered.revision)
        assertEquals(box, ViewModelProvider(h.store, h.factory)[ProfilesViewModel::class.java].editing)
        assertNull(h.vm.pendingPrefill.value)
        h.vm.startAdd("https://review.test/", "Review")
        h.vm.acceptPrefill(form.formId, h.vm.pendingPrefill.value!!.revision)
        assertTrue(h.vm.screen.value is Screen.AddEdit)
        assertNull(h.vm.editing)
        assertNull(h.vm.hostKeyToTrust)
        assertEquals("https://review.test/", h.vm.addPrefill.value!!.url)
        assertEquals(listOf(url, box), h.dao.rows.value)
        assertTrue(h.vault.stored.isEmpty())
    }

    @Test fun deliberate_selections_remount_forms_and_disposed_submit_callbacks_are_inert() = lifecycleTest {
        val h = harness(listOf(url, box))
        h.vm.startAdd()
        val oldRemote = (h.vm.screen.value as Screen.AddEdit).formId
        h.vm.startEdit(box)
        val oldSsh = (h.vm.screen.value as Screen.AddEditSsh).formId
        h.vm.submitAdd("stale", "https://stale.test/", AuthType.NONE, "fixture-password", formId = oldRemote)
        h.vm.backToList(oldRemote)
        assertEquals(box, h.vm.editing)
        h.vm.startAdd()
        assertNotEquals(oldRemote, (h.vm.screen.value as Screen.AddEdit).formId)
        h.vm.submitSsh("stale-ssh", box.ssh!!, "fixture-password", oldSsh)
        h.vm.trustHostKey(oldSsh)
        assertEquals(0, h.dao.writes)
        assertTrue(h.vault.stored.isEmpty())
    }

    @Test fun late_scan_and_paste_results_belong_only_to_the_form_that_requested_them() = lifecycleTest {
        val h = harness(listOf(first, box))
        h.vm.startAdd()
        val disposed = (h.vm.screen.value as Screen.AddEdit).formId
        h.vm.startEdit(box)
        val sshForm = h.vm.screen.value
        val link = com.tofu.client.session.DeepLink.Connect("https://review.test/", "Review")
        h.vm.offerConnect(link, disposed)
        h.vm.offerConnect(com.tofu.client.session.DeepLink.Connect("https://review.test/?token=fixture-value", "Unsafe"), disposed)
        assertEquals("an old scan cannot displace an SSH form", sshForm, h.vm.screen.value)
        assertEquals(box, h.vm.editing)
        assertNull(h.vm.pendingPrefill.value)
        assertEquals(UiStatus.Idle, h.vm.status.value)

        h.vm.startEdit(first)
        val current = (h.vm.screen.value as Screen.AddEdit).formId
        h.vm.offerConnect(link, current)
        assertEquals(first, h.vm.editing)
        assertEquals(link.url, h.vm.pendingPrefill.value!!.url)
        h.vm.backToList()
        h.vm.offerConnect(link, current)
        assertEquals("a late scanner must not reopen a left form", Screen.List, h.vm.screen.value)
        assertNull(h.vm.pendingPrefill.value)
        assertEquals(0, h.dao.writes)
        assertTrue(h.vault.stored.isEmpty())
    }

    @Test fun this_phone_is_pinned_above_every_stored_kernel() = lifecycleTest {
        val h = harness(listOf(url, box))
        assertEquals(listOf(KernelList.THIS_PHONE, url, box), h.vm.kernelList.value)
        assertEquals("onboarding still sees only stored rows", listOf(url, box), h.vm.profiles.value)
        val stored = Profile(9, "This phone", baseUrl = "", kind = KernelKind.LOCAL)
        h.dao.rows.value = listOf(url, stored, box)
        assertEquals(listOf(stored, url, box), h.vm.kernelList.value)
    }

    @Test fun an_unavailable_kernel_shows_its_reason_and_does_not_navigate() = lifecycleTest {
        val h = harness(listOf(url, box))
        val reason = "not available in this build yet"
        assertEquals(KernelState.Failed(reason), h.vm.kernelState(KernelList.THIS_PHONE).value)
        assertEquals(KernelState.Failed(reason), h.vm.kernelState(box).value)
        h.vm.activate(KernelList.THIS_PHONE); h.vm.awaitLogin()
        assertEquals(UiStatus.Error(reason), h.vm.status.value)
        assertEquals(Screen.List, h.vm.screen.value)
        h.vm.activate(box); h.vm.awaitLogin()
        assertEquals(UiStatus.Error(reason), h.vm.status.value)
        assertNull(h.vm.activeKernel.value)
    }

    @Test fun opening_a_url_kernel_puts_it_in_front_until_it_is_deleted() = lifecycleTest {
        val h = harness(listOf(url, box))
        h.vm.activate(url); h.vm.awaitLogin()
        val opened = h.dao.rows.value.single { it.id == url.id }
        assertEquals(Screen.Web(opened), h.vm.screen.value)
        assertEquals(opened, h.vm.activeKernel.value)
        assertEquals(KernelState.Ready, h.vm.kernelState(opened).value)
        h.vm.backToList()
        assertEquals("still in front on returning to the list", opened, h.vm.activeKernel.value)
        h.vm.deleteProfile(box)
        assertEquals(opened, h.vm.activeKernel.value)
        h.vm.deleteProfile(opened)
        assertNull(h.vm.activeKernel.value)
    }

    @Test fun this_phone_is_put_in_front_at_tap_before_readiness_and_opens_the_live_endpoint() = lifecycleTest {
        val gate = CompletableDeferred<Unit>()
        val local = ReadyKernel(KernelEndpoint("http://127.0.0.1:43567/", "127.0.0.1"), gate)
        val h = harness(listOf(url), local = local, answerMeta = true)
        h.releaseOnClose(gate)
        h.vm.activate(KernelList.THIS_PHONE)
        assertTrue("lifecycle front notification must precede the readiness wait", local.fronts.any { it.kind == KernelKind.LOCAL })
        assertEquals(Screen.List, h.vm.screen.value)
        gate.complete(Unit)
        h.vm.awaitLogin()
        val opened = (h.vm.screen.value as Screen.Web).profile
        assertEquals(KernelKind.LOCAL, opened.kind)
        assertEquals("http://127.0.0.1:43567/", opened.baseUrl)
        assertEquals(opened, h.vm.activeKernel.value)
        assertEquals("the placeholder/live endpoint is never saved", listOf(url), h.dao.rows.value)
        h.vm.startAdd("https://review.test/", "Review")
        assertEquals("a link must not displace the local WebView", Screen.Web(opened), h.vm.screen.value)
        h.vm.acceptPrefill(revision = h.vm.pendingPrefill.value!!.revision)
        assertTrue(h.vm.screen.value is Screen.AddEdit)
        assertNull(h.vm.editing)
        assertEquals(opened, h.vm.activeKernel.value)
    }

    @Test fun adding_an_ssh_kernel_saves_it_and_returns_to_the_list_without_connecting() = lifecycleTest {
        val h = harness(listOf(url))
        h.vm.startAddSsh()
        assertTrue(h.vm.screen.value is Screen.AddEditSsh)
        assertNull(h.vm.editing)
        val endpoint = SshEndpoint(host = "dev.example", port = 2222, user = "me")
        h.vm.submitSsh("box", endpoint, "fixture-password"); h.vm.awaitLogin()
        val saved = h.dao.rows.value.single { it.alias == "box" }
        assertEquals(KernelKind.SSH, saved.kind)
        assertEquals(endpoint, saved.ssh)
        assertEquals("", saved.baseUrl)
        assertEquals("fixture-password", h.vault.secretFor("box"))
        assertEquals(Screen.List, h.vm.screen.value)
        assertEquals(UiStatus.Idle, h.vm.status.value)
        assertEquals(listOf(KernelList.THIS_PHONE, url, saved), h.vm.kernelList.value)
        assertNull(h.vm.activeKernel.value)
        h.vm.startAddSsh()
        h.vm.submitSsh("url", endpoint); h.vm.awaitLogin()
        assertTrue("a taken name is refused", h.vm.status.value is UiStatus.Error)
        assertTrue(h.vm.screen.value is Screen.AddEditSsh)
    }

    @Test fun an_unpinned_ssh_host_opens_its_form_to_trust_the_presented_key_then_connects() = lifecycleTest {
        val h = harness(listOf(url, box), sshFactory = { FirstTrustSsh(it, "SHA256:presented") }, answerMeta = true)
        h.vm.activate(box); h.vm.awaitLogin()
        assertTrue(h.vm.screen.value is Screen.AddEditSsh)
        assertEquals(box.id, h.vm.editing?.id)
        assertEquals("SHA256:presented", h.vm.hostKeyToTrust)
        assertEquals("ssh-ed25519 AAAAkey tofu-android", h.vm.sshPublicKey.value)
        assertEquals(UiStatus.Idle, h.vm.status.value)
        h.vm.trustHostKey(); h.vm.awaitLogin()
        assertEquals("SHA256:presented", h.dao.rows.value.single { it.id == box.id }.ssh?.hostKeySha256)
        assertNull(h.vm.hostKeyToTrust)
        assertEquals("http://127.0.0.2:41234", (h.vm.screen.value as Screen.Web).profile.baseUrl)
        assertEquals("", h.dao.rows.value.single { it.id == box.id }.baseUrl)
    }

    @Test fun leaving_the_ssh_form_forgets_the_key_it_offered() = lifecycleTest {
        val h = harness(listOf(url, box), sshFactory = { FirstTrustSsh(it, "SHA256:presented") })
        h.vm.activate(box); h.vm.awaitLogin()
        assertEquals("SHA256:presented", h.vm.hostKeyToTrust)
        h.vm.backToList()
        assertNull(h.vm.hostKeyToTrust)
        h.vm.trustHostKey()
        assertNull("nothing is pinned without the prompt", h.dao.rows.value.single { it.id == box.id }.ssh?.hostKeySha256)
    }

    @Test fun late_ssh_trust_prompts_and_public_keys_cannot_reopen_a_left_form() = lifecycleTest {
        val connectGate = CompletableDeferred<Unit>()
        val keyGate = CompletableDeferred<Unit>()
        val h = harness(listOf(box), sshFactory = { FirstTrustSsh(it, "SHA256:presented", connectGate, keyGate) })
        h.releaseOnClose(connectGate); h.releaseOnClose(keyGate)
        h.vm.activate(box)
        h.vm.backToList()
        connectGate.complete(Unit)
        testScheduler.runCurrent()
        assertEquals(Screen.List, h.vm.screen.value)
        assertNull(h.vm.hostKeyToTrust)
        h.vm.startEdit(box)
        h.vm.backToList()
        keyGate.complete(Unit)
        testScheduler.runCurrent()
        assertNull("no public-key result belongs to a disposed form", h.vm.sshPublicKey.value)
        assertEquals(UiStatus.Idle, h.vm.status.value)
    }

    @Test fun a_late_explicit_host_key_pin_cannot_navigate_after_back() = lifecycleTest {
        val trustGate = CompletableDeferred<Unit>()
        val h = harness(listOf(box), sshFactory = { FirstTrustSsh(it, "SHA256:presented", trustGate = trustGate) }, answerMeta = true)
        h.releaseOnClose(trustGate)
        h.vm.activate(box); h.vm.awaitLogin()
        h.vm.trustHostKey()
        h.vm.backToList()
        trustGate.complete(Unit)
        testScheduler.runCurrent()
        assertEquals(Screen.List, h.vm.screen.value)
        assertNull(h.vm.activeKernel.value)
        assertEquals("a cancelled trust completion cannot reconnect", 1, (h.ssh as FirstTrustSsh).connects)
    }

    @Test fun editing_routes_by_kind_and_this_phone_has_no_form() = lifecycleTest {
        val h = harness(listOf(url, box))
        h.vm.startEdit(url)
        assertTrue(h.vm.screen.value is Screen.AddEdit)
        h.vm.backToList()
        h.vm.startEdit(KernelList.THIS_PHONE)
        assertEquals(Screen.List, h.vm.screen.value)
        h.vm.startEdit(box)
        assertTrue(h.vm.screen.value is Screen.AddEditSsh)
        h.vm.submitSsh("box", box.ssh!!.copy(user = "other")); h.vm.awaitLogin()
        assertEquals("other", h.dao.rows.value.single { it.id == box.id }.ssh?.user)
        assertEquals(Screen.List, h.vm.screen.value)
    }

    @Test fun failed_checks_retry_after_cooldown_and_network_restore_but_success_is_throttled() = lifecycleTest {
        var now = 0L
        var calls = 0
        var available = false
        val h = harness(listOf(first), UpdateChecker("0.1.18") {
            calls++; if (available) AdvertisedClient("0.1.20", "https://first.test/client.apk") else null
        }, clock = { now })
        h.vm.checkForUpdate(); h.vm.awaitUpdateCheck()
        assertEquals(1, calls)
        assertNull("failed check is not a successful up-to-date verdict", h.vm.update.value)
        h.vm.checkForUpdate(); h.vm.awaitUpdateCheck()
        assertEquals("failure must have a retry cooldown", 1, calls)
        now = 30_001
        h.vm.checkForUpdate(); h.vm.awaitUpdateCheck()
        assertEquals(2, calls)
        available = true
        h.vm.onNetworkRestored(); h.vm.awaitUpdateCheck()
        assertEquals(3, calls)
        assertTrue(h.vm.update.value!!.available)
        h.vm.onNetworkRestored(); h.vm.checkForUpdate(); h.vm.awaitUpdateCheck()
        assertEquals("successful checks keep their cooldown", 3, calls)
        now += 6 * 60 * 60 * 1000L
        h.vm.checkForUpdate(); h.vm.awaitUpdateCheck()
        assertEquals(4, calls)
    }

    @Test fun kernel_only_lists_do_not_schedule_updates_and_late_removed_remote_results_are_unknown() = lifecycleTest {
        val started = CompletableDeferred<Unit>()
        val gate = CompletableDeferred<Unit>()
        var calls = 0
        val h = harness(listOf(first), UpdateChecker("0.1.18") {
            calls++; started.complete(Unit)
            withContext(NonCancellable) { gate.await() }
            AdvertisedClient("0.1.20", "https://first.test/client.apk")
        })
        h.releaseOnClose(gate)
        h.vm.checkForUpdate()
        started.await()
        h.dao.rows.value = listOf(box, KernelList.THIS_PHONE)
        h.vm.checkForUpdate()
        gate.complete(Unit)
        h.vm.awaitUpdateCheck()
        assertEquals(1, calls)
        assertNull("removed remote URLs cannot publish an old banner", h.vm.update.value)
        h.vm.onNetworkRestored(); h.vm.awaitUpdateCheck()
        assertEquals("SSH/local rows are not update origins", 1, calls)
    }
}
