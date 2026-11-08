package com.tofu.client.ssh

import android.content.Context
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import com.tofu.client.session.KernelController
import com.tofu.client.session.KernelEndpoint
import com.tofu.client.session.KernelState
import com.tofu.client.session.KernelUnavailableException
import com.tofu.client.session.SecretLookup
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.withContext

/** What the SSH form needs from the SSH carrier beyond [KernelController]. */
interface SshSetup {
    /** This phone's public key as an `authorized_keys` line; generates the key on first use. */
    suspend fun publicKeyLine(): String

    /** The host key [profile]'s unpinned host presented on its last, refused, connect. */
    fun hostKeyToTrust(profile: Profile): String?

    /** Pin [fingerprint] for [profile] if it is the presented key; true when pinned. */
    suspend fun trustHostKey(profile: Profile, fingerprint: String): Boolean
}

/**
 * [KernelController] for [com.tofu.client.data.KernelKind.SSH] kernels: a Tofu
 * already running on a remote host, reached through a tunnel the phone dials
 * out ([SshTunnels]). The WebView loads `http://127.0.0.2:<port>/`; the remote
 * Tofu sees a loopback peer, and its own sign-in (open mode, or an API key in
 * private mode) stays with the session layer's login path.
 *
 * Sign-in to the host offers this phone's Ed25519 key ([SshIdentity]) and,
 * when the vault holds one under the kernel's alias, the account password — the
 * same alias-keyed slot a URL profile's code-server password uses.
 *
 * Host keys are pinned on first trust only: a first connect is refused and
 * remembers the key its host presented ([hostKeyToTrust]); [trustHostKey] pins
 * exactly that key. A different key later is a hard stop, never a re-pin.
 *
 * Entry points: [ensureRunning], [stop], [state] (the seam), plus [SshSetup]
 * for the SSH form. Built per Activity over the process's [SshTunnels] by
 * [forApp].
 */
class SshKernelController(
    private val tunnels: SshTunnels,
    private val dao: ProfileDao,
    private val secrets: SecretLookup,
) : KernelController, SshSetup {

    override suspend fun ensureRunning(profile: Profile): KernelEndpoint {
        val endpoint = checkNotNull(profile.ssh) { "SSH kernel ${profile.id} has no endpoint" }
        val port = withContext(Dispatchers.IO) {
            try {
                tunnels.open(profile.id, endpoint, secrets.secretFor(profile.alias))
            } catch (failure: SshFailure) {
                throw KernelUnavailableException(failure.reason)
            }
        }
        return KernelEndpoint("http://$SSH_FORWARD_HOST:$port", SSH_FORWARD_HOST)
    }

    override suspend fun stop(profile: Profile) {
        val endpoint = profile.ssh ?: return
        withContext(Dispatchers.IO) { tunnels.close(profile.id, endpoint) }
    }

    override fun state(profile: Profile): StateFlow<KernelState> =
        tunnels.state(profile.id, checkNotNull(profile.ssh) { "SSH kernel ${profile.id} has no endpoint" })

    override suspend fun publicKeyLine(): String = withContext(Dispatchers.IO) { tunnels.publicKeyLine() }

    override fun hostKeyToTrust(profile: Profile): String? = profile.ssh?.let { tunnels.hostKeyToTrust(profile.id, it) }

    /**
     * Only the key the host presented ([hostKeyToTrust]) can be pinned, and only
     * while the stored row is still that endpoint without a pin
     * ([ProfileDao.pinSshHostKey]).
     */
    override suspend fun trustHostKey(profile: Profile, fingerprint: String): Boolean {
        val endpoint = profile.ssh ?: return false
        if (hostKeyToTrust(profile) != fingerprint) return false
        return dao.pinSshHostKey(profile.id, endpoint.host, endpoint.port, fingerprint) == 1
    }

    companion object {
        /** This Activity's controller over the process's tunnels ([AndroidSsh]). */
        fun forApp(context: Context, dao: ProfileDao, secrets: SecretLookup): SshKernelController =
            SshKernelController(AndroidSsh.tunnels(context), dao, secrets)
    }
}
