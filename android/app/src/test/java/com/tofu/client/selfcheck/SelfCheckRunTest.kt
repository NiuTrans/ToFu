package com.tofu.client.selfcheck

import com.tofu.client.runtime.LocalKernelState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Run order, the runtime-missing skip paths, and the kernel check's restore semantics. */
class SelfCheckRunTest {
    private fun environment(missing: String? = null, rootfs: Boolean = true, payload: Boolean = true): FakeEnvironment {
        val runtime = FakeRuntime(selfCheckTempDir(), missingRuntime = missing, rootfs = rootfs, payload = payload)
        // A foreground app makes the background phase skip itself in [awaitBackground] (see runStressPhase).
        return FakeEnvironment(runtimeProbe = runtime).apply {
            foreground = false
            // A real start ends Ready (and, with no rootfs, the install fails) rather than Starting forever.
            kernelControl.settleState = kernelControl.ready()
        }
    }

    /** Wait for the run to settle, with a real-time bound so a stuck run fails instead of hanging the tier. */
    private suspend fun finish(run: SelfCheckRun, environment: FakeEnvironment): RunUiState =
        kotlinx.coroutines.withTimeout(60_000) {
            run.state.first { !it.running && it.report != null }
        }

    @Test
    fun runtime_not_in_this_build_skips_exec_b_c_kernel_and_stress() = runBlocking(Dispatchers.Default) {
        val env = environment(missing = "assets/kernel/index.json")
        val run = SelfCheckRun(env)
        run.start(0)
        val state = finish(run, env)
        val report = state.report!!
        assertEquals(RunStatus.FINISHED, report.status)
        assertEquals(StepStatus.SKIPPED, report.kernel.status)
        assertEquals(SkipReason.RUNTIME_NOT_IN_THIS_BUILD, report.kernel.skipReason)
        assertEquals(StepStatus.SKIPPED, report.exec.status)
        assertEquals(SkipReason.RUNTIME_NOT_IN_THIS_BUILD, report.exec.skipReason)
        assertEquals(StepStatus.SKIPPED, report.stress.status)
        assertEquals(SkipReason.RUNTIME_NOT_IN_THIS_BUILD, report.stress.skipReason)
        // Device facts and the phantom setting are still collected, and proot --version still runs.
        assertEquals("Google", report.facts?.manufacturer)
        assertTrue(report.exec.checks.any { it.id == ExecChecks.PROOT_VERSION })
        assertTrue(report.exec.checks.filter { it.id != ExecChecks.PROOT_VERSION }.all { it.skipReason == SkipReason.RUNTIME_NOT_IN_THIS_BUILD })
        assertTrue(env.kernelControl.state.value is LocalKernelState.Stopped)
        assertTrue(env.stress.launches.isEmpty())
        assertTrue(SelfCheckSummary.lines(report).any { it.contains("Runtime not in this build") })
    }

    @Test
    fun missing_rootfs_skips_guest_exec_and_stress_but_reports_why() = runBlocking(Dispatchers.Default) {
        val env = environment(rootfs = false)
        val run = SelfCheckRun(env)
        run.start(0)
        val report = finish(run, env).report!!
        assertEquals(RunStatus.FINISHED, report.status)
        assertEquals(SkipReason.ROOTFS_NOT_INSTALLED, report.exec.skipReason)
        assertEquals(SkipReason.ROOTFS_NOT_INSTALLED, report.stress.skipReason)
        assertTrue(report.exec.checks.any { it.id == "rootfs" })
    }

    @Test
    fun an_already_ready_kernel_is_probed_not_restarted_and_stress_is_skipped() = runBlocking(Dispatchers.Default) {
        val env = environment()
        env.kernelControl.publish(env.kernelControl.ready())
        val run = SelfCheckRun(env)
        run.start(0)
        val report = finish(run, env).report!!
        assertEquals(KernelCheck.ALREADY_RUNNING, report.kernel.startResult)
        assertEquals(KernelCheck.READY, report.kernel.outcome)
        assertNull(report.kernel.timeToReadyMs)
        assertTrue(env.kernelControl.state.value is LocalKernelState.Ready)
        assertEquals(SkipReason.LOCAL_KERNEL_ACTIVE, report.stress.skipReason)
        assertEquals(3, report.kernel.probes.size)
        assertEquals(200, report.kernel.probes[0].status)
        assertEquals(401, report.kernel.probes[1].status)
        assertEquals(true, report.kernel.probes[2].credential)
        assertTrue(env.httpProbe.calls.none { it.credential && it.url.contains("meta") })
    }

    @Test
    fun a_kernel_this_check_started_is_stopped_afterwards() = runBlocking(Dispatchers.Default) {
        val env = environment()
        val run = SelfCheckRun(env)
        run.start(0)
        val report = finish(run, env).report!!
        assertEquals(KernelCheck.READY, report.kernel.outcome)
        assertEquals(KernelCheck.STOPPED, report.kernel.restoredTo)
        assertTrue(report.kernel.timeToReadyMs != null)
        assertTrue(env.kernelControl.state.value is LocalKernelState.Stopped)
        assertEquals(1, report.kernel.probes.count { it.credential })
    }

    @Test
    fun a_starting_kernel_is_not_touched() = runBlocking(Dispatchers.Default) {
        val env = environment()
        env.kernelControl.publish(LocalKernelState.Starting)
        val run = SelfCheckRun(env)
        run.start(0)
        val report = finish(run, env).report!!
        assertEquals(SkipReason.LOCAL_KERNEL_BUSY, report.kernel.skipReason)
        assertTrue(env.kernelControl.state.value is LocalKernelState.Starting)
    }

    @Test
    fun direct_exec_classification_matches_w_x_expectations() {
        val denied = FakeCommandRunner().outcome(started = false, status = null, error = "Cannot run program \"/data/true\": error=13, Permission denied")
        val missing = FakeCommandRunner().outcome(started = false, status = null, error = "error=2, No such file or directory")
        assertEquals(ExecChecks.DENIED, ExecChecks.classifyDirectExec(denied))
        assertEquals(ExecChecks.PERMITTED_INTERPRETER_MISSING, ExecChecks.classifyDirectExec(missing))
        assertEquals(ExecChecks.PERMITTED, ExecChecks.classifyDirectExec(FakeCommandRunner().outcome(started = true, status = 0)))
        assertEquals(ExecChecks.ERROR, ExecChecks.classifyDirectExec(FakeCommandRunner().outcome(started = false, status = null, error = "weird")))
    }

    @Test
    fun guest_checks_pass_on_expected_output() {
        val runtime = FakeRuntime(selfCheckTempDir())
        val runner = FakeCommandRunner()
        val (_, checks) = ExecChecks.underProot(runtime, runner)
        assertTrue("checks: ${checks.map { it.id to Triple(it.pass, it.exitStatus, it.stdout) }}", checks.all { it.pass == true })
        assertEquals(4, checks.size)
        val python = runner.calls.first { it.argv.contains("-V") }
        assertTrue(python.argv.contains(ExecChecks.PAYLOAD_PYTHON))
        assertEquals(runtime.layout.prootTmp.path, python.environment["PROOT_TMP_DIR"])
        assertTrue(python.environment["PROOT_LOADER"]!!.endsWith("libproot-loader.so"))
    }

    @Test
    fun payload_missing_skips_only_python() {
        val runtime = FakeRuntime(selfCheckTempDir(), payload = false)
        val (_, checks) = ExecChecks.underProot(runtime, FakeCommandRunner())
        assertEquals(SkipReason.PAYLOAD_NOT_INSTALLED, checks.first { it.id == ExecChecks.PROOT_PYTHON }.skipReason)
        assertEquals("checks: ${checks.map { it.id to Triple(it.pass, it.exitStatus, it.stdout) }}", 3, checks.count { it.pass == true })
    }

    @Test
    fun direct_exec_passes_when_denied_at_target_34() {
        val runtime = FakeRuntime(selfCheckTempDir())
        val runner = FakeCommandRunner().apply {
            byMarker["true"] = outcome(started = false, status = null, error = "error=13, Permission denied")
        }
        val check = ExecChecks.directExec(runtime, runner, targetSdk = 34)
        assertEquals(ExecChecks.DENIED, check.classification)
        assertEquals(true, check.pass)
    }
}
