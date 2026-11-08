package com.tofu.client.session

import com.tofu.client.data.Profile
import com.tofu.client.runtime.KernelSecretValue
import com.tofu.client.runtime.LocalKernelEndpoint
import com.tofu.client.runtime.LocalKernelFailure
import com.tofu.client.runtime.LocalKernelState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import okhttp3.Cookie
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * The LOCAL adapter over a fake runtime: availability refusals, the state
 * mapping (Stopping kept, the runtime's SafeMode reported as Failed), the
 * start/await protocol, the credential installed before the health check, and
 * front-kernel notifications.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class LocalKernelControllerTest {

    private val ready = LocalKernelState.Ready(LocalKernelEndpoint(43567))
    private val secret = KernelSecretValue("tofu_admin_" + "ab".repeat(32))
    private val phone = KernelList.THIS_PHONE
    private val urlKernel = Profile(id = 3, alias = "lab", baseUrl = "https://lab.example/")

    /** The runtime as [LocalKernelPort]: like `LocalKernelSupervisor`, a start while running is a no-op. */
    private inner class FakePort(
        private val availability: LocalAvailability = LocalAvailability.Available,
        initial: LocalKernelState = LocalKernelState.Stopped,
    ) : LocalKernelPort {
        val flow = MutableStateFlow(initial)
        var startResult = LocalStart.REQUESTED
        /** What follows Starting; the default comes up at once. */
        var afterStart: () -> Unit = { flow.value = ready }
        var secretError: Exception? = null
        var starts = 0
        var stops = 0

        override fun availability() = availability
        override fun start(): LocalStart {
            starts++
            if (startResult != LocalStart.REQUESTED) return startResult
            val current = flow.value
            if (current == LocalKernelState.Starting || current is LocalKernelState.Ready || current == LocalKernelState.Stopping) {
                return LocalStart.REQUESTED
            }
            flow.value = LocalKernelState.Starting
            afterStart()
            return LocalStart.REQUESTED
        }
        override fun stop() {
            stops++
            flow.value = if (flow.value is LocalKernelState.Ready || flow.value == LocalKernelState.Starting) LocalKernelState.Stopping else LocalKernelState.Stopped
        }
        override val state: StateFlow<LocalKernelState> = flow
        override fun secret(): KernelSecretValue = secretError?.let { throw it } ?: secret
    }

    private class Text : LocalKernelText {
        override fun notArm64() = "needs arm64"
        override fun notInThisBuild() = "not in this build"
        override fun notInForeground() = "open the app first"
        override fun stoppedBeforeReady() = "stopped before ready"
        override fun healthCheckFailed(verdict: TofuProbe.Verdict) = "health check: $verdict"
        override fun describe(state: LocalKernelState) = "runtime says $state"
    }

    private class Jar : CookieSink {
        val injected = mutableListOf<Pair<String, List<Cookie>>>()
        val purged = mutableListOf<Pair<String, String>>()
        override fun inject(origin: String, cookies: List<Cookie>) { injected += origin to cookies }
        override fun purgeOrigin(origin: String, baseUrl: String) { purged += origin to baseUrl }
        override fun purgeHost(host: String) {}
        override fun cookieHeader(origin: String): String? = null
    }

    private class Harness(val controller: LocalKernelController, val jar: Jar, val probed: MutableList<String>, val fronts: MutableList<Boolean>)

    private fun TestScope.harness(port: FakePort, verdict: TofuProbe.Verdict = TofuProbe.Verdict.TOFU, jar: Jar = Jar()): Harness {
        val probed = mutableListOf<String>()
        val fronts = mutableListOf<Boolean>()
        val controller = LocalKernelController(
            port, Text(), jar,
            healthCheck = { url ->
                assertTrue("the credential is installed before the health check", jar.injected.isNotEmpty())
                probed += url
                verdict
            },
            scope = backgroundScope,
            io = UnconfinedTestDispatcher(testScheduler),
            onFront = { fronts += it },
        )
        return Harness(controller, jar, probed, fronts)
    }

    private suspend fun refusal(block: suspend () -> Unit): String {
        try {
            block()
        } catch (refused: KernelUnavailableException) {
            return refused.reason
        }
        fail("expected KernelUnavailableException")
        throw AssertionError()
    }

    @Test fun this_phone_starts_then_gets_its_credential_and_a_health_check() = runTest(UnconfinedTestDispatcher()) {
        val port = FakePort()
        val h = harness(port)
        assertEquals(KernelState.Stopped, published(h))

        val endpoint = h.controller.ensureRunning(phone)

        assertEquals(KernelEndpoint("http://127.0.0.1:43567/", "127.0.0.1"), endpoint)
        assertEquals(1, port.starts)
        val (origin, cookies) = h.jar.injected.single()
        assertEquals("http://127.0.0.1:43567", origin)
        val cookie = cookies.single()
        assertEquals(LocalKernelCookie.NAME, cookie.name)
        assertEquals(secret.reveal(), cookie.value)
        assertTrue(cookie.httpOnly && cookie.hostOnly && cookie.domain == "127.0.0.1" && cookie.path == "/")
        assertEquals(listOf("http://127.0.0.1:43567/"), h.probed)
        assertEquals(KernelState.Ready, published(h))
    }

    @Test fun a_device_without_arm64_or_a_build_without_the_runtime_is_refused_with_its_reason() = runTest(UnconfinedTestDispatcher()) {
        for ((availability, reason) in listOf(
            LocalAvailability.NotArm64(listOf("x86_64")) to "needs arm64",
            LocalAvailability.NotInThisBuild("assets/kernel/index.json") to "not in this build",
        )) {
            val port = FakePort(availability)
            val h = harness(port)
            assertEquals(KernelState.Failed(reason), published(h))
            assertEquals(reason, refusal { h.controller.ensureRunning(phone) })
            assertEquals(KernelState.Failed(reason), published(h))
            h.controller.stop(phone)
            assertEquals("nothing is started or stopped", 0, port.starts + port.stops)
        }
    }

    @Test fun a_start_refused_outside_the_foreground_is_failed_until_a_start_succeeds() = runTest(UnconfinedTestDispatcher()) {
        val port = FakePort().apply { startResult = LocalStart.NOT_IN_FOREGROUND }
        val h = harness(port)

        assertEquals("open the app first", refusal { h.controller.ensureRunning(phone) })
        assertEquals(KernelState.Failed("open the app first"), published(h))
        assertTrue("no credential without a kernel", h.jar.injected.isEmpty())

        port.startResult = LocalStart.REQUESTED
        h.controller.ensureRunning(phone)
        assertEquals(KernelState.Ready, published(h))
    }

    @Test fun runtime_failures_and_its_safe_mode_are_failed_with_the_runtimes_explanation() = runTest(UnconfinedTestDispatcher()) {
        for (end in listOf(
            LocalKernelState.Failed(LocalKernelFailure.KilledBySystem),
            LocalKernelState.SafeMode(crashes = 5, lastExitStatus = 1),
        )) {
            val port = FakePort().apply { afterStart = { flow.value = end } }
            val h = harness(port)
            assertEquals("runtime says $end", refusal { h.controller.ensureRunning(phone) })
            assertEquals(KernelState.Failed("runtime says $end"), published(h))
            assertTrue(h.jar.injected.isEmpty())
            assertTrue(h.probed.isEmpty())
        }
    }

    @Test fun the_same_failure_again_is_still_the_outcome_of_the_new_start() = runTest(UnconfinedTestDispatcher()) {
        val killed = LocalKernelState.Failed(LocalKernelFailure.KilledBySystem)
        val port = FakePort(initial = killed).apply { afterStart = { flow.value = killed } }
        val h = harness(port)

        assertEquals("runtime says $killed", refusal { h.controller.ensureRunning(phone) })
        assertEquals(1, port.starts)
    }

    @Test fun a_kernel_that_stops_while_starting_fails_the_start() = runTest(UnconfinedTestDispatcher()) {
        val port = FakePort().apply { afterStart = { flow.value = LocalKernelState.Stopped } }
        val h = harness(port)
        assertEquals("stopped before ready", refusal { h.controller.ensureRunning(phone) })
        assertEquals(KernelState.Failed("stopped before ready"), published(h))
    }

    @Test fun stopping_is_reported_and_a_start_waits_for_the_drain() = runTest(UnconfinedTestDispatcher()) {
        val port = FakePort(initial = LocalKernelState.Stopping)
        val h = harness(port)
        assertEquals(KernelState.Stopping, published(h))

        val opening = async { h.controller.ensureRunning(phone) }
        assertEquals("a start during a drain would be ignored by the runtime", 0, port.starts)
        assertFalse(opening.isCompleted)

        port.flow.value = LocalKernelState.Stopped
        assertEquals(KernelEndpoint("http://127.0.0.1:43567/", "127.0.0.1"), opening.await())
        assertEquals(1, port.starts)
    }

    @Test fun a_ready_kernel_is_reused_without_a_restart() = runTest(UnconfinedTestDispatcher()) {
        val port = FakePort(initial = ready).apply { afterStart = { fail("restarted") } }
        val h = harness(port)
        val seen = mutableListOf<KernelState>()
        backgroundScope.launchCollect(h.controller.state(phone), seen)

        h.controller.ensureRunning(phone)
        assertEquals(listOf<KernelState>(KernelState.Ready), seen)
        assertEquals("the credential is refreshed on every open", 1, h.jar.injected.size)
    }

    @Test fun a_refused_health_check_fails_the_start_until_the_runtime_moves_on() = runTest(UnconfinedTestDispatcher()) {
        val port = FakePort()
        val h = harness(port, verdict = TofuProbe.Verdict.TOFU_AUTH)

        assertEquals("health check: TOFU_AUTH", refusal { h.controller.ensureRunning(phone) })
        assertEquals(KernelState.Failed("health check: TOFU_AUTH"), published(h))

        port.stop()
        assertEquals(KernelState.Stopping, published(h))
    }

    @Test fun an_unreadable_secret_fails_the_start_without_a_credential() = runTest(UnconfinedTestDispatcher()) {
        val port = FakePort().apply { secretError = java.io.IOException("keystore") }
        val h = harness(port)
        val reason = refusal { h.controller.ensureRunning(phone) }
        assertTrue(reason, reason.contains("SecretUnavailable"))
        assertTrue(h.jar.injected.isEmpty())
    }

    @Test fun stop_returns_once_the_kernel_has_exited() = runTest(UnconfinedTestDispatcher()) {
        val port = FakePort(initial = ready)
        val h = harness(port)

        val stopping = async { h.controller.stop(phone) }
        assertEquals(KernelState.Stopping, published(h))
        assertFalse(stopping.isCompleted)

        port.flow.value = LocalKernelState.Stopped
        stopping.await()
        assertEquals(KernelState.Stopped, published(h))
        assertEquals(1, port.stops)
    }

    @Test fun the_credential_is_withdrawn_once_the_kernel_is_no_longer_ready_on_its_port() = runTest(UnconfinedTestDispatcher()) {
        val port = FakePort()
        val h = harness(port)
        h.controller.ensureRunning(phone)
        runCurrent()
        assertTrue("kept while it serves", h.jar.purged.isEmpty())

        port.stop() // Stopping: the old port will be free for any app once it exits
        runCurrent()
        assertEquals(listOf("http://127.0.0.1:43567" to "http://127.0.0.1:43567/"), h.jar.purged)

        port.flow.value = LocalKernelState.Stopped
        h.controller.ensureRunning(phone)
        port.flow.value = LocalKernelState.Starting // a crash restart, on a new port later
        runCurrent()
        assertEquals(2, h.jar.purged.size)
        port.flow.value = LocalKernelState.Ready(LocalKernelEndpoint(43999))
        runCurrent()
        assertEquals("nothing installed for the new port until the next open", 2, h.jar.purged.size)
    }

    @Test fun the_front_kernel_tells_whether_this_phone_is_in_front() = runTest(UnconfinedTestDispatcher()) {
        val h = harness(FakePort())
        val kernels = KernelControllers(remoteUrl = UnavailableKernelController(), local = h.controller, ssh = UnavailableKernelController())

        kernels.putInFront(phone)
        kernels.putInFront(urlKernel)
        assertEquals(listOf(true, false), h.fronts)
    }

    @Test fun opening_this_phone_puts_it_in_front_before_it_is_ready_so_the_lifecycle_keeps_it() = runTest(UnconfinedTestDispatcher()) {
        val port = FakePort().apply { afterStart = {} } // stays Starting until the test moves it
        val inFront = MutableStateFlow(false) // e.g. a URL kernel was in front before
        var stops = 0
        val idle = object : LocalKernelActivity {
            override suspend fun runningWork(endpoint: LocalKernelEndpoint) = false
            override suspend fun enabledSchedule(endpoint: LocalKernelEndpoint) = false
        }
        val controller = LocalKernelController(
            port, Text(), Jar(), { TofuProbe.Verdict.TOFU }, backgroundScope, UnconfinedTestDispatcher(testScheduler),
            onFront = { inFront.value = it },
        )
        LocalKernelLifecycle(port.state, inFront, MutableStateFlow(true), MutableStateFlow(false), idle, {}, { stops++ })
            .start(backgroundScope)

        val opening = async { controller.ensureRunning(phone) }
        assertTrue(inFront.value)
        port.flow.value = ready
        opening.await()
        runCurrent()
        assertEquals("an idle kernel the user is opening is not stopped", 0, stops)
    }

    private fun <T> CoroutineScope.launchCollect(flow: StateFlow<T>, into: MutableList<T>) =
        launch(start = CoroutineStart.UNDISPATCHED) { flow.collect { into += it } }

    /** The controller's state once every pending update has propagated. */
    private fun TestScope.published(h: Harness): KernelState {
        runCurrent()
        return h.controller.state(phone).value
    }
}
