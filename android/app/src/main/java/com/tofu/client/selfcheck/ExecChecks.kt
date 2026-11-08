package com.tofu.client.selfcheck

import com.tofu.client.runtime.GuestPaths
import com.tofu.client.runtime.KernelCommand
import com.tofu.client.runtime.KernelCommandLine
import com.tofu.client.runtime.KernelLayout
import java.io.File

/** One command run to completion or to [ExecChecks.DEADLINE_MS]. */
data class CommandOutcome(
    val started: Boolean,
    /** java.lang.Process status: a signal death reads as 0x80 + signal (libcore `UNIXProcess_md.c`). */
    val exitStatus: Int?,
    val stdout: String,
    val stderr: String,
    /** Why it could not start: the IOException message, which carries `error=<errno>`. */
    val startError: String?,
    val timedOut: Boolean,
    val durationMs: Long,
)

fun interface CommandRunner {
    fun run(command: KernelCommand): CommandOutcome
}

/** Where this build's local runtime lives on this device. */
interface RuntimeProbe {
    val layout: KernelLayout
    val nativeLibraryDir: File
    /** Null when this build carries the runtime; otherwise what is missing. */
    fun missing(): String?
    fun rootfsInstalled(): Boolean
    /** The payload a launch would run, if one is installed. */
    fun payloadDirectory(): File?
}

/**
 * Exec checks (S1, docs/ANDROID_KERNELS.md):
 * a. `libproot.so --version` straight from `nativeLibraryDir`;
 * b. guest programs under proot, which prove the loader-based execution of
 *    app-data files: proot never hands a guest file to `execve()`, its loader
 *    maps the ELF itself (android/native/README.md);
 * c. a direct `execve()` of a rootfs binary without proot, expected to be
 *    refused at targetSdk >= 29 (W^X). It is harmless whatever happens:
 *    coreutils `true` does nothing.
 */
object ExecChecks {
    const val PROOT_VERSION = "proot-version"
    const val PROOT_TRUE = "proot-true"
    const val PROOT_SH = "proot-sh"
    const val PROOT_PYTHON = "proot-python"
    const val PROOT_GIT = "proot-git"
    const val DIRECT_EXEC = "direct-exec"
    val UNDER_PROOT_IDS = setOf(PROOT_TRUE, PROOT_SH, PROOT_PYTHON, PROOT_GIT)

    const val DENIED = "denied"
    const val PERMITTED = "permitted"
    const val PERMITTED_INTERPRETER_MISSING = "permitted_interpreter_missing"
    const val ERROR = "error"

    /** The payload's interpreter: python-build-standalone under `python/` (android/runtime/README.md, payload layout). */
    const val PAYLOAD_PYTHON = "${GuestPaths.PAYLOAD}/python/bin/python3"
    /** The userland's git (runtime contract: Ubuntu 24.04 arm64 with git). */
    const val ROOTFS_GIT = "/usr/bin/git"
    const val DIRECT_EXEC_TARGET = "usr/bin/true"

    /**
     * The self-check's liveness contract for one command (android/README.md
     * "Device self-check"): proot's `--version` and `/bin/true` take well
     * under a second, so a minute separates "hung" from "slow" on any phone.
     */
    const val DEADLINE_MS = 60_000L

    /** Apps targeting API 29+ may not `execve()` files in their data directory (Android 10 behavior change). */
    const val W_X_TARGET_SDK = 29

    private const val ABSENT_PAYLOAD = ".absent"
    private val ERRNO = Regex("error=(\\d+)")
    private const val EACCES = 13
    private const val ENOENT = 2
    private const val MAX_SIGNAL = 64

    /**
     * The kernel's own proot invocation ([KernelCommandLine]) up to its guest
     * program, so every check exercises exactly the options, binds and
     * environment the kernel launches with. Without an installed payload the
     * `/opt/tofu` bind is left out; the rootfs carries the empty mount point.
     */
    fun prootPrefix(runtime: RuntimeProbe, payload: File?): KernelCommand {
        val placeholder = runtime.layout.payload(ABSENT_PAYLOAD)
        val full = KernelCommandLine.build(runtime.layout, runtime.nativeLibraryDir, payload ?: placeholder)
        val unbound = "--bind=${placeholder.path}:${GuestPaths.PAYLOAD}"
        val argv = full.argv.takeWhile { it != GuestPaths.ENTRY }.filter { payload != null || it != unbound }
        return full.copy(argv = argv)
    }

    fun prootVersion(runtime: RuntimeProbe, runner: CommandRunner): ExecResult {
        val proot = File(runtime.nativeLibraryDir, KernelCommandLine.PROOT)
        val argv = listOf(proot.path, "--version")
        val expectation = "exit 0 and proot's version banner"
        if (!proot.isFile) return skipped(PROOT_VERSION, argv, expectation, SkipReason.PROOT_NOT_IN_THIS_BUILD)
        val command = KernelCommand(argv, prootPrefix(runtime, null).environment, runtime.layout.root.parentFile ?: runtime.layout.root)
        return result(PROOT_VERSION, argv, expectation, runner.run(command)) { it.exitStatus == 0 && it.stdout.contains("accelerators") }
    }

    /** Check b; requires an installed rootfs (the caller skips the step otherwise). */
    fun underProot(runtime: RuntimeProbe, runner: CommandRunner): Pair<List<String>, List<ExecResult>> {
        val payload = runtime.payloadDirectory()
        val prefix = prootPrefix(runtime, payload)
        val checks = listOf(
            Spec(PROOT_TRUE, listOf("/bin/true"), "exit 0") { it.exitStatus == 0 },
            Spec(PROOT_SH, listOf("/bin/sh", "-c", "echo ok"), "exit 0 and stdout ok") { it.exitStatus == 0 && it.stdout.trim() == "ok" },
            Spec(PROOT_PYTHON, listOf(PAYLOAD_PYTHON, "-V"), "exit 0 and stdout Python 3.x", needsPayload = true) {
                it.exitStatus == 0 && it.stdout.trim().startsWith("Python 3")
            },
            Spec(PROOT_GIT, listOf(ROOTFS_GIT, "--version"), "exit 0 and stdout git version …") {
                it.exitStatus == 0 && it.stdout.trim().startsWith("git version")
            },
        ).map { spec ->
            if (spec.needsPayload && payload == null) skipped(spec.id, spec.argv, spec.expectation, SkipReason.PAYLOAD_NOT_INSTALLED)
            else result(spec.id, spec.argv, spec.expectation, runner.run(prefix.copy(argv = prefix.argv + spec.argv)), spec.pass)
        }
        return prefix.argv to checks
    }

    /** Check c; requires an installed rootfs (the caller skips the step otherwise). */
    fun directExec(runtime: RuntimeProbe, runner: CommandRunner, targetSdk: Int): ExecResult {
        val target = File(runtime.layout.rootfs, DIRECT_EXEC_TARGET)
        val argv = listOf(target.path)
        val expectDenied = targetSdk >= W_X_TARGET_SDK
        val expectation = if (expectDenied) "$DENIED (W^X: no exec from app data at targetSdk >= $W_X_TARGET_SDK)" else "$PERMITTED (targetSdk < $W_X_TARGET_SDK)"
        val outcome = runner.run(KernelCommand(argv, emptyMap(), runtime.layout.root))
        val classification = classifyDirectExec(outcome)
        val base = result(DIRECT_EXEC, argv, expectation, outcome) { false }
        val pass = when (classification) {
            DENIED -> expectDenied
            PERMITTED, PERMITTED_INTERPRETER_MISSING -> !expectDenied
            else -> false
        }
        return base.copy(pass = pass, classification = classification)
    }

    /**
     * EACCES: the kernel refused to exec the app-data file (SELinux denies
     * `execute_no_trans` on `app_data_file` at targetSdk >= 29). ENOENT on a
     * file that exists: the exec itself was allowed and only glibc's
     * `/lib/ld-linux-aarch64.so.1`, absent outside proot, was missing.
     */
    fun classifyDirectExec(outcome: CommandOutcome): String {
        if (outcome.started) return PERMITTED
        return when (outcome.startError?.let { ERRNO.find(it) }?.groupValues?.get(1)?.toIntOrNull()) {
            EACCES -> DENIED
            ENOENT -> PERMITTED_INTERPRETER_MISSING
            else -> ERROR
        }
    }

    /** A signal death of the started process itself (0x80 + signal), else null. */
    fun signalOf(status: Int?): Int? = status?.takeIf { it > 0x80 && it <= 0x80 + MAX_SIGNAL }?.minus(0x80)

    fun skipped(id: String, argv: List<String>, expectation: String, reason: String) =
        ExecResult(id = id, argv = argv, expectation = expectation, pass = null, outcome = null, skipReason = reason)

    private class Spec(
        val id: String,
        val argv: List<String>,
        val expectation: String,
        val needsPayload: Boolean = false,
        val pass: (CommandOutcome) -> Boolean,
    )

    private fun result(id: String, argv: List<String>, expectation: String, outcome: CommandOutcome, pass: (CommandOutcome) -> Boolean) = ExecResult(
        id = id,
        argv = argv,
        expectation = expectation,
        pass = outcome.started && !outcome.timedOut && pass(outcome),
        outcome = when {
            !outcome.started -> ExecOutcome.START_FAILED
            outcome.timedOut -> ExecOutcome.TIMED_OUT
            else -> ExecOutcome.EXITED
        },
        exitStatus = outcome.exitStatus,
        signal = signalOf(outcome.exitStatus),
        durationMs = outcome.durationMs,
        stdout = outcome.stdout.takeIf { it.isNotEmpty() },
        stderr = outcome.stderr.takeIf { it.isNotEmpty() },
        error = outcome.startError,
    )
}
