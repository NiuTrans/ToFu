package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import okhttp3.Cookie
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * The local kernel's credential can reach no kernel but the local one: it is
 * written for 127.0.0.1 only, no URL kernel may use that host however it is
 * spelled, and the WebView sends nothing there but the front LOCAL kernel's
 * own origin. OkHttp's RFC 6265 matcher is the oracle for "which requests
 * carry this cookie".
 */
class LocalKernelCookieTest {

    private val secret = "tofu_admin_" + "cd".repeat(32)
    private val local = KernelList.THIS_PHONE.boundTo(KernelEndpoint("http://127.0.0.1:43567/", "127.0.0.1"))
    private val ssh = Profile(id = 7, alias = "box", baseUrl = "", kind = KernelKind.SSH)
        .boundTo(KernelEndpoint("http://127.0.0.2:41234/", "127.0.0.2"))
    private val url = Profile(id = 1, alias = "lab", baseUrl = "https://lab.example/proxy/15000/", authType = AuthType.CODE_SERVER_PASSWORD)

    private class Jar : CookieSink {
        val injected = mutableListOf<Pair<String, List<Cookie>>>()
        val purged = mutableListOf<Pair<String, String>>()
        override fun inject(origin: String, cookies: List<Cookie>) { injected += origin to cookies }
        override fun purgeOrigin(origin: String, baseUrl: String) { purged += origin to baseUrl }
        override fun purgeHost(host: String) {}
        override fun cookieHeader(origin: String): String? = null
    }

    @Test fun withdrawing_purges_exactly_the_local_kernels_origin() {
        val jar = Jar()
        LocalKernelCookie.withdraw(jar, KernelEndpoint("http://127.0.0.1:43567/", "127.0.0.1"))
        assertEquals(listOf("http://127.0.0.1:43567" to "http://127.0.0.1:43567/"), jar.purged)
    }

    @Test fun the_credential_is_a_host_only_http_only_cookie_for_127_0_0_1() {
        val cookie = LocalKernelCookie.build(secret)
        assertEquals("tofu_session", cookie.name)
        assertEquals(secret, cookie.value)
        assertEquals("127.0.0.1", cookie.domain)
        assertTrue(cookie.hostOnly && cookie.httpOnly && !cookie.secure)
        assertEquals("/", cookie.path)
        val header = CookieHeaders.toPersistentHeader(cookie)
        assertTrue(header, header.startsWith("tofu_session=$secret; Path=/; ") && header.contains("; HttpOnly"))
        assertFalse("host-only: no Domain attribute", header.contains("Domain="))
    }

    @Test fun it_is_sent_to_every_port_of_127_0_0_1_and_to_no_other_host() {
        val cookie = LocalKernelCookie.build(secret)
        for (target in listOf("http://127.0.0.1:43567/", "http://127.0.0.1:43567/api/v4/conversations", "http://127.0.0.1:15000/")) {
            assertTrue(target, cookie.matches(target.toHttpUrl()))
        }
        for (target in listOf(
            "http://127.0.0.2:41234/", // an SSH kernel's forward
            "http://127.0.0.2:43567/",
            "http://localhost:43567/",
            "http://[::1]:43567/",
            "https://lab.example/proxy/15000/",
            "http://10.0.2.2:15000/",
        )) {
            assertFalse(target, cookie.matches(target.toHttpUrl()))
        }
    }

    @Test fun install_writes_only_the_local_kernels_origin_and_refuses_any_other_endpoint() {
        val jar = Jar()
        LocalKernelCookie.install(jar, KernelEndpoint("http://127.0.0.1:43567/", "127.0.0.1"), secret)
        assertEquals("http://127.0.0.1:43567", jar.injected.single().first)

        for (endpoint in listOf(
            KernelEndpoint("http://127.0.0.2:41234/", "127.0.0.2"),
            KernelEndpoint("http://127.0.0.2:41234/", "127.0.0.1"),
            KernelEndpoint("http://127.0.0.1:43567/", "127.0.0.2"),
            KernelEndpoint("https://127.0.0.1:43567/", "127.0.0.1"),
            KernelEndpoint("https://lab.example/", "lab.example"),
            KernelEndpoint("not a url", "127.0.0.1"),
        )) {
            try {
                LocalKernelCookie.install(jar, endpoint, secret)
                fail("installed for $endpoint")
            } catch (refused: IllegalArgumentException) {
                assertFalse("the refusal never carries the secret", refused.message.orEmpty().contains(secret))
            }
        }
        assertEquals(1, jar.injected.size)
    }

    @Test fun the_reserved_host_is_127_0_0_1_in_every_spelling_the_webview_accepts() {
        for (host in listOf("127.0.0.1", "127.1", "127.0.1", "2130706433", "0x7f000001", "0x7f.1", "0X7F.0.0.1", "0177.0.0.1", "017700000001", "127.0.0.1.")) {
            assertTrue(host, LocalKernelCookie.isReservedHost(host))
        }
        for (host in listOf("127.0.0.2", "127.0.0.10", "localhost", "127.0.0.1.example.com", "::1", "10.0.0.1", "1.2.3.4.5", "127.0.0.256", "0x7f.0x100.1", "lab.example", "")) {
            assertFalse(host, LocalKernelCookie.isReservedHost(host))
        }
        assertTrue(LocalKernelCookie.isReservedUrl("http://127.1:8080/"))
        assertFalse(LocalKernelCookie.isReservedUrl("http://localhost:8080/"))
        assertFalse(LocalKernelCookie.isReservedUrl("not a url"))
    }

    @Test fun the_webview_reaches_127_0_0_1_only_as_the_front_local_kernels_own_origin() {
        assertTrue(LocalKernelCookie.webViewMayLoad(local, "http://127.0.0.1:43567/api/v4/meta"))
        assertFalse("another listener on the device", LocalKernelCookie.webViewMayLoad(local, "http://127.0.0.1:9999/"))
        assertFalse(LocalKernelCookie.webViewMayLoad(local, "https://127.0.0.1:43567/"))
        assertTrue(LocalKernelCookie.webViewMayLoad(local, "https://cdn.example/app.js"))
        assertTrue("not http(s): not the cookie's concern", LocalKernelCookie.webViewMayLoad(local, "data:text/plain,hi"))

        for (front in listOf(ssh, url)) {
            assertFalse("${front.kind} front", LocalKernelCookie.webViewMayLoad(front, "http://127.0.0.1:43567/"))
            assertTrue("${front.kind} front", LocalKernelCookie.webViewMayLoad(front, front.baseUrl))
        }
        assertTrue(LocalKernelCookie.webViewMayLoad(ssh, "http://127.0.0.2:41234/api/push"))
    }

    @Test fun a_url_kernel_cannot_be_saved_on_this_phones_host() {
        for (address in listOf("http://127.0.0.1:15000/", "http://127.1:15000/", "http://2130706433:15000/")) {
            val result = ProfileForm.validate("termux", address, AuthType.NONE, "", emptySet())
            assertEquals(address, LocalKernelCookie.RESERVED_HOST_MESSAGE, result.errors["baseUrl"])
        }
        assertTrue(ProfileForm.validate("termux", "http://localhost:15000/", AuthType.NONE, "", emptySet()).ok)
    }
}
