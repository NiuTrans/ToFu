package com.tofu.client.selfcheck

/**
 * The device self-check report: schema [SELFCHECK_VERSION], one JSON document
 * per run (android/README.md "Device self-check"). Field names are the
 * contract; a change that removes or renames a field, or changes its meaning,
 * bumps [SELFCHECK_VERSION]. Durations and `…AtMs` offsets are milliseconds of
 * `SystemClock.elapsedRealtime()` (deep sleep included); `…At` fields are
 * ISO-8601 UTC wall-clock times.
 */
const val SELFCHECK_VERSION = 1

enum class RunStatus { RUNNING, FINISHED, CANCELLED, INTERRUPTED }

/** A step's own progress; whether its checks passed is recorded inside the step. */
enum class StepStatus { PENDING, RUNNING, DONE, SKIPPED, CANCELLED }

/** Stable skip reasons (a report field, so never reworded). */
object SkipReason {
    const val RUNTIME_NOT_IN_THIS_BUILD = "runtime_not_in_this_build"
    const val PROOT_NOT_IN_THIS_BUILD = "proot_not_in_this_build"
    const val ROOTFS_NOT_INSTALLED = "rootfs_not_installed"
    const val PAYLOAD_NOT_INSTALLED = "payload_not_installed"
    const val LOCAL_KERNEL_ACTIVE = "local_kernel_active"
    const val LOCAL_KERNEL_BUSY = "local_kernel_busy"
    const val SKIPPED_BY_USER = "skipped_by_user"
    const val CANCELLED = "cancelled"
}

data class SettingRead(val key: String, val readable: Boolean, val value: String?, val note: String)

/** The phantom-process switches, each only as far as an app may read it (see [PhantomProcessSettings]). */
data class PhantomSettingFacts(
    val monitorFlagGlobal: SettingRead,
    val monitorFlagProperty: SettingRead,
    val maxPhantomProcesses: SettingRead,
)

data class NativeLibraryFact(val name: String, val present: Boolean, val sizeBytes: Long?, val sha256: String?)

data class RuntimeFacts(
    val inThisBuild: Boolean,
    val missing: String?,
    val rootfsVersion: String?,
    val payloadVersion: String?,
    val indexError: String?,
    val nativeLibraries: List<NativeLibraryFact>,
    val installedRootfsVersion: String?,
    val currentPayload: String?,
)

data class DeviceFacts(
    val manufacturer: String?,
    val model: String?,
    val device: String?,
    val release: String?,
    val sdkInt: Int,
    val securityPatch: String?,
    val pageSizeBytes: Long?,
    val clockTicksPerSecond: Long?,
    val supportedAbis: List<String>,
    val kernelRelease: String?,
    val appVersionName: String,
    val appVersionCode: Int,
    val appBuildId: String,
    val targetSdk: Int,
    val freeStorageBytes: Long?,
    val totalStorageBytes: Long?,
    val totalRamBytes: Long?,
    val ignoringBatteryOptimizations: Boolean?,
    val standbyBucket: Int?,
    val backgroundRestricted: Boolean?,
    val postNotificationsGranted: Boolean?,
    val notificationsEnabled: Boolean?,
    val runtime: RuntimeFacts,
    val phantomProcesses: PhantomSettingFacts,
    /** Facts that threw while being read: name → exception class. */
    val unreadable: Map<String, String> = emptyMap(),
)

enum class ExecOutcome { EXITED, START_FAILED, TIMED_OUT }

data class ExecResult(
    val id: String,
    val argv: List<String>,
    val expectation: String,
    val pass: Boolean?,
    val outcome: ExecOutcome?,
    val exitStatus: Int? = null,
    val signal: Int? = null,
    val durationMs: Long? = null,
    val stdout: String? = null,
    val stderr: String? = null,
    val error: String? = null,
    /** Direct exec only: `denied`, `permitted`, `permitted_interpreter_missing` or `error`. */
    val classification: String? = null,
    val skipReason: String? = null,
)

data class ExecSection(
    val status: StepStatus = StepStatus.PENDING,
    val skipReason: String? = null,
    /** The kernel's proot invocation up to its guest program, shared by every check under proot. */
    val prootCommand: List<String>? = null,
    val checks: List<ExecResult> = emptyList(),
)

data class HttpResult(
    val path: String,
    val credential: Boolean,
    val expectedStatus: Int,
    val status: Int?,
    val durationMs: Long,
    val error: String?,
    val apiMajor: Int? = null,
    val serverBuild: String? = null,
    /** [com.tofu.client.session.ApiMetaGate]'s verdict for this app build; meta only. */
    val compatible: Boolean? = null,
) {
    val pass: Boolean get() = status == expectedStatus
}

data class StateTransition(val atMs: Long, val state: String)

data class KernelSection(
    val status: StepStatus = StepStatus.PENDING,
    val skipReason: String? = null,
    val previousState: String? = null,
    val startResult: String? = null,
    val firstInstall: Boolean? = null,
    val transitions: List<StateTransition> = emptyList(),
    val timeToReadyMs: Long? = null,
    /** `ready`, `failed`, `safe_mode`, `stopped`, `timed_out` or `not_started`. */
    val outcome: String? = null,
    val failure: String? = null,
    val probes: List<HttpResult> = emptyList(),
    val restoredTo: String? = null,
    val stopDurationMs: Long? = null,
)

/** One process of a stress generation; times are offsets from the generation's launch. */
data class StressProcess(
    val kind: String,
    val pid: Int,
    val spawnedAtMs: Long,
    val diedAtMs: Long? = null,
    val signal: Int? = null,
    val exitStatus: Int? = null,
    /** `orchestrator` (its `wait`), `proot-wait` (the app's own wait), `proc` (seen gone in /proc) or `cleanup`. */
    val deathSource: String? = null,
    val cpuTicks: Long? = null,
)

data class CleanupResult(val signalled: Int, val leftovers: Int, val durationMs: Long)

data class StressPhase(
    val phase: String,
    val status: StepStatus = StepStatus.PENDING,
    val skipReason: String? = null,
    /** `completed`, `cancelled`, `local_kernel_started`, `all_processes_died`, `orchestrator_failed` or `launch_failed`. */
    val endReason: String? = null,
    val error: String? = null,
    val startedAtMs: Long? = null,
    val readyAtMs: Long? = null,
    val endedAtMs: Long? = null,
    val deepSleepMs: Long? = null,
    val samples: Int = 0,
    val foregroundSamples: Int = 0,
    val foregroundServiceSamples: Int = 0,
    val otherImportanceSamples: Int = 0,
    val screenOnSamples: Int = 0,
    val deviceIdleSamples: Int = 0,
    val processes: List<StressProcess> = emptyList(),
    val prootExitStatus: Int? = null,
    val orchestratorStderr: String? = null,
    val cleanup: CleanupResult? = null,
) {
    /** Deaths before cleanup began: the system's (or the device's) doing, not the self-check's. */
    val deaths: List<StressProcess> get() = processes.filter { it.diedAtMs != null && it.deathSource != CLEANUP }
    val survivors: Int get() = processes.count { it.diedAtMs == null || it.deathSource == CLEANUP }

    companion object {
        const val FOREGROUND = "foreground"
        const val BACKGROUND = "background"
        const val CLEANUP = "cleanup"
    }
}

data class StressPlan(
    val sleepers: Int,
    val busyWorkers: Int,
    val phaseDurationMs: Long,
    val sampleIntervalMs: Long,
    val phantomProcessCap: Int,
)

data class StressSection(
    val status: StepStatus = StepStatus.PENDING,
    val skipReason: String? = null,
    val plan: StressPlan? = null,
    val phases: List<StressPhase> = emptyList(),
)

data class ExitReasonRecord(
    val timestampMs: Long,
    val pid: Int,
    val processName: String,
    val reason: Int,
    val status: Int,
    val importance: Int,
    val pssKb: Long,
    val rssKb: Long,
    /** System-authored text only: crash and ANR descriptions (app text) are never kept. */
    val description: String?,
)

data class SelfCheckReport(
    val reportId: String,
    val status: RunStatus,
    val startedAtWallMs: Long,
    val finishedAtWallMs: Long? = null,
    val presetMinutes: Int,
    val facts: DeviceFacts? = null,
    val exec: ExecSection = ExecSection(),
    val kernel: KernelSection = KernelSection(),
    val stress: StressSection = StressSection(),
    val exitReasons: List<ExitReasonRecord> = emptyList(),
)
