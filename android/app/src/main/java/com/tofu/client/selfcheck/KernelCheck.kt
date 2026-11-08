package com.tofu.client.selfcheck

import com.tofu.client.api.ApiV4Contract
import com.tofu.client.runtime.KernelSecretValue
import com.tofu.client.runtime.LocalKernelEndpoint
import com.tofu.client.runtime.LocalKernelState
import com.tofu.client.session.ApiMetaGate
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

enum class KernelStartOutcome { REQUESTED, NOT_AVAILABLE, NOT_IN_FOREGROUND }

/** The local kernel as the self-check drives it ([com.tofu.client.runtime.LocalKernel] on a device). */
interface KernelControl {
    val state: StateFlow<LocalKernelState>
    fun start(): KernelStartOutcome
    fun stop()
    /** Disk and Keystore I/O: call off the main thread; never log it. */
    fun secret(): KernelSecretValue
}

/** One GET; [body] is read only when asked for ([KernelCheck] reads it for `/api/v4/meta` alone). */
data class HttpOutcome(val status: Int?, val error: String?, val durationMs: Long, val body: String? = null)

fun interface HttpProbe {
    /** [bearer], when given, is sent as `Authorization: Bearer`; it is never part of the outcome. */
    fun get(url: String, bearer: String?, readBody: Boolean): HttpOutcome
}

/**
 * The kernel check: start the local kernel through its public API, time it
 * to Ready, then GET `/api/v4/meta` (public) and one cheap authenticated
 * read twice — without a credential (the local kernel's default deny must
 * answer 401) and with `Authorization: Bearer <secret>` (200). The kernel is
 * left as it was found: a kernel that was not running is stopped again, a
 * Ready one is only probed, and a starting or stopping one is not touched.
 */
class KernelCheck(
    private val control: KernelControl,
    private val http: HttpProbe,
    private val clock: SelfCheckClock,
    private val appVersionCode: Int,
    private val deadlineMs: Long = DEADLINE_MS,
) {
    /** The secret, once the authenticated probe loaded it, so the report can scrub its exact value. */
    @Volatile var loadedSecret: KernelSecretValue? = null
        private set

    suspend fun run(firstInstall: Boolean?): KernelSection {
        val previous = control.state.value
        val base = KernelSection(status = StepStatus.RUNNING, previousState = stateName(previous), firstInstall = firstInstall)
        return when (previous) {
            is LocalKernelState.Ready -> base.copy(
                status = StepStatus.DONE,
                startResult = ALREADY_RUNNING,
                outcome = READY,
                probes = probe(previous.endpoint),
                restoredTo = READY,
            )
            LocalKernelState.Starting, LocalKernelState.Stopping -> base.copy(status = StepStatus.SKIPPED, skipReason = SkipReason.LOCAL_KERNEL_BUSY)
            else -> startAndRestore(previous, base)
        }
    }

    private suspend fun startAndRestore(previous: LocalKernelState, base: KernelSection): KernelSection {
        val startedAt = clock.elapsedMs()
        val outcome = control.start()
        if (outcome != KernelStartOutcome.REQUESTED) {
            return base.copy(status = StepStatus.DONE, startResult = outcome.name.lowercase(), outcome = NOT_STARTED)
        }
        val transitions = mutableListOf<StateTransition>()
        var section = base.copy(startResult = outcome.name.lowercase())
        try {
            var begun = false // the supervisor has taken up this start (StateFlow conflates, so Starting may be missed)
            val settled = withTimeoutOrNull(deadlineMs) {
                control.state.first { state ->
                    if (transitions.lastOrNull()?.state != stateName(state)) {
                        transitions += StateTransition(clock.elapsedMs() - startedAt, stateName(state))
                    }
                    when (state) {
                        is LocalKernelState.Ready -> true
                        LocalKernelState.Starting, LocalKernelState.Stopping -> { begun = true; false }
                        // A fresh Failed/SafeMode instance is this start's outcome; the one found before is not.
                        is LocalKernelState.Failed, is LocalKernelState.SafeMode -> state !== previous
                        LocalKernelState.Stopped -> begun
                    }
                }
            }
            section = when (settled) {
                null -> section.copy(outcome = TIMED_OUT)
                is LocalKernelState.Ready -> section.copy(
                    outcome = READY,
                    timeToReadyMs = transitions.last().atMs,
                    probes = probe(settled.endpoint),
                )
                is LocalKernelState.Failed -> section.copy(outcome = FAILED, failure = settled.failure.toString())
                is LocalKernelState.SafeMode -> section.copy(outcome = SAFE_MODE, failure = settled.toString())
                else -> section.copy(outcome = STOPPED)
            }
        } finally {
            // Restore even when the run is cancelled: a kernel this check started never outlives it.
            withContext(NonCancellable) {
                section = section.copy(transitions = transitions.toList())
                val stopFrom = clock.elapsedMs()
                control.stop()
                val stopped = withTimeoutOrNull(deadlineMs) { control.state.first { it.isAtRest() } }
                section = section.copy(
                    restoredTo = stateName(stopped ?: control.state.value),
                    stopDurationMs = clock.elapsedMs() - stopFrom,
                )
            }
        }
        return section.copy(status = StepStatus.DONE)
    }

    private fun probe(endpoint: LocalKernelEndpoint): List<HttpResult> {
        val origin = "http://${endpoint.host}:${endpoint.port}"
        val meta = http.get(origin + ApiV4Contract.META_PATH, bearer = null, readBody = true)
        val apiMajor = meta.body?.let { API_MAJOR.find(it) }?.groupValues?.get(1)?.toIntOrNull()
        val results = mutableListOf(
            HttpResult(
                path = ApiV4Contract.META_PATH,
                credential = false,
                expectedStatus = 200,
                status = meta.status,
                durationMs = meta.durationMs,
                error = meta.error,
                apiMajor = apiMajor,
                serverBuild = meta.body?.let { SERVER_BUILD.find(it) }?.groupValues?.get(1),
                compatible = if (meta.status == 200 && apiMajor != null) ApiMetaGate.incompatibilityReason(200, meta.body, appVersionCode) == null else null,
            ),
        )
        val anonymous = http.get(origin + AUTHENTICATED_READ, bearer = null, readBody = false)
        results += HttpResult(AUTHENTICATED_READ, credential = false, expectedStatus = 401, status = anonymous.status, durationMs = anonymous.durationMs, error = anonymous.error)
        val secret = try {
            control.secret().also { loadedSecret = it }
        } catch (error: Exception) {
            results += HttpResult(AUTHENTICATED_READ, credential = true, expectedStatus = 200, status = null, durationMs = 0, error = "secret unavailable: ${error.javaClass.simpleName}")
            return results
        }
        val authenticated = http.get(origin + AUTHENTICATED_READ, bearer = secret.reveal(), readBody = false)
        results += HttpResult(AUTHENTICATED_READ, credential = true, expectedStatus = 200, status = authenticated.status, durationMs = authenticated.durationMs, error = authenticated.error)
        return results
    }

    companion object {
        /**
         * The cheapest read behind the local kernel's default deny: `@require_auth`
         * over a static role registry (routes/api_v1/swarm.py; mounted per
         * contracts/api_endpoint_index_v1.json). `keys/whoami`, `users/me` and
         * `auth/mode` answer without a credential, so they prove nothing.
         */
        const val AUTHENTICATED_READ = "/api/v4/swarm/config"

        /**
         * The self-check's liveness bound for each wait of this step
         * (android/README.md "Device self-check"): a first start installs the
         * runtime from the APK before the server boots.
         */
        const val DEADLINE_MS = 15 * 60_000L

        const val READY = "ready"
        const val FAILED = "failed"
        const val SAFE_MODE = "safe_mode"
        const val STOPPED = "stopped"
        const val TIMED_OUT = "timed_out"
        const val NOT_STARTED = "not_started"
        const val ALREADY_RUNNING = "already_running"

        private val API_MAJOR = Regex("\"apiMajor\"\\s*:\\s*(\\d+)")
        private val SERVER_BUILD = Regex("\"serverBuild\"\\s*:\\s*\"([A-Za-z0-9._+-]{1,64})\"")

        fun stateName(state: LocalKernelState): String = when (state) {
            LocalKernelState.Stopped -> "stopped"
            LocalKernelState.Starting -> "starting"
            is LocalKernelState.Ready -> READY
            LocalKernelState.Stopping -> "stopping"
            is LocalKernelState.Failed -> FAILED
            is LocalKernelState.SafeMode -> SAFE_MODE
        }

        /** Not running and not about to: the state the self-check may restore to or leave. */
        fun LocalKernelState.isAtRest(): Boolean =
            this is LocalKernelState.Stopped || this is LocalKernelState.Failed || this is LocalKernelState.SafeMode
    }
}
