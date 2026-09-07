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
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Guards the graceful-degradation contract for headless login: a server that
 * 302s WITHOUT the `code-server-session` cookie (bare Tofu, a different gate,
 * or a changed login form) must NOT hard-Error and strand the user on the
 * profile list. [SessionManager.login] should return [LoginResult.Success] and
 * let WebScreen load `baseUrl`, handing any real auth to the server's own login
 * page / [ReauthWebViewClient] inside the WebView.
 */
class SessionManagerLoginDegradeTest {

    // ── Fakes ─────────────────────────────────────────────────────────────
    private class FakeCookieSink : CookieSink {
        val injected = mutableListOf<String>()
        override fun inject(origin: String, cookies: List<Cookie>) { injected += origin }
        override fun purgeHost(host: String) {}
        override fun cookieHeader(origin: String): String? = null
    }

    private class FakeSecrets(private val secret: String?) : SecretLookup {
        override fun secretFor(alias: String): String? = secret
    }

    private class FakeDao(private var current: Profile) : ProfileDao {
        val updates = mutableListOf<Profile>()
        override fun observeAll(): Flow<List<Profile>> = flowOf(listOf(current))
        override suspend fun getById(id: Long): Profile? = current
        override suspend fun getAllOnce(): List<Profile> = listOf(current)
        override suspend fun getByAlias(alias: String): Profile? = current
        override suspend fun insert(profile: Profile): Long = 1
        override suspend fun update(profile: Profile): Int { current = profile; updates += profile; return 1 }
        override suspend fun deleteById(id: Long) {}
        val stamps = mutableListOf<String?>()
        override suspend fun setCookieHost(id: Long, host: String?): Int {
            current = current.copy(cookieHost = host); stamps += host; return 1
        }
        override suspend fun touchLastUsed(id: Long, timestamp: Long): Int {
            current = current.copy(lastUsedAt = timestamp); return 1
        }
        override suspend fun setAuthType(id: Long, authType: AuthType): Int {
            current = current.copy(authType = authType); return 1
        }
    }

    /**
     * Canned-response interceptor: the GET (resolveLoginUrl form probe) returns
     * a bodyless 200 so the login URL falls back to the origin root; the POST
     * /login returns a 302 whose only Set-Cookie is NOT the code-server session
     * cookie — the exact shape a non-code-server gate produces.
     */
    private class NonCodeServerGate : Interceptor {
        override fun intercept(chain: Interceptor.Chain): Response {
            val req = chain.request()
            val builder = Response.Builder()
                .request(req)
                .protocol(Protocol.HTTP_1_1)
            return if (req.method == "POST") {
                builder.code(302).message("Found")
                    // Some other cookie, deliberately not `code-server-session`.
                    .header("Set-Cookie", "gateway_hint=abc; Path=/; SameSite=Lax")
                    .body("".toResponseBody("text/html".toMediaType()))
                    .build()
            } else {
                builder.code(200).message("OK")
                    .body("<html></html>".toResponseBody("text/html".toMediaType()))
                    .build()
            }
        }
    }

    private fun profile() = Profile(
        id = 1,
        alias = "bare tofu",
        baseUrl = "https://tofu.example.com/",
        authType = AuthType.CODE_SERVER_PASSWORD,
        cookieHost = null,
    )

    @Test
    fun redirect_without_session_cookie_degrades_to_success() = runTest {
        val cookies = FakeCookieSink()
        val dao = FakeDao(profile())
        val http = OkHttpClient.Builder()
            .followRedirects(false)
            .followSslRedirects(false)
            .addInterceptor(NonCodeServerGate())
            .build()

        val mgr = SessionManager(
            dao = dao,
            secrets = FakeSecrets("pw"),
            cookies = cookies,
            http = http,
        )

        val result = mgr.login(profile())

        // NEUTER CHECK: restore the old hard-fail
        // (`return@withContext LoginResult.Error("302 without session cookie")`)
        // in SessionManager.login and this assertion fails — the user would be
        // stranded on a 302-without-cookie Error instead of loading the WebView.
        assertTrue("expected graceful Success, got $result", result is LoginResult.Success)

        // We must NOT have injected any cookie (there was no session cookie to
        // replay) nor stamped cookieHost (no cached jar to bind).
        assertTrue("must not inject a cookie: ${cookies.injected}", cookies.injected.isEmpty())
        assertTrue("must not stamp cookieHost: ${dao.updates}", dao.updates.isEmpty())
    }

    @Test
    fun successful_code_server_login_still_injects_cookie() = runTest {
        // Sanity anti-neuter: the degrade path must not swallow the REAL
        // code-server login — a 302 with `code-server-session` still injects.
        val cookies = FakeCookieSink()
        val dao = FakeDao(profile())
        val realGate = object : Interceptor {
            override fun intercept(chain: Interceptor.Chain): Response {
                val req = chain.request()
                val b = Response.Builder().request(req).protocol(Protocol.HTTP_1_1)
                return if (req.method == "POST") {
                    b.code(302).message("Found")
                        .header("Set-Cookie",
                            "code-server-session=tok; Path=/; SameSite=Lax")
                        .body("".toResponseBody("text/html".toMediaType())).build()
                } else {
                    b.code(200).message("OK")
                        .body("<html></html>".toResponseBody("text/html".toMediaType())).build()
                }
            }
        }
        val http = OkHttpClient.Builder()
            .followRedirects(false).followSslRedirects(false)
            .addInterceptor(realGate).build()

        val mgr = SessionManager(dao, FakeSecrets("pw"), cookies, http)

        val result = mgr.login(profile())

        assertTrue("expected Success, got $result", result is LoginResult.Success)
        assertEquals(listOf("https://tofu.example.com"), cookies.injected)
    }

    /** Interceptor that FAILS the test if any HTTP call is attempted. */
    private class NoHttpAllowed : Interceptor {
        override fun intercept(chain: Interceptor.Chain): Response =
            throw AssertionError(
                "login() made an HTTP request for an AuthType.NONE profile: " +
                chain.request().method + " " + chain.request().url,
            )
    }

    /**
     * A bare Tofu server (no code-server gate) answers the password POST with
     * 401 HTML when unauthenticated — a NON-302, NON-200 status. The GET form
     * probe still returns 200 so the login URL falls back to the origin root.
     */
    private class BareTofu401Gate : Interceptor {
        override fun intercept(chain: Interceptor.Chain): Response {
            val req = chain.request()
            val b = Response.Builder().request(req).protocol(Protocol.HTTP_1_1)
            return if (req.method == "POST") {
                b.code(401).message("Unauthorized")
                    .body("<html>unauthorized</html>".toResponseBody("text/html".toMediaType()))
                    .build()
            } else {
                b.code(200).message("OK")
                    .body("<html></html>".toResponseBody("text/html".toMediaType()))
                    .build()
            }
        }
    }

    @Test
    fun non_302_non_200_status_degrades_to_success() = runTest {
        // A CODE_SERVER_PASSWORD profile pointed at a server that is NOT a
        // standard code-server gate (bare Tofu → 401, a fronting gateway →
        // 4xx/5xx). The password replay reaches neither the 302-success branch
        // nor the 200=BadCredentials signal, so the login method must degrade
        // gracefully to Success (defer to WebView) — NOT hard-Error and strand
        // the user on the profile list.
        //
        // NEUTER CHECK: restore the old
        // `return@withContext LoginResult.Error("Unexpected status ${resp.code}")`
        // and this assertion fails — the user hits an "Unexpected status 401"
        // hard error instead of loading the WebView.
        val cookies = FakeCookieSink()
        val dao = FakeDao(profile())
        val http = OkHttpClient.Builder()
            .followRedirects(false)
            .followSslRedirects(false)
            .addInterceptor(BareTofu401Gate())
            .build()

        val mgr = SessionManager(dao, FakeSecrets("pw"), cookies, http)

        val result = mgr.login(profile())

        assertTrue("expected graceful Success on 401, got $result",
            result is LoginResult.Success)
        // Nothing to inject/stamp: there was no replayable session cookie.
        assertTrue("must not inject a cookie: ${cookies.injected}", cookies.injected.isEmpty())
        assertTrue("must not stamp cookieHost: ${dao.updates}", dao.updates.isEmpty())
    }

    @Test
    fun bad_password_200_still_reports_bad_credentials() = runTest {
        // Anti-neuter for the degrade change: the 200=BadCredentials signal
        // must survive. A code-server that re-serves the login page (200) on a
        // wrong password must still surface BadCredentials — NOT be swallowed
        // into a defer-to-WebView Success by the new fall-through.
        val cookies = FakeCookieSink()
        val dao = FakeDao(profile())
        val badPwGate = object : Interceptor {
            override fun intercept(chain: Interceptor.Chain): Response {
                val req = chain.request()
                val b = Response.Builder().request(req).protocol(Protocol.HTTP_1_1)
                return b.code(200).message("OK")
                    .body("<html>login</html>".toResponseBody("text/html".toMediaType()))
                    .build()
            }
        }
        val http = OkHttpClient.Builder()
            .followRedirects(false).followSslRedirects(false)
            .addInterceptor(badPwGate).build()

        val mgr = SessionManager(dao, FakeSecrets("pw"), cookies, http)

        val result = mgr.login(profile())

        assertTrue("expected BadCredentials on 200, got $result",
            result is LoginResult.BadCredentials)
    }

    @Test
    fun none_profile_short_circuits_with_zero_http() = runTest {
        // This is the CODE-LEVEL MIRROR of the real on-device path a user hits
        // on a bare Tofu server: a new profile now defaults to AuthType.NONE
        // (Profile.kt / AddEditScreen.kt), and SessionManager.login must return
        // Success at the top (SessionManager.kt:53) WITHOUT sending any request
        // — so it can never reach the code-server /login POST and can never
        // surface the "302 No session cookie" error the user reported.
        //
        // NEUTER CHECK: delete the `if (profile.authType == AuthType.NONE)`
        // short-circuit in SessionManager.login and this test fails — the
        // NoHttpAllowed interceptor throws the moment login() issues the GET/POST
        // handshake, exactly reproducing the pre-fix on-device failure path.
        val cookies = FakeCookieSink()
        val dao = FakeDao(profile())
        val http = OkHttpClient.Builder()
            .followRedirects(false)
            .followSslRedirects(false)
            .addInterceptor(NoHttpAllowed())
            .build()

        val noneProfile = profile().copy(authType = AuthType.NONE)
        // A NONE profile also has no stored secret; login must not need one.
        val mgr = SessionManager(dao, FakeSecrets(null), cookies, http)

        val result = mgr.login(noneProfile)

        assertTrue("expected Success for NONE, got $result", result is LoginResult.Success)
        assertEquals("bare Tofu host", "tofu.example.com",
            (result as LoginResult.Success).host)
        // No handshake ⇒ nothing injected, nothing stamped.
        assertTrue("must not inject a cookie: ${cookies.injected}", cookies.injected.isEmpty())
        assertTrue("must not stamp cookieHost: ${dao.updates}", dao.updates.isEmpty())
    }
}
