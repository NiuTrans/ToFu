package com.tofu.client.session

import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import kotlinx.coroutines.test.runTest
import okhttp3.Cookie
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList

/** Update health must never borrow This phone's or an SSH forward's cookies. */
class UpdateKernelIsolationTest {
    private val advertised = AdvertisedClient("0.1.20", "https://updates.test/client.apk")
    private val json = """{"mobile_client_version":"0.1.20","mobile_client_url":"https://updates.test/client.apk"}"""
    private val reservedHosts = listOf(
        "127.0.0.1", "127.1", "2130706433", "0x7f.1", "0177.0.0.1", "127.0.0.1.",
        "127.0.0.2", "127.2", "2130706434", "0x7f.2", "0177.0.0.2", "127.0.0.2.",
    )

    @Test fun only_remote_url_rows_are_probed_even_when_legacy_kernel_rows_have_an_address() = runTest {
        val urls = CopyOnWriteArrayList<String>()
        val checker = UpdateChecker("0.1.18") { urls += it; advertised }
        val remote = Profile(1, "remote", baseUrl = "https://remote.test/")
        val local = Profile(2, "phone", baseUrl = "https://legacy-local.test/", kind = KernelKind.LOCAL)
        val ssh = Profile(3, "ssh", baseUrl = "https://legacy-ssh.test/", kind = KernelKind.SSH)
        assertTrue(checker.check(listOf(local, remote, ssh))!!.available)
        assertEquals(listOf(remote.baseUrl), urls)
        urls.clear()
        assertNull(checker.check(listOf(local, ssh)))
        assertTrue("a kernel-only list cannot trigger an update request", urls.isEmpty())
    }

    @Test fun unsafe_remote_rows_never_reach_the_injected_probe_and_cannot_prove_up_to_date() = runTest {
        val urls = CopyOnWriteArrayList<String>()
        val checker = UpdateChecker("0.1.20") { urls += it; advertised }
        val good = Profile(1, "remote", baseUrl = "https://remote.test/")
        val unsafe = reservedHosts.mapIndexed { index, host ->
            Profile(index.toLong() + 2, "legacy-$index", baseUrl = "http://$host:43567/")
        } + Profile(99, "credential", baseUrl = "https://remote.test/?token=fixture-token")
        assertNull(checker.check(unsafe))
        assertTrue(urls.isEmpty())
        assertNull("partial knowledge cannot certify the remote set", checker.check(listOf(good) + unsafe))
        assertEquals(listOf(good.baseUrl), urls)
        val newer = UpdateChecker("0.1.18") { advertised }
        assertTrue("a known newer release is still useful during a partial failure", newer.check(listOf(good) + unsafe)!!.available)
    }

    @Test fun reserved_cookie_hosts_are_refused_before_cookie_lookup_or_http_in_both_probe_paths() = runTest {
        var cookieReads = 0
        var requests = 0
        val cookies = object : CookieSink {
            override fun inject(origin: String, cookies: List<Cookie>) {}
            override fun purgeHost(host: String) {}
            override fun cookieHeader(origin: String): String? { cookieReads++; return "tofu_session=fixture-install-secret" }
        }
        val client = OkHttpClient.Builder().addInterceptor { chain ->
            requests++
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(200).message("OK")
                .body(json.toResponseBody("application/json".toMediaType())).build()
        }.build()
        val probe = UpdateProbe(http = client, cookies = cookies)
        try {
            for (host in reservedHosts) {
                val url = "http://$host:43567/dev/absproxy/15005/?theme=dark"
                assertNull("sync: $host", probe.advertised(url))
                assertNull("cancellable: $host", probe.advertisedCancellable(url))
            }
            assertEquals("reserved hosts must not read a cookie at any port", 0, cookieReads)
            assertEquals("reserved hosts must not allocate an HTTP call", 0, requests)
            assertEquals(advertised, probe.advertised("http://localhost:43567/"))
            assertEquals("localhost remains an ordinary remote URL", 1, requests)
        } finally {
            probe.close()
            client.connectionPool.evictAll()
            client.dispatcher.executorService.shutdown()
        }
    }
}
