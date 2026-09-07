package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.runTest
import okhttp3.Cookie
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pins the bounded login retry: transient TRANSPORT failures retry up to
 * [LoginRetryPolicy.MAX_ATTEMPTS] with the declared backoff; definitive
 * answers (BadCredentials, SSO handoff, Success) never retry.
 */
class SessionManagerLoginRetryTest {

    // ── Fakes ─────────────────────────────────────────────────────────────
    private class FakeCookieSink : CookieSink {
        override fun inject(origin: String, cookies: List<Cookie>) {}
        override fun purgeHost(host: String) {}
        override fun cookieHeader(origin: String): String? = null
    }

    private class FakeSecrets(private val secret: String?) : SecretLookup {
        override fun secretFor(alias: String): String? = secret
    }

    private class FakeDao(private var current: Profile) : ProfileDao {
        override fun observeAll(): Flow<List<Profile>> = flowOf(listOf(current))
        override suspend fun getById(id: Long): Profile? = current
        override suspend fun getAllOnce(): List<Profile> = listOf(current)
        override suspend fun getByAlias(alias: String): Profile? = current
        override suspend fun insert(profile: Profile): Long = 1
        override suspend fun update(profile: Profile): Int { current = profile; return 1 }
        override suspend fun deleteById(id: Long) {}
        override suspend fun setCookieHost(id: Long, host: String?): Int {
            current = current.copy(cookieHost = host); return 1
        }
        override suspend fun touchLastUsed(id: Long, timestamp: Long): Int = 1
        override suspend fun setAuthType(id: Long, authType: AuthType): Int = 1
    }

    private fun profile(baseUrl: String, authType: AuthType = AuthType.CODE_SERVER_PASSWORD) =
        Profile(id = 1, alias = "dc1", baseUrl = baseUrl, authType = authType)

    /** Serves [status]/[body] for every request; counts what it saw. */
    private class StaticServer(
        private val status: Int,
        private val body: String,
    ) : okhttp3.Interceptor {
        var requests = 0
            private set

        override fun intercept(chain: okhttp3.Interceptor.Chain): Response {
            requests++
            return Response.Builder()
                .request(chain.request())
                .protocol(Protocol.HTTP_1_1)
                .code(status)
                .message("static")
                .body(body.toResponseBody("text/html".toMediaType()))
                .build()
        }
    }

    @Test
    fun backoff_schedule_pins() {
        assertEquals(1_000L, LoginRetryPolicy.backoffMs(1))
        assertEquals(2_500L, LoginRetryPolicy.backoffMs(2))
        assertEquals(2_500L, LoginRetryPolicy.backoffMs(9))
        // The warming ladder stretches for a cold sandbox, capped at 8s.
        assertEquals(2_000L, LoginRetryPolicy.backoffMs(1, warming = true))
        assertEquals(4_000L, LoginRetryPolicy.backoffMs(2, warming = true))
        assertEquals(6_000L, LoginRetryPolicy.backoffMs(3, warming = true))
        assertEquals(8_000L, LoginRetryPolicy.backoffMs(4, warming = true))
        assertEquals(8_000L, LoginRetryPolicy.backoffMs(9, warming = true))
        assertEquals(LoginRetryPolicy.MAX_WARMING_ATTEMPTS,
            LoginRetryPolicy.maxAttempts(warming = true))
        assertEquals(LoginRetryPolicy.MAX_ATTEMPTS,
            LoginRetryPolicy.maxAttempts(warming = false))
    }

    @Test
    fun transport_error_retries_with_bounded_backoff() = runTest {
        // Nothing listens on port 1 → every attempt fails fast at connect.
        val p = profile("http://127.0.0.1:1/proxy/15000/")
        var requests = 0
        val counting = OkHttpClient.Builder()
            .addInterceptor { chain -> requests++; chain.proceed(chain.request()) }
            .build()
        val sleeps = mutableListOf<Long>()
        val mgr = SessionManager(
            dao = FakeDao(p),
            secrets = FakeSecrets("pw"),
            cookies = FakeCookieSink(),
            http = counting,
            sleeper = { sleeps += it },
        )

        val result = mgr.login(p)

        // NEUTER: delete the retry loop and sleeps is empty / one attempt only.
        assertTrue("transport failure must surface as Error: $result",
            result is LoginResult.Error)
        assertEquals(listOf(1_000L, 2_500L), sleeps)
        // Each attempt is GET (resolveLoginUrl) + POST (login): 3 attempts → 6.
        assertEquals(
            "attempts must be bounded at ${LoginRetryPolicy.MAX_ATTEMPTS}",
            LoginRetryPolicy.MAX_ATTEMPTS * 2, requests,
        )
    }

    @Test
    fun definitive_bad_credentials_is_not_retried() = runTest {
        // code-server re-serves the login page (200) on a wrong password — a
        // DEFINITIVE answer, so no backoff, no second attempt.
        val server = StaticServer(200, "<html></html>")
        val http = OkHttpClient.Builder().addInterceptor(server).build()
        val p = profile("https://h.example.com/proxy/15000/")
        val sleeps = mutableListOf<Long>()
        val mgr = SessionManager(
            dao = FakeDao(p),
            secrets = FakeSecrets("pw"),
            cookies = FakeCookieSink(),
            http = http,
            sleeper = { sleeps += it },
        )

        val result = mgr.login(p)

        assertTrue("expected BadCredentials: $result", result is LoginResult.BadCredentials)
        assertTrue("a definitive answer must not sleep: $sleeps", sleeps.isEmpty())
        assertEquals("one GET (form) + one POST (login), no retry", 2, server.requests)
    }

    @Test
    fun proxy_edge_502_uses_the_warming_ladder() = runTest {
        // Sandbox cold-start behind the vscode proxy: the edge answers 502
        // while the container boots. That is WARMING, not an "unconfirmed
        // gate" — degrading to Success would drop the user into a WebView
        // rendering the proxy's raw error page. NEUTER: let the 502 fall
        // through to the unconfirmed degrade branch and this fails (result
        // would be Success with empty sleeps).
        val server = StaticServer(502, "<html>Bad Gateway</html>")
        val http = OkHttpClient.Builder().addInterceptor(server).build()
        val p = profile("https://h.example.com/proxy/15000/")
        val sleeps = mutableListOf<Long>()
        val mgr = SessionManager(
            dao = FakeDao(p),
            secrets = FakeSecrets("pw"),
            cookies = FakeCookieSink(),
            http = http,
            sleeper = { sleeps += it },
        )

        val result = mgr.login(p)

        assertTrue("warming exhaustion must surface as Error: $result",
            result is LoginResult.Error)
        assertTrue("message must name the waking sandbox: $result",
            (result as LoginResult.Error).message.contains("waking"))
        assertEquals(listOf(2_000L, 4_000L, 6_000L, 8_000L, 8_000L), sleeps)
        // Each attempt is GET (resolveLoginUrl) + POST (login).
        assertEquals(LoginRetryPolicy.MAX_WARMING_ATTEMPTS * 2, server.requests)
    }

    @Test
    fun warming_status_set_and_transport_fingerprint_are_pinned() {
        assertTrue(LoginRetryPolicy.isWarmingStatus(502))
        assertTrue(LoginRetryPolicy.isWarmingStatus(503))
        assertTrue(LoginRetryPolicy.isWarmingStatus(504))
        // 500 is a REAL server error (something answered), not a cold edge.
        assertTrue(!LoginRetryPolicy.isWarmingStatus(500))
        assertTrue(!LoginRetryPolicy.isWarmingStatus(401))
        assertTrue(LoginRetryPolicy.isWarmingTransport("connect timed out"))
        assertTrue(LoginRetryPolicy.isWarmingTransport("Read error: timeout"))
        assertTrue(!LoginRetryPolicy.isWarmingTransport("Failed to connect to /10.0.0.1"))
        assertTrue(!LoginRetryPolicy.isWarmingTransport(null))
    }
}
