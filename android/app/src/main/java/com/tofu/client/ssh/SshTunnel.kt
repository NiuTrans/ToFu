package com.tofu.client.ssh

import com.tofu.client.data.SshEndpoint
import com.tofu.client.session.KernelState
import org.apache.sshd.client.SshClient
import org.apache.sshd.client.channel.ChannelDirectTcpip
import org.apache.sshd.client.config.hosts.HostConfigEntryResolver
import org.apache.sshd.client.keyverifier.ServerKeyVerifier
import org.apache.sshd.client.session.ClientSession
import org.apache.sshd.common.config.keys.KeyUtils
import org.apache.sshd.common.digest.BuiltinDigests
import org.apache.sshd.common.keyprovider.KeyIdentityProvider
import org.apache.sshd.common.util.net.SshdSocketAddress
import java.io.Closeable
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.KeyPair
import java.security.PublicKey
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException

/** OpenSSH's host-key fingerprint, `SHA256:` + unpadded base64, as `ssh-keygen -lf` prints it. */
fun sshFingerprint(key: PublicKey): String = KeyUtils.getFingerPrint(BuiltinDigests.sha256, key)

/**
 * Why a tunnel is not up; [reason] is user-facing. [untrustedHostKey] is the
 * fingerprint an unpinned host presented: the one key the user may trust.
 */
class SshFailure(val reason: String, val untrustedHostKey: String? = null) : IOException(reason)

/**
 * One SSH kernel's tunnel: SSH sessions to [endpoint] beneath a listener on
 * [forwardHost], whose every accepted connection rides a direct-tcpip channel
 * to the remote's 127.0.0.1:[SshEndpoint.remotePort].
 *
 * The listener is bound once, by the first successful [open], and stays bound
 * until [close]: the forward's port, and the WebView origin built on it,
 * survive reconnects. A connection that arrives while no session is up waits
 * for the next one, which it starts itself if nothing else has.
 *
 * The host key is checked against [SshEndpoint.hostKeySha256] during key
 * exchange, before any credential is sent: no pin, or another key, aborts the
 * connection. The pin is never changed here.
 *
 * Publishes Starting, Ready and Failed through [publish] as connects begin and
 * end; [close] publishes nothing (its caller says why the tunnel ended).
 */
internal class SshTunnel(
    val endpoint: SshEndpoint,
    private val identity: () -> KeyPair,
    private val forwardHost: String,
    private val publish: (KernelState) -> Unit,
) : Closeable {

    /** The account password from the vault, or null for key-only sign-in. Read at every connect. */
    @Volatile var password: String? = null

    private val lock = Any()
    @Volatile private var session: ClientSession? = null
    @Volatile private var listener: ServerSocket? = null
    @Volatile private var closed = false

    /** Set by the host-key check of the connect in progress when it refuses the key. */
    @Volatile private var hostKeyRefusal: SshFailure? = null

    private val workers = Executors.newCachedThreadPool { task -> Thread(task, "ssh-forward").apply { isDaemon = true } }
    private val remote = SshdSocketAddress(REMOTE_HOST, endpoint.remotePort)
    private val client = SshClient.setUpDefaultClient().apply {
        serverKeyVerifier = ServerKeyVerifier { _, _, key -> accepts(key) }
        // A phone has no ~/.ssh: no config file, default identities or known_hosts.
        hostConfigEntryResolver = HostConfigEntryResolver.EMPTY
        keyIdentityProvider = KeyIdentityProvider.EMPTY_KEYS_PROVIDER
        start()
    }

    /** The forward's port once the tunnel has been up; null before. */
    val port: Int? get() = listener?.localPort

    /** Connect if no session is up, then return the forward's port. Blocking; throws [SshFailure]. */
    fun open(): Int = synchronized(lock) {
        liveSession()
        (listener?.takeUnless { it.isClosed } ?: bind()).localPort
    }

    /** The device's network changed, so the session's socket is dead: replace the session now. */
    fun reconnect() {
        if (listener == null || closed) return
        try {
            workers.execute {
                synchronized(lock) {
                    session?.close(true)
                    session = null
                    try { liveSession() } catch (_: SshFailure) { /* published; the next connection retries */ }
                }
            }
        } catch (_: RejectedExecutionException) {
            // Closed concurrently; nothing to reconnect.
        }
    }

    override fun close() {
        closed = true
        listener?.close()
        client.stop()
        workers.shutdownNow()
    }

    private fun accepts(key: PublicKey): Boolean {
        val presented = sshFingerprint(key)
        val pinned = endpoint.hostKeySha256
        hostKeyRefusal = when (pinned) {
            presented -> null
            null -> SshFailure(
                "First connection to ${place()}: confirm its host key $presented to connect.",
                untrustedHostKey = presented,
            )
            else -> SshFailure(
                "The host key of ${place()} changed: it presented $presented, but $pinned is pinned. " +
                    "Refusing to connect. If the host was reinstalled, delete this kernel and add it again.",
            )
        }
        return hostKeyRefusal == null
    }

    /** The authenticated session, connecting first when there is none. Caller holds [lock]. */
    private fun liveSession(): ClientSession {
        session?.let { if (it.isOpen && it.isAuthenticated) return it }
        session?.close(true)
        session = null
        if (closed) throw SshFailure(CLOSED)
        report(KernelState.Starting)
        val fresh = try {
            connect()
        } catch (failure: SshFailure) {
            report(KernelState.Failed(failure.reason))
            throw failure
        }
        session = fresh
        report(KernelState.Ready)
        return fresh
    }

    private fun connect(): ClientSession {
        val key = try {
            identity()
        } catch (error: Exception) {
            throw SshFailure("This phone's SSH key could not be read: ${describe(error)}")
        }
        hostKeyRefusal = null
        val fresh = try {
            client.connect(endpoint.user, endpoint.host, endpoint.port).verify().session
        } catch (error: IOException) {
            throw hostKeyRefusal ?: SshFailure("Cannot reach ${place()}: ${describe(error)}")
        }
        try {
            fresh.addPublicKeyIdentity(key)
            password?.takeIf { it.isNotEmpty() }?.let(fresh::addPasswordIdentity)
            fresh.auth().verify()
        } catch (error: IOException) {
            val stillOpen = fresh.isOpen
            fresh.close(true)
            throw hostKeyRefusal ?: if (stillOpen) {
                SshFailure(
                    "${endpoint.user}@${place()} refused this phone's SSH sign-in. Add this phone's public key " +
                        "to ~/.ssh/authorized_keys on the host, or save the account's password.",
                )
            } else {
                SshFailure("Lost the connection to ${place()}: ${describe(error)}")
            }
        }
        try {
            probe(fresh)
        } catch (failure: SshFailure) {
            fresh.close(true)
            throw failure
        }
        return fresh
    }

    /**
     * Open and drop one channel to the remote Tofu, so a host that refuses
     * forwarding, or has nothing listening there, fails here with a reason
     * instead of as a WebView connection reset.
     */
    private fun probe(session: ClientSession) {
        val channel = session.createDirectTcpipChannel(SshdSocketAddress(forwardHost, 0), remote)
        try {
            channel.open().verify()
        } catch (error: IOException) {
            throw SshFailure(
                "${place()} signed this phone in but could not open $REMOTE_HOST:${endpoint.remotePort} " +
                    "(${describe(error)}). Check that Tofu is running there and that sshd allows TCP forwarding.",
            )
        } finally {
            channel.close(true)
        }
    }

    private fun bind(): ServerSocket {
        val server = ServerSocket()
        try {
            server.bind(InetSocketAddress(InetAddress.getByName(forwardHost), 0))
        } catch (error: IOException) {
            server.close()
            throw SshFailure("Cannot listen on $forwardHost on this phone: ${describe(error)}").also {
                report(KernelState.Failed(it.reason))
            }
        }
        listener = server
        workers.execute { accept(server) }
        return server
    }

    private fun accept(server: ServerSocket) {
        while (true) {
            val socket = try {
                server.accept()
            } catch (error: IOException) {
                if (!closed) report(KernelState.Failed("The SSH forward stopped listening: ${describe(error)}"))
                return
            }
            try {
                workers.execute { forward(socket) }
            } catch (_: RejectedExecutionException) {
                socket.close()
                return
            }
        }
    }

    /** Carry one WebView connection over its own channel; either side closing ends both. */
    private fun forward(socket: Socket): Unit = socket.use {
        val channel = try {
            val live = synchronized(lock) { liveSession() }
            live.createDirectTcpipChannel(SshdSocketAddress(socket.inetAddress.hostAddress, socket.port), remote)
                .also { it.open().verify() }
        } catch (_: IOException) {
            // Closing the socket fails this one request; a failed connect is already published.
            return
        }
        try {
            workers.execute { upload(socket, channel) }
            pump(channel.invertedOut, socket.getOutputStream())
        } catch (_: IOException) {
            // A side went away mid-exchange; closing both ends finishes it.
        } catch (_: RejectedExecutionException) {
            // Closed concurrently.
        } finally {
            channel.close(false)
        }
    }

    private fun upload(socket: Socket, channel: ChannelDirectTcpip) {
        try {
            pump(socket.getInputStream(), channel.invertedIn)
            // EOF upstream: the remote may still be answering.
            channel.invertedIn.close()
        } catch (_: IOException) {
            // The download side closes the socket and the channel.
        }
    }

    private fun report(state: KernelState) {
        if (!closed) publish(state)
    }

    private fun place() = "${endpoint.host}:${endpoint.port}"

    private companion object {
        /** Where the remote Tofu listens, on the SSH host's own loopback. */
        const val REMOTE_HOST = "127.0.0.1"
        const val CLOSED = "The SSH tunnel was stopped."

        fun describe(error: Throwable): String = error.message ?: error.javaClass.simpleName

        /** Interactive traffic: forward each read as it arrives instead of filling a buffer first. */
        fun pump(input: InputStream, output: OutputStream) {
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) return
                output.write(buffer, 0, read)
                output.flush()
            }
        }
    }
}
