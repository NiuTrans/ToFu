package com.tofu.client.session

import com.tofu.client.runtime.LocalKernelEndpoint
import com.tofu.client.runtime.LocalKernelState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request

/**
 * When "This phone" keeps running (design defaults, decided): while a task
 * runs, while a schedule is enabled, or while the user keeps it running;
 * otherwise it stops once the user puts another kernel in front or leaves the
 * app. Unknown work or schedules count as present: the kernel is never stopped
 * on a guess, the same fail-busy rule as the server's own quiescence probe.
 */
object LocalKernelPolicy {
    fun keepRunning(keepRunningToggle: Boolean, inFront: Boolean, appVisible: Boolean, runningWork: Boolean?, enabledSchedule: Boolean?): Boolean =
        keepRunningToggle || (inFront && appVisible) || runningWork != false || enabledSchedule != false
}

/** What the running local kernel reports about its own activity; null = could not tell. */
interface LocalKernelActivity {
    /** A task is running (`GET /health/quiescence`). */
    suspend fun runningWork(endpoint: LocalKernelEndpoint): Boolean?

    /** At least one schedule is enabled (`GET /api/v1/scheduler/tasks`). */
    suspend fun enabledSchedule(endpoint: LocalKernelEndpoint): Boolean?
}

/**
 * Applies [LocalKernelPolicy] and feeds the runtime's wakelock hook
 * (`LocalKernel.setHasRunningWork`).
 *
 * Event-driven: every change of the kernel's state, the front kernel, the
 * app's visibility or the "Keep running" toggle re-evaluates at once. Nothing
 * is decided before the kernel is Ready, because only a ready kernel can say
 * whether it is busy. While the user looks at This phone, nothing is read:
 * the kernel stays and the screen keeps the device awake. Away from it (another
 * kernel in front, or the app left), the kernel's running-work signal is read
 * at the app's existing server-status cadence
 * ([ServerLifecycle.START_POLL_INTERVAL_MS], also the cadence at which the
 * desktop supervisor polls the same `/health/quiescence` before a deferred
 * restart): it holds the partial wakelock while a task runs, and stops the
 * kernel once nothing runs and no schedule is enabled. Schedules are read when
 * that decision first comes up and again whenever a task has just finished
 * (a one-shot schedule may have been its trigger).
 */
class LocalKernelLifecycle(
    private val kernel: StateFlow<LocalKernelState>,
    private val inFront: StateFlow<Boolean>,
    private val appVisible: StateFlow<Boolean>,
    private val keepRunning: StateFlow<Boolean>,
    private val activity: LocalKernelActivity,
    private val setHasRunningWork: (Boolean) -> Unit,
    private val stop: () -> Unit,
    private val pollMs: Long = ServerLifecycle.START_POLL_INTERVAL_MS,
) {
    private data class Inputs(val kernel: LocalKernelState, val inFront: Boolean, val appVisible: Boolean, val keepRunning: Boolean)

    fun start(scope: CoroutineScope): Job = scope.launch {
        combine(kernel, inFront, appVisible, keepRunning) { k, front, visible, keep -> Inputs(k, front, visible, keep) }
            .collectLatest(::supervise)
    }

    private suspend fun supervise(inputs: Inputs) {
        val ready = inputs.kernel as? LocalKernelState.Ready
        if (ready == null || (inputs.inFront && inputs.appVisible)) {
            setHasRunningWork(false)
            return
        }
        var previousWork: Boolean? = null
        var schedule: Boolean? = null
        while (true) {
            val work = activity.runningWork(ready.endpoint)
            setHasRunningWork(work == true)
            if (!inputs.keepRunning && work == false) {
                if (schedule == null || previousWork != false) schedule = activity.enabledSchedule(ready.endpoint)
                if (!LocalKernelPolicy.keepRunning(inputs.keepRunning, inputs.inFront, inputs.appVisible, work, schedule)) {
                    setHasRunningWork(false)
                    stop()
                    return
                }
            }
            previousWork = work
            delay(pollMs)
        }
    }
}

/**
 * [LocalKernelActivity] over the kernel's existing HTTP surface, as native
 * calls with `Authorization: Bearer <install secret>` (launch contract §6):
 * - `GET /health/quiescence` (`routes/common.py`): the process-wide running-
 *   task count, the same judge as the update-restart guard; a registry failure
 *   already answers busy (`quiet: false`);
 * - `GET /api/v1/scheduler/tasks` (`routes/api_v1/scheduler.py`): enabled
 *   scheduled tasks only.
 * Blocking I/O: [call] runs each request on its dispatcher. Bodies are read
 * with anchored regexes, as [ApiMetaGate] does (org.json is not on the pure
 * JVM test classpath).
 */
class HttpLocalKernelActivity(
    private val http: OkHttpClient,
    private val secret: () -> String,
    private val call: suspend (() -> Boolean?) -> Boolean?,
) : LocalKernelActivity {

    override suspend fun runningWork(endpoint: LocalKernelEndpoint): Boolean? = call {
        get(endpoint, "health/quiescence") { body ->
            when (QUIET.find(body)?.groupValues?.get(1)) {
                "true" -> false
                "false" -> true
                else -> null
            }
        }
    }

    override suspend fun enabledSchedule(endpoint: LocalKernelEndpoint): Boolean? = call {
        get(endpoint, "api/v1/scheduler/tasks") { body ->
            when {
                NO_TASKS.containsMatchIn(body) -> false
                TASKS.containsMatchIn(body) -> true
                else -> null
            }
        }
    }

    private fun get(endpoint: LocalKernelEndpoint, path: String, read: (String) -> Boolean?): Boolean? = try {
        val request = Request.Builder().url(endpoint.baseUrl + path)
            .header("Authorization", "Bearer ${secret()}")
            .get().build()
        http.newCall(request).execute().use { response ->
            if (response.code == 200) read(response.body?.string().orEmpty()) else null
        }
    } catch (unknown: Exception) {
        null // unreachable, or no secret to present: the kernel cannot tell, so nothing is decided on it
    }

    private companion object {
        val QUIET = Regex("\"quiet\"\\s*:\\s*(true|false)")
        val NO_TASKS = Regex("\"tasks\"\\s*:\\s*\\[\\s*]")
        val TASKS = Regex("\"tasks\"\\s*:\\s*\\[")
    }
}
