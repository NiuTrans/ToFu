package com.tofu.client.runtime

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout

/** Launch-contract pieces of the kernel process: command line, stdout record, exit statuses, readiness, process table. */
class KernelProcessTest {

    @Test
    fun the_proot_command_follows_the_runtime_contract() {
        val root = Files.createTempDirectory("kernel-command").toFile()
        val layout = KernelLayout(File(root, "files"))
        val nativeLibraryDir = File(root, "lib/arm64")
        val payload = layout.payload("2026.11.05")

        val command = KernelCommandLine.build(layout, nativeLibraryDir, payload)

        assertEquals(File(nativeLibraryDir, "libproot.so").path, command.argv.first())
        assertEquals(
            listOf(
                "--kill-on-exit",
                "--link2symlink",
                "--root-id",
                "--rootfs=${File(root, "files/kernel/rootfs").path}",
                "--bind=/dev",
                "--bind=/proc",
                "--bind=/sys",
                "--sysvipc",
                "--ashmem-memfd",
                "--bind=${File(root, "files/kernel/shm").path}:/dev/shm",
                "--bind=${File(root, "files/kernel/payload/2026.11.05").path}:/opt/tofu",
                "--bind=${File(root, "files/kernel/data").path}:/var/lib/tofu",
                "--bind=${File(root, "files/kernel/home").path}:/root",
                "--cwd=/opt/tofu",
                "/opt/tofu/bin/tofu-local-kernel",
                "--data-dir",
                "/var/lib/tofu",
            ),
            command.argv.drop(1),
        )
        assertEquals(
            mapOf(
                "PROOT_LOADER" to File(nativeLibraryDir, "libproot-loader.so").path,
                "PROOT_TMP_DIR" to File(root, "files/kernel/proot-tmp").path,
                "PATH" to KernelCommandLine.GUEST_PATH,
                "HOME" to "/root",
                "LANG" to "C.UTF-8",
            ),
            command.environment,
        )
        assertFalse("proot hands its environment to glibc guests", command.environment.keys.any { it.startsWith("LD_") })
        root.deleteRecursively()
    }

    @Test
    fun the_listening_record_is_parsed_strictly() {
        assertEquals(
            ListeningRecord(43567, 4242),
            LaunchProtocol.parseListening("{\"event\": \"listening\", \"host\": \"127.0.0.1\", \"port\": 43567, \"pid\": 4242}"),
        )
        assertEquals(ListeningRecord(1, 7), LaunchProtocol.parseListening("{\"pid\":7,\"port\":1,\"host\":\"127.0.0.1\",\"event\":\"listening\"}\r"))
        assertNull(LaunchProtocol.parseListening("{\"event\": \"listening\", \"host\": \"0.0.0.0\", \"port\": 43567, \"pid\": 4242}"))
        assertNull(LaunchProtocol.parseListening("{\"event\": \"ready\", \"host\": \"127.0.0.1\", \"port\": 43567, \"pid\": 4242}"))
        assertNull(LaunchProtocol.parseListening("{\"event\": \"listening\", \"host\": \"127.0.0.1\", \"port\": 0, \"pid\": 4242}"))
        assertNull(LaunchProtocol.parseListening("{\"event\": \"listening\", \"host\": \"127.0.0.1\", \"port\": 70000, \"pid\": 4242}"))
        assertNull(LaunchProtocol.parseListening("{\"event\": \"listening\", \"host\": \"127.0.0.1\", \"port\": 43567}"))
        assertNull(LaunchProtocol.parseListening("Listening on 127.0.0.1:43567"))
    }

    @Test
    fun exit_statuses_follow_the_launch_contract() {
        assertEquals(KernelExit.REQUESTED_STOP, LaunchProtocol.classifyExit(0, stopRequested = false))
        assertEquals(KernelExit.REQUESTED_STOP, LaunchProtocol.classifyExit(1, stopRequested = true))
        assertEquals(KernelExit.REQUESTED_STOP, LaunchProtocol.classifyExit(137, stopRequested = true))
        assertEquals(KernelExit.USAGE_ERROR, LaunchProtocol.classifyExit(2, stopRequested = false))
        assertEquals(KernelExit.KILLED_BY_SYSTEM, LaunchProtocol.classifyExit(137, stopRequested = false))
        assertEquals(KernelExit.CRASH, LaunchProtocol.classifyExit(1, stopRequested = false))
        assertEquals(KernelExit.CRASH, LaunchProtocol.classifyExit(134, stopRequested = false))
        assertEquals(KernelExit.CRASH, LaunchProtocol.classifyExit(255, stopRequested = false))
        assertEquals("TOFU_SHUTDOWN_HARD_SECS default + server_manager margin", 35_000L, LaunchProtocol.STOP_ESCALATION_MS)
    }

    @Test
    fun readiness_follows_the_boot_gate() {
        assertEquals(Readiness.Ready, MetaReadinessProbe.classify(200, null))
        assertEquals(Readiness.Booting, MetaReadinessProbe.classify(503, "{\"code\":\"service_unavailable\",\"detail\":\"server_booting\"}"))
        assertEquals(Readiness.BootFailed, MetaReadinessProbe.classify(503, "{\"code\":\"service_unavailable\",\"detail\":\"server_boot_failed\"}"))
        assertEquals(Readiness.BootFailed, MetaReadinessProbe.classify(503, "{\"ok\":false,\"error\":\"server_boot_failed\",\"retryAfter\":5}"))
        assertEquals(Readiness.Booting, MetaReadinessProbe.classify(502, null))
        assertEquals(Readiness.Refused(403), MetaReadinessProbe.classify(403, null))
        assertEquals(Readiness.Refused(401), MetaReadinessProbe.classify(401, null))
        assertEquals(Readiness.Refused(302), MetaReadinessProbe.classify(302, null))
    }

    @Test
    fun the_restart_budget_matches_the_desktop_lifecycle_manager() {
        assertEquals(5, RestartBudget.MAX_FAILURES)
        assertEquals(120_000L, RestartBudget.WINDOW_MS)
        val budget = RestartBudget()
        assertEquals(listOf(1, 2, 3, 4, 5), (0 until 5).map { budget.recordCrash(it * 30_000L) })
        budget.reset()
        assertEquals(1, budget.recordCrash(0))
        assertEquals("a crash exactly at the window edge still counts", 2, budget.recordCrash(120_000))
        assertEquals(2, budget.recordCrash(120_001))
    }

    @Test
    fun guidance_and_wakelock_policies() {
        assertEquals(SystemKillGuidance.DEVELOPER_OPTION, SystemKillGuidance.forSdk(34))
        assertEquals(SystemKillGuidance.DEVELOPER_OPTION, SystemKillGuidance.forSdk(36))
        assertEquals(SystemKillGuidance.DEVICE_CONFIG, SystemKillGuidance.forSdk(33))
        assertEquals(SystemKillGuidance.DEVICE_CONFIG, SystemKillGuidance.forSdk(31))
        assertEquals(SystemKillGuidance.RESTART_ONLY, SystemKillGuidance.forSdk(30))
        assertEquals(SystemKillGuidance.RESTART_ONLY, SystemKillGuidance.forSdk(26))

        val ready = LocalKernelState.Ready(LocalKernelEndpoint(1234))
        assertTrue(WakeLockPolicy.shouldHold(ready, hasRunningWork = true))
        assertFalse(WakeLockPolicy.shouldHold(ready, hasRunningWork = false))
        assertFalse(WakeLockPolicy.shouldHold(LocalKernelState.Starting, hasRunningWork = true))
        assertFalse(WakeLockPolicy.shouldHold(LocalKernelState.Stopping, hasRunningWork = true))
        assertFalse(WakeLockPolicy.shouldHold(LocalKernelState.Failed(LocalKernelFailure.KilledBySystem), hasRunningWork = true))
    }

    @Test
    fun the_app_process_table_finds_proot_and_every_other_app_process() {
        val proc = Files.createTempDirectory("proc").toFile()
        fun process(pid: String, parent: Int, uid: Int?) {
            val directory = File(proc, pid).apply { mkdirs() }
            if (uid != null) File(directory, "status").writeText("Name:\tx\nUmask:\t0077\nPPid:\t$parent\nUid:\t$uid\t$uid\t$uid\t$uid\nGid:\t$uid\n")
        }
        process("1", 0, 0)
        process("100", 1, 10123) // the app itself (forked by zygote)
        process("101", 100, 10123) // proot, the app's direct child
        process("102", 101, 10123) // the server, proot's child
        process("103", 1, 10123) // a guest orphaned by a killed proot
        process("104", 100, null) // exited between listing and reading
        process("105", 100, 10200) // another app (invisible under hidepid=2, excluded by uid anyway)
        process("self", 100, 10123)

        assertEquals(listOf(101), AppProcessTable.childPids(proc, parentPid = 100, uid = 10123))
        assertEquals(listOf(101, 102, 103), AppProcessTable.otherPids(proc, selfPid = 100, uid = 10123).sorted())
        proc.deleteRecursively()
    }

    @Test
    fun the_launcher_passes_only_the_explicit_environment() {
        val child = ProcessBuilderLauncher().start(KernelCommand(listOf("/usr/bin/env"), mapOf("ONLY" to "this"), File("/")))
        child.stdin.close()
        val output = child.stdout.readBytes().toString(Charsets.UTF_8)
        assertEquals(0, child.waitFor())
        assertEquals("ONLY=this\n", output)
    }

    /**
     * Real processes and real signals: a /bin/sh stand-in for the guest entry
     * reads the secret from stdin, prints the listening record with its own
     * pid, and drains on SIGTERM. A SIGKILL reads as 0x80 + 9 = 137 on the
     * JVM, the same convention as Android's libcore.
     */
    @Test
    fun a_real_child_reads_the_secret_reports_listening_and_stops_on_sigterm() {
        val directory = Files.createTempDirectory("fake-kernel").toFile()
        val fifo = File(directory, "block")
        assertEquals(0, ProcessBuilder("mkfifo", fifo.path).start().waitFor())
        val supervisor = realSupervisor(fakeKernelScript(directory, finale = "read never < \"${'$'}FIFO\""), fifo)
        supervisor.start()
        val ready = awaitState(supervisor) { it is LocalKernelState.Ready } as LocalKernelState.Ready
        assertEquals(43567, ready.endpoint.port)
        supervisor.stop()
        assertEquals(LocalKernelState.Stopped, awaitState(supervisor) { it == LocalKernelState.Stopped })
        directory.deleteRecursively()
    }

    @Test
    fun a_real_sigkill_is_classified_as_killed_by_the_system() {
        val directory = Files.createTempDirectory("fake-kernel").toFile()
        val supervisor = realSupervisor(fakeKernelScript(directory, finale = "kill -9 ${'$'}${'$'}"), File(directory, "unused"))
        supervisor.start()
        assertEquals(
            LocalKernelState.Failed(LocalKernelFailure.KilledBySystem),
            awaitState(supervisor) { it is LocalKernelState.Failed },
        )
        directory.deleteRecursively()
    }

    private fun fakeKernelScript(directory: File, finale: String): KernelCommand {
        val script = listOf(
            "read secret",
            "[ ${'$'}{#secret} -eq 75 ] || exit 2",
            "trap 'exit 0' TERM",
            "printf '{\"event\": \"listening\", \"host\": \"127.0.0.1\", \"port\": 43567, \"pid\": %d}\\n' ${'$'}${'$'}",
            "echo 'kernel booted' >&2",
            finale,
        ).joinToString("\n")
        return KernelCommand(listOf("/bin/sh", "-c", script), emptyMap(), directory)
    }

    private fun realSupervisor(command: KernelCommand, fifo: File): LocalKernelSupervisor {
        val withFifo = command.copy(environment = mapOf("FIFO" to fifo.path))
        val runtime = object : KernelRuntime {
            override fun prepare(cancelled: () -> Boolean) = PreparedLaunch(withFifo, testSecret(), "v1", pending = false)
            override fun commit(launch: PreparedLaunch) = Unit
            override fun reject(launch: PreparedLaunch) = Unit
        }
        val signals = object : ProcessSignals {
            override fun terminate(pid: Int) {
                ProcessBuilder("kill", "-TERM", pid.toString()).start().waitFor(10, TimeUnit.SECONDS)
            }
            override fun forceStop(): Int = 0
            override fun killAppChildren(): Int = 0
        }
        return LocalKernelSupervisor(runtime, ProcessBuilderLauncher(), signals, ScriptedReadiness(Readiness.Ready), readinessPollMs = 5, stopEscalationMs = 10_000)
    }

    private fun awaitState(supervisor: LocalKernelSupervisor, predicate: (LocalKernelState) -> Boolean): LocalKernelState =
        runBlocking { withTimeout(10_000) { supervisor.state.first(predicate) } }
}
