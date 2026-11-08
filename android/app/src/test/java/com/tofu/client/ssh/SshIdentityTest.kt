package com.tofu.client.ssh

import net.i2p.crypto.eddsa.EdDSAPrivateKey
import net.i2p.crypto.eddsa.EdDSAPublicKey
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.DataOutputStream
import java.io.ByteArrayOutputStream
import java.security.GeneralSecurityException
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.spec.GCMParameterSpec

/** [SecretSealer] with a software AES-GCM key: the JVM stand-in for [KeystoreSealer]. */
internal class SoftwareSealer : SecretSealer {
    private val key = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()

    override fun seal(plain: ByteArray): ByteArray {
        val iv = ByteArray(12).also(SecureRandom()::nextBytes)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key, GCMParameterSpec(128, iv)) }
        return iv + cipher.doFinal(plain)
    }

    override fun open(sealed: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            .apply { init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, sealed, 0, 12)) }
        return cipher.doFinal(sealed, 12, sealed.size - 12)
    }
}

class SshIdentityTest {

    @get:Rule val tmp = TemporaryFolder()

    @Test fun the_key_is_made_once_sealed_at_rest_and_the_same_for_every_reader() {
        val sealer = SoftwareSealer()
        val file = tmp.root.resolve("ssh/identity")
        val first = SshIdentity(file, sealer)

        val line = first.publicKeyLine()
        val seed = (first.keyPair().private as EdDSAPrivateKey).seed

        assertTrue(file.exists())
        assertFalse("the private seed is never stored in the clear", file.readBytes().contains(seed))
        assertArrayEquals(seed, sealer.open(file.readBytes()))
        assertEquals("a second reader derives the same key", line, SshIdentity(file, sealer).publicKeyLine())
    }

    @Test fun a_seal_that_does_not_open_fails_instead_of_replacing_the_identity() {
        val file = tmp.root.resolve("ssh/identity")
        SshIdentity(file, SoftwareSealer()).keyPair()
        val stored = file.readBytes()

        assertThrows(GeneralSecurityException::class.java) { SshIdentity(file, SoftwareSealer()).keyPair() }
        assertArrayEquals("the authorized key is kept", stored, file.readBytes())
    }

    @Test fun the_public_key_is_one_openssh_ed25519_authorized_keys_line() {
        val identity = SshIdentity(tmp.root.resolve("id"), SoftwareSealer())
        val public = identity.keyPair().public as EdDSAPublicKey

        val (type, blob, comment) = identity.publicKeyLine().split(" ")

        assertEquals("ssh-ed25519", type)
        assertEquals("tofu-android", comment)
        assertArrayEquals(wireBlob(public.abyte), Base64.getDecoder().decode(blob))
    }

    @Test fun fingerprints_match_ssh_keygen() {
        val public = SshIdentity(tmp.root.resolve("id"), SoftwareSealer()).keyPair().public as EdDSAPublicKey
        val digest = MessageDigest.getInstance("SHA-256").digest(wireBlob(public.abyte))

        assertEquals("SHA256:" + Base64.getEncoder().withoutPadding().encodeToString(digest), sshFingerprint(public))
    }

    /** RFC 8709 §4: string "ssh-ed25519", string key. */
    private fun wireBlob(key: ByteArray): ByteArray = ByteArrayOutputStream().also { bytes ->
        DataOutputStream(bytes).apply {
            val type = "ssh-ed25519".toByteArray()
            writeInt(type.size); write(type)
            writeInt(key.size); write(key)
        }
    }.toByteArray()

    private fun ByteArray.contains(part: ByteArray): Boolean =
        (0..size - part.size).any { start -> part.indices.all { this[start + it] == part[it] } }
}
