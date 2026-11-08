package com.tofu.client.session

import com.tofu.client.data.Profile
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.withContext
import java.util.concurrent.ConcurrentHashMap

/**
 * [KernelController] for [com.tofu.client.data.KernelKind.REMOTE_URL] kernels:
 * a Tofu server someone else runs, reached at the profile's own URL, wrapping
 * today's [SupervisorClient].
 *
 * Behaviour is the pre-kernel app's. Opening never starts the server
 * ([SessionController.activate] signs straight in for this kind, and Start
 * stays the card's explicit supervisor action), so [ensureRunning] only
 * resolves the endpoint. [stop] is the supervisor's login-then-act stop
 * ([executeSupervisorCall]) for a profile that opted into management with a
 * project path; the app can stop no other remote server.
 *
 * [KernelState.Ready] here means "attached", not "healthy": the app does not
 * own this process, so server health stays with the supervisor chip and the
 * WebView. State is keyed by profile id AND URL, so an edited URL starts from
 * a fresh state instead of inheriting the old address's.
 */
class UrlKernelController(
    private val supervisor: SupervisorClient,
    /** Establishes the gateway session a stop needs ([SessionManager.login]). */
    private val login: suspend (Profile) -> LoginResult,
) : KernelController {

    private data class Key(val id: Long, val baseUrl: String)

    private val states = ConcurrentHashMap<Key, MutableStateFlow<KernelState>>()

    override suspend fun ensureRunning(profile: Profile): KernelEndpoint {
        val server = ServerUrl.parse(profile.baseUrl)
        if (server == null) {
            flowFor(profile).value = KernelState.Failed(INVALID_URL)
            throw KernelUnavailableException(INVALID_URL)
        }
        flowFor(profile).value = KernelState.Ready
        return KernelEndpoint(profile.baseUrl, server.host)
    }

    override suspend fun stop(profile: Profile) {
        if (!ServerLifecycle.isManaged(profile)) return
        val signedIn = ServerLifecycle.isSignedIn(profile)
        val outcome = executeSupervisorCall(
            profile = profile,
            action = SupervisorAction.STOP,
            plan = ServerLifecycle.probePlan(ProbeTrigger.USER, signedIn),
            signedIn = signedIn,
            login = login,
            call = { action, p ->
                withContext(Dispatchers.IO) {
                    when (action) {
                        SupervisorAction.START -> supervisor.start(p)
                        SupervisorAction.STOP -> supervisor.stop(p)
                        SupervisorAction.STATUS -> supervisor.status(p)
                    }
                }
            },
            isCurrent = { true },
        )
        flowFor(profile).value = when {
            // A user-triggered plan always carries the explanation.
            outcome.failed -> KernelState.Failed(outcome.message.orEmpty())
            outcome.running == true -> KernelState.Ready
            else -> KernelState.Stopped
        }
    }

    // The stored flow itself, so every call hands out the same instance.
    override fun state(profile: Profile): StateFlow<KernelState> = flowFor(profile)

    private fun flowFor(profile: Profile): MutableStateFlow<KernelState> =
        states.getOrPut(Key(profile.id, profile.baseUrl)) {
            MutableStateFlow(
                if (ServerUrl.parse(profile.baseUrl) != null) KernelState.Ready else KernelState.Failed(INVALID_URL),
            )
        }

    private companion object {
        /** Same wording as [SessionManager]'s refusal of an unparseable URL. */
        const val INVALID_URL = "Invalid server URL"
    }
}
