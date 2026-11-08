package com.tofu.client.ssh

import com.tofu.client.data.SshEndpoint
import com.tofu.client.session.KernelState
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.util.concurrent.ConcurrentHashMap

/**
 * Where SSH kernels' forwards listen on the phone: a loopback address of their
 * own, so the local kernel's session cookie (127.0.0.1) is never sent to a
 * remote host. Cookies are scoped by host, not port (docs/ANDROID_KERNELS.md,
 * "Security rules" §4).
 */
const val SSH_FORWARD_HOST = "127.0.0.2"

/** Fires when the device's default network changes, which leaves every SSH session's socket dead. */
fun interface NetworkChanges {
    fun watch(onChange: () -> Unit)
}

/**
 * Every SSH kernel's tunnel, keyed by profile id, for the life of the process:
 * an Activity recreated while a tunnel runs finds it here, port and all.
 *
 * Owns this phone's [identity]. When the default network changes
 * ([networkChanges]) every tunnel replaces its session; that and a connection
 * arriving with no session up are the only reconnect triggers. No timer.
 */
class SshTunnels(private val identity: SshIdentity, private val networkChanges: NetworkChanges) {

    private data class Kernel(val id: Long, val endpoint: SshEndpoint)

    private val tunnels = HashMap<Long, SshTunnel>() // guarded by this
    private var watching = false // guarded by this
    private val states = ConcurrentHashMap<Kernel, MutableStateFlow<KernelState>>()
    private val untrusted = ConcurrentHashMap<Long, Pair<SshEndpoint, String>>()

    fun publicKeyLine(): String = identity.publicKeyLine()

    /** Keyed by the endpoint too, so an edited kernel starts from a fresh state. */
    fun state(id: Long, endpoint: SshEndpoint): StateFlow<KernelState> = flow(Kernel(id, endpoint))

    /** The key [id]'s host presented when its last connect was refused for lacking a pin. */
    fun hostKeyToTrust(id: Long, endpoint: SshEndpoint): String? =
        untrusted[id]?.takeIf { it.first == endpoint }?.second

    /**
     * Bring [id]'s tunnel to [endpoint] up, reusing it when it already leads
     * there, and return the forward's port. Blocking; throws [SshFailure] after
     * publishing `Failed` with the same reason.
     */
    fun open(id: Long, endpoint: SshEndpoint, password: String?): Int {
        val kernel = Kernel(id, endpoint)
        val tunnel = synchronized(this) {
            tunnels[id]?.takeIf { it.endpoint == endpoint } ?: run {
                tunnels.remove(id)?.close()
                SshTunnel(endpoint, identity::keyPair, SSH_FORWARD_HOST) { flow(kernel).value = it }
                    .also { tunnels[id] = it }
            }
        }
        tunnel.password = password
        untrusted.remove(id)
        val port = try {
            tunnel.open()
        } catch (failure: SshFailure) {
            failure.untrustedHostKey?.let { untrusted[id] = endpoint to it }
            // A tunnel that never came up has no port worth keeping.
            if (tunnel.port == null) discard(id, tunnel)
            throw failure
        }
        watchNetwork()
        return port
    }

    /** Close [id]'s tunnel, if any, and publish Stopped. */
    fun close(id: Long, endpoint: SshEndpoint) {
        synchronized(this) { tunnels.remove(id) }?.close()
        untrusted.remove(id)
        flow(Kernel(id, endpoint)).value = KernelState.Stopped
    }

    private fun discard(id: Long, tunnel: SshTunnel) {
        synchronized(this) { if (tunnels[id] === tunnel) tunnels.remove(id) }
        tunnel.close()
    }

    private fun watchNetwork() {
        synchronized(this) {
            if (watching) return
            watching = true
        }
        networkChanges.watch { synchronized(this) { tunnels.values.toList() }.forEach(SshTunnel::reconnect) }
    }

    private fun flow(kernel: Kernel) = states.getOrPut(kernel) { MutableStateFlow(KernelState.Stopped) }
}
