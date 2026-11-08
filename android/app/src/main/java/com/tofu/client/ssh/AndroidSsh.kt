package com.tofu.client.ssh

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import org.apache.sshd.common.util.io.PathUtils
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * The Android side of SSH kernels: the process's one [SshTunnels], with this
 * phone's identity sealed by AndroidKeyStore under `noBackupFilesDir/ssh/` and
 * the default-network callback. Process-scoped because tunnels must outlive the
 * Activity that opened them.
 */
object AndroidSsh {
    @Volatile private var tunnels: SshTunnels? = null

    fun tunnels(context: Context): SshTunnels =
        tunnels ?: synchronized(this) { tunnels ?: create(context.applicationContext).also { tunnels = it } }

    private fun create(app: Context): SshTunnels {
        // MINA resolves ~ for its ~/.ssh defaults; Android sets no user.home
        // (MINA docs/android.md, "Detecting the user's home directory").
        val home = app.noBackupFilesDir.toPath()
        PathUtils.setUserHomeFolderResolver { home }
        val identity = SshIdentity(File(app.noBackupFilesDir, "ssh/identity_ed25519.sealed"), KeystoreSealer(KEY_ALIAS))
        return SshTunnels(identity, DefaultNetworkChanges(app))
    }

    private const val KEY_ALIAS = "tofu_ssh_identity"
}

/**
 * [SecretSealer] over a non-exportable AES-256-GCM key that AndroidKeyStore
 * holds under [alias], created on first use. Sealed form: the IV the Keystore
 * chose, then the ciphertext and its tag.
 */
class KeystoreSealer(private val alias: String) : SecretSealer {

    override fun seal(plain: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        return cipher.iv + cipher.doFinal(plain)
    }

    override fun open(sealed: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(TAG_BITS, sealed, 0, IV_BYTES))
        return cipher.doFinal(sealed, IV_BYTES, sealed.size - IV_BYTES)
    }

    private fun key(): SecretKey {
        val store = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (store.getKey(alias, null) as SecretKey?)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(AES_KEY_BITS)
                .build(),
        )
        return generator.generateKey()
    }

    private companion object {
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val AES_KEY_BITS = 256

        /** The IV length AndroidKeyStore generates for GCM. */
        const val IV_BYTES = 12

        /** GCM's full tag, AndroidKeyStore's default. */
        const val TAG_BITS = 128
    }
}

/** [NetworkChanges] from the platform's default-network callback. */
class DefaultNetworkChanges(private val context: Context) : NetworkChanges {
    override fun watch(onChange: () -> Unit) {
        context.getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(
            object : ConnectivityManager.NetworkCallback() {
                private var current: Network? = null

                override fun onAvailable(network: Network) {
                    val previous = current
                    current = network
                    // The first call names the network the sessions were opened on.
                    if (previous != null && previous != network) onChange()
                }
            },
        )
    }
}
