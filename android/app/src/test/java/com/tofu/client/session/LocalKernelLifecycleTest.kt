package com.tofu.client.session

import com.tofu.client.runtime.LocalKernelEndpoint
import com.tofu.client.runtime.LocalKernelState
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The decided lifecycle of "This phone": it keeps running for running work,
 * an enabled schedule or the "Keep running" toggle, and otherwise stops once
 * another kernel is in front or the app is left. The running-work signal is
 * read only while the user is away from This phone, at the existing cadence.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class LocalKernelLifecycleTest {

    private val endpoint = LocalKernelEndpoint(43567)
    private val ready = LocalKernelState.Ready(endpoint)
    private val cadence = ServerLifecycle.START_POLL_INTERVAL_MS

    private class FakeActivity : LocalKernelActivity {
        var work: Boolean? = false
        var schedule: Boolean? = false
        val workReads = mutableListOf<Long>()
        var scheduleReads = 0
        lateinit var clock: () -> Long
        override suspend fun runningWork(endpoint: LocalKernelEndpoint): Boolean? { workReads += clock(); return work }
        override suspend fun enabledSchedule(endpoint: LocalKernelEndpoint): Boolean? { scheduleReads++; return schedule }
    }

    private inner class World(
        scope: TestScope,
        kernel: LocalKernelState = ready,
        front: Boolean = true,
        visible: Boolean = true,
        keep: Boolean = false,
        work: Boolean? = false,
    ) {
        val kernel = MutableStateFlow(kernel)
        val inFront = MutableStateFlow(front)
        val appVisible = MutableStateFlow(visible)
        val keepRunning = MutableStateFlow(keep)
        val activity = FakeActivity().apply {
            clock = { scope.testScheduler.currentTime }
            this.work = work
        }
        val wakeLock = mutableListOf<Boolean>()
        var stops = 0

        init {
            LocalKernelLifecycle(this.kernel, inFront, appVisible, keepRunning, activity, { wakeLock += it }, { stops++; this.kernel.value = LocalKernelState.Stopping })
                .start(scope.backgroundScope)
            scope.runCurrent()
        }
    }

    @Test fun the_policy_keeps_it_for_work_a_schedule_the_toggle_or_the_user_looking_at_it() {
        assertFalse(LocalKernelPolicy.keepRunning(false, inFront = false, appVisible = true, runningWork = false, enabledSchedule = false))
        assertFalse(LocalKernelPolicy.keepRunning(false, inFront = true, appVisible = false, runningWork = false, enabledSchedule = false))
        assertTrue(LocalKernelPolicy.keepRunning(false, inFront = true, appVisible = true, runningWork = false, enabledSchedule = false))
        assertTrue(LocalKernelPolicy.keepRunning(true, inFront = false, appVisible = false, runningWork = false, enabledSchedule = false))
        assertTrue(LocalKernelPolicy.keepRunning(false, inFront = false, appVisible = false, runningWork = true, enabledSchedule = false))
        assertTrue(LocalKernelPolicy.keepRunning(false, inFront = false, appVisible = false, runningWork = false, enabledSchedule = true))
        assertTrue("unknown work is never a reason to stop", LocalKernelPolicy.keepRunning(false, false, false, runningWork = null, enabledSchedule = false))
        assertTrue("unknown schedules neither", LocalKernelPolicy.keepRunning(false, false, false, runningWork = false, enabledSchedule = null))
    }

    @Test fun nothing_is_read_while_the_user_looks_at_this_phone_or_before_it_is_ready() = runTest {
        val world = World(this, kernel = LocalKernelState.Starting, front = false, visible = false)
        advanceTimeBy(10 * cadence)
        assertTrue("a starting kernel cannot say whether it is busy", world.activity.workReads.isEmpty())

        world.inFront.value = true
        world.appVisible.value = true
        world.kernel.value = ready
        advanceTimeBy(10 * cadence)
        assertTrue(world.activity.workReads.isEmpty())
        assertEquals(0, world.stops)
        assertEquals(false, world.wakeLock.last())
    }

    @Test fun leaving_the_app_with_nothing_running_and_no_schedule_stops_it() = runTest {
        val world = World(this)
        world.appVisible.value = false
        runCurrent()
        assertEquals(1, world.activity.workReads.size)
        assertEquals(1, world.activity.scheduleReads)
        assertEquals(1, world.stops)
        assertEquals(false, world.wakeLock.last())
    }

    @Test fun putting_another_kernel_in_front_stops_it_when_idle() = runTest {
        val world = World(this)
        world.inFront.value = false
        runCurrent()
        assertEquals(1, world.stops)
    }

    @Test fun running_work_keeps_it_and_holds_the_wakelock_until_the_work_ends() = runTest {
        val world = World(this)
        world.activity.work = true
        world.appVisible.value = false
        runCurrent()
        assertEquals(true, world.wakeLock.last())
        assertEquals("no schedule read while busy", 0, world.activity.scheduleReads)

        advanceTimeBy(cadence)
        runCurrent()
        assertEquals("read again at the existing cadence", listOf(0L, cadence), world.activity.workReads)
        assertEquals(0, world.stops)

        world.activity.work = false
        advanceTimeBy(cadence)
        runCurrent()
        assertEquals(1, world.activity.scheduleReads)
        assertEquals(1, world.stops)
        assertEquals(false, world.wakeLock.last())
    }

    @Test fun an_enabled_schedule_keeps_it_and_is_read_again_after_a_task_finishes() = runTest {
        val world = World(this)
        world.activity.schedule = true
        world.inFront.value = false
        runCurrent()
        advanceTimeBy(3 * cadence)
        runCurrent()
        assertEquals(0, world.stops)
        assertEquals("schedules are not polled while nothing changes", 1, world.activity.scheduleReads)

        world.activity.work = true
        advanceTimeBy(cadence)
        runCurrent()
        world.activity.work = false
        world.activity.schedule = false // e.g. a one-shot schedule that has just run
        advanceTimeBy(cadence)
        runCurrent()
        assertEquals(2, world.activity.scheduleReads)
        assertEquals(1, world.stops)
    }

    @Test fun keep_running_never_stops_it_but_the_wakelock_still_follows_the_work() = runTest {
        val world = World(this, keep = true)
        world.activity.work = true
        world.appVisible.value = false
        runCurrent()
        assertEquals(true, world.wakeLock.last())

        world.activity.work = false
        advanceTimeBy(5 * cadence)
        runCurrent()
        assertEquals(false, world.wakeLock.last())
        assertEquals(0, world.activity.scheduleReads)
        assertEquals(0, world.stops)

        world.keepRunning.value = false
        runCurrent()
        assertEquals("turning the toggle off re-decides at once", 1, world.stops)
    }

    @Test fun an_unknown_answer_never_stops_it_nor_holds_the_wakelock() = runTest {
        val world = World(this)
        world.activity.work = null
        world.appVisible.value = false
        advanceTimeBy(3 * cadence)
        runCurrent()
        assertEquals(0, world.stops)
        assertEquals(false, world.wakeLock.last())

        world.activity.work = false
        world.activity.schedule = null
        advanceTimeBy(3 * cadence)
        runCurrent()
        assertEquals(0, world.stops)
    }

    @Test fun coming_back_to_this_phone_ends_the_watch() = runTest {
        val world = World(this)
        world.activity.work = true
        world.appVisible.value = false
        runCurrent()
        advanceTimeBy(cadence)
        runCurrent()
        val reads = world.activity.workReads.size

        world.appVisible.value = true
        advanceTimeBy(10 * cadence)
        runCurrent()
        assertEquals(reads, world.activity.workReads.size)
        assertEquals(false, world.wakeLock.last())
        assertEquals(0, world.stops)
    }

    @Test fun a_restarted_kernel_is_watched_at_its_new_endpoint() = runTest {
        val world = World(this, front = false, work = true)
        assertEquals(true, world.wakeLock.last())
        world.kernel.value = LocalKernelState.Starting
        runCurrent()
        assertEquals("not ready: no wakelock", false, world.wakeLock.last())
        world.kernel.value = LocalKernelState.Ready(LocalKernelEndpoint(43999))
        runCurrent()
        assertEquals(true, world.wakeLock.last())
    }

    /** Answers the kernel's two activity endpoints and records each request. */
    private class KernelHttp(private val quiescence: Pair<Int, String>, private val schedules: Pair<Int, String>) : okhttp3.Interceptor {
        val requests = mutableListOf<Request>()
        override fun intercept(chain: okhttp3.Interceptor.Chain): Response {
            val request = chain.request()
            requests += request
            val (code, body) = if (request.url.encodedPath == "/health/quiescence") quiescence else schedules
            return Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(code).message("stub")
                .body(body.toResponseBody("application/json".toMediaType())).build()
        }
    }

    private fun activity(kernel: KernelHttp) =
        HttpLocalKernelActivity(OkHttpClient.Builder().addInterceptor(kernel).build(), { "tofu_admin_secret" }) { it() }

    @Test fun activity_is_read_from_the_kernels_existing_endpoints_with_the_secret() = runTest {
        val busy = KernelHttp(200 to """{"ok":true,"quiet":false,"runningTasks":2}""", 200 to """{"ok":true,"tasks":[{"id":"t1","enabled":true}]}""")
        assertEquals(true, activity(busy).runningWork(endpoint))
        assertEquals(true, activity(busy).enabledSchedule(endpoint))
        assertEquals(listOf("http://127.0.0.1:43567/health/quiescence", "http://127.0.0.1:43567/api/v1/scheduler/tasks"), busy.requests.map { it.url.toString() })
        assertTrue(busy.requests.all { it.header("Authorization") == "Bearer tofu_admin_secret" })

        val idle = KernelHttp(200 to """{"ok":true,"quiet":true,"runningTasks":0}""", 200 to """{"ok":true,"tasks":[ ]}""")
        assertEquals(false, activity(idle).runningWork(endpoint))
        assertEquals(false, activity(idle).enabledSchedule(endpoint))

        val registryFailure = KernelHttp(200 to """{"ok":true,"quiet":false,"runningTasks":null}""", 401 to "{}")
        assertEquals("the server reports busy when it cannot tell", true, activity(registryFailure).runningWork(endpoint))
        assertNull(activity(registryFailure).enabledSchedule(endpoint))

        val broken = KernelHttp(503 to "{}", 200 to "<html>")
        assertNull(activity(broken).runningWork(endpoint))
        assertNull(activity(broken).enabledSchedule(endpoint))

        val unreachable = HttpLocalKernelActivity(OkHttpClient.Builder().addInterceptor { throw java.io.IOException("refused") }.build(), { "s" }) { it() }
        assertNull(unreachable.runningWork(endpoint))
    }
}
