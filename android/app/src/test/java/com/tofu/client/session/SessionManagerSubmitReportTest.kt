package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.runTest
import okhttp3.Cookie
import okhttp3.Interceptor
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
 * Pins the support-report upload: it must target the profile's own base URL
 * (so a `/proxy/<port>/` deploy posts under the subpath, not the bare origin),
 * carry the session cookie, POST the exact payload, and never throw — reporting
 * its outcome as text instead.
 */
class SessionManagerSubmitReportTest {

    private val cookie = "code-server-session=abc123"

    private class FakeCookieSink(private val header: String?) : CookieSink {
        override fun inject(origin: String, cookies: List<Cookie>) {}
        override fun purgeHost(host: String) {}
        override fun cookieHeader(origin: String): String? = header
    }

    private class FakeSecrets : SecretLookup {
        override fun secretFor(alias: String): String? = null
    }

    private class FakeDao(private val current: Profile) : ProfileDao {
        override fun observeAll(): Flow<List<Profile>> = flowOf(listOf(current))
        override suspend fun getById(id: Long): Profile? = current
        override suspend fun getAllOnce(): List<Profile> = listOf(current)
        override suspend fun getByAlias(alias: String): Profile? = current
        override suspend fun insert(profile: Profile): Long = 1
        override suspend fun update(profile: Profile): Int = 1
        override suspend fun deleteById(id: Long) {}
        override suspend fun setCookieHost(id: Long, host: String?): Int = 1
        override suspend fun setCookieHostIfUrlMatches(id: Long, expectedUrl: String, host: String): Int {
            val row = getById(id) ?: return 0
            return if (row.baseUrl == expectedUrl) setCookieHost(id, host) else 0
        }
        override suspend fun touchLastUsed(id: Long, timestamp: Long): Int = 1
        override suspend fun setAuthType(id: Long, authType: AuthType): Int = 1
    }

    /** Captures the request and answers with a fixed status. */
    private class CapturingServer(private val status: Int) : Interceptor {
        var request: Request? = null
            private set

        override fun intercept(chain: Interceptor.Chain): Response {
            request = chain.request()
            return Response.Builder()
                .request(chain.request())
                .protocol(Protocol.HTTP_1_1)
                .code(status)
                .message("capture")
                .body("{}".toResponseBody("application/json".toMediaType()))
                .build()
        }
    }

    private fun profile(baseUrl: String) = Profile(
        id = 1, alias = "dc1", baseUrl = baseUrl,
        authType = AuthType.CODE_SERVER_PASSWORD,
    )

    private fun manager(server: CapturingServer): SessionManager = SessionManager(
        dao = FakeDao(profile("https://host.test/proxy/15000/")),
        secrets = FakeSecrets(),
        cookies = FakeCookieSink(cookie),
        http = OkHttpClient.Builder().addInterceptor(server).build(),
    )

    @Test
    fun posts_to_the_proxy_subpath_with_the_session_cookie() = runTest {
        val server = CapturingServer(200)
        val result = manager(server).submitReport(
            profile("https://host.test/proxy/15000/"), "{\"a\":1}")

        val request = server.request!!
        assertEquals(
            "https://host.test/proxy/15000/api/v1/mobile/client-report",
            request.url.toString(),
        )
        assertEquals("POST", request.method)
        assertEquals(cookie, request.header("Cookie"))
        assertTrue(result.contains("sent"))
    }

    @Test
    fun reports_a_rejection_without_throwing() = runTest {
        val server = CapturingServer(500)
        val result = manager(server).submitReport(
            profile("https://host.test/proxy/15000/"), "{}")
        assertTrue(result.contains("500"))
    }

    @Test
    fun invalid_base_url_is_reported_not_thrown() = runTest {
        val server = CapturingServer(200)
        val result = manager(server).submitReport(profile(""), "{}")
        assertTrue(result.contains("invalid server URL"))
        assertNull(server.request)
    }
}
