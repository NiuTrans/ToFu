package com.tofu.client.selfcheck

import android.app.ActivityManager
import android.app.ApplicationExitInfo
import android.app.Application
import android.app.NotificationManager
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.os.PowerManager
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/**
 * Device facts against fake system services (Robolectric, Android 14):
 * power manager, standby bucket, background restriction, notifications, and
 * the exit-reason history. The phantom-setting facts must only claim what an
 * app may read.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], application = com.tofu.client.TofuApplication::class)
class AndroidSelfCheckFactsTest {
    private val context: Context = ApplicationProvider.getApplicationContext()
    private val runtime = AndroidRuntimeProbe(context)

    @org.junit.Before
    fun clearFileProviderCache() {
        // FileProvider.sCache is static per authority and survives Robolectric's per-method
        // sandboxes; without clearing it, the second method reuses the first one's cacheDir.
        val field = androidx.core.content.FileProvider::class.java.getDeclaredField("sCache")
        field.isAccessible = true
        (field.get(null) as HashMap<*, *>).clear()
    }

    @Test
    fun collects_device_app_and_runtime_facts_without_a_runtime_build() {
        shadowOf(context.getSystemService(PowerManager::class.java)).setIgnoringBatteryOptimizations(context.packageName, true)
        shadowOf(context.getSystemService(UsageStatsManager::class.java)).setCurrentAppStandbyBucket(UsageStatsManager.STANDBY_BUCKET_RARE)
        val facts = AndroidDeviceFacts(context, runtime).collect()
        assertEquals(34, facts.sdkInt)
        assertEquals(context.applicationInfo.targetSdkVersion, facts.targetSdk)
        assertEquals(true, facts.ignoringBatteryOptimizations)
        assertEquals(UsageStatsManager.STANDBY_BUCKET_RARE, facts.standbyBucket)
        assertNotNull(facts.pageSizeBytes)
        // Robolectric's SUPPORTED_ABIS comes from system properties (often empty off-device);
        // the fact must simply mirror whatever the framework reports.
        assertEquals(android.os.Build.SUPPORTED_ABIS.toList(), facts.supportedAbis)
        assertTrue("ram=${facts.totalRamBytes}", facts.totalRamBytes == null || facts.totalRamBytes!! > 0)
        // Robolectric's StatFs on the sandbox data dir may report 0; the fact must not throw.
        assertTrue(facts.freeStorageBytes == null || facts.freeStorageBytes!! >= 0)
        assertFalse(facts.runtime.inThisBuild) // this test APK has no assets/kernel/index.json
        assertEquals(2, facts.runtime.nativeLibraries.size)
        assertTrue(facts.runtime.nativeLibraries.all { !it.present && it.sha256 == null })
    }

    @Test
    fun phantom_setting_only_claims_the_settings_global_read() {
        val facts = AndroidDeviceFacts(context, runtime).collect().phantomProcesses
        assertTrue(facts.monitorFlagGlobal.readable)
        assertFalse(facts.monitorFlagProperty.readable)
        assertNull(facts.monitorFlagProperty.value)
        assertFalse(facts.maxPhantomProcesses.readable)
        assertNull(facts.maxPhantomProcesses.value)
        assertTrue(facts.maxPhantomProcesses.note.contains("READ_DEVICE_CONFIG"))
        assertEquals("settings_enable_monitor_phantom_procs", facts.monitorFlagGlobal.key)
    }

    @Test
    fun notification_facts_come_from_the_system_service() {
        val facts = AndroidDeviceFacts(context, runtime).collect()
        val manager = context.getSystemService(NotificationManager::class.java)
        assertEquals(manager.areNotificationsEnabled(), facts.notificationsEnabled)
    }

    @Test
    fun exit_reasons_are_projected_without_crash_descriptions() {
        val activity = context.getSystemService(ActivityManager::class.java)
        val excessive = ApplicationExitInfo().apply {
            reason = ApplicationExitInfo.REASON_EXCESSIVE_RESOURCE_USAGE
            status = 0
            pid = 4321
            timestamp = 1_700_000_000_000L
            importance = ActivityManager.RunningAppProcessInfo.IMPORTANCE_CACHED
            processName = context.packageName
            description = "Caused by child process: excessive cpu 9000 during 5000"
        }
        val crash = ApplicationExitInfo().apply {
            reason = ApplicationExitInfo.REASON_CRASH
            pid = 4322
            timestamp = 1_700_000_001_000L
            description = "java.lang.RuntimeException: token=should-not-appear"
        }
        shadowOf(activity).addApplicationExitInfo(excessive)
        shadowOf(activity).addApplicationExitInfo(crash)
        val records = selfCheckExitReasons(context)
        assertEquals(2, records.size)
        val first = records.first { it.reason == ApplicationExitInfo.REASON_EXCESSIVE_RESOURCE_USAGE }
        assertEquals(4321, first.pid)
        assertTrue(first.description.orEmpty().contains("excessive cpu"))
        assertNull(records.first { it.reason == ApplicationExitInfo.REASON_CRASH }.description)
    }

    @Test
    fun share_copy_lands_in_the_file_provider_cache_path() {
        val files = SelfCheckFiles(context)
        files.writeReport(ReportJson.of(SelfCheckReport("share1", RunStatus.FINISHED, 0L, presetMinutes = 1)).render(SelfCheckRedactor()))
        val copy = files.shareCopy("share1")
        assertTrue(copy.isFile)
        assertTrue(copy.path.startsWith(context.cacheDir.path))

        val uri = androidx.core.content.FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", copy)
        assertEquals("content", uri.scheme)
        assertTrue(context.contentResolver.openInputStream(uri)!!.bufferedReader().readText().contains("\"selfcheckVersion\": 1"))
    }

    @Test
    fun share_intent_carries_a_grant_and_the_json_mime_type() {
        val files = SelfCheckFiles(context)
        files.writeReport("{\"selfcheckVersion\":1,\"reportId\":\"share2\"}")
        shareSelfCheckReport(context)
        val started = shadowOf(context as Application).nextStartedActivity
        assertNotNull(started)
        assertEquals(Intent.ACTION_CHOOSER, started.action)
        val send = started.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)!!
        assertEquals("application/json", send.type)
        assertTrue(send.flags and Intent.FLAG_GRANT_READ_URI_PERMISSION != 0)
        assertNotNull(send.clipData)
        assertEquals("content", send.getParcelableExtra<android.net.Uri>(Intent.EXTRA_STREAM)!!.scheme)
    }
}
