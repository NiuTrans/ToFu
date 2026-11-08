package com.tofu.client.session

import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.runtime.KernelSecretValue
import com.tofu.client.runtime.LocalKernelFailure
import com.tofu.client.runtime.LocalKernelState
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.concurrent.atomic.AtomicReference

/** Whether this build and device can run the local kernel at all. */
sealed interface LocalAvailability {
    data object Available : LocalAvailability

    /** proot ships for arm64-v8a only; [abis] are the device's. */
    data class NotArm64(val abis: List<String>) : LocalAvailability

    /** The APK was assembled without the runtime; [missing] names what is absent. */
    data class NotInThisBuild(val missing: String) : LocalAvailability
}

/** What a start request did: the runtime's `LocalKernel.StartResult` without its Android types. */
enum class LocalStart { REQUESTED, NOT_AVAILABLE, NOT_IN_FOREGROUND }

/**
 * The runtime's local kernel (`runtime/LocalKernel`) as this adapter uses it.
 * Pure Kotlin so the adapter runs on the plain JVM; the device implementation
 * is [AndroidLocalKernelPort].
 */
interface LocalKernelPort {
    fun availability(): LocalAvailability

    /** Once it returns [LocalStart.REQUESTED], [state] already shows this start: Starting or a later state. */
    fun start(): LocalStart

    /** Asynchronous: [state] goes Stopping, then Stopped once the kernel exited. */
    fun stop()

    val state: StateFlow<LocalKernelState>

    /** The per-install secret. Disk and Keystore I/O: never on the main thread. */
    fun secret(): KernelSecretValue
}

/** The adapter's user-facing text: resources on a device, fixed strings in tests. */
interface LocalKernelText {
    fun notArm64(): String
    fun notInThisBuild(): String
    fun notInForeground(): String
    fun stoppedBeforeReady(): String
    fun healthCheckFailed(verdict: TofuProbe.Verdict): String

    /** The runtime's own explanation of a Failed or SafeMode state. */
    fun describe(state: LocalKernelState): String
}

/**
 * [KernelController] for "This phone" ([KernelKind.LOCAL]) over the runtime's
 * local kernel ([LocalKernelPort]).
 *
 * [ensureRunning]: refuse when the device or build cannot run it; let a
 * draining stop finish (the runtime ignores a start while Stopping); start;
 * await Ready, Failed, SafeMode or Stopped; then set up the WebView session
 * the launch contract (docs/ANDROID_LOCAL_KERNEL.md §6) asks for: the
 * HttpOnly `tofu_session` cookie for 127.0.0.1 only ([LocalKernelCookie]),
 * then the existing [HealthProbe] verdict against the endpoint, carrying that
 * cookie. The session layer then runs [ApiMetaGate] before the first load.
 * The cookie is withdrawn as soon as the kernel leaves Ready on that port
 * ([LocalKernelCookie.withdraw]).
 *
 * State mapping (runtime → [KernelState]): Stopped, Starting, Ready and
 * Stopping map one to one. Failed and the runtime's SafeMode both become
 * `Failed(reason)`: the runtime's SafeMode means "not restarted after a crash
 * loop", a stopped kernel, whereas [KernelState.SafeMode] promises a running
 * one, so this controller never reports it. A refusal of the adapter itself
 * (not in the foreground, health check) shows as `Failed(reason)` until the
 * runtime's state moves on.
 */
class LocalKernelController(
    private val port: LocalKernelPort,
    private val text: LocalKernelText,
    private val cookies: CookieSink,
    /** The [HealthProbe] verdict for `{baseUrl}/api/health`, sent with the installed cookie. */
    private val healthCheck: suspend (String) -> TofuProbe.Verdict,
    scope: CoroutineScope,
    private val io: CoroutineDispatcher = Dispatchers.IO,
    /** Hears whether "This phone" is the kernel in front after each switch. */
    private val onFront: (Boolean) -> Unit = {},
) : KernelController {

    private class Refusal(val reason: String, val during: LocalKernelState)

    private val unavailableReason: String? = when (port.availability()) {
        LocalAvailability.Available -> null
        is LocalAvailability.NotArm64 -> text.notArm64()
        is LocalAvailability.NotInThisBuild -> text.notInThisBuild()
    }

    private val refusal = MutableStateFlow<Refusal?>(null)

    private val published: StateFlow<KernelState> = unavailableReason
        ?.let { MutableStateFlow<KernelState>(KernelState.Failed(it)).asStateFlow() }
        ?: combine(port.state, refusal) { runtime, refused -> kernelState(runtime, refused) }
            .stateIn(scope, SharingStarted.Eagerly, kernelState(port.state.value, null))

    /** The endpoint whose credential is in the jar; withdrawn once the kernel is no longer ready there. */
    private val installed = AtomicReference<KernelEndpoint?>(null)

    init {
        if (unavailableReason == null) scope.launch {
            port.state.collect { runtime ->
                val current = installed.get() ?: return@collect
                if (runtime !is LocalKernelState.Ready || runtime.endpoint.baseUrl != current.baseUrl) withdraw(current)
            }
        }
    }

    override suspend fun ensureRunning(profile: Profile): KernelEndpoint {
        unavailableReason?.let { throw KernelUnavailableException(it) }
        // The user is opening This phone: it is the kernel meant to be in front from
        // now on, or the lifecycle would stop it the moment it became ready.
        onFront(true)
        refusal.value = null
        port.state.first { it != LocalKernelState.Stopping }
        when (port.start()) {
            LocalStart.REQUESTED -> Unit
            LocalStart.NOT_AVAILABLE -> refuse(text.notInThisBuild())
            LocalStart.NOT_IN_FOREGROUND -> refuse(text.notInForeground())
        }
        val outcome = port.state.first { it != LocalKernelState.Starting && it != LocalKernelState.Stopping }
        val ready = when (outcome) {
            is LocalKernelState.Ready -> outcome
            is LocalKernelState.Failed, is LocalKernelState.SafeMode -> throw KernelUnavailableException(text.describe(outcome))
            else -> refuse(text.stoppedBeforeReady())
        }
        val endpoint = KernelEndpoint(ready.endpoint.baseUrl, ready.endpoint.cookieHost)
        val secret = try {
            withContext(io) { port.secret() }
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (unreadable: Exception) {
            refuse(text.describe(LocalKernelState.Failed(LocalKernelFailure.SecretUnavailable(unreadable.javaClass.simpleName))), ready)
        }
        withContext(io) { LocalKernelCookie.install(cookies, endpoint, secret.reveal()) }
        installed.set(endpoint)
        if (port.state.value != ready) {
            withdraw(endpoint)
            refuse(text.stoppedBeforeReady())
        }
        val verdict = healthCheck(endpoint.baseUrl)
        if (verdict != TofuProbe.Verdict.TOFU) refuse(text.healthCheckFailed(verdict), ready)
        return endpoint
    }

    override suspend fun stop(profile: Profile) {
        if (unavailableReason != null) return
        refusal.value = null
        port.stop()
        port.state.first { it == LocalKernelState.Stopped }
    }

    override fun state(profile: Profile): StateFlow<KernelState> = published

    override fun frontChanged(front: Profile) = onFront(front.kind == KernelKind.LOCAL)

    private fun withdraw(endpoint: KernelEndpoint) {
        if (installed.compareAndSet(endpoint, null)) LocalKernelCookie.withdraw(cookies, endpoint)
    }

    private fun refuse(reason: String, during: LocalKernelState = port.state.value): Nothing {
        refusal.value = Refusal(reason, during)
        throw KernelUnavailableException(reason)
    }

    private fun kernelState(runtime: LocalKernelState, refused: Refusal?): KernelState {
        if (refused != null && refused.during == runtime) return KernelState.Failed(refused.reason)
        return when (runtime) {
            LocalKernelState.Stopped -> KernelState.Stopped
            LocalKernelState.Starting -> KernelState.Starting
            is LocalKernelState.Ready -> KernelState.Ready
            LocalKernelState.Stopping -> KernelState.Stopping
            is LocalKernelState.Failed, is LocalKernelState.SafeMode -> KernelState.Failed(text.describe(runtime))
        }
    }
}
