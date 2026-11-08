package com.tofu.client.selfcheck

import com.tofu.client.runtime.KernelCommand
import com.tofu.client.runtime.LaunchProtocol
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.supervisorScope
import kotlinx.coroutines.withContext
import java.io.File
import java.io.InputStream

/** One `/proc/<pid>/stat` snapshot (proc(5)); [startTimeTicks] identifies the process against pid reuse. */
data class ProcStat(
    val pid: Int,
    val state: Char,
    val parentPid: Int,
    val startTimeTicks: Long,
    val cpuTicks: Long,
    /** Linux wait status of a zombie (stat field 52), when the kernel exposes it; 0 otherwise. */
    val exitCode: Int,
) {
    val zombie: Boolean get() = state == 'Z'
    /** A zombie's exit signal, e.g. 9 after SIGKILL. */
    val exitSignal: Int? get() = if (zombie && exitCode and 0x7f != 0) exitCode and 0x7f else null
    /** A zombie's own exit status, when it exited normally. */
    val exitStatus: Int? get() = if (zombie && exitCode and 0x7f == 0) (exitCode shr 8) and 0xff else null

    companion object {
        /**
         * Split after the last `)` of the comm field: comm is parenthesised and
         * may itself contain spaces and parentheses. Following fields are
         * 1-based after comm: state=3, ppid=4, utime=14, stime=15,
         * starttime=22, exit_code=52.
         */
        fun parse(text: String): ProcStat? {
            val open = text.indexOf('(')
            val close = text.lastIndexOf(')')
            if (open <= 0 || close <= open) return null
            val pid = text.substring(0, open).trim().toIntOrNull() ?: return null
            val fields = text.substring(close + 1).trim().split(Regex("\\s+"))
            if (fields.size < 20) return null
            return ProcStat(
                pid = pid,
                state = fields[0].firstOrNull() ?: return null,
                parentPid = fields[1].toIntOrNull() ?: return null,
                cpuTicks = (fields[11].toLongOrNull() ?: 0L) + (fields[12].toLongOrNull() ?: 0L),
                startTimeTicks = fields[19].toLongOrNull() ?: return null,
                exitCode = fields.getOrNull(49)?.toLongOrNull()?.toInt() ?: 0,
            )
        }
    }
}

/** The device's process table, signals and visibility, so the bookkeeping runs on the plain JVM. */
interface StressHost {
    val procRoot: File
    fun launch(command: KernelCommand): StressChild
    /** Null when the process is gone (or unreadable). */
    fun stat(pid: Int): ProcStat?
    fun signal(pid: Int, signal: Int)
    /** `ActivityManager.getMyMemoryState` importance. */
    fun importance(): Int
    fun screenOn(): Boolean
    fun deviceIdle(): Boolean

    fun alive(pid: Int, expectedStartTimeTicks: Long? = null): Boolean {
        val stat = stat(pid) ?: return false
        if (stat.zombie) return false
        return expectedStartTimeTicks == null || stat.startTimeTicks == expectedStartTimeTicks
    }
}

/** The launched proot process (Android's `java.lang.Process` on a device). */
interface StressChild {
    /** From `UNIXProcess.toString()`; Android exposes no `Process.pid()`. Null when unavailable. */
    val pid: Int?
    val stdout: InputStream
    val stderr: InputStream
    fun isAlive(): Boolean
    fun waitFor(): Int
    fun closeStreams()
}

/** Bookkeeping of one stress generation. The first death observation fixes the time; later ones only add detail. */
class StressGeneration {
    private val records = LinkedHashMap<Int, StressProcess>()

    fun noteSpawn(kind: String, pid: Int, atMs: Long) {
        if (pid > 0 && !records.containsKey(pid)) records[pid] = StressProcess(kind = kind, pid = pid, spawnedAtMs = atMs)
    }

    fun noteDeath(pid: Int, atMs: Long, source: String, signal: Int? = null, exitStatus: Int? = null) {
        val existing = records[pid] ?: return
        if (existing.diedAtMs != null) {
            if (existing.signal == null && signal != null || existing.exitStatus == null && exitStatus != null) {
                records[pid] = existing.copy(signal = existing.signal ?: signal, exitStatus = existing.exitStatus ?: exitStatus)
            }
            return
        }
        records[pid] = existing.copy(diedAtMs = atMs, deathSource = source, signal = signal, exitStatus = exitStatus)
    }

    fun noteCpu(pid: Int, ticks: Long) {
        val existing = records[pid] ?: return
        if (existing.diedAtMs == null) records[pid] = existing.copy(cpuTicks = ticks)
    }

    fun list(): List<StressProcess> = records.values.toList()
    fun deaths(): List<StressProcess> = list().filter { it.diedAtMs != null && it.deathSource != StressPhase.CLEANUP }
    fun living(): List<StressProcess> = list().filter { it.diedAtMs == null }
}

/**
 * The phantom-process stress test (docs/ANDROID_KERNELS.md "Risk: Android
 * kills app child processes"). One proot generation per phase runs a bash
 * orchestrator that spawns [SLEEPERS] sleepers plus [BUSY_WORKERS] CPU-busy
 * workers — more than the system-wide cap of [PHANTOM_PROCESS_CAP] phantom
 * processes (AOSP `ActivityManagerConstants.DEFAULT_MAX_PHANTOM_PROCESSES`,
 * `activity_manager/max_phantom_processes`; `PhantomProcessList` reads every
 * app's `cgroup.procs`, so the proot tree counts).
 *
 * - The orchestrator's `wait -n -p` reports each child's signal as it dies;
 *   the app independently polls `/proc` every [SAMPLE_INTERVAL_MS] so deaths
 *   still appear when the orchestrator itself is killed first (the cap kills
 *   the earliest-known phantom of the least important app first, which is
 *   proot). A zombie's `exit_code` carries the signal when it is observed.
 * - `--kill-on-exit` is dropped for this test only: the kernel's own flag
 *   would make proot SIGKILL every guest when the orchestrator dies, and
 *   those deaths would be proot's, not the system's.
 * - Cleanup is unconditional: SIGQUIT to proot (which SIGKILLs its guests),
 *   then SIGKILL to every tracked pid still alive, matched by start time so a
 *   recycled pid is never signalled. Deaths during cleanup are recorded as
 *   [StressPhase.CLEANUP], not as system kills.
 */
class ChildStress(
    private val host: StressHost,
    private val clock: SelfCheckClock,
    private val prefix: KernelCommand,
) {
    suspend fun run(
        phaseName: String,
        plan: StressPlan,
        cancelled: () -> Boolean,
        kernelActive: () -> Boolean,
        onTick: (StressPhase) -> Unit = {},
    ): StressPhase = supervisorScope {
        val launchedAt = clock.elapsedMs()
        var phase = StressPhase(phase = phaseName, status = StepStatus.RUNNING, startedAtMs = 0L)
        val generation = StressGeneration()
        val child = try {
            host.launch(prefix.copy(argv = prefix.argv + listOf(ORCHESTRATOR, "-c", script(plan))))
        } catch (error: Exception) {
            return@supervisorScope phase.copy(status = StepStatus.DONE, endReason = LAUNCH_FAILED, error = "${error.javaClass.simpleName}: ${error.message}", endedAtMs = clock.elapsedMs() - launchedAt)
        }
        val startTimes = HashMap<Int, Long>()
        var ready = false
        var prootExit: Int? = null
        val stderrTail = BoundedTail()
        val lines = Channel<String>(Channel.UNLIMITED)

        fun now() = clock.elapsedMs() - launchedAt

        fun handle(line: String) {
            val fields = line.trim().split(' ')
            if (fields.firstOrNull() != MARKER) return
            val values = fields.drop(2).mapNotNull { field ->
                val key = field.substringBefore('=')
                val value = field.substringAfter('=', "")
                if (value.isEmpty()) null else key to value
            }.toMap()
            when (fields.getOrNull(1)) {
                "hello" -> {
                    values["pid"]?.toIntOrNull()?.let { generation.noteSpawn(ORCHESTRATOR_KIND, it, now()) }
                    if (child.pid == null) values["parent"]?.toIntOrNull()?.let { generation.noteSpawn(PROOT_KIND, it, 0L) }
                }
                "spawn" -> values["pid"]?.toIntOrNull()?.let { pid -> generation.noteSpawn(values["kind"] ?: "child", pid, now()) }
                "ready" -> { ready = true; phase = phase.copy(readyAtMs = now()) }
                "exit" -> values["pid"]?.toIntOrNull()?.let { pid ->
                    val status = values["status"]?.toIntOrNull()
                    generation.noteDeath(pid, now(), "orchestrator", signal = status?.let { if (it > 128) it - 128 else null }, exitStatus = status?.let { if (it <= 128) it else null })
                }
                else -> Unit
            }
        }

        child.pid?.let { generation.noteSpawn(PROOT_KIND, it, 0L) }
        val waitJob = launch(Dispatchers.IO) { prootExit = child.waitFor() }
        val stdoutJob = launch(Dispatchers.IO) {
            child.stdout.bufferedReader().useLines { sequence -> sequence.forEach { lines.trySend(it) } }
            lines.close()
        }
        val stderrJob = launch(Dispatchers.IO) {
            child.stderr.bufferedReader().useLines { sequence -> sequence.forEach { stderrTail.append(it) } }
        }

        var lastElapsed = clock.elapsedMs()
        var lastUptime = clock.uptimeMs()
        var deepSleep = 0L
        var endReason = COMPLETED
        try {
            while (true) {
                while (true) handle(lines.tryReceive().getOrNull() ?: break)
                val nowMs = now()
                for (process in generation.living()) {
                    val stat = host.stat(process.pid)
                    if (stat == null || stat.zombie || startTimes[process.pid]?.let { it != stat.startTimeTicks } == true) {
                        generation.noteDeath(process.pid, nowMs, "proc", signal = stat?.exitSignal, exitStatus = stat?.exitStatus)
                    } else {
                        startTimes.putIfAbsent(process.pid, stat.startTimeTicks)
                        generation.noteCpu(process.pid, stat.cpuTicks)
                    }
                }
                prootExit?.let { status ->
                    generation.list().filter { it.kind == PROOT_KIND }.forEach {
                        generation.noteDeath(it.pid, nowMs, "proot-wait", signal = ExecChecks.signalOf(status), exitStatus = status)
                    }
                }
                phase = sample(phase, host, generation, nowMs)
                val elapsedDelta = clock.elapsedMs() - lastElapsed
                val uptimeDelta = clock.uptimeMs() - lastUptime
                if (elapsedDelta > uptimeDelta) deepSleep += elapsedDelta - uptimeDelta
                lastElapsed = clock.elapsedMs()
                lastUptime = clock.uptimeMs()
                onTick(phase)
                when {
                    cancelled() -> { endReason = CANCELLED; break }
                    kernelActive() -> { endReason = LOCAL_KERNEL_STARTED; break }
                    ready && generation.living().isEmpty() -> { endReason = ALL_DIED; break }
                    // The phase is measured from the orchestrator's ready marker; before it,
                    // only cancellation, a started kernel or a launch failure can end the phase.
                    ready && nowMs - phase.readyAtMs!! >= plan.phaseDurationMs -> { endReason = COMPLETED; break }
                }
                delay(plan.sampleIntervalMs)
            }
        } finally {
            // Everything below runs even on cancellation: no child may outlive the test.
            withContext(kotlinx.coroutines.NonCancellable) {
                val cleanupFrom = clock.elapsedMs()
                var signalled = 0
                for (process in generation.living()) {
                    val expected = startTimes[process.pid]
                    if (!host.alive(process.pid, expected)) continue
                    val signal = if (process.kind == PROOT_KIND) SIGNAL_QUIT else LaunchProtocol.SIGKILL
                    runCatching { host.signal(process.pid, signal) }
                    signalled++
                }
                // Wall-clock bound: elapsedRealtime includes deep sleep, and cleanup must not
                // depend on the injected clock advancing (nor run long on a frozen device).
                val deadline = System.nanoTime() + CLEANUP_DEADLINE_MS * 1_000_000L
                while (System.nanoTime() < deadline) {
                    while (true) handle(lines.tryReceive().getOrNull() ?: break)
                    if (generation.living().none { host.alive(it.pid, startTimes[it.pid]) }) break
                    delay(CLEANUP_POLL_MS)
                }
                // Last resort for proot alone, and only after its guests were signalled
                // (proot sets no PTRACE_O_EXITKILL: killing it first would orphan them).
                for (process in generation.living().filter { it.kind == PROOT_KIND }) {
                    if (host.alive(process.pid, startTimes[process.pid])) runCatching { host.signal(process.pid, LaunchProtocol.SIGKILL) }
                    signalled++
                }
                var leftovers = 0
                for (process in generation.living()) {
                    if (host.alive(process.pid, startTimes[process.pid])) leftovers++
                    else generation.noteDeath(process.pid, now(), StressPhase.CLEANUP, signal = LaunchProtocol.SIGKILL)
                }
                child.closeStreams()
                stdoutJob.join()
                stderrJob.join()
                while (true) handle(lines.tryReceive().getOrNull() ?: break)
                waitJob.join()
                prootExit?.let { status ->
                    generation.list().filter { it.kind == PROOT_KIND && it.diedAtMs == null }.forEach {
                        generation.noteDeath(it.pid, now(), StressPhase.CLEANUP, signal = ExecChecks.signalOf(status), exitStatus = status)
                    }
                }
                phase = phase.copy(
                    status = if (endReason == CANCELLED) StepStatus.CANCELLED else StepStatus.DONE,
                    endReason = endReason,
                    endedAtMs = now(),
                    deepSleepMs = deepSleep,
                    processes = generation.list(),
                    prootExitStatus = prootExit,
                    orchestratorStderr = stderrTail.text.takeIf { it.isNotEmpty() },
                    cleanup = CleanupResult(signalled = signalled, leftovers = leftovers, durationMs = clock.elapsedMs() - cleanupFrom),
                )
            }
        }
        phase
    }

    private fun sample(phase: StressPhase, host: StressHost, generation: StressGeneration, nowMs: Long): StressPhase {
        val importance = host.importance()
        return phase.copy(
            samples = phase.samples + 1,
            foregroundSamples = phase.foregroundSamples + if (importance <= IMPORTANCE_FOREGROUND) 1 else 0,
            foregroundServiceSamples = phase.foregroundServiceSamples + if (importance == IMPORTANCE_FOREGROUND_SERVICE) 1 else 0,
            otherImportanceSamples = phase.otherImportanceSamples + if (importance > IMPORTANCE_FOREGROUND_SERVICE) 1 else 0,
            screenOnSamples = phase.screenOnSamples + if (host.screenOn()) 1 else 0,
            deviceIdleSamples = phase.deviceIdleSamples + if (host.deviceIdle()) 1 else 0,
        )
    }

    companion object {
        const val ORCHESTRATOR = "/bin/bash"
        const val MARKER = "SC1"
        const val PROOT_KIND = "proot"
        const val ORCHESTRATOR_KIND = "orchestrator"
        const val SLEEPER = "sleeper"
        const val BUSY = "busy"

        /** AOSP `ActivityManagerConstants.DEFAULT_MAX_PHANTOM_PROCESSES` (system-wide, all apps). */
        const val PHANTOM_PROCESS_CAP = 32
        const val SLEEPERS = PHANTOM_PROCESS_CAP
        /** Two CPU-bound workers: sustained CPU is the second phantom-process rule. */
        const val BUSY_WORKERS = 2
        /** The self-check's death-timeline resolution (android/README.md "Device self-check"). */
        const val SAMPLE_INTERVAL_MS = 1_000L
        /** The self-check's liveness bounds for cleanup. */
        const val CLEANUP_DEADLINE_MS = 15_000L
        const val CLEANUP_POLL_MS = 100L

        /** AOSP power-check interval: each phase should span at least one system scan. */
        const val POWER_CHECK_INTERVAL_MINUTES = 5
        val PRESET_MINUTES = listOf(1, POWER_CHECK_INTERVAL_MINUTES, 3 * POWER_CHECK_INTERVAL_MINUTES, 12 * POWER_CHECK_INTERVAL_MINUTES)
        const val DEFAULT_PRESET_MINUTES = POWER_CHECK_INTERVAL_MINUTES

        const val COMPLETED = "completed"
        const val CANCELLED = "cancelled"
        const val LOCAL_KERNEL_STARTED = "local_kernel_started"
        const val ALL_DIED = "all_processes_died"
        const val LAUNCH_FAILED = "launch_failed"

        /** AOSP `RunningAppProcessInfo.IMPORTANCE_FOREGROUND` / `_FOREGROUND_SERVICE`. */
        private const val IMPORTANCE_FOREGROUND = 100
        private const val IMPORTANCE_FOREGROUND_SERVICE = 125
        /** android.os.Process.SIGNAL_QUIT: proot SIGKILLs every guest, then exits (android/native/README.md). */
        const val SIGNAL_QUIT = 3

        fun plan(minutes: Int) = StressPlan(
            sleepers = SLEEPERS,
            busyWorkers = BUSY_WORKERS,
            phaseDurationMs = minutes * 60_000L,
            sampleIntervalMs = SAMPLE_INTERVAL_MS,
            phantomProcessCap = PHANTOM_PROCESS_CAP,
        )

        /**
         * `$PPID` is proot's real pid (guest pids are kernel pids). `wait -n -p`
         * needs bash >= 5.1; Ubuntu 24.04 ships 5.2, and the loop simply reports
         * nothing on an older bash — the app's /proc polling still records deaths.
         */
        fun script(plan: StressPlan): String = buildString {
            fun line(text: String) = append(text).append('\n')
            line("printf '$MARKER hello pid=%s parent=%s\\n' \"\$\$\" \"\$PPID\"")
            line("pids=()")
            line("for i in \$(seq 1 ${plan.sleepers}); do")
            line("  sleep infinity & p=\$!; pids+=(\"\$p\"); printf '$MARKER spawn kind=$SLEEPER pid=%s\\n' \"\$p\"")
            line("done")
            line("for i in \$(seq 1 ${plan.busyWorkers}); do")
            line("  ( while :; do :; done ) & p=\$!; pids+=(\"\$p\"); printf '$MARKER spawn kind=$BUSY pid=%s\\n' \"\$p\"")
            line("done")
            line("printf '$MARKER ready children=%s\\n' \"\${#pids[@]}\"")
            line("n=\${#pids[@]}")
            line("while [ \"\$n\" -gt 0 ]; do")
            line("  p=0")
            line("  if wait -n -p p \"\${pids[@]}\" 2>/dev/null; then s=0; else s=\$?; fi")
            line("  if [ -n \"\$p\" ] && [ \"\$p\" != \"0\" ]; then")
            line("    printf '$MARKER exit pid=%s status=%s\\n' \"\$p\" \"\$s\"")
            line("  else")
            line("    break")
            line("  fi")
            line("  n=\$((n - 1))")
            line("done")
            line("printf '$MARKER done\\n'")
        }
    }
}

/** A bounded tail of a child's stderr; only an excerpt reaches the report. */
class BoundedTail(private val capacity: Int = 4096) {
    private val buffer = StringBuilder()

    @Synchronized
    fun append(line: String) {
        buffer.append(line).append('\n')
        if (buffer.length > capacity) buffer.delete(0, buffer.length - capacity)
    }

    val text: String get() = synchronized(this) { buffer.toString() }
}
