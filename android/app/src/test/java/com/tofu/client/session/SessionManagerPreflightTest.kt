package com.tofu.client.session

import com.tofu.client.api.ApiV4Contract
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
 * Pins the post-login v4 meta preflight on the SessionManager path: a
 * definitive refusal (wrong apiMajor / minAndroidBuild above this build)
 * swaps Success for [LoginResult.Incompatible]; every partial-knowledge
 * shape (404, garbage body) keeps Success; non-Success logins and the SSO
 * handoff never fire the preflight at all.
 */
class SessionManagerPreflightTest {

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

    /**
     * A code-server gate that answers the login handshake (GET form probe →
     * 200 page, POST → 302 + session cookie) and serves the meta endpoint
     * from [metaStatus]/[metaBody]. The preflight only fires after a real
     * credential login, so the tests ride the full handshake.
     */
    private class MetaServer(
        var metaStatus: Int,
        var metaBody: String,
    ) : okhttp3.Interceptor {
        var metaRequests = 0
            private set

        override fun intercept(chain: okhttp3.Interceptor.Chain): Response {
            val req = chain.request()
            val b = Response.Builder().request(req).protocol(Protocol.HTTP_1_1)
            if (req.url.toString().contains("/api/v4/meta")) {
                metaRequests++
                return b.code(metaStatus).message("stub")
                    .body(metaBody.toResponseBody("application/json".toMediaType()))
                    .build()
            }
            if (req.method == "POST") {
                return b.code(302).message("Found")
                    .header("Set-Cookie", "code-server-session=tok; Path=/; HttpOnly")
                    .body("".toResponseBody("text/html".toMediaType()))
                    .build()
            }
            return b.code(200).message("OK")
                .body("<html><form action=\"./login\" method=\"post\"></form></html>"
                    .toResponseBody("text/html".toMediaType()))
                .build()
        }
    }

    private fun profile(
        authType: AuthType = AuthType.CODE_SERVER_PASSWORD,
        baseUrl: String = "https://h.example.com/proxy/15000/",
    ) = Profile(id = 1, alias = "dc1", baseUrl = baseUrl, authType = authType)

    private fun manager(
        p: Profile,
        server: MetaServer,
        appVersionCode: Int = 17,
    ) = SessionManager(
        dao = FakeDao(p),
        secrets = FakeSecrets("pw"),
        cookies = FakeCookieSink(),
        http = OkHttpClient.Builder().addInterceptor(server).build(),
        appVersionCode = appVersionCode,
        sleeper = {},
    )

    private fun metaEnvelope(apiMajor: Int = ApiV4Contract.API_MAJOR, minAndroidBuild: Int = 1) =
        """{"data":{"apiMajor":$apiMajor,"minAndroidBuild":$minAndroidBuild,""" +
            """"schemaVersion":1,"serverBuild":"x","minDesktopBuild":"0.0.1"},""" +
            """"meta":{"requestId":"r","serverTimeMs":0}}"""

    @Test
    fun compatible_meta_keeps_success() = runTest {
        val server = MetaServer(200, metaEnvelope())
        val result = manager(profile(), server).login(profile())
        assertTrue("expected Success: $result", result is LoginResult.Success)
        assertEquals(1, server.metaRequests)
    }

    @Test
    fun api_major_mismatch_swaps_success_for_incompatible() = runTest {
        val server = MetaServer(200, metaEnvelope(apiMajor = ApiV4Contract.API_MAJOR + 1))
        val result = manager(profile(), server).login(profile())
        // NEUTER: drop withApiPreflight and this stays Success.
        assertTrue("expected Incompatible: $result", result is LoginResult.Incompatible)
        assertTrue((result as LoginResult.Incompatible).message.contains("update the app"))
    }

    @Test
    fun min_android_build_above_this_build_blocks() = runTest {
        val server = MetaServer(200, metaEnvelope(minAndroidBuild = 18))
        val result = manager(profile(), server, appVersionCode = 17).login(profile())
        assertTrue("expected Incompatible: $result", result is LoginResult.Incompatible)
        assertTrue((result as LoginResult.Incompatible).message.contains("18"))
    }

    @Test
    fun missing_meta_endpoint_never_blocks() = runTest {
        // A server that predates /api/v4/meta: fail OPEN, the WebView decides.
        val server = MetaServer(404, "")
        val result = manager(profile(), server).login(profile())
        assertTrue("expected Success: $result", result is LoginResult.Success)
        assertEquals(1, server.metaRequests)
    }

    @Test
    fun unparseable_meta_never_blocks() = runTest {
        val server = MetaServer(200, "<html>proxy error page</html>")
        val result = manager(profile(), server).login(profile())
        assertTrue("expected Success: $result", result is LoginResult.Success)
    }

    @Test
    fun sso_handoff_skips_the_preflight() = runTest {
        val server = MetaServer(200, metaEnvelope(apiMajor = 99))
        val p = profile(authType = AuthType.INTERACTIVE_SSO)
        val result = manager(p, server).login(p)
        assertTrue("expected NeedsInteractiveSso: $result",
            result is LoginResult.NeedsInteractiveSso)
        assertEquals("no login succeeded → no preflight", 0, server.metaRequests)
    }

    @Test
    fun failed_login_skips_the_preflight() = runTest {
        // Wrong password → 200 login page re-served → BadCredentials, so the
        // preflight must NOT fire (no session exists to judge compatibility
        // with, and the meta endpoint itself may be gated).
        val server = object : okhttp3.Interceptor {
            override fun intercept(chain: okhttp3.Interceptor.Chain): Response =
                Response.Builder()
                    .request(chain.request())
                    .protocol(Protocol.HTTP_1_1)
                    .code(200).message("OK")
                    .body("<html></html>".toResponseBody("text/html".toMediaType()))
                    .build()
        }
        var metaRequests = 0
        val counting = okhttp3.Interceptor { chain ->
            if (chain.request().url.toString().contains("/api/v4/meta")) metaRequests++
            server.intercept(chain)
        }
        val p = profile()
        val mgr = SessionManager(
            dao = FakeDao(p), secrets = FakeSecrets("pw"), cookies = FakeCookieSink(),
            http = OkHttpClient.Builder().addInterceptor(counting).build(),
            appVersionCode = 17, sleeper = {},
        )
        val result = mgr.login(p)
        assertTrue("expected BadCredentials: $result", result is LoginResult.BadCredentials)
        assertEquals(0, metaRequests)
    }

    @Test
    fun none_profile_never_fires_the_preflight() = runTest {
        // NONE stays a zero-request short-circuit: no gateway to unlock, the
        // SPA's own version handling applies inside the WebView.
        val server = MetaServer(200, metaEnvelope(apiMajor = 99))
        val p = profile(authType = AuthType.NONE)
        val result = manager(p, server).login(p)
        assertTrue("expected Success: $result", result is LoginResult.Success)
        assertEquals(0, server.metaRequests)
    }
}
