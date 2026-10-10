package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** A supervisor call must never carry the gateway cookie into an IdP redirect. */
@RunWith(RobolectricTestRunner::class)
@Config(manifest = Config.NONE, sdk = [34])
class SupervisorClientGatewayTest {
    @Test fun redirecting_control_plane_is_not_followed_even_with_an_injected_client() {
        val requests = mutableListOf<Request>()
        val cookieUrls = mutableListOf<String>()
        val http = OkHttpClient.Builder().followRedirects(true).addInterceptor { chain ->
            requests += chain.request()
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
                .code(302).message("Sign in").header("Location", "https://idp.example/login")
                .body("{}".toResponseBody("application/json".toMediaType())).build()
        }.build()
        val profile = Profile(id = 1, alias = "test", baseUrl = "https://host.example:8443/dev/absproxy/15000/", authType = AuthType.INTERACTIVE_SSO, projectPath = "/project with spaces")
        val client = SupervisorClient(http) { url -> cookieUrls += url; "gateway=test-only" }
        val result = client.status(profile)
        assertTrue(result is SupervisorClient.Result.Failed)
        assertEquals(302, (result as SupervisorClient.Result.Failed).code)
        val request = requests.single()
        assertEquals("/dev/proxy/15001/status", request.url.encodedPath)
        assertEquals(8443, request.url.port)
        assertEquals(profile.projectPath, request.url.queryParameter("projectPath"))
        assertEquals("gateway=test-only", request.header("Cookie"))
        assertEquals(listOf(request.url.toString()), cookieUrls)
    }
}
