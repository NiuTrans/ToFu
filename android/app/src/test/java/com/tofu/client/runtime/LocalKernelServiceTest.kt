package com.tofu.client.runtime

import android.app.Application
import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.os.Looper
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.android.controller.ServiceController
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowPowerManager

/**
 * The foreground-service shell around the supervisor (Robolectric, Android 14):
 * ongoing notification with Open/Stop, both channels, the wakelock hook, stop
 * from the notification, and the "killed by the system" guidance.
 * Gradle tier only: the service observes state on Dispatchers.Main.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], application = Application::class)
class LocalKernelServiceTest {
    private val context: Context = ApplicationProvider.getApplicationContext()
    private lateinit var harness: Harness

    private fun install(script: (FakeChild, Int) -> Unit) {
        harness = Harness(script = script)
        LocalKernel.installForTest(harness.supervisor)
    }

    @Before
    fun setUp() = install { child, _ -> child.listen(43567) }

    @After
    fun tearDown() {
        harness.supervisor.stop()
        LocalKernel.installForTest(null)
        LocalKernel.isAppInForeground = { true }
        LocalKernel.availabilityOf = LocalKernelAvailability::of
    }

    private fun idle() = shadowOf(Looper.getMainLooper()).idle()

    /** What the app does: LocalKernel.start, then the system runs the service it asked for. */
    private fun startService(): ServiceController<LocalKernelService> {
        LocalKernel.availabilityOf = { LocalKernelAvailability.Available }
        LocalKernel.isAppInForeground = { true }
        assertEquals(LocalKernel.StartResult.Requested, LocalKernel.start(context))
        val intent = shadowOf(context as Application).nextStartedService
        assertEquals(LocalKernelService.ACTION_START, intent.action)
        return Robolectric.buildService(LocalKernelService::class.java, intent).create().startCommand(0, 1)
    }

    private fun notifications() = shadowOf(context.getSystemService(NotificationManager::class.java))

    private fun title(notification: Notification) = notification.extras.getCharSequence(Notification.EXTRA_TITLE).toString()

    @Test
    fun start_enters_the_foreground_and_shows_the_running_notification() {
        val controller = startService()
        val shadow = shadowOf(controller.get())
        assertFalse(shadow.isForegroundStopped)
        assertEquals(LocalKernelNotifications.ONGOING_ID, shadow.lastForegroundNotificationId)

        harness.await { it is LocalKernelState.Ready }
        idle()
        val ongoing = notifications().getNotification(LocalKernelNotifications.ONGOING_ID)
        assertEquals("Tofu is running on this phone", title(ongoing))
        assertTrue(ongoing.flags and Notification.FLAG_ONGOING_EVENT != 0)
        assertEquals(LocalKernelNotifications.KERNEL_CHANNEL, ongoing.channelId)
        assertEquals(listOf("Open", "Stop"), ongoing.actions.map { it.title.toString() })
        assertNotNull(ongoing.contentIntent)
        assertEquals(Intent.ACTION_MAIN, shadowOf(ongoing.contentIntent).savedIntent.action)
        val stop = shadowOf(ongoing.actions[1].actionIntent)
        assertTrue(stop.isServiceIntent)
        assertEquals(LocalKernelService.ACTION_STOP, stop.savedIntent.action)
    }

    @Test
    fun a_start_shows_at_once_and_the_service_never_starts_a_second_generation() {
        LocalKernel.availabilityOf = { LocalKernelAvailability.Available }
        LocalKernel.isAppInForeground = { true }
        val gate = java.util.concurrent.CountDownLatch(1)
        harness.runtime.gate = gate
        harness.runtime.failure = LocalRuntimeUnavailableException("missing payload")

        assertEquals(LocalKernel.StartResult.Requested, LocalKernel.start(context))
        // The adapter awaits the outcome of THIS start, so it must show before the service runs.
        assertEquals(LocalKernelState.Starting, LocalKernel.state(context).value)

        gate.countDown()
        harness.await { it is LocalKernelState.Failed }
        // The system runs the service only after that generation ended: no second one.
        val controller = Robolectric.buildService(LocalKernelService::class.java, shadowOf(context as Application).nextStartedService)
            .create().startCommand(0, 1)
        idle()
        assertEquals(1, harness.runtime.prepares.get())
        assertTrue(shadowOf(controller.get()).isForegroundStopped)
        assertNotNull(notifications().getNotification(LocalKernelNotifications.ALERT_ID))
    }

    @Test
    fun both_notification_channels_exist() {
        startService()
        val manager = context.getSystemService(NotificationManager::class.java)
        val kernel = manager.getNotificationChannel(LocalKernelNotifications.KERNEL_CHANNEL)
        assertEquals("Local kernel", kernel.name.toString())
        assertEquals(NotificationManager.IMPORTANCE_LOW, kernel.importance)
        val approval = manager.getNotificationChannel(LocalKernelNotifications.APPROVAL_CHANNEL)
        assertEquals("Approval needed", approval.name.toString())
        assertEquals(NotificationManager.IMPORTANCE_HIGH, approval.importance)
    }

    @Test
    fun the_partial_wakelock_follows_the_running_work_signal() {
        startService()
        harness.await { it is LocalKernelState.Ready }
        idle()
        assertTrue("no work, no wakelock", ShadowPowerManager.getLatestWakeLock()?.isHeld != true)

        LocalKernel.setHasRunningWork(true)
        idle()
        val wakeLock = ShadowPowerManager.getLatestWakeLock()!!
        assertTrue(wakeLock.isHeld)
        assertEquals(LocalKernelService.WAKE_LOCK_TAG, shadowOf(wakeLock).tag)
        assertFalse("released structurally, not by a timeout", shadowOf(wakeLock).isReferenceCounted)

        LocalKernel.setHasRunningWork(false)
        idle()
        assertFalse(wakeLock.isHeld)

        LocalKernel.setHasRunningWork(true)
        idle()
        assertTrue(wakeLock.isHeld)
        harness.supervisor.stop()
        harness.await { it == LocalKernelState.Stopped }
        idle()
        assertFalse("released when the kernel stops", wakeLock.isHeld)
    }

    @Test
    fun stop_from_the_notification_stops_the_kernel_and_the_service() {
        val controller = startService()
        harness.await { it is LocalKernelState.Ready }
        idle()

        controller.withIntent(Intent(context, LocalKernelService::class.java).setAction(LocalKernelService.ACTION_STOP)).startCommand(0, 2)
        harness.await { it == LocalKernelState.Stopped }
        idle()
        val shadow = shadowOf(controller.get())
        assertTrue(shadow.isForegroundStopped)
        assertTrue(shadow.isStoppedBySelf)
        assertEquals("SIGTERM went to the kernel pid", listOf(harness.launcher.children.single().pid), harness.signals.terminated)
    }

    @Test
    fun a_system_kill_posts_the_child_process_guidance() {
        install { child, _ ->
            child.listen(43567)
            Thread.sleep(50)
            child.exit(LaunchProtocol.EXIT_SIGKILL)
        }
        val controller = startService()
        harness.await { it is LocalKernelState.Failed }
        idle()
        assertTrue(shadowOf(controller.get()).isForegroundStopped)
        val alert = notifications().getNotification(LocalKernelNotifications.ALERT_ID)
        assertEquals("Android stopped Tofu's processes", title(alert))
        val text = alert.extras.getCharSequence(Notification.EXTRA_BIG_TEXT).toString()
        assertTrue(text, text.contains("Disable child process restrictions"))
    }

    @Test
    fun guidance_text_matches_each_android_release() {
        val developerOption = context.getString(LocalKernelNotifications.guidance(SystemKillGuidance.forSdk(34)))
        assertTrue(developerOption.contains("Developer options") && developerOption.contains("Disable child process restrictions"))
        val deviceConfig = context.getString(LocalKernelNotifications.guidance(SystemKillGuidance.forSdk(32)))
        assertTrue(deviceConfig.contains("device_config set_sync_disabled_for_tests persistent"))
        assertTrue(deviceConfig.contains("device_config put activity_manager max_phantom_processes 2147483647"))
    }

    @Test
    fun start_is_refused_outside_the_foreground_and_without_a_runtime() {
        val missing = LocalKernel.start(context)
        assertTrue("this unit-test build ships no runtime", missing is LocalKernel.StartResult.NotAvailable)
        assertEquals(
            "not available in this build",
            "Not available in this build",
            context.getString(com.tofu.client.R.string.local_kernel_not_available),
        )

        LocalKernel.availabilityOf = { LocalKernelAvailability.Available }
        LocalKernel.isAppInForeground = { false }
        assertEquals(LocalKernel.StartResult.NotInForeground, LocalKernel.start(context))
        assertEquals(null, shadowOf(context as Application).nextStartedService)

        LocalKernel.isAppInForeground = { true }
        assertEquals(LocalKernel.StartResult.Requested, LocalKernel.start(context))
        val started = shadowOf(context as Application).nextStartedService
        assertEquals(LocalKernelService.ACTION_START, started.action)
        assertEquals(LocalKernelService::class.java.name, started.component!!.className)
    }
}
