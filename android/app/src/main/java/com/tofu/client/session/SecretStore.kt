package com.tofu.client.session

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Encrypted-at-rest credential store, keyed by profile ALIAS (never by URL).
 *
 * Binding the secret to the alias — the stable logical identity — is what lets
 * a re-provisioned sandbox reuse the saved password after the user edits the
 * URL: the alias survives the URL change, so [secretFor] still resolves.
 *
 * Backed by Android Keystore via [MasterKey] (AES256-GCM). Values are the raw
 * layer-2 secret (the code-server password); nothing here is ever logged.
 */
class SecretStore(context: Context) : SecretVault {

    private val prefs: SharedPreferences = EncryptedSharedPreferences.create(
        context,
        FILE_NAME,
        MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    override fun secretFor(alias: String): String? = prefs.getString(key(alias), null)

    override fun putSecret(alias: String, secret: String) {
        prefs.edit().putString(key(alias), secret).apply()
    }

    override fun removeSecret(alias: String) {
        prefs.edit().remove(key(alias)).apply()
    }

    private fun key(alias: String) = "secret::$alias"

    private companion object {
        const val FILE_NAME = "tofu_secrets"
    }
}
