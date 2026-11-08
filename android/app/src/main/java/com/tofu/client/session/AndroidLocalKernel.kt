package com.tofu.client.session

import android.app.Activity
import android.app.Application
import android.content.Context
import android.os.Build
import android.os.Bundle
import android.webkit.CookieManager
import com.tofu.client.R
import com.tofu.client.clientDiagnostics
import com.tofu.client.runtime.KernelSecretValue
import com.tofu.client.runtime.LocalKernel
import com.tofu.client.runtime.LocalKernelAvailability
import com.tofu.client.runtime.LocalKernelNotifications
import com.tofu.client.runtime.LocalKernelState
import com.tofu.client.runtime.RuntimeIndex
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import java.net.Proxy

/** "This phone" for `MainActivity`'s [KernelControllers]: the process-wide [LocalKernelHost]'s controller. */
fun localKernelController(context: Context, diagnostics: DiagLog?): KernelController =
    LocalKernelHost.of(context, diagnostics).controller

/**
 * The device side of "This phone", one per process: the [LocalKernelController]
 * over `runtime/LocalKernel`, the [LocalKernelLifecycle] that stops the kernel
 * by the decided policy and drives the wakelock hook, the "Keep running"
 * setting, and whether the app is on screen.
 *
 * Created by `MainActivity` (through [localKernelController]) before its
 * first `onStart`, so [AppVisibility] sees every activity start. The UI reads
 * [current], which never creates it: previews and screenshots have none.
 */
class LocalKernelHost private constructor(context: Context, diagnostics: DiagLog?) {
    private val port = AndroidLocalKernelPort(context)
    private val inFront = MutableStateFlow(false)
    private val visibility = AppVisibility().also { (context as Application).registerActivityLifecycleCallbacks(it) }
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val loopback = OkHttpClient.Builder().proxy(Proxy.NO_PROXY).followRedirects(false).build()
    private val localHealth = HealthProbe(
        http = loopback,
        diagnostics = diagnostics,
        // This kernel's own credential, the one cookie the default provider withholds.
        cookieProvider = { url -> CookieManager.getInstance().getCookie(url) },
    )

    val settings = LocalKernelSettings(context)

    /** False when this device or build cannot run the local kernel: no lifecycle, no toggle. */
    val available: Boolean = port.availability() == LocalAvailability.Available

    val controller = LocalKernelController(
        port = port,
        text = AndroidLocalKernelText(context),
        cookies = CookieBridge,
        healthCheck = { url -> withContext(Dispatchers.IO) { localHealth.probe(url).verdict } },
        scope = scope,
        onFront = { inFront.value = it },
    )

    init {
        if (available) {
            val secret by lazy { port.secret().reveal() }
            LocalKernelLifecycle(
                kernel = port.state,
                inFront = inFront,
                appVisible = visibility.visible,
                keepRunning = settings.keepRunning,
                activity = HttpLocalKernelActivity(loopback, { secret }) { read -> withContext(Dispatchers.IO) { read() } },
                setHasRunningWork = LocalKernel::setHasRunningWork,
                stop = port::stop,
            ).start(scope)
        }
    }

    companion object {
        @Volatile private var instance: LocalKernelHost? = null

        /** The host `MainActivity` created, or null (previews, screenshots, tests). */
        val current: LocalKernelHost? get() = instance

        fun of(context: Context, diagnostics: DiagLog? = context.clientDiagnostics()): LocalKernelHost =
            instance ?: synchronized(this) {
                instance ?: LocalKernelHost(context.applicationContext, diagnostics).also { instance = it }
            }
    }
}

/** [LocalKernelPort] over `runtime/LocalKernel`. */
class AndroidLocalKernelPort(private val context: Context) : LocalKernelPort {
    override fun availability(): LocalAvailability {
        val abis = Build.SUPPORTED_ABIS.toList()
        if (RuntimeIndex.ABI !in abis) return LocalAvailability.NotArm64(abis)
        return when (val availability = LocalKernel.availability(context)) {
            LocalKernelAvailability.Available -> LocalAvailability.Available
            is LocalKernelAvailability.NotInThisBuild -> LocalAvailability.NotInThisBuild(availability.missing)
        }
    }

    override fun start(): LocalStart = when (LocalKernel.start(context)) {
        LocalKernel.StartResult.Requested -> LocalStart.REQUESTED
        is LocalKernel.StartResult.NotAvailable -> LocalStart.NOT_AVAILABLE
        LocalKernel.StartResult.NotInForeground -> LocalStart.NOT_IN_FOREGROUND
    }

    override fun stop() = LocalKernel.stop(context)

    override val state: StateFlow<LocalKernelState> get() = LocalKernel.state(context)

    override fun secret(): KernelSecretValue = LocalKernel.secret(context)
}

/** [LocalKernelText] from resources; runtime failures keep the runtime's own wording. */
class AndroidLocalKernelText(private val context: Context) : LocalKernelText {
    override fun notArm64() = context.getString(R.string.this_phone_not_arm64)
    override fun notInThisBuild() = context.getString(R.string.this_phone_not_in_build)
    override fun notInForeground() = context.getString(R.string.this_phone_not_in_foreground)
    override fun stoppedBeforeReady() = context.getString(R.string.this_phone_stopped_before_ready)
    override fun healthCheckFailed(verdict: TofuProbe.Verdict) = context.getString(R.string.this_phone_health_check_failed, verdict.name)
    override fun describe(state: LocalKernelState): String {
        val (title, text) = LocalKernelNotifications.describe(context, state)
        return text.ifEmpty { title }
    }
}

/**
 * Whether any activity of the app is started (on screen). A configuration
 * change stops and restarts the activity without the app leaving the screen.
 */
class AppVisibility : Application.ActivityLifecycleCallbacks {
    private var started = 0
    private val mutableVisible = MutableStateFlow(false)
    val visible: StateFlow<Boolean> = mutableVisible.asStateFlow()

    override fun onActivityStarted(activity: Activity) {
        started++
        mutableVisible.value = true
    }

    override fun onActivityStopped(activity: Activity) {
        started--
        if (started == 0 && !activity.isChangingConfigurations) mutableVisible.value = false
    }

    override fun onActivityCreated(activity: Activity, savedInstanceState: Bundle?) {}
    override fun onActivityResumed(activity: Activity) {}
    override fun onActivityPaused(activity: Activity) {}
    override fun onActivitySaveInstanceState(activity: Activity, outState: Bundle) {}
    override fun onActivityDestroyed(activity: Activity) {}
}

/**
 * "This phone" settings, app-private (excluded from backup and transfer like
 * all app data): the "Keep running" toggle and whether the notification
 * permission was already requested.
 */
class LocalKernelSettings(context: Context) {
    private val prefs = context.getSharedPreferences(FILE, Context.MODE_PRIVATE)
    private val keep = MutableStateFlow(prefs.getBoolean(KEEP_RUNNING, false))

    val keepRunning: StateFlow<Boolean> = keep.asStateFlow()

    fun setKeepRunning(on: Boolean) {
        prefs.edit().putBoolean(KEEP_RUNNING, on).apply()
        keep.value = on
    }

    /**
     * Whether to ask for POST_NOTIFICATIONS now: a runtime permission from
     * Android 13 (API 33), asked only the first time the user starts the local
     * kernel and only while it is not granted. True at most once.
     */
    fun shouldRequestNotifications(sdkInt: Int, granted: Boolean): Boolean {
        if (sdkInt < 33 || granted || prefs.getBoolean(NOTIFICATIONS_ASKED, false)) return false
        prefs.edit().putBoolean(NOTIFICATIONS_ASKED, true).apply()
        return true
    }

    private companion object {
        const val FILE = "this_phone"
        const val KEEP_RUNNING = "keep_running"
        const val NOTIFICATIONS_ASKED = "notification_permission_asked"
    }
}
