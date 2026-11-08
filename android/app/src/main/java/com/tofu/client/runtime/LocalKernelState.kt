package com.tofu.client.runtime

/** Where the local kernel answers once it is ready. Cookies for it are scoped to [cookieHost]. */
data class LocalKernelEndpoint(val port: Int) {
    val host: String get() = LaunchProtocol.LOOPBACK
    val cookieHost: String get() = LaunchProtocol.LOOPBACK
    val baseUrl: String get() = "http://${LaunchProtocol.LOOPBACK}:$port/"
}

/** Why the local kernel is not running; terminal until the user starts it again. */
sealed interface LocalKernelFailure {
    /** This build or device carries no local runtime (no assets, no arm64 proot). */
    data class NotAvailable(val detail: String) : LocalKernelFailure
    data class InstallFailed(val detail: String) : LocalKernelFailure
    /** The Keystore-wrapped secret exists but cannot be decrypted; it is never silently replaced. */
    data class SecretUnavailable(val detail: String) : LocalKernelFailure
    data class LaunchFailed(val detail: String) : LocalKernelFailure
    /** Exit status 2: usage or secret-format error (launch contract). Restarting cannot help. */
    data object UsageError : LocalKernelFailure
    /** The kernel broke the launch contract (bad stdout record, refused readiness probe). */
    data class ProtocolViolation(val detail: String) : LocalKernelFailure
    /** SIGKILL the app did not send — on Android 12+ most likely the phantom-process limit. */
    data object KilledBySystem : LocalKernelFailure
}

/**
 * The service-level state machine: Stopped → Starting → Ready, with Stopping
 * while a requested stop drains. A crash restarts within the launch
 * contract's budget; exhausting it is [SafeMode]. Failed and SafeMode hold
 * until the user starts the kernel again.
 */
sealed interface LocalKernelState {
    data object Stopped : LocalKernelState
    data object Starting : LocalKernelState
    data class Ready(val endpoint: LocalKernelEndpoint) : LocalKernelState
    data object Stopping : LocalKernelState
    data class Failed(val failure: LocalKernelFailure) : LocalKernelState
    data class SafeMode(val crashes: Int, val lastExitStatus: Int) : LocalKernelState
}

/** The partial wakelock is held only while the ready kernel reports running work: a structural rule, not a timer. */
object WakeLockPolicy {
    fun shouldHold(state: LocalKernelState, hasRunningWork: Boolean): Boolean =
        hasRunningWork && state is LocalKernelState.Ready
}

/**
 * What to tell the user after [LocalKernelFailure.KilledBySystem], verified
 * against AOSP: Android 12 introduced the phantom-process limit (32 app child
 * processes system-wide, `ActivityManagerConstants` `max_phantom_processes`;
 * the least important app's oldest child goes first, so proot itself is the
 * first victim). A foreground service orders the app's children later but does
 * not exempt them. Android 14 added the Developer options switch "Disable child
 * process restrictions"; on 12–13 the limit is raised with `device_config`.
 */
enum class SystemKillGuidance {
    DEVELOPER_OPTION,
    DEVICE_CONFIG,
    RESTART_ONLY;

    companion object {
        fun forSdk(sdkInt: Int): SystemKillGuidance = when {
            sdkInt >= 34 -> DEVELOPER_OPTION
            sdkInt >= 31 -> DEVICE_CONFIG
            else -> RESTART_ONLY
        }
    }
}
