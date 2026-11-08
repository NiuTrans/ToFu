package com.tofu.client.runtime

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import androidx.annotation.VisibleForTesting
import androidx.core.content.ContextCompat
import com.tofu.client.clientDiagnostics
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.File

/**
 * The local kernel's public Kotlin API — the surface a `KernelController`
 * adapter wraps. One kernel per app process; [state] is the single source of
 * truth for what runs on this phone.
 *
 * Entry points: [start] (foreground only: Android forbids starting a
 * foreground service from the background for apps targeting 31+, and the
 * design starts the kernel only on a user action), [stop], [state],
 * [endpoint], [secret] (for the `tofu_session` cookie and `Authorization:
 * Bearer` header), and [setHasRunningWork], the hook that will gate the
 * partial wakelock once the server reports running tasks.
 */
object LocalKernel {
    sealed interface StartResult {
        /** The service was asked to start; follow [state]. */
        data object Requested : StartResult
        data class NotAvailable(val availability: LocalKernelAvailability.NotInThisBuild) : StartResult
        /** Refused: the app is not in the foreground. */
        data object NotInForeground : StartResult
    }

    @Volatile private var supervisor: LocalKernelSupervisor? = null
    @Volatile private var secretStore: KernelSecret? = null
    private val runningWork = MutableStateFlow(false)

    /** Whether the kernel reports active work; the service holds a partial wakelock only while this is true. */
    val hasRunningWork: StateFlow<Boolean> = runningWork.asStateFlow()

    @VisibleForTesting
    internal var isAppInForeground: () -> Boolean = {
        ActivityManager.RunningAppProcessInfo().also(ActivityManager::getMyMemoryState).importance <=
            ActivityManager.RunningAppProcessInfo.IMPORTANCE_FOREGROUND
    }

    @VisibleForTesting
    internal var availabilityOf: (Context) -> LocalKernelAvailability = LocalKernelAvailability::of

    fun availability(context: Context): LocalKernelAvailability = availabilityOf(context)

    fun state(context: Context): StateFlow<LocalKernelState> = supervisor(context).state

    /** `http://127.0.0.1:<port>/` while [state] is Ready, else null. */
    fun endpoint(context: Context): LocalKernelEndpoint? = supervisor(context).endpoint

    /**
     * Asks for the foreground service, then starts the supervisor here, so
     * [state] shows this start (Starting, or what followed) when this returns
     * [StartResult.Requested]: a caller awaiting its outcome cannot mistake the
     * previous run's state, even an identical failure, for it. The service only
     * enters the foreground and follows the supervisor.
     */
    fun start(context: Context): StartResult {
        val availability = availability(context)
        if (availability is LocalKernelAvailability.NotInThisBuild) return StartResult.NotAvailable(availability)
        if (!isAppInForeground()) return StartResult.NotInForeground
        ContextCompat.startForegroundService(
            context,
            Intent(context, LocalKernelService::class.java).setAction(LocalKernelService.ACTION_START),
        )
        supervisor(context).start()
        return StartResult.Requested
    }

    fun stop(context: Context) = supervisor(context).stop()

    /**
     * The per-install secret the kernel admits: the value of the HttpOnly
     * `tofu_session` cookie for 127.0.0.1 and of `Authorization: Bearer`. One
     * store serves the launcher and the caller, so both always see the same
     * secret. Disk and Keystore I/O: call it off the main thread, never log it.
     */
    fun secret(context: Context): KernelSecretValue = secretStore(context).load()

    fun setHasRunningWork(running: Boolean) {
        runningWork.value = running
    }

    internal fun supervisor(context: Context): LocalKernelSupervisor =
        supervisor ?: synchronized(this) {
            supervisor ?: context.applicationContext.let { application ->
                LocalKernelSupervisor(
                    runtime = AndroidKernelRuntime(application, secretStore(application)),
                    launcher = ProcessBuilderLauncher(),
                    signals = AndroidProcessSignals(),
                    readiness = MetaReadinessProbe(),
                    diagnostics = application.clientDiagnostics(),
                )
            }.also { supervisor = it }
        }

    private fun secretStore(context: Context): KernelSecret =
        secretStore ?: synchronized(this) {
            secretStore ?: KernelSecret(
                File(context.applicationContext.noBackupFilesDir, KernelSecret.STORE_FILE),
                AndroidKeyStoreSecretWrapper(),
            ).also { secretStore = it }
        }

    @VisibleForTesting
    internal fun installForTest(instance: LocalKernelSupervisor?) {
        supervisor = instance
        runningWork.value = false
    }
}
