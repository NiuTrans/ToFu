package com.tofu.client.selfcheck

/**
 * The human-readable summary screen: the same facts as the JSON, one short
 * line per check, with the S1 reading of each (docs/ANDROID_KERNELS.md).
 * Pure Kotlin so the pure-JVM tier can pin its wording.
 */
object SelfCheckSummary {
    fun lines(report: SelfCheckReport): List<String> {
        val lines = mutableListOf<String>()
        val facts = report.facts
        if (facts != null) {
            lines += "${facts.manufacturer.orUnknown()} ${facts.model.orUnknown()} (${facts.device.orUnknown()})"
            lines += "Android ${facts.release.orUnknown()} (SDK ${facts.sdkInt}), patch ${facts.securityPatch.orUnknown()}, targetSdk ${facts.targetSdk}"
            lines += "Page size ${facts.pageSizeBytes?.toString().orUnknown()} bytes; ABIs ${facts.supportedAbis.joinToString()}; kernel ${facts.kernelRelease.orUnknown()}"
            lines += "App ${facts.appVersionName} (${facts.appVersionCode}), build ${facts.appBuildId}"
            lines += "Storage free ${mb(facts.freeStorageBytes)}, RAM total ${mb(facts.totalRamBytes)}"
            lines += "Battery-optimization exempt ${facts.ignoringBatteryOptimizations}, standby bucket ${ReportJson.STANDBY_BUCKETS[facts.standbyBucket] ?: facts.standbyBucket}, background restricted ${facts.backgroundRestricted}"
            lines += "POST_NOTIFICATIONS granted ${facts.postNotificationsGranted}, notifications enabled ${facts.notificationsEnabled}"
            lines += if (facts.runtime.inThisBuild) {
                "Runtime in this build: rootfs ${facts.runtime.rootfsVersion.orUnknown()}, payload ${facts.runtime.payloadVersion.orUnknown()}"
            } else {
                "Runtime not in this build: ${facts.runtime.missing.orUnknown()} — exec, kernel and stress checks skipped"
            }
            facts.runtime.nativeLibraries.forEach { library ->
                lines += "${library.name}: ${if (library.present) "sha256 ${library.sha256.orUnknown()}" else "missing"}"
            }
            val flag = facts.phantomProcesses.monitorFlagGlobal
            lines += "Phantom-process switch: Settings.Global ${flag.key} = ${flag.value ?: "unset"}; system property and cap not readable"
        }

        report.exec.checks.forEach { check ->
            lines += when {
                check.skipReason != null -> "${check.id}: skipped (${check.skipReason})"
                check.id == ExecChecks.DIRECT_EXEC -> "${check.id}: ${check.classification} (expected ${check.expectation}); ${check.error ?: "exit ${check.exitStatus}"}; ${check.durationMs} ms"
                else -> "${check.id}: ${passWord(check.pass)} exit=${check.exitStatus} in ${check.durationMs} ms${check.error?.let { " — $it" }.orEmpty()}"
            }
        }

        val kernel = report.kernel
        lines += when {
            kernel.skipReason != null -> "Kernel: skipped (${kernel.skipReason})"
            kernel.outcome == KernelCheck.READY -> "Kernel: ready in ${kernel.timeToReadyMs} ms (first install ${kernel.firstInstall}); restored to ${kernel.restoredTo}"
            else -> "Kernel: ${kernel.outcome ?: kernel.status.name.lowercase()}${kernel.failure?.let { " — $it" }.orEmpty()}"
        }
        kernel.probes.forEach { probe ->
            lines += "  GET ${probe.path}${if (probe.credential) " (Bearer)" else ""}: ${probe.status ?: probe.error} (expected ${probe.expectedStatus}) in ${probe.durationMs} ms"
        }

        val stress = report.stress
        if (stress.skipReason != null && stress.phases.isEmpty()) {
            lines += "Child-process stress: skipped (${stress.skipReason})"
        }
        stress.phases.forEach { phase ->
            lines += if (phase.skipReason != null && phase.processes.isEmpty()) {
                "Stress ${phase.phase}: skipped (${phase.skipReason})"
            } else {
                "Stress ${phase.phase}: ${phase.deaths.size} of ${phase.processes.size} processes died before cleanup (${phase.endReason}); ${phase.survivors} alive at cleanup; ${phase.samples} samples, deep sleep ${phase.deepSleepMs} ms"
            }
            phase.deaths.sortedBy { it.diedAtMs }.take(FIRST_DEATHS).forEach { death ->
                lines += "  ${death.kind} pid ${death.pid} died at ${death.diedAtMs} ms${death.signal?.let { " signal $it (${ReportJson.signalName(it)})" }.orEmpty()} (${death.deathSource})"
            }
            phase.cleanup?.let { cleanup ->
                lines += "  cleanup: ${cleanup.signalled} signalled, ${cleanup.leftovers} left alive, ${cleanup.durationMs} ms"
            }
        }
        report.exitReasons.forEach { record ->
            lines += "App exit ${ReportJson.iso(record.timestampMs)}: ${ReportJson.EXIT_REASONS[record.reason] ?: record.reason}, status ${record.status}, ${ReportJson.IMPORTANCES[record.importance] ?: record.importance}${record.description?.let { " — $it" }.orEmpty()}"
        }
        return lines
    }

    private const val FIRST_DEATHS = 8

    private fun passWord(pass: Boolean?): String = when (pass) {
        true -> "PASS"
        false -> "FAIL"
        null -> "not run"
    }

    private fun mb(bytes: Long?): String = bytes?.let { "${it / (1024 * 1024)} MB" }.orUnknown()

    private fun String?.orUnknown(): String = this?.takeIf { it.isNotBlank() } ?: "unknown"
}
