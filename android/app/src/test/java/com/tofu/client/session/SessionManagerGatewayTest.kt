package com.tofu.client.session

import com.tofu.client.data.*
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.test.runTest
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.cancelAndJoin
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.*
import org.junit.Test

/** Observable gateway contracts: credential destination, cookies, handoff and stale writes. */
class SessionManagerGatewayTest {
    private val base = "https://host.example:8443/dev/proxy/15000/"
    private fun profile(auth: AuthType = AuthType.CODE_SERVER_PASSWORD) = Profile(id = 1, alias = "test", baseUrl = base, authType = auth)

    private class Dao(var row: Profile?) : ProfileDao {
        val stamps = mutableListOf<String>()
        override fun observeAll(): Flow<List<Profile>> = flowOf(listOfNotNull(row))
        override suspend fun getById(id: Long) = row?.takeIf { it.id == id }
        override suspend fun getAllOnce() = listOfNotNull(row)
        override suspend fun getByAlias(alias: String) = row?.takeIf { it.alias == alias }
        override suspend fun insert(profile: Profile): Long { row = profile; return profile.id }
        override suspend fun update(profile: Profile): Int { row = profile; return 1 }
        override suspend fun deleteById(id: Long) { row = null }
        override suspend fun setCookieHost(id: Long, host: String?): Int { row = row?.copy(cookieHost = host); return 1 }
        override suspend fun setCookieHostIfUrlMatches(id: Long, expectedUrl: String, host: String): Int {
            if (row?.id != id || row?.baseUrl != expectedUrl) return 0
            stamps += host; row = row?.copy(cookieHost = host); return 1
        }
        override suspend fun touchLastUsed(id: Long, timestamp: Long) = 1
        override suspend fun setAuthType(id: Long, authType: AuthType): Int { row = row?.copy(authType = authType); return 1 }
    }
    private class Cookies : CookieSink {
        val jar = mutableListOf<Cookie>()
        var flushes = 0
        override fun inject(origin: String, cookies: List<Cookie>) { jar.addAll(cookies) }
        override fun purgeHost(host: String) { jar.removeAll { it.domain == host } }
        override fun cookieHeader(origin: String): String? {
            val matching = jar.filter { it.matches(origin.toHttpUrl()) && it.expiresAt > System.currentTimeMillis() }
            return matching.takeIf { it.isNotEmpty() }?.joinToString("; ") { "${it.name}=${it.value}" }
        }
        override fun flush() { flushes++ }
    }
    private class Harness(val profile: Profile, secret: String? = "test-secret", val route: (Request) -> Response) {
        val dao = Dao(profile)
        val cookies = Cookies()
        val requests = mutableListOf<Request>()
        val sleeps = mutableListOf<Long>()
        val http = OkHttpClient.Builder().addInterceptor { chain -> requests += chain.request(); route(chain.request()) }.build()
        val manager = SessionManager(dao, object : SecretLookup { override fun secretFor(alias: String) = secret }, cookies, http, sleeper = { sleeps += it })
    }
    private fun reply(req: Request, code: Int = 200, body: String = "", location: String? = null, cookie: String? = null): Response =
        Response.Builder().request(req).protocol(Protocol.HTTP_1_1).code(code).message("fixture")
            .apply { location?.let { header("Location", it) }; cookie?.let { header("Set-Cookie", it) } }
            .body(body.toResponseBody((if (body.trimStart().startsWith("{")) "application/json" else "text/html").toMediaType())).build()

    @Test fun prefixed_gateway_replays_hidden_fields_and_retains_cookie_path() = runTest {
        val h = Harness(profile()) { req -> when {
            req.url.encodedPath.endsWith("api/v4/meta") -> reply(req, 404)
            req.method == "POST" -> reply(req, 303, location = "./proxy/15000/", cookie = "code-server-session=ok; Path=/dev/; HttpOnly")
            req.url.encodedPath.endsWith("/login") -> reply(req, body = """<form class="login-form" method="post"><input type="hidden" name="base" value="./.."><input type="hidden" name="csrf" value="one&amp;two"><input type="hidden" name="href" value=""><input type="password" name="password"></form>""", cookie = "csrf=one; Path=/dev/")
            else -> reply(req, 302, location = "../../login?to=%2Fdev%2Fproxy%2F15000%2F")
        } }
        assertTrue(h.manager.login(h.profile) is LoginResult.Success)
        val post = h.requests.single { it.method == "POST" }
        assertEquals("https://host.example:8443/dev/login?to=%2Fdev%2Fproxy%2F15000%2F", post.url.toString())
        val fields = post.body as FormBody
        val values = (0 until fields.size).associate { fields.name(it) to fields.value(it) }
        assertEquals("test-secret", values["password"])
        assertEquals("./..", values["base"])
        assertEquals("one&two", values["csrf"])
        assertEquals(post.url.toString(), values["href"])
        assertEquals("csrf=one", post.header("Cookie"))
        assertTrue(h.requests.last().header("Cookie")!!.contains("code-server-session=ok"))
        assertEquals(listOf("host.example"), h.dao.stamps)
    }

    @Test fun discovery_session_is_verified_and_stamped_without_password_replay() = runTest {
        val h = Harness(profile()) { req -> when {
            req.url.encodedPath.endsWith("api/v4/conversations") -> reply(req, body = tofuSessionProbeJson())
            req.url.encodedPath.endsWith("api/v4/meta") -> reply(req, 404)
            else -> reply(req, body = "<html>Tofu application</html>", cookie = "gateway=ok; Path=/dev/")
        } }
        assertTrue(h.manager.login(h.profile) is LoginResult.Success)
        assertEquals(listOf("host.example"), h.dao.stamps)
        assertFalse(h.requests.any { it.method == "POST" })
    }

    @Test fun foreign_redirect_is_not_requested_and_never_receives_credentials() = runTest {
        val h = Harness(profile()) { req -> reply(req, 302, location = "https://idp.example/login") }
        assertTrue(h.manager.login(h.profile) is LoginResult.NeedsInteractiveSso)
        assertEquals(1, h.requests.size)
        assertFalse(h.requests.any { it.method == "POST" || it.url.host == "idp.example" })
        assertTrue(h.dao.stamps.isEmpty())
    }

    @Test fun foreign_form_and_same_host_other_port_cannot_receive_a_password() = runTest {
        for (action in listOf("https://idp.example/login", "https://host.example:9443/login", "http://host.example:8443/login")) {
            val h = Harness(profile()) { req -> reply(req, body = codeServerForm(action)) }
            assertTrue(h.manager.login(h.profile) is LoginResult.NeedsInteractiveSso)
            assertFalse(h.requests.any { it.method == "POST" })
        }
    }

    @Test fun arbitrary_password_page_is_interactive_without_a_secret_post() = runTest {
        val h = Harness(profile()) { req -> reply(req, body = "<form method='post'><input type='password' name='password'></form>") }
        assertTrue(h.manager.login(h.profile) is LoginResult.NeedsInteractiveSso)
        assertFalse(h.requests.any { it.method == "POST" })
    }

    @Test fun redirect_loop_is_bounded_and_not_retried() = runTest {
        val h = Harness(profile()) { req -> reply(req, 302, location = req.url.toString()) }
        assertTrue(h.manager.login(h.profile) is LoginResult.NeedsInteractiveSso)
        assertEquals(9, h.requests.size)
        assertTrue(h.sleeps.isEmpty())
    }

    @Test fun rate_limit_never_retries_or_posts() = runTest {
        val h = Harness(profile()) { req -> reply(req, 429) }
        assertTrue(h.manager.login(h.profile) is LoginResult.Error)
        assertEquals(1, h.requests.size)
        assertTrue(h.sleeps.isEmpty())
    }

    @Test fun csrf_cookie_and_unauthenticated_health_never_stamp_a_session() = runTest {
        val h = Harness(profile(AuthType.INTERACTIVE_SSO)) { req -> reply(req, 401, "{\"error\":\"Unauthorized\"}") }
        h.cookies.jar += Cookie.parse(base.toHttpUrl(), "csrf=one; Path=/dev/")!!
        assertFalse(h.manager.noteInteractiveSignIn(h.profile, base))
        assertTrue(h.dao.stamps.isEmpty())
        assertEquals(0, h.cookies.flushes)
    }

    @Test fun public_health_with_csrf_cookie_is_not_a_verified_session() = runTest {
        val h = Harness(profile(AuthType.INTERACTIVE_SSO)) { req ->
            if (req.url.encodedPath.endsWith("api/health")) reply(req, body = "{\"bootId\":\"public\"}")
            else reply(req, 401, "{\"error\":\"Unauthorized\"}")
        }
        h.cookies.jar += Cookie.parse(base.toHttpUrl(), "csrf=state; Path=/dev/")!!
        assertFalse(h.manager.noteInteractiveSignIn(h.profile, base))
        assertEquals("/dev/proxy/15000/api/v4/conversations", h.requests.single().url.encodedPath)
        assertEquals("1", h.requests.single().url.queryParameter("limit"))
        assertTrue(h.dao.stamps.isEmpty())
        assertEquals(0, h.cookies.flushes)
    }

    @Test fun tofu_own_auth_refusal_proves_outer_gateway_was_passed() = runTest {
        val h = Harness(profile(AuthType.INTERACTIVE_SSO)) { req -> reply(req, 401, """{"type":"urn:tofu:problem:unauthorized","status":401,"instance":"/api/v4/conversations","requestId":"test"}""") }
        h.cookies.jar += Cookie.parse(base.toHttpUrl(), "gateway=ok; Path=/dev/")!!
        assertTrue(h.manager.noteInteractiveSignIn(h.profile, base))
        assertEquals("host.example", h.dao.row!!.cookieHost)
    }

    @Test fun verified_protected_api_promotes_session_and_is_reused_without_password_post() = runTest {
        val h = Harness(profile(AuthType.INTERACTIVE_SSO)) { req ->
            if (req.url.encodedPath.endsWith("api/v4/conversations")) reply(req, body = tofuSessionProbeJson()) else reply(req, 404)
        }
        h.cookies.jar += Cookie.parse(base.toHttpUrl(), "gateway=ok; Path=/dev/")!!
        assertTrue(h.manager.noteInteractiveSignIn(h.profile, base))
        assertEquals(1, h.cookies.flushes)
        assertTrue(h.manager.login(h.profile) is LoginResult.Success)
        assertFalse(h.requests.any { it.method == "POST" })
        assertEquals(1, h.dao.stamps.size)
    }

    @Test fun editing_url_or_deleting_profile_during_verification_blocks_late_stamp() = runTest {
        for (deleted in listOf(false, true)) {
            lateinit var h: Harness
            h = Harness(profile(AuthType.INTERACTIVE_SSO)) { req ->
                h.dao.row = if (deleted) null else h.profile.copy(baseUrl = "https://new.example/")
                reply(req, body = tofuSessionProbeJson())
            }
            h.cookies.jar += Cookie.parse(base.toHttpUrl(), "gateway=ok; Path=/dev/")!!
            assertFalse(h.manager.noteInteractiveSignIn(h.profile, base))
            assertTrue(h.dao.stamps.isEmpty())
        }
    }

    @Test fun no_saved_password_opens_interactive_without_network() = runTest {
        val h = Harness(profile(), secret = null) { error("No headless request expected") }
        val result = h.manager.login(h.profile)
        assertTrue(result is LoginResult.NeedsInteractiveSso)
        assertEquals(AuthType.INTERACTIVE_SSO, InteractiveSso.profileForWebView(h.profile, result).authType)
        assertEquals(AuthType.CODE_SERVER_PASSWORD, h.profile.authType)
    }
    @Test fun outer_sso_cookie_allows_saved_password_continuation_without_changing_profile_mode() = runTest {
        val h = Harness(profile()) { req -> when {
            req.url.encodedPath.endsWith("api/health") -> reply(req, 401, "{\"error\":\"Unauthorized\"}")
            req.url.encodedPath.endsWith("api/v4/meta") -> reply(req, 404)
            req.method == "POST" -> reply(req, 302, cookie = "code-server-session=ok; Path=/dev/")
            else -> reply(req, body = codeServerForm("/dev/login"))
        } }
        h.cookies.jar += Cookie.parse(base.toHttpUrl(), "sso=one; Path=/dev/")!!
        val outcome = h.manager.resumePasswordSignIn(h.profile.copy(authType = AuthType.INTERACTIVE_SSO), "https://host.example:8443/dev/login")
        assertTrue(outcome is LoginResult.Success)
        assertEquals("sso=one", h.requests.single { it.method == "POST" }.header("Cookie"))
        assertEquals(AuthType.CODE_SERVER_PASSWORD, h.dao.row!!.authType)
        h.dao.row = h.profile.copy(baseUrl = "https://edited.example/")
        val count = h.requests.size
        assertNull(h.manager.resumePasswordSignIn(h.profile, "https://host.example:8443/dev/login"))
        assertEquals(count, h.requests.size)
    }

    @Test fun supervisor_can_verify_the_gateway_while_tofu_is_stopped() = runTest {
        val p = profile(AuthType.INTERACTIVE_SSO).copy(projectPath = "/project")
        val h = Harness(p) { req ->
            if (req.url.encodedPath == "/dev/proxy/15001/status") reply(req, body = "{\"ok\":true,\"supervisorProtocolVersion\":1,\"supervisorSourceFingerprint\":\"test\",\"running\":false}")
            else reply(req, 503)
        }
        h.cookies.jar += Cookie.parse(base.toHttpUrl(), "gateway=ok; Path=/dev/")!!
        assertTrue(h.manager.noteInteractiveSignIn(p, base))
        assertEquals("gateway=ok", h.requests.last().header("Cookie"))
        assertEquals("host.example", h.dao.row!!.cookieHost)
    }

    @Test fun oversized_or_html_probe_cannot_promote_a_session() = runTest {
        for (body in listOf("<script>const response = {\"bootId\":\"fake\"}</script>", "x".repeat(262_145))) {
            val h = Harness(profile(AuthType.INTERACTIVE_SSO)) { req -> reply(req, body = body) }
            h.cookies.jar += Cookie.parse(base.toHttpUrl(), "csrf=state; Path=/dev/")!!
            assertFalse(h.manager.noteInteractiveSignIn(h.profile, base))
            assertTrue(h.dao.stamps.isEmpty())
        }
    }

    @Test fun cancelled_verification_never_flushes_or_writes_a_late_session() = runTest {
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val finished = CountDownLatch(1)
        val h = Harness(profile(AuthType.INTERACTIVE_SSO)) { req ->
            entered.countDown()
            try { check(release.await(5, TimeUnit.SECONDS)); reply(req, body = tofuSessionProbeJson()) }
            finally { finished.countDown() }
        }
        h.cookies.jar += Cookie.parse(base.toHttpUrl(), "gateway=ok; Path=/dev/")!!
        val job = launch(Dispatchers.Default) { h.manager.noteInteractiveSignIn(h.profile, base) }
        try {
            assertTrue(entered.await(5, TimeUnit.SECONDS))
            job.cancelAndJoin()
            release.countDown()
            assertTrue(finished.await(5, TimeUnit.SECONDS))
            assertTrue(h.dao.stamps.isEmpty())
            assertEquals(0, h.cookies.flushes)
        } finally { release.countDown(); job.cancelAndJoin() }
    }

}

/** The maintained code-server form contract; values are test credentials only. */
internal fun codeServerForm(action: String = "/login"): String =
    """<form class="login-form" method="post" action="$action"><input type="hidden" name="base" value="."><input type="password" name="password"></form>"""

/** Current v4 metadata envelope: no legacy success response can verify a session. */
internal fun tofuSessionProbeJson(): String =
    """{"data":{"ok":true,"items":[],"page":{"hasMore":false}},"meta":{"requestId":"test","serverTimeMs":1}}"""
