package com.tofu.client.runtime

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.io.IOException
import java.nio.file.Files

/**
 * The local kernel state machine against fake process/probe/runtime seams:
 * start → listening record → health → Ready; crash restarts within the launch
 * contract's budget; a crash loop reaches SafeMode; SIGKILL is classified as
 * "killed by the system"; stop is "SIGTERM the kernel, then await exit".
 */
class LocalKernelSupervisorTest {

    @Test
    fun start_reaches_ready_after_listening_record_and_health() {
        val harness = Harness(readiness = ScriptedReadiness(Readiness.Booting, Readiness.Unreachable, Readiness.Booting, Readiness.Ready)) { child, _ ->
            child.listen(43567)
        }
        assertTrue(harness.supervisor.start())

        val ready = harness.await { it is LocalKernelState.Ready } as LocalKernelState.Ready
        assertEquals(LocalKernelEndpoint(43567), ready.endpoint)
        assertEquals("http://127.0.0.1:43567/", ready.endpoint.baseUrl)
        assertEquals(harness.supervisor.endpoint, ready.endpoint)
        val child = harness.launcher.children.single()
        assertEquals("the secret is the only stdin line", "$TEST_SECRET\n", child.stdinBytes.toString(Charsets.US_ASCII.name()))
        assertTrue("stdin closes after the secret", child.stdinClosed)
        assertEquals("the pending payload is committed once ready", listOf("v1"), harness.runtime.commits)
    }

    @Test
    fun a_committed_payload_is_not_committed_again() {
        val harness = Harness(pending = false) { child, _ -> child.listen(1234) }
        harness.supervisor.start()
        harness.await { it is LocalKernelState.Ready }
        assertTrue(harness.runtime.commits.isEmpty())
    }

    @Test
    fun stop_sends_sigterm_to_the_kernel_pid_and_waits_for_exit() {
        val harness = Harness { child, _ -> child.listen(1234) }
        harness.supervisor.start()
        harness.await { it is LocalKernelState.Ready }

        harness.supervisor.stop()
        assertEquals(LocalKernelState.Stopped, harness.await { it == LocalKernelState.Stopped })
        val child = harness.launcher.children.single()
        assertEquals("SIGTERM goes to the kernel's reported pid, never to proot", listOf(child.pid), harness.signals.terminated)
        assertEquals("the kernel drained and exited by itself", 0, child.exitValue())
        assertEquals(1, harness.launcher.children.size)
    }

    @Test
    fun a_crash_restarts_within_the_budget() {
        val harness = Harness { child, index ->
            child.listen(2000 + index)
            if (index == 0) {
                Thread.sleep(50)
                child.err.emit("Traceback (most recent call last):\nRuntimeError: boom\n")
                child.exit(1)
            }
        }
        harness.supervisor.start()
        val ready = harness.await { it is LocalKernelState.Ready && it.endpoint.port == 2001 } as LocalKernelState.Ready
        assertEquals(2001, ready.endpoint.port)
        assertEquals(2, harness.launcher.children.size)
        assertTrue(harness.diagnosticsText().contains("RuntimeError: boom"))
    }

    @Test
    fun a_crash_loop_enters_safe_mode_and_rejects_the_pending_payload() {
        val harness = Harness { child, _ -> child.exit(1) }
        harness.supervisor.start()

        val safe = harness.await { it is LocalKernelState.SafeMode } as LocalKernelState.SafeMode
        assertEquals(RestartBudget.MAX_FAILURES, safe.crashes)
        assertEquals(1, safe.lastExitStatus)
        assertEquals("no restart after the budget is spent", RestartBudget.MAX_FAILURES, harness.launcher.children.size)
        assertEquals("the never-ready payload falls back to last-known-good", listOf("v1"), harness.runtime.rejects)
    }

    @Test
    fun crashes_spread_beyond_the_window_keep_restarting() {
        // 61 s between crashes: at most two fall inside the 120 s window.
        val harness = Harness(clockStepMs = 61_000) { child, index ->
            if (index < 6) child.exit(1) else child.listen(3000)
        }
        harness.supervisor.start()
        harness.await { it is LocalKernelState.Ready }
        assertEquals(7, harness.launcher.children.size)
        assertTrue(harness.runtime.rejects.isEmpty())
    }

    @Test
    fun an_unrequested_sigkill_is_killed_by_the_system_not_a_crash() {
        val harness = Harness { child, _ ->
            child.listen(1234)
            Thread.sleep(50)
            child.exit(LaunchProtocol.EXIT_SIGKILL)
        }
        harness.supervisor.start()

        val failed = harness.await { it is LocalKernelState.Failed } as LocalKernelState.Failed
        assertEquals(LocalKernelFailure.KilledBySystem, failed.failure)
        assertEquals("a system kill is not restarted", 1, harness.launcher.children.size)
        assertTrue("guests a killed proot left behind are swept", harness.signals.killCalls.get() >= 2)
        assertEquals("nothing to force: proot is already gone", 0, harness.signals.forceStops.get())
    }

    @Test
    fun exit_status_2_is_a_usage_error_without_restart() {
        val harness = Harness { child, _ -> child.exit(2) }
        harness.supervisor.start()
        assertEquals(LocalKernelState.Failed(LocalKernelFailure.UsageError), harness.await { it is LocalKernelState.Failed })
        assertEquals(1, harness.launcher.children.size)
    }

    @Test
    fun an_unrequested_exit_0_is_the_in_app_shutdown() {
        val harness = Harness { child, _ ->
            child.listen(1234)
            Thread.sleep(50)
            child.exit(0)
        }
        harness.supervisor.start()
        assertEquals(LocalKernelState.Stopped, harness.await { it == LocalKernelState.Stopped })
        assertEquals("it was ready before the shutdown", listOf("v1"), harness.runtime.commits)
        assertEquals(1, harness.launcher.children.size)
    }

    @Test
    fun a_malformed_stdout_record_is_a_protocol_violation() {
        val harness = Harness { child, _ -> child.out.emit("Starting Tofu...\n") }
        harness.supervisor.start()
        val failed = harness.await { it is LocalKernelState.Failed } as LocalKernelState.Failed
        assertTrue(failed.failure is LocalKernelFailure.ProtocolViolation)
        assertEquals("the generation was forced to stop through proot", 1, harness.signals.forceStops.get())
        assertEquals(PROOT_NO_NORMAL_EXIT, harness.launcher.children.single().exitValue())
    }

    @Test
    fun a_refused_readiness_probe_is_a_protocol_violation() {
        val harness = Harness(readiness = ScriptedReadiness(Readiness.Refused(403))) { child, _ -> child.listen(1234) }
        harness.supervisor.start()
        val failed = harness.await { it is LocalKernelState.Failed } as LocalKernelState.Failed
        assertEquals(LocalKernelFailure.ProtocolViolation("readiness probe answered HTTP 403"), failed.failure)
    }

    @Test
    fun a_boot_failure_counts_as_a_crash_and_restarts() {
        val readiness = object : ReadinessProbe {
            override fun check(port: Int) = if (port == 5000) Readiness.BootFailed else Readiness.Ready
        }
        val harness = Harness(readiness = readiness) { child, index -> child.listen(5000 + index) }
        harness.supervisor.start()
        val ready = harness.await { it is LocalKernelState.Ready } as LocalKernelState.Ready
        assertEquals(5001, ready.endpoint.port)
        assertEquals("the failed boot was stopped with SIGTERM", harness.launcher.children[0].pid, harness.signals.terminated.first())
    }

    @Test
    fun stop_before_the_listening_record_kills_the_generation() {
        val harness = Harness { _, _ -> /* never reports listening */ }
        harness.supervisor.start()
        while (harness.launcher.children.isEmpty()) Thread.sleep(5)
        harness.supervisor.stop()
        assertEquals(LocalKernelState.Stopped, harness.await { it == LocalKernelState.Stopped })
        assertTrue("no pid to SIGTERM yet", harness.signals.terminated.isEmpty())
        assertEquals("forced through proot (SIGQUIT), never a lone SIGKILL", 1, harness.signals.forceStops.get())
        assertEquals(PROOT_NO_NORMAL_EXIT, harness.launcher.children.single().exitValue())
    }

    @Test
    fun stop_escalates_when_the_kernel_outlives_its_drain_deadline() {
        val harness = Harness { child, _ -> child.listen(1234) }
        harness.signals.onTerminate = { /* ignores SIGTERM */ }
        harness.supervisor.start()
        harness.await { it is LocalKernelState.Ready }
        harness.supervisor.stop()
        assertEquals(LocalKernelState.Stopping, harness.supervisor.state.value)
        assertEquals(LocalKernelState.Stopped, harness.await { it == LocalKernelState.Stopped })
        assertEquals(1, harness.signals.forceStops.get())
        assertEquals(PROOT_NO_NORMAL_EXIT, harness.launcher.children.single().exitValue())
        assertTrue(harness.diagnosticsText().contains("outlived its drain deadline"))
    }

    @Test
    fun the_secret_never_reaches_argv_environment_or_diagnostics() {
        val root = Files.createTempDirectory("kernel-layout").toFile()
        val layout = KernelLayout(File(root, "files"))
        val command = KernelCommandLine.build(layout, File(root, "lib"), layout.payload("v1"))
        assertFalse(command.argv.any { it.contains(TEST_SECRET) || it.contains(KernelSecretValue.PREFIX) })
        assertFalse(command.environment.any { (key, value) -> (key + value).contains(TEST_SECRET) })

        // Even if a child echoed the secret to stderr, the recorded crash tail is scrubbed.
        val harness = Harness { child, index ->
            if (index == 0) {
                child.err.emit("leaked $TEST_SECRET by a buggy tool\n")
                child.exit(1)
            } else {
                child.listen(1234)
            }
        }
        harness.supervisor.start()
        harness.await { it is LocalKernelState.Ready }
        val text = harness.diagnosticsText()
        assertFalse(text.contains(TEST_SECRET))
        assertTrue(text.contains("leaked ${KernelSecretValue.REDACTED}"))
        assertEquals("KernelSecret(${KernelSecretValue.REDACTED})", testSecret().toString())
        root.deleteRecursively()
    }

    @Test
    fun launch_and_install_failures_are_reported() {
        val launchFailure = Harness { _, _ -> }
        launchFailure.launcher.failure = IOException("error=13, Permission denied")
        launchFailure.supervisor.start()
        assertEquals(
            LocalKernelState.Failed(LocalKernelFailure.LaunchFailed("error=13, Permission denied")),
            launchFailure.await { it is LocalKernelState.Failed },
        )

        val installFailure = Harness { _, _ -> }
        installFailure.runtime.failure = RuntimeInstallException("refusing payload-v1.tgz: sha256 mismatch")
        installFailure.supervisor.start()
        assertEquals(
            LocalKernelState.Failed(LocalKernelFailure.InstallFailed("refusing payload-v1.tgz: sha256 mismatch")),
            installFailure.await { it is LocalKernelState.Failed },
        )

        val unavailable = Harness { _, _ -> }
        unavailable.runtime.failure = LocalRuntimeUnavailableException("missing arm64-v8a/libproot.so")
        unavailable.supervisor.start()
        assertEquals(
            LocalKernelState.Failed(LocalKernelFailure.NotAvailable("missing arm64-v8a/libproot.so")),
            unavailable.await { it is LocalKernelState.Failed },
        )
    }

    @Test
    fun start_is_idempotent_and_a_new_start_after_safe_mode_resets_the_budget() {
        val harness = Harness { child, index -> if (index < RestartBudget.MAX_FAILURES) child.exit(1) else child.listen(1234) }
        assertTrue(harness.supervisor.start())
        assertFalse("a second start while running is a no-op", harness.supervisor.start())
        harness.await { it is LocalKernelState.SafeMode }

        assertTrue(harness.supervisor.start())
        harness.await { it is LocalKernelState.Ready }
        assertEquals(RestartBudget.MAX_FAILURES + 1, harness.launcher.children.size)
    }
}
