package com.tofu.client.selfcheck

import android.app.ActivityManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.tofu.client.R
import com.tofu.client.runtime.LocalKernel
import com.tofu.client.session.ClientReport
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/** Process-wide owner of the self-check run, its environment and its persisted report. */
object SelfCheckController {
    private val mutableStatus = MutableStateFlow<String?>(null)
    val status: StateFlow<String?> = mutableStatus.asStateFlow()

    @Volatile private var environment: AndroidEnvironment? = null
    @Volatile private var run: SelfCheckRun? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    fun environment(context: Context): AndroidEnvironment = environment ?: synchronized(this) {
        environment ?: AndroidEnvironment(context.applicationContext).also { environment = it }
    }

    fun state(context: Context): StateFlow<RunUiState> = runner(context).state

    private fun runner(context: Context): SelfCheckRun = run ?: synchronized(this) {
        run ?: SelfCheckRun(environment(context), scope).also { run = it }
    }

    fun start(context: Context, minutes: Int) {
        val app = context.applicationContext
        recoverInterruptedRun(app)
        if (!runner(app).start(minutes)) return
        mutableStatus.value = null
        ContextCompat.startForegroundService(app, Intent(app, SelfCheckService::class.java).setAction(SelfCheckService.ACTION_START))
        watch(app)
    }

    fun cancel(context: Context) = runner(context).cancel()
    fun skipBackground(context: Context) = runner(context).skipBackgroundPhase()

    @Volatile private var watcher: Job? = null

    private fun watch(context: Context) {
        if (watcher?.isActive == true) return
        watcher = scope.launch {
            var finished = false
            runner(context).state.collect { state ->
                if (state.running) {
                    SelfCheckService.update(context, state)
                } else if (!finished && state.report != null) {
                    finished = true
                    context.stopService(Intent(context, SelfCheckService::class.java))
                    SelfCheckNotifications.finished(context, state)
                }
            }
        }
    }

    /**
     * The previous process died mid-run: kill the pids it recorded (matched by
     * start time, so a recycled pid is untouched), then turn its last report
     * into an interrupted one with the app's own exit records — the system's
     * account of why the process ended.
     */
    fun recoverInterruptedRun(context: Context) {
        val environment = environment(context)
        val store = environment.store
        if (!store.interruptedRun()) return
        val host = environment.stressHost()
        for (pid in store.pids()) {
            val stat = host.stat(pid) ?: continue
            if (stat.zombie) continue
            runCatching { host.signal(pid, android.os.Process.SIGNAL_KILL) }
        }
        val previous = store.previousReportJson()
        val startedAt = "\"startedAt\"\\s*:\\s*\"([^\"]+)\"".toRegex().find(previous.orEmpty())?.groupValues?.get(1)
            ?.let { runCatching { java.time.Instant.parse(it).toEpochMilli() }.getOrNull() }
        store.writeInterrupted(
            reportId = ClientReport.newReportId(),
            detectedAtWallMs = environment.clock.wallMs(),
            lastHeartbeatWallMs = store.heartbeat(),
            runStartedAtWallMs = startedAt,
            exitReasons = runCatching { environment.exitReasons() }.getOrDefault(emptyList()),
            previousReportJson = previous,
        )
        store.clear()
        mutableStatus.value = "interrupted"
    }
}

/** The Android half of [SelfCheckEnvironment]. */
class AndroidEnvironment(context: Context) : SelfCheckEnvironment {
    private val app = context.applicationContext
    override val clock: SelfCheckClock = AndroidClock
    private val runtimeProbe = AndroidRuntimeProbe(app)
    private val host = AndroidStressHost(app)
    private val kernelControl = AndroidKernelControl(app)
    private val factsCollector = AndroidDeviceFacts(app, runtimeProbe)
    @Volatile private var secret: com.tofu.client.runtime.KernelSecretValue? = null

    override val store: SelfCheckStore = AndroidSelfCheckStore(app) { SelfCheckRedactor(secret) }

    override fun facts(): DeviceFacts = factsCollector.collect()
    override fun runtime(): RuntimeProbe = runtimeProbe
    override fun runner(): CommandRunner = AndroidCommandRunner(host)
    override fun kernel(): KernelControl = kernelControl
    override fun http(): HttpProbe = SelfCheckHttpProbe()
    override fun stressHost(): StressHost = host
    override fun exitReasons(): List<ExitReasonRecord> = selfCheckExitReasons(app)
    override fun kernelActive(): Boolean = localKernelActive(kernelControl.state.value)

    override fun inForeground(): Boolean =
        host.importance() <= ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND

    override fun newReportId(): String = ClientReport.newReportId()

    /** The kernel check's secret is loaded off the main thread; remembered only for scrubbing. */
    fun noteSecret(value: com.tofu.client.runtime.KernelSecretValue?) {
        secret = value
    }
}

/** `specialUse` foreground service: the background stress phase runs under exactly the kernel's conditions. */
class SelfCheckService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        SelfCheckNotifications.channel(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(SelfCheckNotifications.ONGOING_ID, SelfCheckNotifications.ongoing(this, null), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(SelfCheckNotifications.ONGOING_ID, SelfCheckNotifications.ongoing(this, null))
        }
        return START_NOT_STICKY
    }

    companion object {
        const val ACTION_START = "com.tofu.client.selfcheck.action.START"

        fun update(context: Context, state: RunUiState) {
            val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.notify(SelfCheckNotifications.ONGOING_ID, SelfCheckNotifications.ongoing(context, state))
        }
    }
}

object SelfCheckNotifications {
    const val CHANNEL = "device_selfcheck"
    const val ONGOING_ID = 0x70f3
    const val DONE_ID = 0x70f4

    fun channel(context: Context) {
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, context.getString(R.string.selfcheck_channel_name), NotificationManager.IMPORTANCE_LOW)
                .apply { description = context.getString(R.string.selfcheck_channel_description) },
        )
    }

    fun ongoing(context: Context, state: RunUiState?): Notification {
        val text = when {
            state == null -> context.getString(R.string.selfcheck_running)
            state.waitingForBackground -> context.getString(R.string.selfcheck_waiting_background)
            else -> state.detail ?: context.getString(R.string.selfcheck_running)
        }
        return NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(context.getString(R.string.selfcheck_title))
            .setContentText(text)
            .setOngoing(true)
            .setSilent(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .setContentIntent(openScreen(context, share = false))
            .build()
    }

    /** Completion notification: Open, and Share report straight from the shade (no need to find the app again). */
    fun finished(context: Context, state: RunUiState) {
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val notification = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(context.getString(R.string.selfcheck_finished))
            .setContentText(context.getString(R.string.selfcheck_finished_text))
            .setAutoCancel(true)
            .setContentIntent(openScreen(context, share = false))
            .addAction(0, context.getString(R.string.selfcheck_share), openScreen(context, share = true))
            .build()
        manager.notify(DONE_ID, notification)
    }

    private fun openScreen(context: Context, share: Boolean): PendingIntent {
        val intent = Intent(context, SelfCheckActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .setAction(if (share) SelfCheckActivity.ACTION_SHARE else Intent.ACTION_MAIN)
        return PendingIntent.getActivity(context, if (share) 1 else 0, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }
}
