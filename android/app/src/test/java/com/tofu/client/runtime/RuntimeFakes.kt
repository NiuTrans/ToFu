package com.tofu.client.runtime

import com.tofu.client.session.DiagLog
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.util.concurrent.CompletableFuture
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

const val TEST_SECRET = "tofu_admin_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

fun testSecret() = KernelSecretValue(TEST_SECRET)

/** A stream the test feeds; reads block until data or EOF arrives, like a pipe. */
class ScriptedStream : InputStream() {
    private val queue = LinkedBlockingQueue<Int>()

    fun emit(text: String) = text.toByteArray(Charsets.UTF_8).forEach { queue.put(it.toInt() and 0xFF) }

    fun eof() = queue.put(-1)

    override fun read(): Int {
        val value = queue.take()
        if (value < 0) queue.put(-1)
        return value
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (length == 0) return 0
        val first = read()
        if (first < 0) return -1
        buffer[offset] = first.toByte()
        var count = 1
        while (count < length) {
            val next = queue.peek() ?: break
            if (next < 0) break
            buffer[offset + count] = queue.take().toByte()
            count++
        }
        return count
    }
}

/** A kernel child whose output and exit the test scripts. */
class FakeChild(val pid: Int) : KernelChild {
    val stdinBytes = ByteArrayOutputStream()
    @Volatile var stdinClosed = false
    val out = ScriptedStream()
    val err = ScriptedStream()
    private val exit = CompletableFuture<Int>()

    override val stdin: OutputStream = object : OutputStream() {
        override fun write(byte: Int) {
            if (stdinClosed) throw IOException("closed")
            stdinBytes.write(byte)
        }

        override fun close() {
            stdinClosed = true
        }
    }
    override val stdout: InputStream get() = out
    override val stderr: InputStream get() = err

    override fun waitFor(): Int = exit.get()
    override fun isAlive(): Boolean = !exit.isDone

    fun listen(port: Int) = out.emit("{\"event\": \"listening\", \"host\": \"127.0.0.1\", \"port\": $port, \"pid\": $pid}\n")

    fun exit(status: Int) {
        if (exit.isDone) return
        out.eof()
        err.eof()
        exit.complete(status)
    }

    fun exitValue(): Int? = if (exit.isDone) exit.get() else null
}

/** Starts [FakeChild]ren and lets each test script them by launch index (0-based). */
class FakeLauncher(private val script: (FakeChild, Int) -> Unit) : KernelLauncher {
    val children = CopyOnWriteArrayList<FakeChild>()
    val commands = CopyOnWriteArrayList<KernelCommand>()
    var failure: IOException? = null

    override fun start(command: KernelCommand): KernelChild {
        failure?.let { throw it }
        commands += command
        val child = FakeChild(pid = 4000 + children.size)
        children += child
        Thread { script(child, children.size - 1) }.apply { isDaemon = true }.start()
        return child
    }
}

/** proot's status when no guest exited normally, e.g. after SIGQUIT made it kill every guest (android/native/README.md). */
const val PROOT_NO_NORMAL_EXIT = 255

/**
 * SIGTERM goes to the kernel pid; SIGQUIT to "proot" makes every live fake
 * child exit 255; "kill app children" SIGKILLs them (exit 137).
 */
class FakeSignals(private val launcher: () -> FakeLauncher) : ProcessSignals {
    val terminated = CopyOnWriteArrayList<Int>()
    val forceStops = AtomicInteger()
    val killCalls = AtomicInteger()
    /** Default: the kernel drains and exits 0 on SIGTERM. */
    @Volatile var onTerminate: (FakeChild) -> Unit = { it.exit(0) }

    override fun terminate(pid: Int) {
        terminated += pid
        launcher().children.firstOrNull { it.pid == pid }?.let { onTerminate(it) }
    }

    override fun forceStop(): Int {
        forceStops.incrementAndGet()
        val alive = launcher().children.filter { it.isAlive() }
        alive.forEach { it.exit(PROOT_NO_NORMAL_EXIT) }
        return alive.size
    }

    override fun killAppChildren(): Int {
        killCalls.incrementAndGet()
        val alive = launcher().children.filter { it.isAlive() }
        alive.forEach { it.exit(LaunchProtocol.EXIT_SIGKILL) }
        return alive.size
    }
}

class ScriptedReadiness(private vararg val answers: Readiness) : ReadinessProbe {
    val checks = AtomicInteger()
    override fun check(port: Int): Readiness {
        val index = checks.getAndIncrement()
        return answers[minOf(index, answers.size - 1)]
    }
}

class FakeRuntime(private val pending: Boolean = true) : KernelRuntime {
    val commits = CopyOnWriteArrayList<String>()
    val rejects = CopyOnWriteArrayList<String>()
    @Volatile var failure: Exception? = null
    /** Counts generations prepared; [gate], when set, holds each prepare until it opens. */
    val prepares = AtomicInteger()
    @Volatile var gate: java.util.concurrent.CountDownLatch? = null
    val command = KernelCommand(listOf("/data/app/lib/arm64/libproot.so", "--kill-on-exit"), mapOf("PROOT_TMP_DIR" to "/tmp"), File("/"))

    override fun prepare(cancelled: () -> Boolean): PreparedLaunch {
        prepares.incrementAndGet()
        gate?.await()
        failure?.let { throw it }
        return PreparedLaunch(command, testSecret(), "v1", pending)
    }

    override fun commit(launch: PreparedLaunch) {
        commits += launch.payloadVersion
    }

    override fun reject(launch: PreparedLaunch) {
        rejects += launch.payloadVersion
    }
}

class Harness(
    pending: Boolean = true,
    readiness: ReadinessProbe = ScriptedReadiness(Readiness.Ready),
    clockStepMs: Long = 0,
    script: (FakeChild, Int) -> Unit,
) {
    val runtime = FakeRuntime(pending)
    val launcher = FakeLauncher(script)
    val signals = FakeSignals { launcher }
    val diagnostics = DiagLog(capacity = 500)
    private val now = AtomicLong(1_000_000)
    val supervisor = LocalKernelSupervisor(
        runtime = runtime,
        launcher = launcher,
        signals = signals,
        readiness = readiness,
        diagnostics = diagnostics,
        scope = CoroutineScope(SupervisorJob() + Dispatchers.IO),
        clock = { now.getAndAdd(clockStepMs) },
        readinessPollMs = 5,
        stopEscalationMs = 200,
    )

    fun await(predicate: (LocalKernelState) -> Boolean): LocalKernelState =
        runBlocking { withTimeout(10_000) { supervisor.state.first(predicate) } }

    fun diagnosticsText(): String = diagnostics.snapshot().joinToString("\n") { "${it.tag}: ${it.message}" }
}
