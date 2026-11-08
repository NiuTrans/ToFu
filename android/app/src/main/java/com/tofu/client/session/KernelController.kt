package com.tofu.client.session

import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * The kernel seam. A kernel is a Tofu server the app puts in front, in the
 * WebView; its [KernelKind] picks the one [KernelController] that owns its
 * lifecycle. Exactly one kernel is in front at a time, and a kernel's
 * conversations live on that kernel.
 *
 * Entry points: [KernelControllers.of] (the only kind-to-controller mapping,
 * used by [SessionController] and the Kernels list) and [KernelList.pinned].
 * Pure Kotlin (no Android types) so the pure-JVM test tier compiles it.
 */
interface KernelController {

    /**
     * Bring [profile]'s kernel up, or attach to it, and return where it answers.
     * Idempotent: a kernel already [KernelState.Ready] (or [KernelState.SafeMode])
     * returns its current endpoint without restarting. While it works it
     * publishes [KernelState.Starting]; on success Ready or SafeMode.
     *
     * Failure is an exception, never a fake endpoint: it throws
     * [KernelUnavailableException] after publishing
     * `KernelState.Failed(reason)` with the same reason.
     */
    suspend fun ensureRunning(profile: Profile): KernelEndpoint

    /**
     * Stop what the app runs for [profile]'s kernel (its process or its
     * tunnel) and publish [KernelState.Stopped], or Failed when stopping
     * failed; [KernelState.Stopping] while the stop drains. Returns once the
     * kernel is stopped. A no-op for a kernel that is not running.
     */
    suspend fun stop(profile: Profile)

    /**
     * Observable lifecycle of [profile]'s kernel. Hot, never completes, always
     * holds a value, and is the same flow for the same kernel every time.
     */
    fun state(profile: Profile): StateFlow<KernelState>

    /**
     * [front] was just put in front (the WebView shows it). A controller whose
     * kernel lives on only for the WebView may stop it once another kernel is
     * in front. Called by [KernelControllers.putInFront]; default: nothing.
     */
    fun frontChanged(front: Profile) {}
}

/**
 * Where a running kernel answers. [baseUrl] is what the WebView loads and the
 * session layer signs in against; [cookieHost] is the host whose cookie jar
 * holds this kernel's session — a different host per kernel kind on loopback
 * (127.0.0.1 local, 127.0.0.2 SSH forwards) so no kernel's session cookie is
 * ever sent to another.
 */
data class KernelEndpoint(val baseUrl: String, val cookieHost: String)

/** Lifecycle of one kernel as its [KernelController] knows it. */
sealed interface KernelState {
    data object Stopped : KernelState
    data object Starting : KernelState
    data object Ready : KernelState

    /**
     * A requested stop is draining: the kernel still runs and holds its data
     * until it exits (for LOCAL up to the launch contract's stop deadline).
     */
    data object Stopping : KernelState

    /** The last [KernelController.ensureRunning] (or stop) failed; [reason] is user-facing. */
    data class Failed(val reason: String) : KernelState

    /** Running degraded after repeated boot failures (no MCP autostart, no plugins). */
    data object SafeMode : KernelState
}

/** Thrown by [KernelController.ensureRunning]; [reason] is user-facing. */
class KernelUnavailableException(val reason: String) : Exception(reason)

/** The copy of [this] the session layer signs in and the WebView loads; never written to Room. */
fun Profile.boundTo(endpoint: KernelEndpoint): Profile =
    copy(baseUrl = endpoint.baseUrl, cookieHost = endpoint.cookieHost)

/** Exactly one controller per [KernelKind]; [of] is the only kind-to-controller mapping. */
class KernelControllers(
    private val remoteUrl: KernelController,
    private val local: KernelController,
    private val ssh: KernelController,
) {
    fun of(kind: KernelKind): KernelController = when (kind) {
        KernelKind.REMOTE_URL -> remoteUrl
        KernelKind.LOCAL -> local
        KernelKind.SSH -> ssh
    }

    fun state(profile: Profile): StateFlow<KernelState> = of(profile.kind).state(profile)

    /** [front] is the kernel the WebView now shows; every controller hears it once. */
    fun putInFront(front: Profile) {
        listOf(remoteUrl, local, ssh).distinct().forEach { it.frontChanged(front) }
    }
}

/**
 * Stands in for a kernel kind this build cannot run yet: always
 * `Failed(reason)`, and [ensureRunning] refuses with that reason. The LOCAL and
 * SSH lanes replace it with their real controllers.
 */
class UnavailableKernelController(private val reason: String = NOT_IN_THIS_BUILD) : KernelController {
    private val failed: StateFlow<KernelState> = MutableStateFlow(KernelState.Failed(reason)).asStateFlow()

    override suspend fun ensureRunning(profile: Profile): KernelEndpoint = throw KernelUnavailableException(reason)

    override suspend fun stop(profile: Profile) {}

    override fun state(profile: Profile): StateFlow<KernelState> = failed

    companion object {
        const val NOT_IN_THIS_BUILD = "not available in this build yet"
    }
}

/** How the Kernels list is ordered. */
object KernelList {

    /**
     * "This phone" while no LOCAL row is stored. Id 0 is never a Room id, so
     * the recency bump and re-read in [SessionController.activate] are no-ops
     * for it.
     */
    val THIS_PHONE = Profile(alias = "This phone", baseUrl = "", kind = KernelKind.LOCAL)

    /**
     * "This phone" pinned first — the stored LOCAL row if there is one, else
     * [THIS_PHONE] — then every URL and SSH kernel in the given (recency)
     * order. The phone has one local kernel, so only the first LOCAL row counts.
     */
    fun pinned(rows: List<Profile>): List<Profile> =
        listOf(rows.firstOrNull { it.kind == KernelKind.LOCAL } ?: THIS_PHONE) +
            rows.filter { it.kind != KernelKind.LOCAL }
}
