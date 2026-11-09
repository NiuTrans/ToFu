package com.tofu.client.session

import kotlinx.coroutines.test.runTest
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
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.atomic.AtomicInteger

/** Health probes share native body/privacy bounds while keeping the intentional LOCAL cookie path. */
class HealthProbeTest {
    @Test fun closing_a_probe_does_not_shut_down_an_injected_client() {
        val client = OkHttpClient()
        val probe = HealthProbe(client, cookieProvider = { null })
        probe.close()
        probe.close()
        assertFalse(client.dispatcher.executorService.isShutdown)
        assertEquals(TofuProbe.Verdict.UNREACHABLE, probe.probe("https://host.test/").verdict)
    }

    @Test fun empty_responses_retain_gateway_waking_and_wrong_server_guidance() = runTest {
        for (status in listOf(302, 401, 403, 502, 503, 504, 500, 200)) {
            val client = OkHttpClient.Builder().addInterceptor { chain ->
                Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
                    .code(status).message("fixture").body("".toResponseBody()).build()
            }.build()
            val probe = HealthProbe(client, cookieProvider = { null })
            val expected = TofuProbe.classify(status, "")
            assertEquals("synchronous HTTP $status", expected, probe.probe("https://host.test/").verdict)
            assertEquals("cancellable HTTP $status", expected, probe.probeCancellable("https://host.test/").verdict)
        }
    }

    @Test fun oversized_health_body_is_not_a_successful_tofu_probe() = runTest {
        val body = " ".repeat(262_145) + """{"bootId":"fixture"}"""
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
                .code(200).message("OK").body(body.toResponseBody("application/json".toMediaType())).build()
        }.build()
        val probe = HealthProbe(client, cookieProvider = { null })
        assertEquals(TofuProbe.Verdict.UNREACHABLE, probe.probe("https://host.test/").verdict)
        assertEquals(TofuProbe.Verdict.UNREACHABLE, probe.probeCancellable("https://host.test/").verdict)
    }

    @Test fun unknown_length_health_bodies_are_bounded_and_closed() {
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
        assertEquals(TofuProbe.Verdict.UNREACHABLE, HealthProbe(client, cookieProvider = { null }).probe("https://host.test/").verdict)
        assertTrue("decoded stream must stop at the native body bound", bytes <= 262_145 + 8192)
        assertTrue("oversized response must close", closed)
    }

    @Test fun response_body_is_not_returned_as_a_user_visible_detail() {
        val body = """{"bootId":"fixture","access_token":"fixture-private-value"}"""
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
                .code(200).message("OK").body(body.toResponseBody("application/json".toMediaType())).build()
        }.build()
        val result = HealthProbe(client, cookieProvider = { null }).probe("https://host.test/")
        assertEquals(TofuProbe.Verdict.TOFU, result.verdict)
        assertFalse(result.detail.contains("fixture-private-value"))
    }

    @Test fun injected_local_cookie_reaches_exact_endpoint_on_wire_without_automatic_jar() {
        var lookup: String? = null
        var received: String? = null
        val jarReads = AtomicInteger()
        val server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        server.soTimeout = 5000
        val endpoint = "http://127.0.0.1:${server.localPort}/api/health"
        val peer = Thread {
            try {
                server.accept().use { socket ->
                    socket.soTimeout = 5000
                    val input = socket.getInputStream().bufferedReader()
                    val request = input.readLine()
                    while (true) {
                        val line = input.readLine() ?: break
                        if (line.isEmpty()) break
                        if (line.startsWith("Cookie:", ignoreCase = true)) received = line.substringAfter(':').trim()
                    }
                    // The fixture actually refuses absent/incorrect authentication.
                    val authenticated = request == "GET /api/health HTTP/1.1" && received == "tofu_session=fixture-local"
                    val body = if (authenticated) """{"bootId":"fixture"}""" else ""
                    val status = if (authenticated) "200 OK" else "401 Unauthorized"
                    val reply = "HTTP/1.1 $status\r\nContent-Type: application/json\r\nContent-Length: ${body.toByteArray().size}\r\nConnection: close\r\n\r\n$body"
                    socket.getOutputStream().write(reply.toByteArray(Charsets.UTF_8))
                    socket.getOutputStream().flush()
                }
            } catch (_: java.io.IOException) { }
        }.apply { name = "health-local-cookie-fixture"; start() }
        val client = OkHttpClient.Builder().cookieJar(object : CookieJar {
            override fun loadForRequest(url: okhttp3.HttpUrl): List<Cookie> {
                jarReads.incrementAndGet()
                return listOf(Cookie.Builder().name("wrong").value("fixture").hostOnlyDomain(url.host).build())
            }
            override fun saveFromResponse(url: okhttp3.HttpUrl, cookies: List<Cookie>) {}
        }).build()
        val probe = HealthProbe(client, cookieProvider = { lookup = it; "tofu_session=fixture-local" })
        try {
            assertEquals(TofuProbe.Verdict.TOFU, probe.probe(endpoint.removeSuffix("api/health")).verdict)
            peer.join(5000)
            assertFalse("fixture producer must settle", peer.isAlive)
            assertEquals(endpoint, lookup)
            assertEquals("tofu_session=fixture-local", received)
            assertEquals("the supplied automatic jar must never replace the selected LOCAL cookie", 0, jarReads.get())
        } finally {
            probe.close()
            server.close()
            peer.join(5000)
            client.connectionPool.evictAll()
            client.dispatcher.executorService.shutdown()
        }
    }
}
