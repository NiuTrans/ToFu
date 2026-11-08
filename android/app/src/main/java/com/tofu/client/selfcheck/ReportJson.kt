package com.tofu.client.selfcheck

import java.time.Instant

/**
 * JSON form of a [SelfCheckReport] (schema [SELFCHECK_VERSION]) plus the
 * derived `summary` that maps a run onto the S1 questions in
 * docs/ANDROID_KERNELS.md. Names of Android constants are taken from AOSP
 * android14-release: `ApplicationExitInfo.REASON_*`,
 * `ActivityManager.RunningAppProcessInfo.IMPORTANCE_*` and
 * `UsageStatsManager.STANDBY_BUCKET_*`.
 */
internal object ReportJson {
    fun of(report: SelfCheckReport): Json.Obj = obj(
        "selfcheckVersion" to SELFCHECK_VERSION,
        "reportId" to report.reportId,
        "status" to report.status.name.lowercase(),
        "startedAt" to iso(report.startedAtWallMs),
        "finishedAt" to report.finishedAtWallMs?.let(::iso),
        "presetMinutes" to report.presetMinutes,
        "summary" to summary(report),
        "facts" to report.facts?.let(::facts),
        "exec" to exec(report.exec),
        "kernel" to kernel(report.kernel),
        "stress" to stress(report.stress),
        "exitReasons" to report.exitReasons.map(::exitReason),
    )

    /**
     * A run that ended with the app process: the last report it persisted,
     * embedded verbatim, and the app's exit records since that run began
     * (the system's own account of why the process ended).
     */
    fun interrupted(reportId: String, detectedAtWallMs: Long, lastHeartbeatWallMs: Long?, runStartedAtWallMs: Long?, exitReasons: List<ExitReasonRecord>, previousReportJson: String?): Json.Obj {
        val since = runStartedAtWallMs?.let { start -> exitReasons.filter { it.timestampMs >= start } }.orEmpty()
        return obj(
            "selfcheckVersion" to SELFCHECK_VERSION,
            "reportId" to reportId,
            "status" to RunStatus.INTERRUPTED.name.lowercase(),
            "detectedAt" to iso(detectedAtWallMs),
            "lastHeartbeatAt" to lastHeartbeatWallMs?.let(::iso),
            "summary" to obj(
                "appProcessExit" to since.minByOrNull { it.timestampMs }?.let(::exitReason),
            ),
            "exitReasons" to exitReasons.map(::exitReason),
            "interruptedRun" to (previousReportJson?.let { Json.Raw(it) } ?: Json.Null),
        )
    }

    private fun summary(report: SelfCheckReport): Json.Obj {
        val underProot = report.exec.checks.filter { it.id in ExecChecks.UNDER_PROOT_IDS }
        val direct = report.exec.checks.firstOrNull { it.id == ExecChecks.DIRECT_EXEC }
        val execUnderProot = when {
            underProot.isEmpty() || underProot.all { it.pass == null } -> report.exec.status.name.lowercase()
            underProot.all { it.pass != false } -> "pass"
            else -> "fail"
        }
        return obj(
            "execUnderProot" to execUnderProot,
            "directExecDenied" to direct?.classification?.let { it == ExecChecks.DENIED },
            "pageSizeBytes" to report.facts?.pageSizeBytes,
            "kernelReady" to report.kernel.outcome?.let { it == "ready" },
            "timeToReadyMs" to report.kernel.timeToReadyMs,
            "stressPhases" to report.stress.phases.map { phase ->
                obj(
                    "phase" to phase.phase,
                    "status" to phase.status.name.lowercase(),
                    "deaths" to phase.deaths.size,
                    "firstVictim" to phase.deaths.minByOrNull { it.diedAtMs ?: Long.MAX_VALUE }?.kind,
                    "survivors" to phase.survivors,
                )
            },
        )
    }

    private fun facts(facts: DeviceFacts): Json.Obj = obj(
        "manufacturer" to facts.manufacturer,
        "model" to facts.model,
        "device" to facts.device,
        "release" to facts.release,
        "sdkInt" to facts.sdkInt,
        "securityPatch" to facts.securityPatch,
        "pageSizeBytes" to facts.pageSizeBytes,
        "clockTicksPerSecond" to facts.clockTicksPerSecond,
        "supportedAbis" to facts.supportedAbis,
        "kernelRelease" to facts.kernelRelease,
        "app" to obj(
            "versionName" to facts.appVersionName,
            "versionCode" to facts.appVersionCode,
            "buildId" to facts.appBuildId,
            "targetSdk" to facts.targetSdk,
        ),
        "freeStorageBytes" to facts.freeStorageBytes,
        "totalStorageBytes" to facts.totalStorageBytes,
        "totalRamBytes" to facts.totalRamBytes,
        "ignoringBatteryOptimizations" to facts.ignoringBatteryOptimizations,
        "standbyBucket" to facts.standbyBucket?.let { obj("value" to it, "name" to STANDBY_BUCKETS[it]) },
        "backgroundRestricted" to facts.backgroundRestricted,
        "postNotificationsGranted" to facts.postNotificationsGranted,
        "notificationsEnabled" to facts.notificationsEnabled,
        "runtime" to obj(
            "inThisBuild" to facts.runtime.inThisBuild,
            "missing" to facts.runtime.missing,
            "rootfsVersion" to facts.runtime.rootfsVersion,
            "payloadVersion" to facts.runtime.payloadVersion,
            "indexError" to facts.runtime.indexError,
            "nativeLibraries" to facts.runtime.nativeLibraries.map {
                obj("name" to it.name, "present" to it.present, "sizeBytes" to it.sizeBytes, "sha256" to it.sha256)
            },
            "installedRootfsVersion" to facts.runtime.installedRootfsVersion,
            "currentPayload" to facts.runtime.currentPayload,
        ),
        "phantomProcesses" to obj(
            "monitorFlagGlobal" to setting(facts.phantomProcesses.monitorFlagGlobal),
            "monitorFlagProperty" to setting(facts.phantomProcesses.monitorFlagProperty),
            "maxPhantomProcesses" to setting(facts.phantomProcesses.maxPhantomProcesses),
        ),
        "unreadable" to Json.Obj(facts.unreadable.entries.sortedBy { it.key }.map { it.key to Json.Str(it.value) }),
    )

    private fun setting(read: SettingRead): Json.Obj =
        obj("key" to read.key, "readable" to read.readable, "value" to read.value, "note" to read.note)

    private fun exec(section: ExecSection): Json.Obj = obj(
        "status" to section.status.name.lowercase(),
        "skipReason" to section.skipReason,
        "prootCommand" to section.prootCommand,
        "checks" to section.checks.map { check ->
            obj(
                "id" to check.id,
                "argv" to check.argv,
                "expectation" to check.expectation,
                "pass" to check.pass,
                "outcome" to check.outcome?.name?.lowercase(),
                "exitStatus" to check.exitStatus,
                "signal" to check.signal,
                "signalName" to check.signal?.let(::signalName),
                "durationMs" to check.durationMs,
                "stdout" to check.stdout,
                "stderr" to check.stderr,
                "error" to check.error,
                "classification" to check.classification,
                "skipReason" to check.skipReason,
            )
        },
    )

    private fun kernel(section: KernelSection): Json.Obj = obj(
        "status" to section.status.name.lowercase(),
        "skipReason" to section.skipReason,
        "previousState" to section.previousState,
        "startResult" to section.startResult,
        "firstInstall" to section.firstInstall,
        "transitions" to section.transitions.map { obj("atMs" to it.atMs, "state" to it.state) },
        "timeToReadyMs" to section.timeToReadyMs,
        "outcome" to section.outcome,
        "failure" to section.failure,
        "probes" to section.probes.map { probe ->
            obj(
                "path" to probe.path,
                "credential" to probe.credential,
                "expectedStatus" to probe.expectedStatus,
                "status" to probe.status,
                "pass" to probe.pass,
                "durationMs" to probe.durationMs,
                "error" to probe.error,
                "apiMajor" to probe.apiMajor,
                "serverBuild" to probe.serverBuild,
                "compatible" to probe.compatible,
            )
        },
        "restoredTo" to section.restoredTo,
        "stopDurationMs" to section.stopDurationMs,
    )

    private fun stress(section: StressSection): Json.Obj = obj(
        "status" to section.status.name.lowercase(),
        "skipReason" to section.skipReason,
        "plan" to section.plan?.let {
            obj(
                "sleepers" to it.sleepers,
                "busyWorkers" to it.busyWorkers,
                "phaseDurationMs" to it.phaseDurationMs,
                "sampleIntervalMs" to it.sampleIntervalMs,
                "phantomProcessCap" to it.phantomProcessCap,
            )
        },
        "phases" to section.phases.map { phase ->
            obj(
                "phase" to phase.phase,
                "status" to phase.status.name.lowercase(),
                "skipReason" to phase.skipReason,
                "endReason" to phase.endReason,
                "error" to phase.error,
                "startedAtMs" to phase.startedAtMs,
                "readyAtMs" to phase.readyAtMs,
                "endedAtMs" to phase.endedAtMs,
                "deepSleepMs" to phase.deepSleepMs,
                "samples" to obj(
                    "total" to phase.samples,
                    "foreground" to phase.foregroundSamples,
                    "foregroundService" to phase.foregroundServiceSamples,
                    "otherImportance" to phase.otherImportanceSamples,
                    "screenOn" to phase.screenOnSamples,
                    "deviceIdle" to phase.deviceIdleSamples,
                ),
                "deaths" to phase.deaths.size,
                "survivors" to phase.survivors,
                "processes" to phase.processes.map { process ->
                    obj(
                        "kind" to process.kind,
                        "pid" to process.pid,
                        "spawnedAtMs" to process.spawnedAtMs,
                        "diedAtMs" to process.diedAtMs,
                        "signal" to process.signal,
                        "signalName" to process.signal?.let(::signalName),
                        "exitStatus" to process.exitStatus,
                        "deathSource" to process.deathSource,
                        "cpuTicks" to process.cpuTicks,
                    )
                },
                "prootExitStatus" to phase.prootExitStatus,
                "orchestratorStderr" to phase.orchestratorStderr,
                "cleanup" to phase.cleanup?.let { obj("signalled" to it.signalled, "leftovers" to it.leftovers, "durationMs" to it.durationMs) },
            )
        },
    )

    private fun exitReason(record: ExitReasonRecord): Json.Obj = obj(
        "at" to iso(record.timestampMs),
        "pid" to record.pid,
        "processName" to record.processName,
        "reason" to record.reason,
        "reasonName" to EXIT_REASONS[record.reason],
        "status" to record.status,
        "importance" to record.importance,
        "importanceName" to IMPORTANCES[record.importance],
        "pssKb" to record.pssKb,
        "rssKb" to record.rssKb,
        "description" to record.description,
    )

    fun iso(wallMs: Long): String = Instant.ofEpochMilli(wallMs).toString()

    fun signalName(signal: Int): String? = SIGNALS[signal]

    /** Linux signal numbers (asm-generic, which arm64 uses). */
    private val SIGNALS = mapOf(
        1 to "SIGHUP", 2 to "SIGINT", 3 to "SIGQUIT", 4 to "SIGILL", 5 to "SIGTRAP", 6 to "SIGABRT", 7 to "SIGBUS",
        8 to "SIGFPE", 9 to "SIGKILL", 10 to "SIGUSR1", 11 to "SIGSEGV", 12 to "SIGUSR2", 13 to "SIGPIPE",
        14 to "SIGALRM", 15 to "SIGTERM",
    )

    /** `ApplicationExitInfo.REASON_*`. */
    val EXIT_REASONS = mapOf(
        0 to "unknown", 1 to "exit_self", 2 to "signaled", 3 to "low_memory", 4 to "crash", 5 to "crash_native",
        6 to "anr", 7 to "initialization_failure", 8 to "permission_change", 9 to "excessive_resource_usage",
        10 to "user_requested", 11 to "user_stopped", 12 to "dependency_died", 13 to "other", 14 to "freezer",
        15 to "package_state_change", 16 to "package_updated",
    )

    /** `RunningAppProcessInfo.IMPORTANCE_*`. */
    val IMPORTANCES = mapOf(
        100 to "foreground", 125 to "foreground_service", 200 to "visible", 230 to "perceptible", 300 to "service",
        325 to "top_sleeping", 350 to "cant_save_state", 400 to "cached", 500 to "empty", 1000 to "gone",
    )

    /** `UsageStatsManager.STANDBY_BUCKET_*`. */
    val STANDBY_BUCKETS = mapOf(
        5 to "exempted", 10 to "active", 20 to "working_set", 30 to "frequent", 40 to "rare", 45 to "restricted", 50 to "never",
    )
}
