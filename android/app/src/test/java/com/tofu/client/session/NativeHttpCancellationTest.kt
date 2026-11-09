package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runTest
import okhttp3.Call
import okhttp3.Cookie
import okhttp3.EventListener
import okhttp3.OkHttpClient
import org.junit.Assert.*
import org.junit.Test
import java.net.InetAddress
import java.net.ServerSocket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Exercise a real stalled response socket, not a mock that just ignores cancellation. */
class NativeHttpCancellationTest {
    private class Cookies : CookieSink {
        var writes = 0
        override fun inject(origin: String, cookies: List<Cookie>) { writes++ }
        override fun injectResponseCookies(url: String, headers: List<String>) { if (headers.isNotEmpty()) writes++ }
        override fun purgeHost(host: String) {}
        override fun cookieHeader(origin: String): String? = null
        override fun flush() { writes++ }
    }
    private class Dao(private val profile: Profile) : ProfileDaoFixture {
        var writes = 0
        override fun observeAll() = flowOf(listOf(profile))
        override suspend fun getById(id: Long) = profile
        override suspend fun getByAlias(alias: String) = profile
        override suspend fun getAllOnce() = listOf(profile)
        override suspend fun insert(profile: Profile): Long { writes++; return 1 }
        override suspend fun update(profile: Profile): Int { writes++; return 1 }
        override suspend fun deleteById(id: Long) { writes++ }
        override suspend fun setCookieHost(id: Long, host: String?): Int { writes++; return 1 }
        override suspend fun setCookieHostIfCurrent(id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType, host: String): Int {
            val row = getById(id) ?: return 0
            if (row.kind != KernelKind.REMOTE_URL || row.baseUrl != expectedUrl || row.alias != expectedAlias || row.authType != expectedAuthType) return 0
            writes++; return 1
        }
        override suspend fun pinSshHostKey(id: Long, host: String, port: Int, sha256: String): Int =
            throw AssertionError("a cancelled native HTTP operation must not pin an SSH key")
        override suspend fun touchLastUsed(id: Long, timestamp: Long): Int { writes++; return 1 }
        override suspend fun setAuthType(id: Long, authType: AuthType): Int { writes++; return 1 }
    }

    @Test fun cancelling_login_or_update_closes_the_socket_before_any_late_cookie_or_profile_write() = runTest {
        for (operation in listOf("login", "update", "health")) {
            val update = operation == "update"
            val responseStarted = CountDownLatch(1)
            val peerClosed = CountDownLatch(1)
            val cancellations = AtomicInteger()
            val server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
            server.soTimeout = 5000
            val peer = Thread {
                try {
                    server.accept().use { socket ->
                        socket.soTimeout = 5000
                        val input = socket.getInputStream().bufferedReader()
                        while (!input.readLine().isNullOrEmpty()) { }
                        val headers = "HTTP/1.1 200 OK\r\nContent-Type: ${if (update) "application/json" else "text/html"}\r\n" +
                            "Content-Length: 4096\r\nSet-Cookie: code-server-session=fixture; Path=/\r\nConnection: close\r\n\r\n"
                        socket.getOutputStream().write((headers + " ").toByteArray(Charsets.UTF_8))
                        socket.getOutputStream().flush()
                        // The client is blocked waiting for the rest of the body.
                        // Cancellation must close its side, not leave a live read.
                        if (input.read() == -1) peerClosed.countDown()
                    }
                } catch (_: java.io.IOException) { }
            }.apply { name = "native-http-cancellation-fixture"; start() }
            val client = OkHttpClient.Builder().eventListener(object : EventListener() {
                override fun responseBodyStart(call: Call) { responseStarted.countDown() }
                override fun canceled(call: Call) { cancellations.incrementAndGet() }
            }).build()
            val profile = Profile(1, "test", baseUrl = "http://localhost:${server.localPort}/dev/proxy/15005/", authType = AuthType.CODE_SERVER_PASSWORD)
            val dao = Dao(profile)
            val cookies = Cookies()
            val manager = SessionManager(dao, object : SecretLookup { override fun secretFor(alias: String) = "fixture-password" }, cookies, client)
            val job = launch(Dispatchers.Default) {
                when (operation) {
                    "update" -> UpdateProbe(client, cookies = cookies).advertisedCancellable(profile.baseUrl)
                    "health" -> HealthProbe(client, cookieProvider = cookies::cookieHeader).probeCancellable(profile.baseUrl)
                    else -> manager.login(profile)
                }
            }
            try {
                assertTrue("response must enter body read", responseStarted.await(5, TimeUnit.SECONDS))
                job.cancelAndJoin()
                assertTrue("cancel must reach the underlying call", cancellations.get() > 0)
                assertTrue("cancel must close the peer socket", peerClosed.await(5, TimeUnit.SECONDS))
                peer.join(5000)
                assertFalse("fixture producer must settle", peer.isAlive)
                assertEquals("no cookie injection/flush after cancellation", 0, cookies.writes)
                assertEquals("no late repository marker after cancellation", 0, dao.writes)
            } finally {
                job.cancelAndJoin()
                server.close()
                peer.join(5000)
                client.connectionPool.evictAll()
                client.dispatcher.executorService.shutdown()
            }
        }
    }
}
