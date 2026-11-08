package com.tofu.client.runtime

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** The per-install secret: generated once, stored only as Keystore-style AES-GCM ciphertext, never silently replaced. */
class KernelSecretTest {

    /** JVM stand-in for AndroidKeyStore: a real AES-256-GCM key that never leaves this object. */
    private class FakeKeystore : SecretKeyWrapper {
        private val key: SecretKey = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        var wraps = 0

        override fun wrap(plaintext: ByteArray): ByteArray {
            wraps++
            val iv = ByteArray(12).also(SecureRandom()::nextBytes)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key, GCMParameterSpec(128, iv)) }
            return iv + cipher.doFinal(plaintext)
        }

        override fun unwrap(blob: ByteArray): ByteArray {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, blob, 0, 12)) }
            return cipher.doFinal(blob, 12, blob.size - 12)
        }
    }

    @Test
    fun the_secret_is_256_random_bits_generated_once_per_install() {
        val directory = Files.createTempDirectory("secret").toFile()
        val keystore = FakeKeystore()
        val store = File(directory, KernelSecret.STORE_FILE)
        val fixed = object : SecureRandom() {
            override fun nextBytes(bytes: ByteArray) = bytes.indices.forEach { bytes[it] = it.toByte() }
        }

        val first = KernelSecret(store, keystore, fixed).load()
        assertEquals("tofu_admin_" + (0 until 32).joinToString("") { "%02x".format(it) }, first.reveal())
        assertTrue(KernelSecretValue.FORMAT.matches(first.reveal()))
        assertArrayEquals("${first.reveal()}\n".toByteArray(), first.stdinRecord())

        val again = KernelSecret(store, keystore, SecureRandom()).load()
        assertEquals("the stored secret is reused, not regenerated", first.reveal(), again.reveal())
        assertEquals(1, keystore.wraps)
        directory.deleteRecursively()
    }

    @Test
    fun only_ciphertext_is_stored() {
        val directory = Files.createTempDirectory("secret").toFile()
        val store = File(directory, KernelSecret.STORE_FILE)
        val secret = KernelSecret(store, FakeKeystore()).load()
        val bytes = store.readBytes()
        val text = String(bytes, Charsets.ISO_8859_1)
        assertFalse(text.contains(secret.reveal()))
        assertFalse(text.contains(KernelSecretValue.PREFIX))
        assertEquals("only the store file remains", listOf(KernelSecret.STORE_FILE), directory.list()!!.toList())
        directory.deleteRecursively()
    }

    @Test
    fun an_undecryptable_secret_is_reported_and_never_replaced() {
        val directory = Files.createTempDirectory("secret").toFile()
        val store = File(directory, KernelSecret.STORE_FILE)
        KernelSecret(store, FakeKeystore()).load()
        val before = store.readBytes()

        try {
            KernelSecret(store, FakeKeystore()).load() // a different key: the Keystore entry was lost
            fail("expected KernelSecretUnavailableException")
        } catch (expected: KernelSecretUnavailableException) {
            assertFalse(expected.message.orEmpty().contains(KernelSecretValue.PREFIX))
        }
        assertArrayEquals("the kernel already admitted the old secret; it must not be overwritten", before, store.readBytes())
        directory.deleteRecursively()
    }

    @Test
    fun a_secret_split_across_pipe_reads_never_survives_the_stderr_tail() {
        val tail = TextTail(capacity = 100, secret = testSecret())
        val stream = "x".repeat(150) + TEST_SECRET + " leaked by a tool\n" + "y".repeat(20)
        // 7-char reads split the secret, and the buffer trims (past 2 x capacity) while it is incomplete.
        stream.chunked(7).forEach { tail.append(it.toCharArray(), it.length) }
        val text = tail.toString()
        assertFalse(text.contains(TEST_SECRET.substring(KernelSecretValue.PREFIX.length, KernelSecretValue.PREFIX.length + 16)))
        assertTrue(text, text.contains("${KernelSecretValue.REDACTED} leaked by a tool"))
        assertTrue(text.length <= 100)
    }

    @Test
    fun the_secret_is_redacted_in_strings_and_diagnostics() {
        val secret = testSecret()
        assertEquals("KernelSecret([redacted])", "$secret")
        assertEquals("token=[redacted] end", secret.scrub("token=$TEST_SECRET end"))
    }
}
