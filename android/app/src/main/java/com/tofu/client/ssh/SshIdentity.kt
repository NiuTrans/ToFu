package com.tofu.client.ssh

import net.i2p.crypto.eddsa.EdDSAPrivateKey
import net.i2p.crypto.eddsa.EdDSAPublicKey
import net.i2p.crypto.eddsa.spec.EdDSANamedCurveTable
import net.i2p.crypto.eddsa.spec.EdDSAPrivateKeySpec
import net.i2p.crypto.eddsa.spec.EdDSAPublicKeySpec
import org.apache.sshd.common.config.keys.PublicKeyEntry
import org.apache.sshd.common.util.security.SecurityUtils
import java.io.File
import java.security.KeyPair
import java.security.SecureRandom

/**
 * Seals a small secret at rest. Production is [KeystoreSealer], an AES-GCM key
 * that never leaves AndroidKeyStore; JVM tests use a software key.
 */
interface SecretSealer {
    fun seal(plain: ByteArray): ByteArray
    fun open(sealed: ByteArray): ByteArray
}

/**
 * This phone's SSH identity: one Ed25519 key, generated on the phone the first
 * time it is needed and offered to every SSH kernel. The user authorizes it by
 * adding [publicKeyLine] to a host's `~/.ssh/authorized_keys`.
 *
 * Only the 32-byte private seed is stored, sealed by [sealer], in [file]
 * (app-private, outside backups). It never reaches Room, the vault, logs or a
 * URL; the public key is derived from it.
 */
class SshIdentity(
    private val file: File,
    private val sealer: SecretSealer,
    private val random: SecureRandom = SecureRandom(),
) {
    private var keyPair: KeyPair? = null

    init {
        // Every SSH path starts from the identity, so MINA is set up here first.
        useNetI2pEdDsa()
    }

    @Synchronized
    fun keyPair(): KeyPair = keyPair ?: fromSeed(loadOrCreateSeed()).also { keyPair = it }

    /** The public key as one OpenSSH `authorized_keys` line. */
    fun publicKeyLine(): String = PublicKeyEntry.toString(keyPair().public) + " " + COMMENT

    private fun loadOrCreateSeed(): ByteArray {
        if (file.exists()) return sealer.open(file.readBytes())
        val seed = ByteArray(SEED_BYTES).also(random::nextBytes)
        file.parentFile?.mkdirs()
        // Stage then rename, so a crash mid-write never leaves a torn key file
        // that would silently replace the identity the user authorized.
        val staged = File(file.path + ".new")
        staged.writeBytes(sealer.seal(seed))
        check(staged.renameTo(file)) { "could not store this phone's SSH key" }
        return seed
    }

    private companion object {
        /** RFC 8032 §5.1.5: an Ed25519 private key is a 32-byte seed. */
        const val SEED_BYTES = 32
        const val COMMENT = "tofu-android"
        val ED25519 = EdDSANamedCurveTable.getByName(EdDSANamedCurveTable.ED_25519)

        fun fromSeed(seed: ByteArray): KeyPair {
            val private = EdDSAPrivateKeySpec(seed, ED25519)
            return KeyPair(EdDSAPublicKey(EdDSAPublicKeySpec(private.a, ED25519)), EdDSAPrivateKey(private))
        }
    }
}

/**
 * Ed25519, the phone's key type and most hosts' key type, comes from net.i2p
 * eddsa: Android has no platform Ed25519 at API 26. BouncyCastle is never in
 * the app, but Robolectric puts it on the JVM test classpath, where MINA would
 * otherwise register it on first use and tests would run a crypto path devices
 * never do. Idempotent; must precede MINA's first crypto call.
 */
internal fun useNetI2pEdDsa() = SecurityUtils.setAPrioriDisabledProvider(SecurityUtils.BOUNCY_CASTLE, true)
