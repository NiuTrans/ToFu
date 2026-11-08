package com.tofu.client.runtime

import android.annotation.SuppressLint
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
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import com.tofu.client.MainActivity
import com.tofu.client.R
import kotlinx.coroutines.Job
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch

/**
 * `specialUse` foreground service that keeps the app process alive while the
 * local kernel runs (manifest subtype: "Runs the user's local Tofu agent server
 * on 127.0.0.1"; no other FGS type describes an open-ended server). It is a
 * thin Android shell around [LocalKernelSupervisor]: it enters the foreground
 * as soon as the system runs it ([LocalKernel.start] already started the
 * supervisor, whose install and launch take far longer), renders
 * [LocalKernelState] as the ongoing
 * "Tofu is running on this phone" notification (Open, Stop), applies
 * [WakeLockPolicy], and stops itself once the kernel is no longer running —
 * posting the failure, with guidance, when it stopped on its own.
 *
 * START_NOT_STICKY: after process death the kernel restarts only when the user
 * starts it again from the foreground.
 */
class LocalKernelService : Service() {
    private val scope = MainScope()
    private var observing: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        LocalKernelNotifications.createChannels(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val supervisor = LocalKernel.supervisor(this)
        when (intent?.action) {
            ACTION_START -> {
                // LocalKernel.start already started the supervisor; a second start here would
                // relaunch a generation that ended before the system ran this service.
                notificationManager().cancel(LocalKernelNotifications.ALERT_ID)
                enterForeground(LocalKernelNotifications.ongoing(this, supervisor.state.value))
                observe(supervisor)
            }
            ACTION_STOP -> {
                supervisor.stop()
                if (observing == null) stopSelf(startId)
            }
            else -> if (observing == null) stopSelf(startId)
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        applyWakeLock(false)
        val supervisor = LocalKernel.supervisor(this)
        val state = supervisor.state.value
        // Kernel networking runs only under this service: never leave it running unowned.
        if (state is LocalKernelState.Starting || state is LocalKernelState.Ready) supervisor.stop()
        super.onDestroy()
    }

    private fun observe(supervisor: LocalKernelSupervisor) {
        if (observing != null) return
        observing = scope.launch {
            combine(supervisor.state, LocalKernel.hasRunningWork) { state, work -> state to work }
                .collect { (state, work) ->
                    applyWakeLock(WakeLockPolicy.shouldHold(state, work))
                    render(state)
                }
        }
    }

    private fun render(state: LocalKernelState) {
        when (state) {
            LocalKernelState.Starting, LocalKernelState.Stopping, is LocalKernelState.Ready ->
                notificationManager().notify(LocalKernelNotifications.ONGOING_ID, LocalKernelNotifications.ongoing(this, state))
            LocalKernelState.Stopped -> finish(null)
            is LocalKernelState.Failed, is LocalKernelState.SafeMode -> finish(LocalKernelNotifications.alert(this, state))
        }
    }

    private fun finish(alert: Notification?) {
        observing?.cancel()
        observing = null
        applyWakeLock(false)
        stopForeground(STOP_FOREGROUND_REMOVE)
        alert?.let { notificationManager().notify(LocalKernelNotifications.ALERT_ID, it) }
        stopSelf()
    }

    private fun enterForeground(notification: Notification) {
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(LocalKernelNotifications.ONGOING_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(LocalKernelNotifications.ONGOING_ID, notification)
        }
    }

    // No acquire timeout on purpose: WakeLockPolicy releases it when the work signal drops or the kernel stops.
    @SuppressLint("WakelockTimeout")
    private fun applyWakeLock(hold: Boolean) {
        val lock = wakeLock ?: (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, WAKE_LOCK_TAG)
            .apply { setReferenceCounted(false) }
            .also { wakeLock = it }
        if (hold && !lock.isHeld) lock.acquire() else if (!hold && lock.isHeld) lock.release()
    }

    private fun notificationManager() = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    companion object {
        const val ACTION_START = "com.tofu.client.runtime.action.START_LOCAL_KERNEL"
        const val ACTION_STOP = "com.tofu.client.runtime.action.STOP_LOCAL_KERNEL"
        const val WAKE_LOCK_TAG = "tofu:local-kernel"
    }
}

/** Channels and notifications of the local kernel. */
object LocalKernelNotifications {
    const val KERNEL_CHANNEL = "local_kernel"
    /** Created now so approval prompts (a later lane) have their high-importance channel. */
    const val APPROVAL_CHANNEL = "approval_needed"
    const val ONGOING_ID = 0x70f1
    const val ALERT_ID = 0x70f2

    fun createChannels(context: Context) {
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.createNotificationChannel(
            NotificationChannel(KERNEL_CHANNEL, context.getString(R.string.local_kernel_channel_name), NotificationManager.IMPORTANCE_LOW)
                .apply { description = context.getString(R.string.local_kernel_channel_description) }
        )
        manager.createNotificationChannel(
            NotificationChannel(APPROVAL_CHANNEL, context.getString(R.string.approval_channel_name), NotificationManager.IMPORTANCE_HIGH)
                .apply { description = context.getString(R.string.approval_channel_description) }
        )
    }

    fun ongoing(context: Context, state: LocalKernelState): Notification {
        val title = when (state) {
            is LocalKernelState.Ready -> R.string.local_kernel_running
            LocalKernelState.Stopping -> R.string.local_kernel_stopping
            else -> R.string.local_kernel_starting
        }
        return NotificationCompat.Builder(context, KERNEL_CHANNEL)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(context.getString(title))
            .setOngoing(true)
            .setSilent(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .setContentIntent(openApp(context))
            .addAction(0, context.getString(R.string.local_kernel_action_open), openApp(context))
            .addAction(0, context.getString(R.string.local_kernel_action_stop), stopKernel(context))
            .build()
    }

    fun alert(context: Context, state: LocalKernelState): Notification {
        val (title, text) = describe(context, state)
        return NotificationCompat.Builder(context, KERNEL_CHANNEL)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(openApp(context))
            .addAction(0, context.getString(R.string.local_kernel_action_open), openApp(context))
            .build()
    }

    /** User-facing title and body for a stopped kernel. */
    fun describe(context: Context, state: LocalKernelState): Pair<String, String> {
        val title = context.getString(R.string.local_kernel_failed_title)
        return when (state) {
            is LocalKernelState.SafeMode -> title to context.getString(R.string.local_kernel_safe_mode, state.crashes)
            is LocalKernelState.Failed -> when (val failure = state.failure) {
                LocalKernelFailure.KilledBySystem -> context.getString(R.string.local_kernel_killed_title) to
                    context.getString(guidance(SystemKillGuidance.forSdk(Build.VERSION.SDK_INT)))
                is LocalKernelFailure.NotAvailable -> title to context.getString(R.string.local_kernel_not_available)
                is LocalKernelFailure.InstallFailed -> title to context.getString(R.string.local_kernel_failed_install, failure.detail)
                is LocalKernelFailure.SecretUnavailable -> title to context.getString(R.string.local_kernel_failed_secret, failure.detail)
                is LocalKernelFailure.LaunchFailed -> title to context.getString(R.string.local_kernel_failed_launch, failure.detail)
                LocalKernelFailure.UsageError -> title to context.getString(R.string.local_kernel_failed_usage)
                is LocalKernelFailure.ProtocolViolation -> title to context.getString(R.string.local_kernel_failed_protocol, failure.detail)
            }
            else -> title to ""
        }
    }

    fun guidance(kind: SystemKillGuidance): Int = when (kind) {
        SystemKillGuidance.DEVELOPER_OPTION -> R.string.local_kernel_killed_guidance_developer_option
        SystemKillGuidance.DEVICE_CONFIG -> R.string.local_kernel_killed_guidance_device_config
        SystemKillGuidance.RESTART_ONLY -> R.string.local_kernel_killed_guidance_restart
    }

    private fun openApp(context: Context): PendingIntent {
        val launch = context.packageManager.getLaunchIntentForPackage(context.packageName)
            ?: Intent(context, MainActivity::class.java)
        return PendingIntent.getActivity(context, 0, launch, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }

    private fun stopKernel(context: Context): PendingIntent = PendingIntent.getService(
        context,
        0,
        Intent(context, LocalKernelService::class.java).setAction(LocalKernelService.ACTION_STOP),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )
}
