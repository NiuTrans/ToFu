package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import kotlinx.coroutines.runBlocking
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * The URL kernel keeps the pre-kernel behaviour: resolving its endpoint never
 * touches the network, and only a managed profile can be stopped — through the
 * same login-then-act supervisor call the card uses.
 */
@RunWith(RobolectricTestRunner::class)
@Config(manifest = Config.NONE, sdk = [34])
class UrlKernelControllerTest {

    private val host = "h.example"
    private val managed = Profile(
        id = 1, alias = "dev", baseUrl = "https://$host/proxy/15000/",
        authType = AuthType.CODE_SERVER_PASSWORD, cookieHost = host, projectPath = "/home/dev/tofu",
    )

    private class Supervisor(private val code: Int, private val body: String) : okhttp3.Interceptor {
        val requests = mutableListOf<Request>()
        override fun intercept(chain: okhttp3.Interceptor.Chain): Response {
            requests += chain.request()
            return Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(code).message("stub")
                .body(body.toResponseBody("application/json".toMediaType())).build()
        }
    }

    private fun controller(
        supervisor: Supervisor,
        login: suspend (Profile) -> LoginResult = { fail("signed-in profiles stop without a login"); LoginResult.Success(host) },
    ) = UrlKernelController(
        SupervisorClient(OkHttpClient.Builder().addInterceptor(supervisor).build()) { "code-server-session=s" },
        login,
    )

    @Test fun the_endpoint_is_the_profile_url_and_needs_no_request() = runBlocking {
        val supervisor = Supervisor(500, "{}")
        val kernel = controller(supervisor)
        assertEquals(KernelState.Ready, kernel.state(managed).value)
        assertSame("one flow per kernel", kernel.state(managed), kernel.state(managed.copy(alias = "renamed")))
        assertEquals(KernelEndpoint(managed.baseUrl, host), kernel.ensureRunning(managed))
        assertEquals(KernelState.Ready, kernel.state(managed).value)
        assertTrue(supervisor.requests.isEmpty())
    }

    @Test fun an_unparseable_url_fails_with_the_session_wording() = runBlocking {
        val broken = managed.copy(baseUrl = "not a url")
        val kernel = controller(Supervisor(500, "{}"))
        try {
            kernel.ensureRunning(broken)
            fail("no endpoint for an invalid URL")
        } catch (e: KernelUnavailableException) {
            assertEquals("Invalid server URL", e.reason)
        }
        assertEquals(KernelState.Failed("Invalid server URL"), kernel.state(broken).value)
    }

    @Test fun stop_asks_the_supervisor_only_for_a_managed_profile() = runBlocking {
        val supervisor = Supervisor(200, """{"ok":true,"running":false}""")
        val kernel = controller(supervisor)

        kernel.stop(managed.copy(projectPath = null))
        assertTrue("an open-only server is not the app's to stop", supervisor.requests.isEmpty())

        kernel.stop(managed)
        assertEquals("/proxy/15001/stop", supervisor.requests.single().url.encodedPath)
        assertEquals(KernelState.Stopped, kernel.state(managed).value)
        assertEquals(
            "an edited URL is a different kernel with its own state",
            KernelState.Ready, kernel.state(managed.copy(baseUrl = "https://other.example/proxy/15000/")).value,
        )
    }

    @Test fun a_refused_stop_is_failed_with_the_supervisor_explanation() = runBlocking {
        val kernel = controller(Supervisor(403, """{"ok":false,"error":"projectPath not allow-listed"}"""))
        kernel.stop(managed)
        val state = kernel.state(managed).value
        assertTrue(state is KernelState.Failed)
        assertEquals(SupervisorUrl.explainFailure(403, "projectPath not allow-listed"), (state as KernelState.Failed).reason)
    }

    @Test fun stop_signs_in_first_and_a_blocked_sign_in_never_reaches_the_supervisor() = runBlocking {
        val supervisor = Supervisor(200, """{"ok":true,"running":false}""")
        val logins = mutableListOf<Profile>()
        val signedOut = managed.copy(cookieHost = null)
        val kernel = controller(supervisor) { logins += it; LoginResult.BadCredentials }

        kernel.stop(signedOut)
        assertEquals(listOf(signedOut), logins)
        assertTrue(supervisor.requests.isEmpty())
        assertEquals(
            KernelState.Failed(ServerLifecycle.explainLoginBlock(LoginResult.BadCredentials)),
            kernel.state(signedOut).value,
        )
    }
}
