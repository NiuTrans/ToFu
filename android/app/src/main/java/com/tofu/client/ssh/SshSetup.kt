package com.tofu.client.ssh

import com.tofu.client.data.Profile
import com.tofu.client.session.KernelController

/** What the SSH form needs from the SSH carrier beyond [KernelController]. */
interface SshSetup {
    /** This phone's public key as an `authorized_keys` line; generates the key on first use. */
    suspend fun publicKeyLine(): String

    /** The host key [profile]'s unpinned host presented on its last, refused, connect. */
    fun hostKeyToTrust(profile: Profile): String?

    /** Pin [fingerprint] for [profile] if it is the presented key; true when pinned. */
    suspend fun trustHostKey(profile: Profile, fingerprint: String): Boolean
}
