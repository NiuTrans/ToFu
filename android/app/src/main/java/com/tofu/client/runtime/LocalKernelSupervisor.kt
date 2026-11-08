package com.tofu.client.runtime

import com.tofu.client.session.DiagLog
import com.tofu.client.session.ServerLifecycle
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
import java.io.IOException
import java.io.InputStreamReader
import java.io.Reader

/** This build or device cannot run a local kernel. */
class LocalRuntimeUnavailableException(message: String) : IOException(message)

/** Everything one kernel generation needs: the command, the secret for its stdin, and which payload it runs. */
class PreparedLaunch(
    val command: KernelCommand,
    val secret: KernelSecretValue,
    val payloadVersion: String,
    /** True until [KernelRuntime.commit] switched `current` to this payload. */
    val pending: Boolean,
)

/** Install, secret and command assembly behind one seam, so the state machine runs on the plain JVM. */
interface KernelRuntime {
    /** [cancelled] is polled during long installs; throws [LocalRuntimeUnavailableException] without a runtime. */
    fun prepare(cancelled: () -> Boolean): PreparedLaunch
    /** The launched payload answered `/api/v4/meta`: make it `current`. */
    fun commit(launch: PreparedLaunch)
    /** The launched pending payload exhausted the restart budget: fall back to last-known-good. */
    fun reject(launch: PreparedLaunch)
}

/**
 * Launch contract §9: safe mode after 5 crashes within 120 s. These are the
 * desktop lifecycle manager's liveness constants (`server_manager.py`
 * `DEFAULT_MAX_FAILURES`, `DEFAULT_FAILURE_WINDOW`); a requested stop never
 * spends the budget, and an explicit start resets it.
 */
class RestartBudget {
    private val failures = ArrayDeque<Long>()

    /** Record a crash at [nowMs]; returns how many crashes fall inside the window, this one included. */
    @Synchronized
    fun recordCrash(nowMs: Long): Int {
        failures.addLast(nowMs)
        while (nowMs - failures.first() > WINDOW_MS) failures.removeFirst()
        return failures.size
    }

    @Synchronized
    fun reset() = failures.clear()

    companion object {
        const val MAX_FAILURES = 5
        const val WINDOW_MS = 120_000L
    }
}

/**
 * Owns the local kernel's lifecycle: install → launch → listening record →
 * readiness → Ready, crash restarts within [RestartBudget], [LocalKernelState.SafeMode]
 * when the budget is spent, and stop as "signal, then await exit" (a
 * generation is over only when its process has exited and its leftover app
 * processes are killed).
 *
 * Signals (android/native/README.md): proot ignores SIGTERM, so a stop sends
 * SIGTERM to the server pid from its listening record and `--kill-on-exit`
 * ends the rest. Before that record exists there is nothing to drain, and a
 * stop is forced at once: SIGQUIT to proot, which SIGKILLs every guest and
 * exits. A server that outlives [LaunchProtocol.STOP_ESCALATION_MS] after
 * SIGTERM is forced the same way. proot is never SIGKILLed alone; leftover
 * app processes are swept only once proot has exited.
 *
 * Pure Kotlin: Android specifics arrive through [KernelRuntime],
 * [KernelLauncher], [ProcessSignals] and [ReadinessProbe].
 */
class LocalKernelSupervisor(
    private val runtime: KernelRuntime,
    private val launcher: KernelLauncher,
    private val signals: ProcessSignals,
    private val readiness: ReadinessProbe,
    private val diagnostics: DiagLog? = null,
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
    private val clock: () -> Long = { System.nanoTime() / 1_000_000 },
    private val readinessPollMs: Long = READINESS_POLL_MS,
    private val stopEscalationMs: Long = LaunchProtocol.STOP_ESCALATION_MS,
) {
    private val lock = Any()
    private val mutableState = MutableStateFlow<LocalKernelState>(LocalKernelState.Stopped)
    val state: StateFlow<LocalKernelState> = mutableState.asStateFlow()

    private val budget = RestartBudget()
    private var running = false // guarded by lock: a supervision loop owns the kernel
    private var generation: Generation? = null // guarded by lock
    @Volatile private var stopRequested = false

    val endpoint: LocalKernelEndpoint? get() = (state.value as? LocalKernelState.Ready)?.endpoint

    /** User start. Idempotent while a kernel is starting, ready or stopping; clears Failed/SafeMode and the crash budget. */
    fun start(): Boolean = synchronized(lock) {
        if (running) return false
        running = true
        stopRequested = false
        budget.reset()
        mutableState.value = LocalKernelState.Starting
        record("I", "start requested")
        scope.launch(Dispatchers.IO) { supervise() }
        true
    }

    /** User stop; completes asynchronously (Stopping → Stopped once the process exited). */
    fun stop() {
        synchronized(lock) {
            if (!running) {
                mutableState.value = LocalKernelState.Stopped
                return
            }
            if (stopRequested) return
            stopRequested = true
            mutableState.value = LocalKernelState.Stopping
            record("I", "stop requested")
            generation?.sendStop()
        }
    }

    private suspend fun supervise() {
        var outcome: LocalKernelState = LocalKernelState.Stopped
        try {
            outcome = superviseGenerations()
        } catch (cancelled: CancellationException) {
            throw cancelled // the owning scope was cancelled; the finally block still publishes Stopped
        } catch (error: Exception) {
            diagnostics?.exception(TAG, error)
            if (!stopRequested) outcome = LocalKernelState.Failed(LocalKernelFailure.LaunchFailed(error.javaClass.simpleName))
        } finally {
            synchronized(lock) {
                running = false
                generation = null
                mutableState.value = outcome
            }
            record("I", "supervision ended state=${outcome.javaClass.simpleName}")
        }
    }

    private suspend fun superviseGenerations(): LocalKernelState {
        while (true) {
            val leftovers = signals.killAppChildren()
            if (leftovers > 0) record("W", "killed $leftovers leftover process(es) of an earlier generation")
            val launch = try {
                runtime.prepare { stopRequested }
            } catch (error: Exception) {
                if (stopRequested) return LocalKernelState.Stopped
                diagnostics?.exception(TAG, error)
                return LocalKernelState.Failed(failureOf(error))
            }
            if (stopRequested) return LocalKernelState.Stopped
            record("I", "launch payload=${launch.payloadVersion} pending=${launch.pending}")
            when (val ended = runGeneration(launch)) {
                is GenerationEnd.Final -> return ended.state
                is GenerationEnd.Crashed -> {
                    if (stopRequested) return LocalKernelState.Stopped
                    val crashes = budget.recordCrash(clock())
                    if (crashes >= RestartBudget.MAX_FAILURES) {
                        if (launch.pending) {
                            try {
                                runtime.reject(launch)
                            } catch (error: IOException) {
                                diagnostics?.exception(TAG, error)
                            }
                        }
                        record("E", "safe mode: $crashes crashes within ${RestartBudget.WINDOW_MS / 1000}s")
                        return LocalKernelState.SafeMode(crashes, ended.status)
                    }
                    record("W", "restarting after crash status=${ended.status} ($crashes within window)")
                    synchronized(lock) { if (!stopRequested) mutableState.value = LocalKernelState.Starting }
                }
            }
        }
    }

    private suspend fun runGeneration(launch: PreparedLaunch): GenerationEnd {
        val child = try {
            launcher.start(launch.command)
        } catch (error: IOException) {
            diagnostics?.exception(TAG, error)
            return GenerationEnd.Final(LocalKernelState.Failed(LocalKernelFailure.LaunchFailed(error.message ?: error.javaClass.simpleName)))
        }
        val current = Generation(child, launch)
        synchronized(lock) {
            generation = current
            if (stopRequested) current.sendStop()
        }
        try {
            return current.supervise()
        } finally {
            synchronized(lock) { generation = null }
        }
    }

    private sealed interface GenerationEnd {
        data class Final(val state: LocalKernelState) : GenerationEnd
        data class Crashed(val status: Int) : GenerationEnd
    }

    private inner class Generation(private val child: KernelChild, private val launch: PreparedLaunch) {
        private val stdout: Reader = InputStreamReader(child.stdout, Charsets.UTF_8)
        private val stderrTail = TextTail(DiagLog.MAX_MESSAGE_CHARS, launch.secret)
        @Volatile private var kernelPid: Int? = null
        private var stopSent = false // guarded by this
        @Volatile private var escalation: Job? = null
        private var violation: String? = null
        private var bootFailed = false

        suspend fun supervise(): GenerationEnd {
            writeSecret()
            val stderrDrain = scope.launch(Dispatchers.IO) {
                drain(InputStreamReader(child.stderr, Charsets.UTF_8)) { chars, count -> stderrTail.append(chars, count) }
            }
            val first = readFirstRecord()
            if (first != null) {
                val record = LaunchProtocol.parseListening(first)
                if (record == null) {
                    violation = "unexpected stdout record: ${launch.secret.scrub(first)}"
                    killNow()
                } else {
                    onListening(record)
                    scope.launch(Dispatchers.IO) { drainLateStdout() }
                    awaitReadiness(record)
                }
            }
            val status = child.waitFor()
            escalation?.cancel()
            signals.killAppChildren()
            stderrDrain.join()
            return end(status)
        }

        /** Launch contract §3: the secret is the first stdin line, then stdin closes. */
        private fun writeSecret() {
            try {
                child.stdin.use { it.write(launch.secret.stdinRecord()) }
            } catch (error: IOException) {
                record("W", "kernel closed stdin before reading the secret") // its exit status is classified below
            }
        }

        /** The first stdout line, bounded by the diagnostic record size; null at EOF before any output. */
        private fun readFirstRecord(): String? {
            val line = StringBuilder()
            while (line.length < DiagLog.MAX_MESSAGE_CHARS) {
                val next = stdout.read()
                if (next < 0) return if (line.isEmpty()) null else line.toString()
                if (next == '\n'.code) return line.toString()
                line.append(next.toChar())
            }
            return line.toString()
        }

        private fun onListening(record: ListeningRecord) {
            record("I", "listening port=${record.port} pid=${record.pid}")
            synchronized(this) {
                kernelPid = record.pid
                if (stopSent) signals.terminate(record.pid)
            }
        }

        private suspend fun awaitReadiness(record: ListeningRecord) {
            while (child.isAlive() && !stopRequested) {
                when (val observed = readiness.check(record.port)) {
                    Readiness.Ready -> {
                        onReady(record)
                        return
                    }
                    Readiness.BootFailed -> {
                        bootFailed = true
                        record("E", "kernel boot failed (server_boot_failed)")
                        sendStop()
                        return
                    }
                    is Readiness.Refused -> {
                        violation = "readiness probe answered HTTP ${observed.status}"
                        sendStop()
                        return
                    }
                    Readiness.Booting, Readiness.Unreachable -> delay(readinessPollMs)
                }
            }
        }

        private fun onReady(record: ListeningRecord) {
            if (launch.pending) {
                try {
                    runtime.commit(launch)
                } catch (error: IOException) {
                    diagnostics?.exception(TAG, error) // the kernel serves; the next launch retries the commit
                }
            }
            synchronized(lock) {
                if (!stopRequested) mutableState.value = LocalKernelState.Ready(LocalKernelEndpoint(record.port))
            }
            record("I", "ready port=${record.port} payload=${launch.payloadVersion}")
        }

        /** SIGTERM to the server (or force at once before it reported its pid), then force after the drain deadline. */
        fun sendStop() {
            synchronized(this) {
                if (stopSent) return
                stopSent = true
                val pid = kernelPid
                if (pid != null) signals.terminate(pid) else killNow()
                escalation = scope.launch {
                    delay(stopEscalationMs)
                    if (child.isAlive()) {
                        record("W", "kernel outlived its drain deadline; forcing it to stop")
                        signals.forceStop()
                    }
                }
            }
        }

        private fun killNow() {
            signals.forceStop()
        }

        private fun drainLateStdout() {
            var late = 0L
            drain(stdout) { _, count -> late += count }
            if (late > 0) record("W", "kernel wrote $late chars to stdout after its listening record")
        }

        private fun end(status: Int): GenerationEnd {
            val kind = LaunchProtocol.classifyExit(status, stopRequested)
            if (!stopRequested && kind != KernelExit.REQUESTED_STOP) {
                record("W", "kernel exited status=$status ($kind)")
                // A record of its own, so the newest stderr lines fit the diagnostic event size.
                record("W", stderrTail.toString())
            }
            val reported = violation
            return when {
                stopRequested -> GenerationEnd.Final(LocalKernelState.Stopped)
                reported != null -> GenerationEnd.Final(LocalKernelState.Failed(LocalKernelFailure.ProtocolViolation(reported)))
                bootFailed -> GenerationEnd.Crashed(status)
                kind == KernelExit.REQUESTED_STOP -> GenerationEnd.Final(LocalKernelState.Stopped)
                kind == KernelExit.USAGE_ERROR -> GenerationEnd.Final(LocalKernelState.Failed(LocalKernelFailure.UsageError))
                kind == KernelExit.KILLED_BY_SYSTEM -> GenerationEnd.Final(LocalKernelState.Failed(LocalKernelFailure.KilledBySystem))
                else -> GenerationEnd.Crashed(status)
            }
        }
    }

    private fun failureOf(error: Exception): LocalKernelFailure = when (error) {
        is LocalRuntimeUnavailableException -> LocalKernelFailure.NotAvailable(error.message.orEmpty())
        is KernelSecretUnavailableException -> LocalKernelFailure.SecretUnavailable(error.message.orEmpty())
        else -> LocalKernelFailure.InstallFailed(error.message ?: error.javaClass.simpleName)
    }

    private fun record(level: String, message: String) {
        diagnostics?.record(level, TAG, message)
    }

    companion object {
        const val TAG = "LocalKernel"
        /** The app's existing cadence for polling a starting Tofu server ([ServerLifecycle]). */
        const val READINESS_POLL_MS = ServerLifecycle.START_POLL_INTERVAL_MS

        private fun drain(reader: Reader, sink: (CharArray, Int) -> Unit) {
            val buffer = CharArray(4096)
            try {
                while (true) {
                    val count = reader.read(buffer)
                    if (count < 0) return
                    sink(buffer, count)
                }
            } catch (error: IOException) {
                return // the pipe closed with the process: nothing more to read
            }
        }
    }
}

/**
 * The last [capacity] characters of a stream, for crash diagnostics, with the
 * secret scrubbed as soon as an occurrence is complete: before any trim could
 * cut it, however the pipe splits its reads.
 */
internal class TextTail(private val capacity: Int, private val secret: KernelSecretValue) {
    private val buffer = StringBuilder()

    @Synchronized
    fun append(chars: CharArray, count: Int) {
        val from = buffer.length - (secret.length - 1)
        buffer.append(chars, 0, count)
        secret.scrubFrom(buffer, from)
        if (buffer.length > 2 * capacity) buffer.delete(0, buffer.length - capacity)
    }

    @Synchronized
    override fun toString(): String = if (buffer.length <= capacity) buffer.toString() else buffer.substring(buffer.length - capacity)
}
