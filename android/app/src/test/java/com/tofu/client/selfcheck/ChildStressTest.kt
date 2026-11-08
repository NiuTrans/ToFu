package com.tofu.client.selfcheck

import com.tofu.client.runtime.LaunchProtocol
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Stress bookkeeping: the deaths timeline, signal attribution and unconditional cleanup. */
class ChildStressTest {
    private fun generation(clock: FakeClock, host: FakeStressHost): Pair<ChildStress, com.tofu.client.runtime.KernelCommand> {
        val runtime = FakeRuntime(selfCheckTempDir())
        val prefix = ExecChecks.prootPrefix(runtime, runtime.payloadDirectory()).let { it.copy(argv = it.argv.filter { arg -> arg != "--kill-on-exit" }) }
        return ChildStress(host, clock, prefix) to prefix
    }

    @Test
    fun proc_stat_parses_comm_with_spaces_and_zombie_exit_code() {
        val fields = (3..52).joinToString(" ") { index ->
            when (index) {
                3 -> "Z"
                4 -> "9000"
                14 -> "120"
                15 -> "30"
                22 -> "424242"
                52 -> "9"
                else -> "0"
            }
        }
        val stat = ProcStat.parse("9001 (my proc) $fields")!!
        assertEquals(9001, stat.pid)
        assertEquals('Z', stat.state)
        assertEquals(9000, stat.parentPid)
        assertEquals(424242L, stat.startTimeTicks)
        assertEquals(150L, stat.cpuTicks)
        assertEquals(9, stat.exitSignal)
        assertNull(stat.exitStatus)
        assertNull(ProcStat.parse("garbage"))
    }

    @Test
    fun script_spawns_above_the_cap_and_reports_signals() {
        val script = ChildStress.script(ChildStress.plan(5))
        assertTrue(script.contains("seq 1 32"))
        assertTrue(script.contains("seq 1 2"))
        assertTrue(script.contains("wait -n -p"))
        assertTrue(script.contains("sleep infinity"))
        assertEquals(34, ChildStress.plan(5).sleepers + ChildStress.plan(5).busyWorkers)
        assertTrue(34 + 2 > ChildStress.PHANTOM_PROCESS_CAP) // plus proot and the orchestrator
    }

    @Test
    fun proot_prefix_is_the_kernel_command_without_its_guest_entry() {
        val runtime = FakeRuntime(selfCheckTempDir())
        val prefix = ExecChecks.prootPrefix(runtime, runtime.payloadDirectory())
        assertTrue(prefix.argv.first().endsWith("libproot.so"))
        assertTrue(prefix.argv.contains("--kill-on-exit"))
        assertFalse(prefix.argv.any { it.contains("tofu-local-kernel") })
        assertEquals(runtime.layout.prootTmp.path, prefix.environment["PROOT_TMP_DIR"])
        assertFalse(prefix.environment.containsKey("LD_PRELOAD"))
        assertFalse(prefix.environment.containsKey("LD_LIBRARY_PATH"))
        // Without a payload there is no /opt/tofu bind.
        val noPayload = ExecChecks.prootPrefix(runtime, null)
        assertFalse(noPayload.argv.any { it.startsWith("--bind=") && it.endsWith(":/opt/tofu") })
    }

    @Test
    fun deaths_are_recorded_with_time_and_signal_and_every_child_is_cleaned_up() = runBlocking(Dispatchers.Default) {
        val clock = FakeClock()
        val host = FakeStressHost(clock)
        val (stress, _) = generation(clock, host)
        // proot 9000, orchestrator 9001, one sleeper 9002 killed by the system, one busy 9003 alive.
        host.spawn(9000); host.spawn(9001); host.spawn(9002); host.spawn(9003)
        val child = FakeStressChild(
            host,
            pid = 9000,
            lines = listOf(
                "SC1 hello pid=9001 parent=9000",
                "SC1 spawn kind=sleeper pid=9002",
                "SC1 spawn kind=busy pid=9003",
                "SC1 ready children=2",
            ),
            aliveForever = true,
        )
        host.nextChild = child
        // Drive the deaths from the sampler itself: after a few polls of the sleeper, the system kills it.
        var polls = 0
        host.onStat = { pid ->
            if (pid == 9002) {
                polls++
                if (polls == 2) host.die(9002)
            }
            if (pid == 9001 && polls >= 2) host.zombie(9001, 9)
        }
        var ticks = 0
        val phase = stress.run(
            phaseName = StressPhase.FOREGROUND,
            plan = ChildStress.plan(5),
            cancelled = { ticks++ > 6 },
            kernelActive = { false },
        )
        val deaths = phase.deaths
        assertTrue(deaths.any { it.pid == 9002 && it.deathSource == "proc" })
        val orchestrator = phase.processes.first { it.pid == 9001 }
        assertEquals(9, orchestrator.signal)
        assertNotNull(phase.cleanup)
        assertEquals(0, phase.cleanup!!.leftovers)
        // Cleanup signalled the survivors; the busy worker got SIGKILL, proot SIGQUIT.
        assertTrue(host.signals.contains(9003 to LaunchProtocol.SIGKILL))
        assertTrue(phase.processes.all { it.diedAtMs != null })
        assertTrue(phase.endedAtMs != null)
    }

    @Test
    fun orchestrator_reported_death_wins_over_a_later_proc_observation() = runBlocking(Dispatchers.Default) {
        val clock = FakeClock()
        val host = FakeStressHost(clock)
        val (stress, _) = generation(clock, host)
        host.spawn(9000); host.spawn(9001); host.spawn(9002)
        host.nextChild = FakeStressChild(
            host,
            pid = 9000,
            lines = listOf("SC1 hello pid=9001 parent=9000", "SC1 spawn kind=sleeper pid=9002", "SC1 ready children=1", "SC1 exit pid=9002 status=137"),
        )
        // A zero-length phase completes after the first tick; the orchestrator's lines are drained first.
        val phase = stress.run(StressPhase.FOREGROUND, ChildStress.plan(5).copy(phaseDurationMs = 0, sampleIntervalMs = 1), cancelled = { false }, kernelActive = { false })
        val death = phase.processes.first { it.pid == 9002 }
        assertEquals("orchestrator", death.deathSource)
        assertEquals(9, death.signal)
        assertNull(death.exitStatus)
    }

    @Test
    fun cancellation_still_cleans_up_and_marks_the_phase_cancelled() = runBlocking(Dispatchers.Default) {
        val clock = FakeClock()
        val host = FakeStressHost(clock)
        val (stress, _) = generation(clock, host)
        host.spawn(9000); host.spawn(9001); host.spawn(9002)
        host.nextChild = FakeStressChild(host, pid = 9000, lines = listOf("SC1 hello pid=9001 parent=9000", "SC1 spawn kind=sleeper pid=9002", "SC1 ready children=1"), aliveForever = true)
        val phase = stress.run(StressPhase.BACKGROUND, ChildStress.plan(5), cancelled = { true }, kernelActive = { false })
        assertEquals(StepStatus.CANCELLED, phase.status)
        assertEquals(ChildStress.CANCELLED, phase.endReason)
        assertNotNull(phase.cleanup)
        assertEquals(0, phase.deaths.size) // cleanup deaths are not system kills
    }

    @Test
    fun a_kernel_started_mid_phase_ends_it() = runBlocking(Dispatchers.Default) {
        val clock = FakeClock()
        val host = FakeStressHost(clock)
        val (stress, _) = generation(clock, host)
        host.spawn(9000)
        host.nextChild = FakeStressChild(host, pid = 9000, lines = listOf("SC1 hello pid=9001 parent=9000", "SC1 ready children=0"), aliveForever = true)
        val phase = stress.run(StressPhase.FOREGROUND, ChildStress.plan(5), cancelled = { false }, kernelActive = { true })
        assertEquals(ChildStress.LOCAL_KERNEL_STARTED, phase.endReason)
    }

    @Test
    fun launch_failure_is_reported_not_thrown() = runBlocking(Dispatchers.Default) {
        val clock = FakeClock()
        val host = FakeStressHost(clock).apply { launchError = java.io.IOException("error=13, Permission denied") }
        val (stress, _) = generation(clock, host)
        val phase = stress.run(StressPhase.FOREGROUND, ChildStress.plan(5), cancelled = { false }, kernelActive = { false })
        assertEquals(ChildStress.LAUNCH_FAILED, phase.endReason)
        assertTrue(phase.error.orEmpty().contains("Permission denied"))
    }

    @Test
    fun importance_and_idle_are_sampled() = runBlocking(Dispatchers.Default) {
        val clock = FakeClock()
        val host = FakeStressHost(clock)
        val (stress, _) = generation(clock, host)
        host.appImportance = 125
        host.idle = true
        host.interactive = false
        host.spawn(9000)
        host.nextChild = FakeStressChild(host, pid = 9000, lines = listOf("SC1 hello pid=9001 parent=9000", "SC1 ready children=0"), aliveForever = true)
        var n = 0
        val phase = stress.run(StressPhase.BACKGROUND, ChildStress.plan(5), cancelled = { ++n > 2 }, kernelActive = { false })
        assertTrue(phase.samples >= 2)
        assertEquals(phase.samples, phase.foregroundServiceSamples)
        assertEquals(0, phase.foregroundSamples)
        assertEquals(phase.samples, phase.deviceIdleSamples)
        assertEquals(0, phase.screenOnSamples)
    }
}
