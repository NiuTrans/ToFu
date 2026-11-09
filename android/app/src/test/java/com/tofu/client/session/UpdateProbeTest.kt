package com.tofu.client.session

import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody
import okhttp3.ResponseBody.Companion.toResponseBody
import okio.Buffer
import okio.buffer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test

/** Updates must measure Tofu behind the gateway, not a login page. */
class UpdateProbeTest {
    private val noCookies = object : CookieSink {
        override fun inject(origin: String, cookies: List<Cookie>) {}
        override fun purgeHost(host: String) {}
        override fun cookieHeader(origin: String): String? = null
    }
    private val advertised = """{"mobile_client_version":"0.1.20","mobile_client_url":"https://host.test/client.apk"}"""

    @Test fun cookies_are_selected_for_the_exact_health_url_not_origin_or_other_proxy() {
        val requested = mutableListOf<String>()
        val cookies = object : CookieSink {
            override fun inject(origin: String, cookies: List<Cookie>) {}
            override fun purgeHost(host: String) {}
            override fun cookieHeader(origin: String): String? {
                requested += origin
                return if (origin == "https://host.test:8443/dev/absproxy/15005/api/health") "gateway=fixture-session" else null
            }
        }
        var received: String? = null
        val client = OkHttpClient.Builder().cookieJar(object : CookieJar {
            override fun loadForRequest(url: okhttp3.HttpUrl) = listOf(Cookie.Builder().name("wrong").value("fixture").domain("host.test").build())
            override fun saveFromResponse(url: okhttp3.HttpUrl, cookies: List<Cookie>) {}
        }).addInterceptor { chain ->
            received = chain.request().header("Cookie")
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
                .code(200).message("OK").body(advertised.toResponseBody("application/json".toMediaType())).build()
        }.build()
        assertEquals("0.1.20", UpdateProbe(client, cookies = cookies).advertised(
            "https://host.test:8443/dev/absproxy/15005/?theme=dark#/chat?conversation=1")!!.version)
        assertEquals(listOf("https://host.test:8443/dev/absproxy/15005/api/health"), requested)
        assertEquals("gateway=fixture-session", received)
    }

    @Test fun unknown_length_bodies_are_bounded_and_closed() {
        var bytes = 0L
        var closed = false
        val source = object : okio.Source {
            override fun read(sink: Buffer, byteCount: Long): Long {
                val count = minOf(byteCount, 8192).toInt()
                sink.writeUtf8(" ".repeat(count)); bytes += count; return count.toLong()
            }
            override fun timeout() = okio.Timeout.NONE
            override fun close() { closed = true }
        }
        val body = object : ResponseBody() {
            private val buffered = source.buffer()
            override fun contentLength() = -1L
            override fun contentType() = "application/json".toMediaType()
            override fun source() = buffered
        }
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
                .code(200).message("OK").body(body).build()
        }.build()
        assertNull(UpdateProbe(client, cookies = noCookies).advertised("https://host.test/"))
        org.junit.Assert.assertTrue("decoded stream must stop at the body bound", bytes <= 262_145 + 8192)
        org.junit.Assert.assertTrue("oversized response must close", closed)
    }

    @Test fun update_clients_never_follow_redirects_including_scheme_changes() {
        val client = UpdateProbe.defaultClient()
        assertFalse(client.followRedirects)
        assertFalse(client.followSslRedirects)
    }

    @Test fun oversized_or_html_health_bodies_are_not_advertisements() {
        for (body in listOf(" ".repeat(262_145) + advertised, "<html>$advertised</html>")) {
            val client = OkHttpClient.Builder().addInterceptor { chain ->
                Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
                    .code(200).message("OK").body(body.toResponseBody("text/html".toMediaType())).build()
            }.build()
            assertNull(UpdateProbe(client, cookies = noCookies).advertised("https://host.test/proxy/15005/"))
        }
    }

    @Test fun redirect_reply_never_advertises_an_update() {
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
                .code(302).message("Found").header("Location", "https://foreign.test/login")
                .body(advertised.toResponseBody("application/json".toMediaType())).build()
        }.build()
        assertNull(UpdateProbe(client, cookies = noCookies).advertised("https://host.test/proxy/15005/"))
    }
}
