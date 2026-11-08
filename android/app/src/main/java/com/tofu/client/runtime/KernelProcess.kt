package com.tofu.client.runtime

import com.tofu.client.api.ApiV4Contract
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.Proxy

/** One proot invocation of the guest entry: argv, the complete environment, and the host working directory. */
data class KernelCommand(val argv: List<String>, val environment: Map<String, String>, val workingDirectory: File)

/**
 * Builds the proot command line: runtime contract v1, the launch contract in
 * docs/ANDROID_LOCAL_KERNEL.md and proot's run-time contract in
 * android/native/README.md. Holds no secret: the secret travels only on stdin.
 * - Required: `--kill-on-exit` (when the server, proot's first guest, exits,
 *   proot SIGKILLs every other guest and exits), `--link2symlink` (AOSP
 *   `app_neverallows.te` forbids hard links for apps), `--root-id` (dpkg/apt;
 *   matches the `/root` home mount), `--rootfs`, and `/dev`, `/proc`, `/sys`.
 * - Recommended by the proot lane, pending the S1 device spike: `--sysvipc`,
 *   `--ashmem-memfd`, and an app directory on `/dev/shm` (Python
 *   multiprocessing's `sem_open`).
 * - The environment is complete and explicit: proot hands it to the guest
 *   unchanged, so `LD_PRELOAD`/`LD_LIBRARY_PATH` must never be present.
 */
object KernelCommandLine {
    const val PROOT = "libproot.so"
    const val PROOT_LOADER = "libproot-loader.so"
    /** Ubuntu's default PATH with the payload's launchers first. */
    const val GUEST_PATH = "/opt/tofu/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    const val GUEST_LANG = "C.UTF-8"

    fun build(layout: KernelLayout, nativeLibraryDir: File, payloadDirectory: File): KernelCommand = KernelCommand(
        argv = listOf(
            File(nativeLibraryDir, PROOT).path,
            "--kill-on-exit",
            "--link2symlink",
            "--root-id",
            "--rootfs=${layout.rootfs.path}",
            "--bind=/dev",
            "--bind=/proc",
            "--bind=/sys",
            "--sysvipc",
            "--ashmem-memfd",
            "--bind=${layout.shm.path}:${GuestPaths.SHM}",
            "--bind=${payloadDirectory.path}:${GuestPaths.PAYLOAD}",
            "--bind=${layout.data.path}:${GuestPaths.DATA}",
            "--bind=${layout.home.path}:${GuestPaths.HOME}",
            "--cwd=${GuestPaths.PAYLOAD}",
            GuestPaths.ENTRY,
            "--data-dir",
            GuestPaths.DATA,
        ),
        environment = linkedMapOf(
            "PROOT_LOADER" to File(nativeLibraryDir, PROOT_LOADER).path,
            "PROOT_TMP_DIR" to layout.prootTmp.path,
            "PATH" to GUEST_PATH,
            "HOME" to GuestPaths.HOME,
            "LANG" to GUEST_LANG,
        ),
        workingDirectory = layout.root,
    )
}

/** The started proot process. */
interface KernelChild {
    val stdin: OutputStream
    val stdout: InputStream
    val stderr: InputStream
    /** Blocks until exit; a signal death reads as 0x80 + signal, as with every Unix shell. */
    fun waitFor(): Int
    fun isAlive(): Boolean
}

fun interface KernelLauncher {
    fun start(command: KernelCommand): KernelChild
}

/** ProcessBuilder with an explicit environment: nothing of the app process's own environment is inherited. */
class ProcessBuilderLauncher : KernelLauncher {
    override fun start(command: KernelCommand): KernelChild {
        val builder = ProcessBuilder(command.argv).directory(command.workingDirectory)
        builder.environment().clear()
        builder.environment().putAll(command.environment)
        val process = builder.start()
        return object : KernelChild {
            override val stdin: OutputStream = process.outputStream
            override val stdout: InputStream = process.inputStream
            override val stderr: InputStream = process.errorStream
            override fun waitFor(): Int = process.waitFor()
            override fun isAlive(): Boolean = process.isAlive
        }
    }
}

/** The single stdout record of the launch contract. */
data class ListeningRecord(val port: Int, val pid: Int)

/** How a kernel generation ended, from its exit status and whether the app asked it to stop. */
enum class KernelExit { REQUESTED_STOP, USAGE_ERROR, KILLED_BY_SYSTEM, CRASH }

/** Launch-contract constants and pure decisions (docs/ANDROID_LOCAL_KERNEL.md). */
object LaunchProtocol {
    const val LOOPBACK = "127.0.0.1"
    const val EXIT_USAGE = 2
    const val SIGKILL = 9
    const val SIGTERM = 15
    /** java.lang.Process reports a signal death as 0x80 + signal (libcore `UNIXProcess_md.c`). */
    const val EXIT_SIGKILL = 0x80 + SIGKILL

    /**
     * Stop escalation: the kernel's own drain deadline (`TOFU_SHUTDOWN_HARD_SECS`,
     * default 30 s, after which it exits 1) plus the desktop lifecycle
     * manager's margin (`server_manager.py` `WORKER_TERMINATE_TIMEOUT_MARGIN`,
     * 5 s), so the kernel's deadline always fires first.
     */
    const val STOP_ESCALATION_MS = (30 + 5) * 1000L

    private val EVENT = Regex("\"event\"\\s*:\\s*\"listening\"")
    private val HOST = Regex("\"host\"\\s*:\\s*\"127\\.0\\.0\\.1\"")
    private val PORT = Regex("\"port\"\\s*:\\s*(\\d{1,5})\\b")
    private val PID = Regex("\"pid\"\\s*:\\s*(\\d{1,10})\\b")

    /**
     * Parse `{"event":"listening","host":"127.0.0.1","port":N,"pid":P}`; null
     * when the line is anything else. The record is flat and server-owned, so
     * (like [com.tofu.client.session.ApiMetaGate]) fields are read with
     * anchored regexes and the pure-JVM tier needs no JSON library.
     */
    fun parseListening(line: String): ListeningRecord? {
        val text = line.trim()
        if (!text.startsWith("{") || !text.endsWith("}")) return null
        if (EVENT.find(text) == null || HOST.find(text) == null) return null
        val port = PORT.find(text)?.groupValues?.get(1)?.toIntOrNull() ?: return null
        val pid = PID.find(text)?.groupValues?.get(1)?.toIntOrNull() ?: return null
        return if (port in 1..65535 && pid > 0) ListeningRecord(port, pid) else null
    }

    /**
     * Classify the end of a generation from proot's status (the app's only child).
     *
     * - Reliable: whether the app asked for the stop, and whether proot itself
     *   died by SIGKILL (0x80 + 9). An unrequested SIGKILL has no error output
     *   of its own — a killed process cannot report anything — and on Android
     *   12+ is most likely the phantom-process limit, whose first victim is
     *   the app's oldest child, proot. It is not counted as a crash.
     * - A hint only: proot exits with the status of the last guest that exited
     *   normally, 255 if none did (android/native/README.md). In the server's
     *   own exits (the in-app `POST /api/v4/update/shutdown` drain = 0, a
     *   usage/secret error before boot = 2, an unhandled error = 1) the server
     *   is that last guest, so the launch contract's statuses apply; a guest's
     *   signal death can surface as any earlier status. Non-zero is restarted
     *   as a crash; a stale 0 stops without a restart loop.
     */
    fun classifyExit(status: Int, stopRequested: Boolean): KernelExit = when {
        stopRequested || status == 0 -> KernelExit.REQUESTED_STOP
        status == EXIT_USAGE -> KernelExit.USAGE_ERROR
        status == EXIT_SIGKILL -> KernelExit.KILLED_BY_SYSTEM
        else -> KernelExit.CRASH
    }
}

/** One readiness observation of `GET /api/v4/meta`. */
sealed interface Readiness {
    data object Ready : Readiness
    data object Booting : Readiness
    data object BootFailed : Readiness
    data class Refused(val status: Int) : Readiness
    data object Unreachable : Readiness
}

fun interface ReadinessProbe {
    fun check(port: Int): Readiness
}

/**
 * `GET http://127.0.0.1:<port>/api/v4/meta` without any credential (launch
 * contract §5): a presented-but-wrong secret is refused even on public
 * probes, and this client has no cookie jar. OkHttp sends `Host:
 * 127.0.0.1:<port>`, the only authority the kernel's Host fence accepts.
 * Loopback never goes through a configured Wi-Fi proxy, which cannot reach it.
 */
class MetaReadinessProbe(
    private val http: OkHttpClient = OkHttpClient.Builder()
        .proxy(Proxy.NO_PROXY)
        .followRedirects(false)
        .followSslRedirects(false)
        .build(),
) : ReadinessProbe {
    override fun check(port: Int): Readiness {
        val request = Request.Builder().url("http://${LaunchProtocol.LOOPBACK}:$port${ApiV4Contract.META_PATH}").get().build()
        return try {
            http.newCall(request).execute().use { response ->
                classify(response.code, if (response.code == 503) response.body?.string() else null)
            }
        } catch (error: IOException) {
            Readiness.Unreachable
        }
    }

    companion object {
        /** The boot gate's failed-boot error code (`lib/http_transport/request_lifecycle.py`). */
        const val BOOT_FAILED = "server_boot_failed"

        fun classify(status: Int, body: String?): Readiness = when {
            status == 200 -> Readiness.Ready
            status == 503 && body?.contains(BOOT_FAILED) == true -> Readiness.BootFailed
            status >= 500 -> Readiness.Booting
            else -> Readiness.Refused(status)
        }
    }
}

/**
 * Signals for the kernel generation (android/native/README.md "Stopping,
 * signals and exit status"); the device implementation wraps `android.os.Process`.
 * proot ignores SIGTERM and must never be SIGKILLed alone: it sets no
 * PTRACE_O_EXITKILL, so its guests would keep running untraced.
 */
interface ProcessSignals {
    /** Graceful stop: SIGTERM to the server's own pid, from its listening record. */
    fun terminate(pid: Int)

    /** Forced stop: SIGQUIT to proot (the app's direct child), which SIGKILLs every guest and exits. Returns how many were signalled. */
    fun forceStop(): Int

    /**
     * SIGKILL every other process of this app — used only when proot is gone:
     * before a launch and after proot exited, for guests a killed proot left
     * behind. Returns how many were signalled.
     */
    fun killAppChildren(): Int
}

/**
 * The app's processes from `/proc/<pid>/status`. Android mounts /proc with
 * hidepid=2, so an app sees only its own uid; the uid filter keeps the scan
 * exact everywhere. An app has one uid, and this app runs one process, so
 * every other process of the uid belongs to the kernel generation, and the
 * app process's direct children are the proot launchers.
 */
object AppProcessTable {
    data class Entry(val pid: Int, val parentPid: Int, val uid: Int)

    fun otherPids(procRoot: File, selfPid: Int, uid: Int): List<Int> =
        entries(procRoot).filter { it.uid == uid && it.pid != selfPid }.map { it.pid }

    fun childPids(procRoot: File, parentPid: Int, uid: Int): List<Int> =
        entries(procRoot).filter { it.uid == uid && it.parentPid == parentPid }.map { it.pid }

    private fun entries(procRoot: File): List<Entry> = procRoot.listFiles().orEmpty().mapNotNull { directory ->
        val pid = directory.name.toIntOrNull() ?: return@mapNotNull null
        val fields = try {
            File(directory, "status").readLines().associate { it.substringBefore(':') to it.substringAfter(':').trim() }
        } catch (error: IOException) {
            return@mapNotNull null // the process exited between listing and reading: nothing left to signal
        }
        val parentPid = fields["PPid"]?.toIntOrNull() ?: return@mapNotNull null
        val uid = fields["Uid"]?.split(Regex("\\s+"))?.firstOrNull()?.toIntOrNull() ?: return@mapNotNull null
        Entry(pid, parentPid, uid)
    }
}
