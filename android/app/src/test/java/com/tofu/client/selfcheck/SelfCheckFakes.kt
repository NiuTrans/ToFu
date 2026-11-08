package com.tofu.client.selfcheck

import com.tofu.client.runtime.KernelCommand
import com.tofu.client.runtime.KernelLayout
import com.tofu.client.runtime.KernelSecretValue
import com.tofu.client.runtime.LocalKernelEndpoint
import com.tofu.client.runtime.LocalKernelState
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.io.ByteArrayInputStream
import java.io.File
import java.io.InputStream

class FakeClock(var now: Long = 1_700_000_000_000L) : SelfCheckClock {
    var elapsed: Long = 0L
    var uptime: Long = 0L

    override fun wallMs(): Long = now
    override fun elapsedMs(): Long = elapsed
    override fun uptimeMs(): Long = uptime

    fun advance(ms: Long, deepSleepMs: Long = 0L) {
        elapsed += ms
        uptime += ms - deepSleepMs
        now += ms
    }
}

class FakeStore : SelfCheckStore {
    val reports = mutableListOf<SelfCheckReport>()
    var interrupted: Json.Obj? = null
    var heartbeatMs: Long? = null
    var pidList: List<Int> = emptyList()
    var previousJson: String? = null
    var unfinished = false

    override fun writeReport(report: SelfCheckReport) {
        reports += report
        unfinished = report.status == RunStatus.RUNNING
    }

    override fun writeInterrupted(reportId: String, detectedAtWallMs: Long, lastHeartbeatWallMs: Long?, runStartedAtWallMs: Long?, exitReasons: List<ExitReasonRecord>, previousReportJson: String?) {
        interrupted = ReportJson.interrupted(reportId, detectedAtWallMs, lastHeartbeatWallMs, runStartedAtWallMs, exitReasons, previousReportJson)
        unfinished = false
    }

    override fun writeHeartbeat(wallMs: Long) { heartbeatMs = wallMs }
    override fun writePids(pids: List<Int>) { pidList = pids }
    override fun pids(): List<Int> = pidList
    override fun heartbeat(): Long? = heartbeatMs
    override fun previousReportJson(): String? = previousJson
    override fun interruptedRun(): Boolean = unfinished
    override fun clear() { unfinished = false; pidList = emptyList() }
}

class FakeRuntime(
    private val files: File,
    private val missingRuntime: String? = null,
    private val rootfs: Boolean = true,
    private val payload: Boolean = true,
) : RuntimeProbe {
    override val layout = KernelLayout(files)
    override val nativeLibraryDir: File = File(files, "native").apply { mkdirs() }
    override fun missing(): String? = missingRuntime
    override fun rootfsInstalled(): Boolean = rootfs
    override fun payloadDirectory(): File? = if (payload) layout.payload("v1").apply { mkdirs() } else null
}

/** Answers by the command's guest program, or by a script keyed on an id substring. */
/** A fresh private temp directory (kotlin.io.path, not the deprecated kotlin.io.createTempDir). */
fun selfCheckTempDir(): File = java.nio.file.Files.createTempDirectory("selfcheck-test").toFile()

class FakeCommandRunner : CommandRunner {
    data class Call(val argv: List<String>, val environment: Map<String, String>)
    val calls = mutableListOf<Call>()
    var defaultStatus = 0
    val byMarker = mutableMapOf<String, CommandOutcome>()

    override fun run(command: KernelCommand): CommandOutcome {
        calls += Call(command.argv, command.environment)
        val key = byMarker.keys.firstOrNull { marker -> command.argv.any { it.contains(marker) } }
        return byMarker[key] ?: CommandOutcome(
            started = true,
            exitStatus = defaultStatus,
            stdout = when {
                command.argv.size == 2 && command.argv.first().endsWith("libproot.so") && command.argv[1] == "--version" -> "proot 5.1.107.102\nbuilt-in accelerators: process_vm = yes, seccomp_filter = yes\n"
                command.argv.contains("echo ok") -> "ok\n"
                command.argv.contains("-V") -> "Python 3.12.8\n"
                command.argv.contains("--version") -> "git version 2.43.0\n"
                else -> ""
            },
            stderr = "",
            startError = null,
            timedOut = false,
            durationMs = 5,
        )
    }

    fun outcome(started: Boolean = true, status: Int? = 0, stdout: String = "", stderr: String = "", error: String? = null, timedOut: Boolean = false) =
        CommandOutcome(started, status, stdout, stderr, error, timedOut, 7)
}

class FakeKernel(initial: LocalKernelState = LocalKernelState.Stopped) : KernelControl {
    private val flow = MutableStateFlow<LocalKernelState>(initial)
    override val state: StateFlow<LocalKernelState> = flow
    var startOutcome = KernelStartOutcome.REQUESTED
    /** The state a requested start settles at; a real supervisor reaches Ready or Failed. */
    var settleState: LocalKernelState? = null
    var onStart: (FakeKernel) -> Unit = {}
    val stops = mutableListOf<Long>()
    var secretValue: KernelSecretValue = KernelSecretValue(KernelSecretValue.PREFIX + "a".repeat(64))

    override fun start(): KernelStartOutcome {
        if (startOutcome != KernelStartOutcome.REQUESTED) return startOutcome
        flow.value = LocalKernelState.Starting
        settleState?.let { flow.value = it }
        onStart(this)
        return startOutcome
    }

    override fun stop() { flow.value = LocalKernelState.Stopped }
    override fun secret(): KernelSecretValue = secretValue

    fun publish(value: LocalKernelState) { flow.value = value }
    fun ready(port: Int = 41001) = LocalKernelState.Ready(LocalKernelEndpoint(port))
}

class FakeHttp : HttpProbe {
    data class Call(val url: String, val credential: Boolean, val readBody: Boolean)
    val calls = mutableListOf<Call>()
    var metaBody = """{"data":{"apiMajor":4,"schemaVersion":1,"serverBuild":"0.16.0","minAndroidBuild":17,"minDesktopBuild":"0.16.0"},"meta":{"requestId":"r1","serverTimeMs":1}}"""
    var statuses = mutableMapOf<String, Int>()

    override fun get(url: String, bearer: String?, readBody: Boolean): HttpOutcome {
        calls += Call(url, bearer != null, readBody)
        val path = url.substringAfter("41001").ifEmpty { url }
        val status = statuses[path] ?: when {
            url.endsWith("/api/v4/meta") -> 200
            bearer != null -> 200
            else -> 401
        }
        return HttpOutcome(status = status, error = null, durationMs = 3, body = if (readBody) metaBody else null)
    }
}

/** An in-memory process table: deaths are injected, signals are recorded, and the orchestrator's stdout is scripted. */
class FakeStressHost(private val clock: FakeClock) : StressHost {
    override val procRoot: File = File("/nonexistent-proc")
    val stats = mutableMapOf<Int, ProcStat>()
    val signals = mutableListOf<Pair<Int, Int>>()
    var appImportance = 100
    var interactive = true
    var idle = false
    var nextChild: FakeStressChild? = null
    var launchError: Exception? = null
    val launches = mutableListOf<KernelCommand>()

    fun spawn(pid: Int, parent: Int = 1, startTicks: Long = pid * 10L, cpu: Long = 0) {
        stats[pid] = ProcStat(pid, 'S', parent, startTicks, cpu, 0)
    }

    fun die(pid: Int, signal: Int? = null) {
        stats.remove(pid)
        if (pid == nextChild?.pid) nextChild?.waitFor()
    }

    fun zombie(pid: Int, exitCode: Int) {
        stats[pid] = stats.getValue(pid).copy(state = 'Z', exitCode = exitCode)
    }

    override fun launch(command: KernelCommand): StressChild {
        launches += command
        launchError?.let { throw it }
        return nextChild ?: FakeStressChild(
            this,
            pid = 9000,
            // A minimal live orchestrator: hello, one sleeper, ready, then silence until cleanup.
            lines = listOf("SC1 hello pid=9001 parent=9000", "SC1 spawn kind=sleeper pid=9002", "SC1 ready children=1"),
            aliveForever = true,
        ).also { if (stats.isEmpty()) { spawn(9000); spawn(9001); spawn(9002) } }
    }

    var onStat: (Int) -> Unit = {}

    override fun stat(pid: Int): ProcStat? {
        onStat(pid)
        // The fake clock only moves when the app observes the world (a real one ticks on its own).
        clock.advance(clockTickMs)
        return stats[pid]
    }

    /** Milliseconds the fake clock advances per /proc poll. */
    var clockTickMs: Long = 1_000L

    override fun signal(pid: Int, signal: Int) {
        signals += pid to signal
        // A real SIGKILL/SIGQUIT removes the process; the fake must too, or cleanup can never finish.
        if (signal == 9 || signal == 3) stats.remove(pid)
    }
    override fun importance(): Int = appImportance
    override fun screenOn(): Boolean = interactive
    override fun deviceIdle(): Boolean = idle
}

class FakeStressChild(
    private val host: FakeStressHost,
    override val pid: Int?,
    lines: List<String> = emptyList(),
    private val exitStatus: Int = 0,
    private val aliveForever: Boolean = false,
) : StressChild {
    private val queue = java.util.concurrent.LinkedBlockingQueue<String>()
    @Volatile var exited = false
    @Volatile var reads = 0

    init {
        queue += lines
        if (!aliveForever) queue += EOF
    }

    override val stdout: InputStream = object : InputStream() {
        private var buffer = ByteArray(0)
        private var offset = 0
        override fun read(): Int {
            while (offset >= buffer.size) {
                val line = queue.take()
                if (line === EOF) return -1
                buffer = (line + "\n").toByteArray()
                offset = 0
                reads++
            }
            return buffer[offset++].toInt() and 0xff

        }

        override fun close() {
            queue.offer(EOF)
        }
    }
    override val stderr: InputStream = ByteArrayInputStream(ByteArray(0))

    override fun isAlive(): Boolean = !exited && host.stat(pid ?: -1) != null
    override fun waitFor(): Int {
        // A real wait blocks until the process exits: the fake waits for its stat entry to disappear.
        val deadline = System.nanoTime() + 5_000_000_000L
        while (host.stat(pid ?: -1) != null && System.nanoTime() < deadline) Thread.sleep(1)
        exited = true
        queue.offer(EOF)
        return exitStatus
    }
    override fun closeStreams() {
        exited = true
        runCatching { stdout.close() }
        runCatching { stderr.close() }
    }

    companion object {
        private const val EOF = "\u0000EOF"
    }
}

class FakeEnvironment(
    private val fakeClock: FakeClock = FakeClock(),
    override val store: FakeStore = FakeStore(),
    val runtimeProbe: FakeRuntime,
    val commandRunner: FakeCommandRunner = FakeCommandRunner(),
    val kernelControl: FakeKernel = FakeKernel(),
    val httpProbe: FakeHttp = FakeHttp(),
    val stress: FakeStressHost = FakeStressHost(fakeClock),
    var runtimeMissingFacts: Boolean = false,
) : SelfCheckEnvironment {
    var factsValue: DeviceFacts? = null
    var active = false
    var foreground = true
    var pollMs = 1L
    var exits: List<ExitReasonRecord> = emptyList()

    override val clock: SelfCheckClock get() = fakeClock
    override fun facts(): DeviceFacts = factsValue ?: deviceFacts(runtimeProbe)
    override fun runtime(): RuntimeProbe = runtimeProbe
    override fun runner(): CommandRunner = commandRunner
    override fun kernel(): KernelControl = kernelControl
    override fun http(): HttpProbe = httpProbe
    override fun stressHost(): StressHost = stress
    override fun exitReasons(): List<ExitReasonRecord> = exits
    override fun kernelActive(): Boolean = active
    override fun inForeground(): Boolean = foreground
    override fun newReportId(): String = "selfchecktest1"
    override fun nextPollMs(): Long = pollMs

    /** Drives the fake clock forward while a run is in flight (cleanup deadlines need elapsed time to advance). */
    suspend fun advanceUntil(run: SelfCheckRun, maxMs: Long = 60_000L) {
        var waited = 0L
        while (run.state.value.running && waited < maxMs) {
            fakeClockStep()
            kotlinx.coroutines.yield()
            kotlinx.coroutines.delay(1)
            waited += 1
        }
    }

    private fun fakeClockStep() = fakeClock.advance(1_000)
}

fun deviceFacts(runtime: RuntimeProbe? = null): DeviceFacts = DeviceFacts(
    manufacturer = "Google",
    model = "Pixel 8",
    device = "shiba",
    release = "14",
    sdkInt = 34,
    securityPatch = "2026-10-05",
    pageSizeBytes = 4096,
    clockTicksPerSecond = 100,
    supportedAbis = listOf("arm64-v8a"),
    kernelRelease = "5.15.149-android14",
    appVersionName = "0.1.18",
    appVersionCode = 19,
    appBuildId = "0123456789abcdef",
    targetSdk = 34,
    freeStorageBytes = 10_000_000_000L,
    totalStorageBytes = 128_000_000_000L,
    totalRamBytes = 8_000_000_000L,
    ignoringBatteryOptimizations = false,
    standbyBucket = 10,
    backgroundRestricted = false,
    postNotificationsGranted = true,
    notificationsEnabled = true,
    runtime = RuntimeFacts(
        inThisBuild = runtime?.missing() == null,
        missing = runtime?.missing(),
        rootfsVersion = "rootfs-v1",
        payloadVersion = "payload-v1",
        indexError = null,
        nativeLibraries = listOf(NativeLibraryFact("libproot.so", true, 280272, "d".repeat(64))),
        installedRootfsVersion = null,
        currentPayload = null,
    ),
    phantomProcesses = PhantomSettingFacts(
        monitorFlagGlobal = SettingRead("settings_enable_monitor_phantom_procs", true, null, "note"),
        monitorFlagProperty = SettingRead("persist.sys.fflag.override.x", false, null, "not readable"),
        maxPhantomProcesses = SettingRead("activity_manager/max_phantom_processes", false, null, "not readable"),
    ),
)
