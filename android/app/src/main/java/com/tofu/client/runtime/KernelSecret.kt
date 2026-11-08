package com.tofu.client.runtime

import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.SecureRandom

/** Encrypts and decrypts the install secret at rest. The production key never leaves AndroidKeyStore. */
interface SecretKeyWrapper {
    fun wrap(plaintext: ByteArray): ByteArray
    fun unwrap(blob: ByteArray): ByteArray
}

/** The stored secret exists but cannot be decrypted (Keystore key lost or blob damaged). */
class KernelSecretUnavailableException(message: String, cause: Throwable? = null) : IOException(message, cause)

/**
 * The local kernel's per-install admin secret (launch contract: `tofu_admin_`
 * + 64 lowercase hex = 256 bits from SecureRandom). It reaches the kernel only
 * through stdin; it is never placed in argv, the environment, Room, URLs,
 * saved state or logs. [toString] is redacted so string templates cannot leak it.
 */
class KernelSecretValue internal constructor(private val value: String) {
    /** The exact stdin record the launch contract expects: the secret and a newline. */
    fun stdinRecord(): ByteArray = "$value\n".toByteArray(Charsets.US_ASCII)

    /** For the adapter that installs the `tofu_session` cookie / `Authorization: Bearer` header. */
    fun reveal(): String = value

    /** Replace every occurrence of the secret in diagnostic text. */
    fun scrub(text: String): String = text.replace(value, REDACTED)

    /** In-place [scrub] of occurrences starting at or after [from]: incremental scrubbing of a growing buffer. */
    internal fun scrubFrom(text: StringBuilder, from: Int) {
        var index = text.indexOf(value, maxOf(0, from))
        while (index >= 0) {
            text.replace(index, index + value.length, REDACTED)
            index = text.indexOf(value, index + REDACTED.length)
        }
    }

    internal val length: Int get() = value.length

    override fun toString(): String = "KernelSecret($REDACTED)"

    companion object {
        const val PREFIX = "tofu_admin_"
        const val REDACTED = "[redacted]"
        internal val FORMAT = Regex("tofu_admin_[0-9a-f]{64}")
    }
}

/**
 * Generates the secret once per install and keeps only its AES-GCM ciphertext
 * in an app-private file ([store]). An existing ciphertext that no longer
 * decrypts is reported, never silently replaced: the kernel's data already
 * admitted the old secret, and a fresh one would fail its boot closed.
 */
class KernelSecret(
    private val store: File,
    private val wrapper: SecretKeyWrapper,
    private val random: SecureRandom = SecureRandom(),
) {
    @Synchronized
    fun load(): KernelSecretValue {
        if (store.isFile) {
            val plaintext = try {
                wrapper.unwrap(store.readBytes())
            } catch (error: Exception) {
                throw KernelSecretUnavailableException("the stored local-kernel secret cannot be decrypted (${error.javaClass.simpleName})", error)
            }
            val value = plaintext.toString(Charsets.US_ASCII)
            plaintext.fill(0)
            if (!KernelSecretValue.FORMAT.matches(value)) throw KernelSecretUnavailableException("the stored local-kernel secret is malformed")
            return KernelSecretValue(value)
        }
        val bytes = ByteArray(SECRET_BYTES).also(random::nextBytes)
        val value = KernelSecretValue.PREFIX + bytes.joinToString("") { "%02x".format(it) }
        bytes.fill(0)
        val plaintext = value.toByteArray(Charsets.US_ASCII)
        val blob = wrapper.wrap(plaintext)
        plaintext.fill(0)
        store.parentFile?.mkdirs()
        val temporary = File(store.parentFile, ".${store.name}.tmp")
        temporary.writeBytes(blob)
        Files.move(temporary.toPath(), store.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        return KernelSecretValue(value)
    }

    companion object {
        /** 256 bits (launch contract). */
        const val SECRET_BYTES = 32
        const val STORE_FILE = "local_kernel_secret.bin"
    }
}
