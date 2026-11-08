package com.tofu.client.session

import android.app.Activity
import android.app.Application
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.tofu.client.ui.Licences
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * The device side of "This phone" (Gradle tier, Robolectric): the "Keep
 * running" setting, the one-time notification-permission request on Android
 * 13+, the licence texts packaged from android/native/licenses, and the app's
 * on-screen signal the lifecycle policy reads.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], application = Application::class)
class ThisPhoneAndroidTest {
    private val context: Context = ApplicationProvider.getApplicationContext()

    @Test
    fun keep_running_persists() {
        assertFalse(LocalKernelSettings(context).keepRunning.value)
        LocalKernelSettings(context).setKeepRunning(true)
        assertTrue(LocalKernelSettings(context).keepRunning.value)
    }

    @Test
    fun notifications_are_requested_once_on_android_13_and_later_while_not_granted() {
        val settings = LocalKernelSettings(context)
        assertFalse("a runtime permission only from API 33", settings.shouldRequestNotifications(32, granted = false))
        assertFalse("already granted", settings.shouldRequestNotifications(33, granted = true))
        assertTrue(settings.shouldRequestNotifications(33, granted = false))
        assertFalse("only the first start asks", settings.shouldRequestNotifications(33, granted = false))
        assertFalse("remembered across restarts", LocalKernelSettings(context).shouldRequestNotifications(34, granted = false))
    }

    @Test
    fun the_licence_texts_are_packaged_from_android_native_licenses() {
        val texts = Licences.TEXTS.associate { (asset, _) -> asset to context.assets.open(asset).bufferedReader().use { it.readText() } }
        assertTrue(texts.getValue("proot-COPYING").contains("STMicroelectronics"))
        assertTrue(texts.getValue("talloc-LICENSE").contains("GNU LESSER GENERAL PUBLIC LICENSE"))
        assertTrue(texts.getValue("GPL-3.0.txt").contains("GNU GENERAL PUBLIC LICENSE"))
    }

    @Test
    fun the_app_is_on_screen_while_one_of_its_activities_is_started() {
        val app = context as Application
        val visibility = AppVisibility()
        app.registerActivityLifecycleCallbacks(visibility)
        try {
            val activity = Robolectric.buildActivity(Activity::class.java).create()
            assertFalse(visibility.visible.value)
            activity.start()
            assertTrue(visibility.visible.value)
            activity.stop()
            assertFalse(visibility.visible.value)
        } finally {
            app.unregisterActivityLifecycleCallbacks(visibility)
        }
    }
}
