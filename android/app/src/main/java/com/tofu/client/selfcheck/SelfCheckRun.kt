package com.tofu.client.selfcheck

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

interface SelfCheckClock {
    /** Wall clock (ms since epoch). */
    fun wallMs(): Long
    /** `SystemClock.elapsedRealtime()`: includes deep sleep, so phases keep their duration with the screen off. */
    fun elapsedMs(): Long
    /** `SystemClock.uptimeMillis()`: the difference from [elapsedMs] is time spent in deep sleep. */
    fun uptimeMs(): Long
}

data class RunUiState(
    val step: String = "idle",
    val detail: String? = null,
    val elapsedMs: Long = 0L,
    val running: Boolean = false,
    val waitingForBackground: Boolean = false,
    val report: SelfCheckReport? = null,
)

/** Android-free services of one run, so the coordinator runs on the plain JVM with fakes. */
interface SelfCheckEnvironment {
    val clock: SelfCheckClock
    val store: SelfCheckStore
    fun facts(): DeviceFacts
    fun runtime(): RuntimeProbe
    fun runner(): CommandRunner
    fun kernel(): KernelControl
    fun http(): HttpProbe
    fun stressHost(): StressHost
    /** The app's own historical exit records (API 30+); empty otherwise or when unreadable. */
    fun exitReasons(): List<ExitReasonRecord>
    /** A kernel started by the adapter lane while the stress test runs would mix its processes with the test's. */
    fun kernelActive(): Boolean
    /** `ActivityManager.getMyMemoryState` importance <= IMPORTANCE_FOREGROUND. */
    fun inForeground(): Boolean
    fun newReportId(): String
    fun nextPollMs(): Long = ChildStress.SAMPLE_INTERVAL_MS
}

/** Persistence of the run: the report itself, a heartbeat and the pids that must not outlive it. */
interface SelfCheckStore {
    fun writeReport(report: SelfCheckReport)
    fun writeInterrupted(reportId: String, detectedAtWallMs: Long, lastHeartbeatWallMs: Long?, runStartedAtWallMs: Long?, exitReasons: List<ExitReasonRecord>, previousReportJson: String?)
    fun writeHeartbeat(wallMs: Long)
    fun writePids(pids: List<Int>)
    fun pids(): List<Int>
    fun heartbeat(): Long?
    fun previousReportJson(): String?
    /** True when a persisted run was left unfinished by the previous process. */
    fun interruptedRun(): Boolean
    fun clear()
}

/**
 * Drives one run: facts → proot's `--version` → the kernel check (whose
 * runtime install also puts the rootfs in place) → the guest exec checks →
 * the foreground stress phase → the background one. The report is written
 * after every step and after every observed death, so "share" always has the
 * newest evidence and a process death leaves a report behind.
 *
 * When the runtime is not in this build the kernel check, the guest exec
 * checks and the stress test are skipped with
 * [SkipReason.RUNTIME_NOT_IN_THIS_BUILD]; device and phantom-setting facts
 * are still reported.
 */
class SelfCheckRun(
    private val environment: SelfCheckEnvironment,
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Default),
) {
    private val mutableState = MutableStateFlow(RunUiState())
    val state: StateFlow<RunUiState> = mutableState.asStateFlow()

    @Volatile private var job: Job? = null
    @Volatile private var cancelled = false
    @Volatile private var skipBackground = false

    fun start(minutes: Int): Boolean {
        if (job?.isActive == true) return false
        cancelled = false
        skipBackground = false
        job = scope.launch { run(minutes) }
        return true
    }

    fun cancel() {
        cancelled = true
    }

    /** Finish the background phase now instead of waiting for the user to leave the app. */
    fun skipBackgroundPhase() {
        skipBackground = true
    }

    private suspend fun run(minutes: Int) {
        val env = environment
        val clock = env.clock
        val startedAtWall = clock.wallMs()
        var report = SelfCheckReport(reportId = env.newReportId(), status = RunStatus.RUNNING, startedAtWallMs = startedAtWall, presetMinutes = minutes)
        val startedAt = clock.elapsedMs()

        fun publish(step: String, detail: String? = null, waiting: Boolean = false) {
            mutableState.value = RunUiState(step, detail, clock.elapsedMs() - startedAt, running = true, waitingForBackground = waiting, report = report)
        }

        fun persist() {
            env.store.writeReport(report)
        }

        try {
            publish("facts")
            val facts = runCatching { env.facts() }.getOrNull()
            report = report.copy(facts = facts)
            env.store.clear()
            persist()
            val runtime = env.runtime()
            val runner = env.runner()
            val missing = runtime.missing()

            val execChecks = mutableListOf(ExecChecks.prootVersion(runtime, runner))
            publish("exec", "proot --version")

            val kernelSection = if (missing != null) {
                KernelSection(status = StepStatus.SKIPPED, skipReason = SkipReason.RUNTIME_NOT_IN_THIS_BUILD)
            } else {
                publish("kernel", "starting the local kernel")
                KernelCheck(env.kernel(), env.http(), clock, facts?.appVersionCode ?: Int.MAX_VALUE)
                    .run(firstInstall = !runtime.rootfsInstalled())
                    .let { if (cancelled) it.copy(status = StepStatus.CANCELLED) else it }
            }
            report = report.copy(kernel = kernelSection)
            persist()
            if (cancelled) throw CancellationException("cancelled")

            val rootfsMissing = !runtime.rootfsInstalled()
            report = report.copy(exec = when {
                missing != null -> ExecSection(
                    status = StepStatus.SKIPPED,
                    skipReason = SkipReason.RUNTIME_NOT_IN_THIS_BUILD,
                    checks = execChecks + listOf(ExecChecks.PROOT_TRUE, ExecChecks.PROOT_SH, ExecChecks.PROOT_PYTHON, ExecChecks.PROOT_GIT, ExecChecks.DIRECT_EXEC)
                        .map { ExecChecks.skipped(it, emptyList(), "", SkipReason.RUNTIME_NOT_IN_THIS_BUILD) },
                )
                rootfsMissing -> ExecSection(
                    status = StepStatus.DONE,
                    skipReason = SkipReason.ROOTFS_NOT_INSTALLED,
                    checks = execChecks + ExecChecks.skipped("rootfs", emptyList(), "an extracted rootfs (installed by the kernel check)", SkipReason.ROOTFS_NOT_INSTALLED),
                )
                else -> {
                    publish("exec", "guest programs under proot")
                    val (prefix, checks) = ExecChecks.underProot(runtime, runner)
                    execChecks += checks
                    execChecks += ExecChecks.directExec(runtime, runner, targetSdk = facts?.targetSdk ?: ExecChecks.W_X_TARGET_SDK)
                    ExecSection(status = StepStatus.DONE, prootCommand = prefix, checks = execChecks)
                }
            })
            persist()

            report = report.copy(stress = stress(minutes, runtime, missing, rootfsMissing, kernelSection, ::publish) {
                report = report.copy(stress = it)
                persist()
            })
            finish(report, RunStatus.FINISHED)
        } catch (cancelledRun: CancellationException) {
            finish(report, RunStatus.CANCELLED)
        } catch (error: Exception) {
            // A step that threw still leaves a shareable report; the failure is recorded in it.
            finish(report.copy(stress = report.stress.copy(status = StepStatus.DONE, skipReason = "${error.javaClass.simpleName}: ${error.message}")), RunStatus.FINISHED)
        } finally {
            env.store.clear()
        }
    }

    private fun finish(report: SelfCheckReport, status: RunStatus) {
        val finished = report.copy(status = status, finishedAtWallMs = environment.clock.wallMs(), exitReasons = runCatching { environment.exitReasons() }.getOrDefault(emptyList()))
        environment.store.writeReport(finished)
        mutableState.value = RunUiState(step = "done", running = false, report = finished)
    }

    private suspend fun stress(
        minutes: Int,
        runtime: RuntimeProbe,
        missing: String?,
        rootfsMissing: Boolean,
        kernel: KernelSection,
        publish: (String, String?, Boolean) -> Unit,
        update: (StressSection) -> Unit,
    ): StressSection {
        val env = environment
        val plan = ChildStress.plan(minutes)
        val skip = when {
            missing != null -> SkipReason.RUNTIME_NOT_IN_THIS_BUILD
            rootfsMissing -> SkipReason.ROOTFS_NOT_INSTALLED
            kernel.previousState == KernelCheck.READY -> SkipReason.LOCAL_KERNEL_ACTIVE
            env.kernelActive() -> SkipReason.LOCAL_KERNEL_ACTIVE
            else -> null
        }
        if (skip != null) return StressSection(status = StepStatus.SKIPPED, skipReason = skip, plan = plan)

        val full = ExecChecks.prootPrefix(runtime, runtime.payloadDirectory())
        // No --kill-on-exit here: it would make proot kill every guest when the
        // orchestrator dies, and those deaths would be proot's, not the system's.
        val prefix = full.copy(argv = full.argv.filter { it != "--kill-on-exit" })
        val stress = ChildStress(env.stressHost(), env.clock, prefix)
        val phases = mutableListOf<StressPhase>()
        var lastDeaths = -1

        for (name in listOf(StressPhase.FOREGROUND, StressPhase.BACKGROUND)) {
            if (cancelled) break
            if (name == StressPhase.BACKGROUND) {
                val skipNow = when {
                    skipBackground -> SkipReason.SKIPPED_BY_USER
                    env.kernelActive() -> SkipReason.LOCAL_KERNEL_ACTIVE
                    else -> null
                }
                if (skipNow != null) {
                    phases += StressPhase(phase = name, status = StepStatus.SKIPPED, skipReason = skipNow)
                    break
                }
                while (env.inForeground() && !skipBackground && !cancelled) {
                    publish("stress", "press Home to leave the app; the phase starts once the app is in the background", true)
                    delay(env.nextPollMs())
                }
                if (skipBackground || cancelled) {
                    phases += StressPhase(phase = name, status = if (cancelled) StepStatus.CANCELLED else StepStatus.SKIPPED, skipReason = if (cancelled) SkipReason.CANCELLED else SkipReason.SKIPPED_BY_USER)
                    break
                }
            }
            publish("stress", "$name phase: ${plan.sleepers} sleepers + ${plan.busyWorkers} busy workers for $minutes min", false)
            val phase = stress.run(
                phaseName = name,
                plan = plan,
                cancelled = { cancelled },
                kernelActive = { env.kernelActive() },
                onTick = { current ->
                    publish("stress", "$name phase: ${current.deaths.size} process deaths, ${current.samples} samples", false)
                    if (current.deaths.size != lastDeaths) {
                        lastDeaths = current.deaths.size
                        update(StressSection(status = StepStatus.RUNNING, plan = plan, phases = phases + current))
                    }
                },
            )
            phases += phase
            update(StressSection(status = StepStatus.RUNNING, plan = plan, phases = phases.toList()))
            if (phase.endReason == ChildStress.CANCELLED || phase.endReason == ChildStress.LAUNCH_FAILED) break
        }
        return StressSection(status = if (cancelled) StepStatus.CANCELLED else StepStatus.DONE, plan = plan, phases = phases.toList())
    }
}
