package com.tofu.client.data

import android.content.Context

/**
 * The one piece of app-level UI state that is not a secret or a profile: whether
 * the user has completed first-run onboarding.
 *
 * Plain (unencrypted) `SharedPreferences` on purpose — this is a UI flag, not
 * durable user data, and it must be readable before the encrypted vault opens.
 * The key lives here so there is exactly one spelling of it.
 */
class OnboardingStore(context: Context) {

    private val prefs =
        context.getSharedPreferences("tofu_prefs", Context.MODE_PRIVATE)

    /** True once the user has connected a server or chosen to skip. */
    fun isDone(): Boolean = prefs.getBoolean(KEY_DONE, false)

    fun markDone() {
        prefs.edit().putBoolean(KEY_DONE, true).apply()
    }

    private companion object {
        const val KEY_DONE = "onboarding_done"
    }
}
