package com.tofu.client.runtime

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.io.IOException
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * AES-256-GCM under a non-exportable AndroidKeyStore key. Blob layout:
 * `[iv length][iv][ciphertext||tag]`; the Keystore chooses the IV.
 */
class AndroidKeyStoreSecretWrapper(private val alias: String = KEY_ALIAS) : SecretKeyWrapper {
    override fun wrap(plaintext: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val iv = cipher.iv
        val sealed = cipher.doFinal(plaintext)
        return byteArrayOf(iv.size.toByte()) + iv + sealed
    }

    override fun unwrap(blob: ByteArray): ByteArray {
        val ivLength = blob.firstOrNull()?.toInt() ?: throw IOException("empty secret blob")
        if (ivLength <= 0 || blob.size <= 1 + ivLength) throw IOException("truncated secret blob")
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(TAG_BITS, blob, 1, ivLength))
        return cipher.doFinal(blob, 1 + ivLength, blob.size - 1 - ivLength)
    }

    private fun key(): SecretKey {
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (keyStore.getKey(alias, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return generator.generateKey()
    }

    companion object {
        const val KEY_ALIAS = "tofu_local_kernel_secret"
        private const val KEYSTORE = "AndroidKeyStore"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val TAG_BITS = 128
    }
}
