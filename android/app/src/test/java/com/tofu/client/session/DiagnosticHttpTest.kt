package com.tofu.client.session

import okhttp3.*
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.ServerSocket
import kotlin.concurrent.thread
import org.junit.Assert.*
import org.junit.Test

/** Real loopback HTTP proves stage/status observations without an Android emulator. */
class DiagnosticHttpTest {
    @Test fun network_stage_and_redirect_evidence_are_copyable_without_secrets() {
        val log = DiagLog()
        var priorConnects = 0
        var priorResponses = 0
        val original = OkHttpClient.Builder().followRedirects(false).eventListener(object : EventListener() {
            override fun connectStart(call: Call, inetSocketAddress: InetSocketAddress, proxy: Proxy) { priorConnects++ }
            override fun responseHeadersEnd(call: Call, response: Response) { priorResponses++ }
        }).build()
        val http = DiagnosticHttp.attach(original, log, "AuthHttp")
        val server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        server.soTimeout = 3000
        val worker = thread(isDaemon = true) {
            server.accept().use { socket ->
                socket.soTimeout = 3000
                val input = socket.getInputStream().bufferedReader()
                while (!input.readLine().isNullOrEmpty()) { /* Read synthetic request headers, never log them. */ }
                socket.getOutputStream().write(("HTTP/1.1 302 Found\r\nContent-Length: 0\r\nContent-Type: text/html\r\n" +
                    "Location: https://idp.example/login?code=one-use&state=nonce\r\n" +
                    "Set-Cookie: code-server-session=private-cookie; Path=/dev/; HttpOnly\r\nConnection: close\r\n\r\n").toByteArray())
            }
        }
        try {
            http.newCall(Request.Builder().url("http://127.0.0.1:${server.localPort}/dev/login?token=private-token")
                .header("Cookie", "gateway=private-input").build()).execute().use { assertEquals(302, it.code) }
            worker.join(3000)
            assertFalse(worker.isAlive)
            val evidence = log.snapshot().joinToString("\n") { it.message }
            assertTrue(evidence.contains("status=302"))
            assertTrue(evidence.contains("redirect=https://idp.example/login"))
            assertTrue(evidence.contains("cookies=true"))
            assertTrue(evidence.contains("code-server-session scope=http://127.0.0.1:"))
            for (secret in listOf("private-token", "private-cookie", "private-input", "one-use", "nonce")) assertFalse(evidence.contains(secret))
            assertEquals(1, priorConnects)
            assertEquals(1, priorResponses)
        } finally {
            server.close(); worker.join(3000)
            http.connectionPool.evictAll(); http.dispatcher.executorService.shutdown()
        }
    }

    @Test fun thrown_transport_failure_keeps_exception_type_and_masks_credential_text() {
        val log = DiagLog()
        val http = DiagnosticHttp.attach(OkHttpClient.Builder().addInterceptor { throw IOException("token=private-exception-value") }.build(), log, "AuthHttp")
        try {
            try { http.newCall(Request.Builder().url("https://host.example/").build()).execute(); fail("Expected transport failure") }
            catch (_: IOException) { /* Synthetic transport failure expected. */ }
            val text = log.snapshot().joinToString { it.message }
            assertTrue(text.contains("failed=IOException"))
            assertFalse(text.contains("private-exception-value"))
        } finally { http.connectionPool.evictAll(); http.dispatcher.executorService.shutdown() }
    }
}
