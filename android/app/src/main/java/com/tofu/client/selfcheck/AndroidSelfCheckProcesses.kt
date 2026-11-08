package com.tofu.client.selfcheck

import android.app.ActivityManager
import android.app.ApplicationExitInfo
import android.content.Context
import android.os.Build
import android.os.PowerManager
import com.tofu.client.runtime.KernelCommand
import com.tofu.client.runtime.LaunchProtocol
import com.tofu.client.runtime.LocalKernel
import com.tofu.client.runtime.LocalKernelState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runInterruptible
import kotlinx.coroutines.withTimeout
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.IOException
import java.net.Proxy
import java.util.concurrent.TimeUnit

/** The device's clocks: elapsedRealtime for durations (deep sleep included), uptime to measure that sleep. */
object AndroidClock : SelfCheckClock {
    override fun wallMs(): Long = System.currentTimeMillis()
    override fun elapsedMs(): Long = android.os.SystemClock.elapsedRealtime()
    override fun uptimeMs(): Long = android.os.SystemClock.uptimeMillis()
}

/** `/proc/<pid>/stat`, signals and app visibility for [ChildStress]. */
class AndroidStressHost(private val context: Context) : StressHost {
    override val procRoot: File = File("/proc")

    override fun launch(command: KernelCommand): StressChild {
        val builder = ProcessBuilder(command.argv).directory(command.workingDirectory)
        builder.environment().clear()
        builder.environment().putAll(command.environment)
        return AndroidStressChild(builder.start())
    }

    override fun stat(pid: Int): ProcStat? = try {
        ProcStat.parse(File(procRoot, "$pid/stat").readText())
    } catch (error: IOException) {
        null // the process exited between listing and reading
    }

    override fun signal(pid: Int, signal: Int) = android.os.Process.sendSignal(pid, signal)

    override fun importance(): Int = ActivityManager.RunningAppProcessInfo().also(ActivityManager::getMyMemoryState).importance

    override fun screenOn(): Boolean = (context.getSystemService(Context.POWER_SERVICE) as PowerManager).isInteractive

    override fun deviceIdle(): Boolean = (context.getSystemService(Context.POWER_SERVICE) as PowerManager).isDeviceIdleMode
}

class AndroidStressChild(private val process: Process) : StressChild {
    /** libcore `UNIXProcess.toString()` is `Process[pid=N …]`; there is no public pid accessor. */
    override val pid: Int? = PID.find(process.toString())?.groupValues?.get(1)?.toIntOrNull()
    override val stdout = process.inputStream
    override val stderr = process.errorStream

    override fun isAlive(): Boolean = process.isAlive
    override fun waitFor(): Int = process.waitFor()

    override fun closeStreams() {
        runCatching { process.outputStream.close() }
        runCatching { stdout.close() }
        runCatching { stderr.close() }
    }

    companion object {
        private val PID = Regex("""Process\[pid=(\d+)""")
    }
}

/** Runs one command to completion or [ExecChecks.DEADLINE_MS], never leaving a child behind. */
class AndroidCommandRunner(private val host: AndroidStressHost) : CommandRunner {
    override fun run(command: KernelCommand): CommandOutcome {
        val startedAt = System.nanoTime()
        val child = try {
            host.launch(command)
        } catch (error: IOException) {
            return CommandOutcome(started = false, exitStatus = null, stdout = "", stderr = "", startError = error.message ?: error.javaClass.simpleName, timedOut = false, durationMs = elapsedMs(startedAt))
        }
        child.closeStreams()
        val stdout = BoundedTail()
        val stderr = BoundedTail()
        val outThread = pump(child.stdout, stdout)
        val errThread = pump(child.stderr, stderr)
        var timedOut = false
        var status: Int? = null
        try {
            val exited = processWait(child, ExecChecks.DEADLINE_MS)
            if (!exited) {
                timedOut = true
                stopTree(child)
                status = runCatching { child.waitFor() }.getOrNull()
            } else {
                status = child.waitFor()
            }
        } finally {
            child.closeStreams()
            outThread.join(2_000)
            errThread.join(2_000)
        }
        return CommandOutcome(
            started = true,
            exitStatus = status,
            stdout = stdout.text,
            stderr = stderr.text,
            startError = null,
            timedOut = timedOut,
            durationMs = elapsedMs(startedAt),
        )
    }

    /** proot ignores SIGTERM: SIGQUIT makes it SIGKILL its guests, then SIGKILL any survivor. */
    private fun stopTree(child: StressChild) {
        val pid = child.pid ?: return
        runCatching { host.signal(pid, ChildStress.SIGNAL_QUIT) }
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
        while (child.isAlive() && System.nanoTime() < deadline) Thread.sleep(50)
        if (child.isAlive()) runCatching { host.signal(pid, LaunchProtocol.SIGKILL) }
    }

    private fun processWait(child: StressChild, timeoutMs: Long): Boolean {
        val deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs)
        while (System.nanoTime() < deadline) {
            if (!child.isAlive()) return true
            Thread.sleep(20)
        }
        return !child.isAlive()
    }

    private fun pump(stream: java.io.InputStream, tail: BoundedTail): Thread = Thread {
        try {
            stream.bufferedReader().useLines { lines -> lines.forEach { tail.append(it) } }
        } catch (_: IOException) {
            // the pipe closed with the process
        }
    }.apply { isDaemon = true; start() }

    private fun elapsedMs(startedAt: Long): Long = (System.nanoTime() - startedAt) / 1_000_000
}

/** `LocalKernel` behind the self-check's seam. */
class AndroidKernelControl(context: Context) : KernelControl {
    private val app = context.applicationContext
    override val state = LocalKernel.state(app)

    override fun start(): KernelStartOutcome = when (LocalKernel.start(app)) {
        LocalKernel.StartResult.Requested -> KernelStartOutcome.REQUESTED
        is LocalKernel.StartResult.NotAvailable -> KernelStartOutcome.NOT_AVAILABLE
        LocalKernel.StartResult.NotInForeground -> KernelStartOutcome.NOT_IN_FOREGROUND
    }

    override fun stop() = LocalKernel.stop(app)
    override fun secret() = LocalKernel.secret(app)
}

/** Loopback only, no proxy, no redirects, no cookie jar; bodies are read for `/api/v4/meta` alone. */
class SelfCheckHttpProbe : HttpProbe {
    private val client = OkHttpClient.Builder()
        .proxy(Proxy.NO_PROXY)
        .followRedirects(false)
        .followSslRedirects(false)
        .build()

    override fun get(url: String, bearer: String?, readBody: Boolean): HttpOutcome {
        val request = Request.Builder().url(url).get()
            .apply { if (bearer != null) header("Authorization", "Bearer $bearer") }
            .build()
        val startedAt = System.nanoTime()
        return try {
            client.newCall(request).execute().use { response ->
                val body = if (readBody) response.body?.string() else null
                HttpOutcome(status = response.code, error = null, durationMs = (System.nanoTime() - startedAt) / 1_000_000, body = body)
            }
        } catch (error: IOException) {
            HttpOutcome(status = null, error = "${error.javaClass.simpleName}: ${error.message}", durationMs = (System.nanoTime() - startedAt) / 1_000_000)
        }
    }
}

/** `ActivityManager.getHistoricalProcessExitReasons` for the app's own process (API 30+). */
fun selfCheckExitReasons(context: Context): List<ExitReasonRecord> {
    if (Build.VERSION.SDK_INT < 30) return emptyList()
    val activity = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
    return activity.getHistoricalProcessExitReasons(context.packageName, 0, 0).map { info ->
        ExitReasonRecord(
            timestampMs = info.timestamp,
            pid = info.pid,
            processName = info.processName.orEmpty(),
            reason = info.reason,
            status = info.status,
            importance = info.importance,
            pssKb = info.pss.toLong(),
            rssKb = info.rss.toLong(),
            description = systemDescription(info),
        )
    }
}

/** Crash and ANR descriptions are app-authored text (and a crash trace quotes source lines): keep only system-authored ones. */
private fun systemDescription(info: ApplicationExitInfo): String? = when (info.reason) {
    ApplicationExitInfo.REASON_CRASH, ApplicationExitInfo.REASON_CRASH_NATIVE, ApplicationExitInfo.REASON_ANR -> null
    else -> info.description
}

/** True while the kernel runs or is coming up: its processes must not mix with the stress test's. */
fun localKernelActive(state: LocalKernelState): Boolean =
    state is LocalKernelState.Starting || state is LocalKernelState.Ready || state is LocalKernelState.Stopping
