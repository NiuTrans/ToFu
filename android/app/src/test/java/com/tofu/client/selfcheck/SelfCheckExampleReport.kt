package com.tofu.client.selfcheck

import org.junit.Test
import java.io.File

/** Not a test: renders the deliverable's example report to a file (run with -Dselfcheck.example.out=…). */
class SelfCheckExampleReport {
    @Test
    fun render() {
        val out = System.getProperty("selfcheck.example.out") ?: System.getenv("SELFCHECK_EXAMPLE_OUT") ?: return
        val runtime = FakeRuntime(selfCheckTempDir())
        val facts = deviceFacts(runtime)
        val exec = ExecSection(
            status = StepStatus.DONE,
            prootCommand = listOf("/data/app/~~abc/com.tofu.client-xyz/lib/arm64/libproot.so", "--kill-on-exit", "--link2symlink", "--root-id", "--rootfs=/data/user/0/com.tofu.client/files/kernel/rootfs", "…"),
            checks = listOf(
                ExecResult(ExecChecks.PROOT_VERSION, listOf("/data/app/…/lib/arm64/libproot.so", "--version"), "exit 0 and proot's version banner", pass = true, outcome = ExecOutcome.EXITED, exitStatus = 0, durationMs = 41, stdout = "proot 5.1.107.102\nbuilt-in accelerators: process_vm = yes, seccomp_filter = yes"),
                ExecResult(ExecChecks.PROOT_TRUE, listOf("/bin/true"), "exit 0", pass = true, outcome = ExecOutcome.EXITED, exitStatus = 0, durationMs = 183),
                ExecResult(ExecChecks.PROOT_SH, listOf("/bin/sh", "-c", "echo ok"), "exit 0 and stdout ok", pass = true, outcome = ExecOutcome.EXITED, exitStatus = 0, durationMs = 205, stdout = "ok"),
                ExecResult(ExecChecks.PROOT_PYTHON, listOf(ExecChecks.PAYLOAD_PYTHON, "-V"), "exit 0 and stdout Python 3.x", pass = true, outcome = ExecOutcome.EXITED, exitStatus = 0, durationMs = 260, stdout = "Python 3.12.8"),
                ExecResult(ExecChecks.PROOT_GIT, listOf(ExecChecks.ROOTFS_GIT, "--version"), "exit 0 and stdout git version …", pass = true, outcome = ExecOutcome.EXITED, exitStatus = 0, durationMs = 240, stdout = "git version 2.43.0"),
                ExecResult(ExecChecks.DIRECT_EXEC, listOf("/data/user/0/com.tofu.client/files/kernel/rootfs/usr/bin/true"), "denied (W^X: no exec from app data at targetSdk >= 29)", pass = true, outcome = ExecOutcome.START_FAILED, durationMs = 2, error = "Cannot run program \"/data/user/0/com.tofu.client/files/kernel/rootfs/usr/bin/true\": error=13, Permission denied", classification = ExecChecks.DENIED),
            ),
        )
        val kernel = KernelSection(
            status = StepStatus.DONE,
            previousState = "stopped",
            startResult = "requested",
            firstInstall = true,
            transitions = listOf(StateTransition(0, "starting"), StateTransition(96_400, "ready")),
            timeToReadyMs = 96_400,
            outcome = KernelCheck.READY,
            probes = listOf(
                HttpResult("/api/v4/meta", false, 200, 200, 9, null, apiMajor = 4, serverBuild = "0.16.0", compatible = true),
                HttpResult(KernelCheck.AUTHENTICATED_READ, false, 401, 401, 7, null),
                HttpResult(KernelCheck.AUTHENTICATED_READ, true, 200, 200, 8, null),
            ),
            restoredTo = "stopped",
            stopDurationMs = 1_200,
        )
        val stress = StressSection(
            status = StepStatus.DONE,
            plan = ChildStress.plan(5),
            phases = listOf(
                StressPhase(
                    phase = StressPhase.FOREGROUND,
                    status = StepStatus.DONE,
                    endReason = ChildStress.COMPLETED,
                    startedAtMs = 0,
                    readyAtMs = 120,
                    endedAtMs = 301_400,
                    deepSleepMs = 0,
                    samples = 300,
                    foregroundSamples = 300,
                    processes = listOf(
                        StressProcess("proot", 21_430, 0, diedAtMs = 4_800, signal = 9, deathSource = "proot-wait"),
                        StressProcess("orchestrator", 21_431, 40, diedAtMs = 4_900, deathSource = "proc"),
                        StressProcess("sleeper", 21_432, 60, diedAtMs = 301_400, deathSource = StressPhase.CLEANUP),
                        StressProcess("busy", 21_464, 110, diedAtMs = 301_400, deathSource = StressPhase.CLEANUP),
                    ),
                    prootExitStatus = 137,
                    cleanup = CleanupResult(signalled = 33, leftovers = 0, durationMs = 220),
                ),
                StressPhase(
                    phase = StressPhase.BACKGROUND,
                    status = StepStatus.DONE,
                    endReason = ChildStress.COMPLETED,
                    startedAtMs = 302_000,
                    readyAtMs = 302_140,
                    endedAtMs = 603_900,
                    deepSleepMs = 121_000,
                    samples = 180,
                    foregroundServiceSamples = 180,
                    deviceIdleSamples = 44,
                    processes = listOf(StressProcess("proot", 22_010, 0), StressProcess("busy", 22_044, 120)),
                    cleanup = CleanupResult(signalled = 36, leftovers = 0, durationMs = 240),
                ),
            ),
        )
        val report = SelfCheckReport(
            reportId = "s1example01",
            status = RunStatus.FINISHED,
            startedAtWallMs = 1_762_000_000_000L,
            finishedAtWallMs = 1_762_000_610_000L,
            presetMinutes = 5,
            facts = facts,
            exec = exec,
            kernel = kernel,
            stress = stress,
            exitReasons = listOf(ExitReasonRecord(1_761_999_000_000L, 20_100, "com.tofu.client", 9, 0, 400, 0, 0, "Caused by child process: excessive cpu 9123 during 5000 dur=600000 limit=25")),
        )
        File(out).writeText(ReportJson.of(report).render(SelfCheckRedactor()))
        println("wrote $out")
    }
}
