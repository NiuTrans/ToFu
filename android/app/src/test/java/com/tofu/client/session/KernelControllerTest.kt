package com.tofu.client.session

import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.data.SshEndpoint
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/** The kernel seam's pure contract: kind dispatch, the placeholder, and list order. */
class KernelControllerTest {

    private class NamedController(val name: String) : KernelController {
        override suspend fun ensureRunning(profile: Profile) = KernelEndpoint("http://127.0.0.1:1/", "127.0.0.1")
        override suspend fun stop(profile: Profile) {}
        override fun state(profile: Profile): StateFlow<KernelState> = MutableStateFlow(KernelState.Failed(name))
    }

    private val url = Profile(id = 1, alias = "url", baseUrl = "https://u.example/")
    private val ssh = Profile(id = 2, alias = "box", baseUrl = "", kind = KernelKind.SSH, ssh = SshEndpoint(host = "h", user = "u"))
    private val url2 = Profile(id = 3, alias = "url2", baseUrl = "https://v.example/")

    @Test fun unavailable_controller_reports_failed_and_refuses_to_start() = runTest {
        val controller = UnavailableKernelController()
        val expected = KernelState.Failed("not available in this build yet")
        assertEquals(expected, controller.state(ssh).value)
        try {
            controller.ensureRunning(ssh)
            fail("an unavailable kernel must not hand out an endpoint")
        } catch (e: KernelUnavailableException) {
            assertEquals("not available in this build yet", e.reason)
        }
        controller.stop(ssh)
        assertEquals("stop leaves the placeholder's state alone", expected, controller.state(ssh).value)
        assertSame("one flow per kernel", controller.state(ssh), controller.state(ssh))
    }

    @Test fun each_kind_dispatches_to_its_own_controller() {
        val remote = NamedController("remote"); val local = NamedController("local"); val tunnel = NamedController("ssh")
        val kernels = KernelControllers(remoteUrl = remote, local = local, ssh = tunnel)
        assertSame(remote, kernels.of(KernelKind.REMOTE_URL))
        assertSame(local, kernels.of(KernelKind.LOCAL))
        assertSame(tunnel, kernels.of(KernelKind.SSH))
        assertEquals(KernelState.Failed("ssh"), kernels.state(ssh).value)
        assertEquals(KernelState.Failed("remote"), kernels.state(url).value)
        assertEquals(KernelState.Failed("local"), kernels.state(KernelList.THIS_PHONE).value)
    }

    @Test fun this_phone_is_pinned_first_and_other_kernels_keep_recency_order() {
        assertEquals(listOf(KernelList.THIS_PHONE), KernelList.pinned(emptyList()))
        assertEquals(listOf(KernelList.THIS_PHONE, url, ssh, url2), KernelList.pinned(listOf(url, ssh, url2)))
        val storedLocal = Profile(id = 9, alias = "This phone", baseUrl = "", kind = KernelKind.LOCAL)
        assertEquals(
            "a stored LOCAL row replaces the placeholder and is not listed twice",
            listOf(storedLocal, url, ssh), KernelList.pinned(listOf(url, storedLocal, ssh)),
        )
        assertEquals(0L, KernelList.THIS_PHONE.id)
        assertEquals(KernelKind.LOCAL, KernelList.THIS_PHONE.kind)
    }

    @Test fun binding_to_an_endpoint_changes_only_the_address_and_cookie_host() {
        val bound = ssh.boundTo(KernelEndpoint("http://127.0.0.2:41234/", "127.0.0.2"))
        assertEquals(ssh.copy(baseUrl = "http://127.0.0.2:41234/", cookieHost = "127.0.0.2"), bound)
        assertTrue(ServerUrl.parse(bound.baseUrl) != null)
    }
}
