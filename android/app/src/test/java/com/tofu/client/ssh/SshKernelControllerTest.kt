package com.tofu.client.ssh

import com.tofu.client.data.AuthType
import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import com.tofu.client.data.SshEndpoint
import com.tofu.client.session.KernelState
import com.tofu.client.session.KernelUnavailableException
import com.tofu.client.session.SecretLookup
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.runBlocking
import org.apache.sshd.common.config.keys.KeyUtils
import org.apache.sshd.common.keyprovider.KeyPairProvider
import org.apache.sshd.common.session.Session
import org.apache.sshd.common.session.SessionListener
import org.apache.sshd.common.util.security.SecurityUtils
import org.apache.sshd.server.SshServer
import org.apache.sshd.server.auth.password.PasswordAuthenticator
import org.apache.sshd.server.auth.pubkey.PublickeyAuthenticator
import org.apache.sshd.server.forward.AcceptAllForwardingFilter
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.IOException
import java.net.ConnectException
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.KeyPair
import java.security.PublicKey
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

/**
 * The SSH carrier end to end on the JVM: a real MINA SSHD server in process,
 * a fake Tofu (an HTTP responder on its loopback) behind it, and the production
 * [SshKernelController] over [SshTunnels] with only the Keystore, the vault,
 * the DAO and the network callback faked.
 */
class SshKernelControllerTest {

    @get:Rule val tmp = TemporaryFolder()

    private val sealer = SoftwareSealer()
    private lateinit var identity: SshIdentity
    private lateinit var hostKey: KeyPair
    private lateinit var sshd: SshServer
    private lateinit var tofu: FakeTofu
    private lateinit var tunnels: SshTunnels
    private val network = FakeNetwork()
    private val vault = FakeVault()

    /** What the server accepts; null refuses that method. */
    @Volatile private var acceptKey: PublicKey? = null
    @Volatile private var acceptPassword: String? = null
    private val passwordAttempts = AtomicInteger()
    private val sessionsCreated = AtomicInteger()
    private var sessionCreated = CountDownLatch(0)

    @Before fun setUp() {
        identity = SshIdentity(tmp.root.resolve("phone/identity"), sealer)
        hostKey = SshIdentity(tmp.root.resolve("host/key"), sealer).keyPair()
        tofu = FakeTofu()
        sshd = SshServer.setUpDefaultServer().apply {
            host = "127.0.0.1"
            port = 0
            keyPairProvider = KeyPairProvider.wrap(hostKey)
            forwardingFilter = AcceptAllForwardingFilter.INSTANCE
            publickeyAuthenticator = PublickeyAuthenticator { _, key, _ -> acceptKey?.let { KeyUtils.compareKeys(it, key) } == true }
            passwordAuthenticator = PasswordAuthenticator { _, password, _ ->
                passwordAttempts.incrementAndGet()
                password == acceptPassword
            }
            addSessionListener(object : SessionListener {
                override fun sessionCreated(session: Session) {
                    sessionsCreated.incrementAndGet()
                    sessionCreated.countDown()
                }
            })
            start()
        }
        tunnels = SshTunnels(identity, network)
    }

    @After fun tearDown() {
        runBlocking { dao.rows.values.forEach { controller.stop(it) } }
        sshd.stop(true)
        tofu.close()
    }

    private val dao = FakeDao()
    private val controller by lazy { SshKernelController(tunnels, dao, vault) }

    private fun kernel(pin: String? = fingerprintOfHost(), remotePort: Int = tofu.port): Profile {
        val profile = Profile(
            id = 7, alias = "box", baseUrl = "", kind = KernelKind.SSH,
            ssh = SshEndpoint(host = "127.0.0.1", port = sshd.port, user = "me", hostKeySha256 = pin, remotePort = remotePort),
        )
        dao.rows[profile.id] = profile
        return profile
    }

    private fun fingerprintOfHost() = sshFingerprint(hostKey.public)

    @Test fun eddsa_comes_from_net_i2p_and_bouncycastle_stays_out() {
        assertTrue(SecurityUtils.isNetI2pCryptoEdDSARegistered())
        assertFalse(SecurityUtils.isBouncyCastleRegistered())
    }

    @Test fun key_sign_in_brings_up_a_forward_on_127_0_0_2_that_carries_http() = runBlocking<Unit> {
        acceptKey = identity.keyPair().public
        val box = kernel()
        assertEquals(KernelState.Stopped, controller.state(box).value)

        val endpoint = controller.ensureRunning(box)

        val port = endpoint.baseUrl.removePrefix("http://127.0.0.2:").toInt()
        assertEquals("http://127.0.0.2:$port", endpoint.baseUrl)
        assertEquals("127.0.0.2", endpoint.cookieHost)
        assertTrue(httpGet(port).endsWith("hello from tofu"))
        assertEquals(KernelState.Ready, controller.state(box).value)
        assertSame(controller.state(box), controller.state(box))
        // Only 127.0.0.2 listens: the local kernel's host never reaches this forward.
        assertThrows(ConnectException::class.java) { Socket("127.0.0.1", port).close() }
        assertEquals("no password was needed or sent", 0, passwordAttempts.get())
        assertEquals("an idempotent second call reuses the tunnel", endpoint, controller.ensureRunning(box))
        assertEquals(1, sessionsCreated.get())
    }

    @Test fun password_sign_in_uses_the_vault_secret_under_the_kernel_alias() = runBlocking<Unit> {
        acceptPassword = "hunter2"
        val box = kernel()
        val refused = assertThrows(KernelUnavailableException::class.java) { runBlocking { controller.ensureRunning(box) } }
        assertTrue(refused.reason, refused.reason.contains("refused this phone's SSH sign-in"))
        assertEquals(KernelState.Failed(refused.reason), controller.state(box).value)

        vault.secrets["box"] = "hunter2"
        val endpoint = controller.ensureRunning(box)

        assertTrue(httpGet(endpoint.port()).endsWith("hello from tofu"))
        assertEquals(KernelState.Ready, controller.state(box).value)
    }

    @Test fun an_unpinned_host_is_refused_before_sign_in_until_its_presented_key_is_trusted() = runBlocking<Unit> {
        acceptPassword = "hunter2"
        vault.secrets["box"] = "hunter2"
        val box = kernel(pin = null)

        val refused = assertThrows(KernelUnavailableException::class.java) { runBlocking { controller.ensureRunning(box) } }

        assertTrue(refused.reason, refused.reason.contains(fingerprintOfHost()))
        assertEquals(KernelState.Failed(refused.reason), controller.state(box).value)
        assertEquals("no credential goes to an unverified host", 0, passwordAttempts.get())
        assertEquals(fingerprintOfHost(), controller.hostKeyToTrust(box))
        assertFalse("only the presented key can be trusted", controller.trustHostKey(box, "SHA256:somethingElse"))
        assertTrue(dao.pins.isEmpty())

        assertTrue(controller.trustHostKey(box, fingerprintOfHost()))
        assertEquals(listOf(Pin(7, "127.0.0.1", sshd.port, fingerprintOfHost())), dao.pins)
        val pinned = dao.rows.getValue(7)
        assertEquals(fingerprintOfHost(), pinned.ssh?.hostKeySha256)

        val endpoint = controller.ensureRunning(pinned)
        assertTrue(httpGet(endpoint.port()).endsWith("hello from tofu"))
        assertNull(controller.hostKeyToTrust(pinned))
    }

    @Test fun a_changed_host_key_is_a_hard_stop_and_never_repinned() = runBlocking<Unit> {
        acceptKey = identity.keyPair().public
        acceptPassword = "hunter2"
        vault.secrets["box"] = "hunter2"
        val otherHost = sshFingerprint(SshIdentity(tmp.root.resolve("other/key"), sealer).keyPair().public)
        val box = kernel(pin = otherHost)

        val refused = assertThrows(KernelUnavailableException::class.java) { runBlocking { controller.ensureRunning(box) } }

        assertTrue(refused.reason, refused.reason.contains("changed"))
        assertTrue(refused.reason.contains(otherHost) && refused.reason.contains(fingerprintOfHost()))
        assertEquals(KernelState.Failed(refused.reason), controller.state(box).value)
        assertNull("a mismatch offers nothing to trust", controller.hostKeyToTrust(box))
        assertFalse(controller.trustHostKey(box, fingerprintOfHost()))
        assertTrue(dao.pins.isEmpty())
        assertEquals(0, passwordAttempts.get())
    }

    @Test fun a_network_change_reconnects_and_the_forward_keeps_its_port() = runBlocking<Unit> {
        acceptKey = identity.keyPair().public
        val box = kernel()
        val endpoint = controller.ensureRunning(box)
        assertEquals(1, sessionsCreated.get())

        sessionCreated = CountDownLatch(1)
        network.change()

        assertTrue("the network callback starts a new session", sessionCreated.await(30, TimeUnit.SECONDS))
        assertEquals(endpoint, controller.ensureRunning(box))
        assertTrue(httpGet(endpoint.port()).endsWith("hello from tofu"))
        assertEquals(2, sessionsCreated.get())
    }

    @Test(timeout = 60_000)
    fun a_session_the_server_drops_is_replaced_on_the_same_port_by_the_next_connection() = runBlocking<Unit> {
        acceptKey = identity.keyPair().public
        val box = kernel()
        val port = controller.ensureRunning(box).port()

        sshd.activeSessions.forEach { it.close(true) }

        // The first request may still meet the dying session and fail, as an
        // in-flight call would; the SPA retries. A later one reconnects.
        var body = ""
        while (!body.endsWith("hello from tofu")) body = runCatching { httpGet(port) }.getOrDefault("")
        assertEquals(2, sessionsCreated.get())
        assertEquals(port, controller.ensureRunning(box).port())
    }

    @Test fun a_host_with_nothing_on_the_remote_port_fails_with_that_reason() = runBlocking<Unit> {
        acceptKey = identity.keyPair().public
        val closedPort = ServerSocket(0, 0, InetAddress.getByName("127.0.0.1")).use { it.localPort }
        val box = kernel(remotePort = closedPort)

        val refused = assertThrows(KernelUnavailableException::class.java) { runBlocking { controller.ensureRunning(box) } }

        assertTrue(refused.reason, refused.reason.contains("could not open 127.0.0.1:$closedPort"))
        assertEquals(KernelState.Failed(refused.reason), controller.state(box).value)
    }

    @Test fun stop_closes_the_forward_and_publishes_stopped() = runBlocking<Unit> {
        acceptKey = identity.keyPair().public
        val box = kernel()
        val port = controller.ensureRunning(box).port()

        controller.stop(box)

        assertEquals(KernelState.Stopped, controller.state(box).value)
        assertThrows(ConnectException::class.java) { Socket("127.0.0.2", port).close() }
    }

    private fun com.tofu.client.session.KernelEndpoint.port() = baseUrl.substringAfterLast(':').toInt()

    /** One HTTP/1.0 exchange through the forward, the way a WebView connection uses it. */
    private fun httpGet(port: Int): String = Socket("127.0.0.2", port).use { socket ->
        socket.getOutputStream().apply {
            write("GET / HTTP/1.0\r\nHost: 127.0.0.2:$port\r\n\r\n".toByteArray())
            flush()
        }
        socket.getInputStream().readBytes().decodeToString()
    }

    /** A stand-in Tofu on the host's loopback: every request gets one fixed HTTP/1.0 answer. */
    private class FakeTofu : AutoCloseable {
        private val server = ServerSocket(0, 0, InetAddress.getByName("127.0.0.1"))
        val port: Int = server.localPort

        init {
            thread(isDaemon = true, name = "fake-tofu") {
                while (true) {
                    val socket = try { server.accept() } catch (_: IOException) { return@thread }
                    thread(isDaemon = true) { answer(socket) }
                }
            }
        }

        private fun answer(socket: Socket) = try {
            socket.use {
                val head = it.getInputStream().bufferedReader()
                while (head.readLine()?.isNotEmpty() == true) continue
                it.getOutputStream().write("HTTP/1.0 200 OK\r\nContent-Length: 15\r\n\r\nhello from tofu".toByteArray())
            }
        } catch (_: IOException) {
            // The tunnel's probe connects and leaves without a request.
        }

        override fun close() = server.close()
    }

    private class FakeNetwork : NetworkChanges {
        private var onChange: (() -> Unit)? = null
        override fun watch(onChange: () -> Unit) { this.onChange = onChange }
        fun change() = checkNotNull(onChange) { "no tunnel is watching the network" }()
    }

    private class FakeVault : SecretLookup {
        val secrets = mutableMapOf<String, String>()
        override fun secretFor(alias: String) = secrets[alias]
    }

    private data class Pin(val id: Long, val host: String, val port: Int, val sha256: String)

    /** Rows in memory; [pinSshHostKey] keeps the DAO's conditional first-trust contract. */
    private class FakeDao : ProfileDao {
        val rows = mutableMapOf<Long, Profile>()
        val pins = mutableListOf<Pin>()
        override fun observeAll(): Flow<List<Profile>> = flowOf(rows.values.toList())
        override suspend fun getById(id: Long) = rows[id]
        override suspend fun getByAlias(alias: String) = rows.values.firstOrNull { it.alias == alias }
        override suspend fun getAllOnce() = rows.values.toList()
        override suspend fun insert(profile: Profile): Long = throw AssertionError("not used")
        override suspend fun update(profile: Profile): Int = throw AssertionError("not used")
        override suspend fun setCookieHost(id: Long, host: String?): Int = throw AssertionError("not used")
        override suspend fun setCookieHostIfCurrent(id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType, host: String): Int =
            throw AssertionError("not used")
        override suspend fun touchLastUsed(id: Long, timestamp: Long): Int = throw AssertionError("not used")
        override suspend fun setAuthType(id: Long, authType: AuthType): Int = throw AssertionError("not used")
        override suspend fun deleteById(id: Long) = throw AssertionError("not used")
        override suspend fun pinSshHostKey(id: Long, host: String, port: Int, sha256: String): Int {
            val row = rows[id] ?: return 0
            val ssh = row.ssh ?: return 0
            if (row.kind != KernelKind.SSH || ssh.host != host || ssh.port != port || ssh.hostKeySha256 != null) return 0
            pins += Pin(id, host, port, sha256)
            rows[id] = row.copy(ssh = ssh.copy(hostKeySha256 = sha256))
            return 1
        }
    }
}
