package com.tofu.client.selfcheck

import android.app.ActivityManager
import android.app.NotificationManager
import android.app.usage.UsageStatsManager
import android.content.Context
import android.os.Build
import android.os.PowerManager
import android.os.StatFs
import android.provider.Settings
import android.system.Os
import android.system.OsConstants
import com.tofu.client.BuildConfig
import com.tofu.client.runtime.AssetManagerRuntimeAssets
import com.tofu.client.runtime.KernelCommandLine
import com.tofu.client.runtime.KernelLayout
import com.tofu.client.runtime.LocalKernelAvailability
import com.tofu.client.runtime.RuntimeInstaller
import java.io.File
import java.security.MessageDigest

/** Whether this build carries the runtime, and where its on-device pieces live. */
class AndroidRuntimeProbe(context: Context) : RuntimeProbe {
    private val app = context.applicationContext
    override val layout = KernelLayout(app.filesDir)
    // Robolectric's ApplicationInfo may leave nativeLibraryDir null; the host dir is then simply absent.
    override val nativeLibraryDir: File = File(app.applicationInfo.nativeLibraryDir ?: File(app.filesDir, "lib").path)
    private val installer = RuntimeInstaller(layout, AssetManagerRuntimeAssets(app.assets))

    override fun missing(): String? = (LocalKernelAvailability.of(app) as? LocalKernelAvailability.NotInThisBuild)?.missing
    override fun rootfsInstalled(): Boolean = layout.rootfs.isDirectory

    override fun payloadDirectory(): File? {
        val current = installer.currentVersion()
        if (current != null) return layout.payload(current)
        return layout.payloadRoot.listFiles().orEmpty()
            .filter { it.isDirectory && !it.name.startsWith(".") }
            .minByOrNull { it.name }
    }

    /** `assets/kernel/index.json` versions; an unreadable index is a fact, not a crash. */
    fun indexFacts(): Triple<String?, String?, String?> = try {
        val index = installer.readIndex()
        if (index == null) Triple(null, null, "no assets/kernel/index.json") else Triple(index.rootfs.version, index.payload.version, null)
    } catch (error: Exception) {
        Triple(null, null, "${error.javaClass.simpleName}: ${error.message}")
    }

    fun installedRootfsVersion(): String? = layout.rootfsVersionFile.takeIf { it.isFile }?.readText()?.trim()
    fun currentPayload(): String? = installer.currentVersion()
}

/**
 * Device facts (self-check step 1). Only documented, third-party-readable
 * sources: nothing here reads accounts, Wi-Fi names, cookies or files'
 * contents. Anything that throws is recorded in [DeviceFacts.unreadable]
 * instead of failing the run.
 */
class AndroidDeviceFacts(private val context: Context, private val runtime: AndroidRuntimeProbe) {
    fun collect(): DeviceFacts {
        val unreadable = LinkedHashMap<String, String>()
        fun <T> fact(name: String, read: () -> T): T? = try {
            read()
        } catch (error: Throwable) {
            unreadable[name] = error.javaClass.simpleName
            null
        }
        val app = context.applicationContext
        val activity = app.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val memory = ActivityManager.MemoryInfo()
        fact("totalRamBytes") { activity.getMemoryInfo(memory) }
        val stats = fact("storage") { StatFs(app.filesDir.path) }
        val availability = runtime.missing()
        val (rootfsVersion, payloadVersion, indexError) = runtime.indexFacts()
        val nativeLibraries = listOf(KernelCommandLine.PROOT, KernelCommandLine.PROOT_LOADER).map { name ->
            val file = File(runtime.nativeLibraryDir, name)
            val present = file.isFile
            NativeLibraryFact(
                name = name,
                present = present,
                sizeBytes = if (present) file.length() else null,
                sha256 = if (present) fact("sha256:$name") { sha256(file) } else null,
            )
        }
        return DeviceFacts(
            manufacturer = Build.MANUFACTURER,
            model = Build.MODEL,
            device = Build.DEVICE,
            release = Build.VERSION.RELEASE,
            sdkInt = Build.VERSION.SDK_INT,
            securityPatch = Build.VERSION.SECURITY_PATCH,
            pageSizeBytes = fact("pageSizeBytes") { Os.sysconf(OsConstants._SC_PAGESIZE) },
            clockTicksPerSecond = fact("clockTicksPerSecond") { Os.sysconf(OsConstants._SC_CLK_TCK) },
            supportedAbis = Build.SUPPORTED_ABIS.toList(),
            kernelRelease = fact("kernelRelease") { Os.uname().release },
            appVersionName = BuildConfig.VERSION_NAME,
            appVersionCode = BuildConfig.VERSION_CODE,
            appBuildId = BuildConfig.DIAGNOSTIC_BUILD_ID,
            targetSdk = app.applicationInfo.targetSdkVersion,
            freeStorageBytes = stats?.availableBytes,
            totalStorageBytes = stats?.totalBytes,
            totalRamBytes = memory.totalMem.takeIf { it > 0 },
            ignoringBatteryOptimizations = fact("ignoringBatteryOptimizations") {
                (app.getSystemService(Context.POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(app.packageName)
            },
            standbyBucket = fact("standbyBucket") { (app.getSystemService(Context.USAGE_STATS_SERVICE) as UsageStatsManager).appStandbyBucket },
            backgroundRestricted = if (Build.VERSION.SDK_INT >= 28) fact("backgroundRestricted") { activity.isBackgroundRestricted } else null,
            postNotificationsGranted = if (Build.VERSION.SDK_INT >= 33) fact("postNotificationsGranted") {
                app.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) == android.content.pm.PackageManager.PERMISSION_GRANTED
            } else null,
            notificationsEnabled = fact("notificationsEnabled") { (app.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).areNotificationsEnabled() },
            runtime = RuntimeFacts(
                inThisBuild = availability == null,
                missing = availability,
                rootfsVersion = rootfsVersion,
                payloadVersion = payloadVersion,
                indexError = indexError,
                nativeLibraries = nativeLibraries,
                installedRootfsVersion = runtime.installedRootfsVersion(),
                currentPayload = runtime.currentPayload(),
            ),
            phantomProcesses = PhantomSettingFacts(
                monitorFlagGlobal = phantomFlagGlobal(app),
                monitorFlagProperty = SettingRead(
                    key = "persist.sys.fflag.override.settings_enable_monitor_phantom_procs",
                    readable = false,
                    value = null,
                    note = "The Android 14 developer option writes this system property (FeatureFlagUtils.setEnabled); android.os.SystemProperties has no public getter.",
                ),
                maxPhantomProcesses = SettingRead(
                    key = "activity_manager/max_phantom_processes",
                    readable = false,
                    value = null,
                    note = "DeviceConfig requires READ_DEVICE_CONFIG (signature|preinstalled) outside its public namespaces, which exclude activity_manager; AOSP default 32.",
                ),
            ),
            unreadable = unreadable,
        )
    }

    /**
     * The only part of the phantom-process switch an app may read: AOSP
     * `FeatureFlagUtils.isEnabled` consults `Settings.Global` first, and
     * `SettingsProvider.checkReadableAnnotation` regards a key that is not a
     * `Settings.*` field as readable — `settings_enable_monitor_phantom_procs`
     * is a `FeatureFlagUtils` constant, not such a field. The system-property
     * override and the DeviceConfig cap stay unreadable, so the effective
     * switch is reported as not fully readable.
     */
    private fun phantomFlagGlobal(context: Context): SettingRead = try {
        SettingRead(
            key = GLOBAL_FLAG,
            readable = true,
            value = Settings.Global.getString(context.contentResolver, GLOBAL_FLAG),
            note = "Settings.Global value only; null means unset. A system-property override and the DeviceConfig cap are not readable, so this is not the effective switch.",
        )
    } catch (error: SecurityException) {
        SettingRead(GLOBAL_FLAG, readable = false, value = null, note = "Settings.Global read refused: ${error.javaClass.simpleName}")
    }

    companion object {
        const val GLOBAL_FLAG = "settings_enable_monitor_phantom_procs"

        fun sha256(file: File): String {
            val digest = MessageDigest.getInstance("SHA-256")
            file.inputStream().buffered().use { input ->
                val buffer = ByteArray(64 * 1024)
                while (true) {
                    val read = input.read(buffer)
                    if (read < 0) break
                    digest.update(buffer, 0, read)
                }
            }
            return digest.digest().joinToString("") { "%02x".format(it) }
        }
    }
}
