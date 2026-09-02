"""tests/test_admission.py — AdmissionController + event-driven waiter.

Validates the primitives that replaced the headless API's busy-wait /
unbounded-thread architecture:
  * AdmissionController.try_acquire/release bounds concurrency + returns
    False at capacity.
  * notify_task wakes await_terminal without polling.
  * terminal callbacks fire exactly once, even with no waiter registered.
  * notify_task from a worker THREAD wakes a coroutine on the loop.
"""

import asyncio
import threading
import time
import unittest

import lib.agent_core.admission as admission


def test_personal_server_default_is_bounded(monkeypatch):
    monkeypatch.delenv('TOFU_MAX_INFLIGHT_TASKS', raising=False)
    monkeypatch.delenv('TOFU_DEPLOYMENT_MODE', raising=False)
    monkeypatch.setattr(
        admission, 'deployment_resource_default', lambda *_args: 4)
    assert admission._default_max_inflight() == 4


def test_invalid_inflight_config_falls_back_safely(monkeypatch):
    monkeypatch.setenv('TOFU_MAX_INFLIGHT_TASKS', 'not-an-int')
    monkeypatch.delenv('TOFU_DEPLOYMENT_MODE', raising=False)
    monkeypatch.setattr(
        admission, 'deployment_resource_default', lambda *_args: 4)
    assert admission._default_max_inflight() == 4


def test_zero_inflight_config_cannot_disable_production_admission(monkeypatch):
    monkeypatch.setenv('TOFU_MAX_INFLIGHT_TASKS', '0')
    monkeypatch.delenv('TOFU_DEPLOYMENT_MODE', raising=False)
    monkeypatch.setattr(
        admission, 'deployment_resource_default', lambda *_args: 4)
    assert admission._default_max_inflight() == 4


def test_memory_pressure_gate_fails_closed_and_is_disableable(monkeypatch):
    import lib.cgroup_guard as cg

    monkeypatch.setenv('TOFU_ADMISSION_CGROUP_PCT', '96')
    monkeypatch.setattr(cg, 'pressure', lambda: {
        'pct': 97.5, 'usage': 975, 'limit': 1000, 'swap': 0})
    assert admission._memory_pressure_allows_admission() is False

    monkeypatch.setattr(cg, 'pressure', lambda: {
        'pct': 95.9, 'usage': 959, 'limit': 1000, 'swap': 0})
    assert admission._memory_pressure_allows_admission() is True

    monkeypatch.setenv('TOFU_ADMISSION_CGROUP_PCT', '0')
    assert admission._memory_pressure_allows_admission() is True


def test_controller_refuses_before_allocating_slot_under_pressure(monkeypatch):
    import lib.runtime_state_store as rss

    rss.reset_for_test()
    monkeypatch.setattr(admission, '_memory_pressure_allows_admission',
                        lambda: False)
    ctrl = admission.AdmissionController(max_inflight=4)
    assert ctrl.try_acquire() is False
    assert ctrl.in_flight == 0
    assert ctrl.stats()['last_refusal_reason'] == 'memory_pressure'


class AdmissionControllerTest(unittest.TestCase):

    def setUp(self):
        # AdmissionController now counts its in-flight slots in the SHARED
        # runtime_state_store (Build Order step 2). Reset it so each test
        # starts from a clean global count (the production controller is a
        # singleton, but tests build fresh controllers that share the store).
        import lib.runtime_state_store as rss
        rss.reset_for_test()

    def tearDown(self):
        # ALSO reset on the way out: test_unbounded_when_zero pumps the
        # shared counter to 1000 (unbounded controller), and without this
        # that count leaks FORWARD into any suite that runs next and reads
        # the same global store (e.g. test_api_v1_agent_run's cap-64
        # production controller would then 503 every request). setUp-only
        # reset protects THIS file's ordering but not the next file's.
        import lib.runtime_state_store as rss
        rss.reset_for_test()

    def test_try_acquire_bounds_and_releases(self):
        async def go():
            ctrl = admission.AdmissionController(max_inflight=2)
            self.assertTrue(ctrl.try_acquire())
            self.assertTrue(ctrl.try_acquire())
            self.assertEqual(ctrl.in_flight, 2)
            # At capacity → refused.
            self.assertFalse(ctrl.try_acquire())
            ctrl.release()
            self.assertEqual(ctrl.in_flight, 1)
            # Slot freed → granted again.
            self.assertTrue(ctrl.try_acquire())
            self.assertFalse(ctrl.try_acquire())
        asyncio.new_event_loop().run_until_complete(go())

    def test_unbounded_when_zero(self):
        async def go():
            ctrl = admission.AdmissionController(max_inflight=0)
            for _ in range(1000):
                self.assertTrue(ctrl.try_acquire())
            self.assertEqual(ctrl.in_flight, 1000)
            self.assertEqual(ctrl.stats()['available'], -1)
        asyncio.new_event_loop().run_until_complete(go())

    def test_over_release_is_safe(self):
        async def go():
            ctrl = admission.AdmissionController(max_inflight=1)
            ctrl.release()  # never acquired
            self.assertEqual(ctrl.in_flight, 0)
            self.assertTrue(ctrl.try_acquire())
        asyncio.new_event_loop().run_until_complete(go())

    def test_living_long_task_refreshes_its_admission_lease(self):
        """Crossing a lease TTL must not make a live LLM task disappear."""
        ctrl = admission.AdmissionController(max_inflight=1)
        ctrl._ttl = 0.12
        try:
            self.assertTrue(ctrl.try_acquire())
            time.sleep(0.08)
            ctrl.refresh_held_slots()
            time.sleep(0.08)
            self.assertEqual(ctrl.in_flight, 1)
            self.assertFalse(ctrl.try_acquire())
        finally:
            ctrl.release()
            ctrl.shutdown()


class WaiterTest(unittest.TestCase):

    def test_await_terminal_already_done_fast_path(self):
        async def go():
            task = {'id': 'tdone', 'status': 'done'}
            ok = await admission.await_terminal(task, timeout_s=1)
            self.assertTrue(ok)
        asyncio.new_event_loop().run_until_complete(go())

    def test_await_terminal_times_out(self):
        async def go():
            task = {'id': 'thang', 'status': 'running'}
            admission.register_waiter('thang')
            t0 = time.time()
            ok = await admission.await_terminal(task, timeout_s=0.3)
            admission.unregister_waiter('thang')
            self.assertFalse(ok)
            self.assertLess(time.time() - t0, 2.0)
        asyncio.new_event_loop().run_until_complete(go())

    def test_notify_from_thread_wakes_coroutine(self):
        async def go():
            task = {'id': 'twake', 'status': 'running'}
            admission.register_waiter('twake')

            def worker():
                time.sleep(0.1)
                task['status'] = 'done'
                # Mimic manager.append_event's terminal notify.
                admission.notify_task('twake', terminal=True)

            threading.Thread(target=worker, daemon=True).start()
            t0 = time.time()
            ok = await admission.await_terminal(task, timeout_s=5)
            elapsed = time.time() - t0
            admission.unregister_waiter('twake')
            self.assertTrue(ok)
            # Woken by the signal, not the 1s defensive re-check.
            self.assertLess(elapsed, 0.9)
        asyncio.new_event_loop().run_until_complete(go())


class TerminalCallbackTest(unittest.TestCase):

    def test_callback_fires_once_without_waiter(self):
        calls = []
        admission.on_terminal('tcb', lambda tid: calls.append(tid))
        # No waiter registered — callback must still fire.
        admission.notify_task('tcb', terminal=True)
        self.assertEqual(calls, ['tcb'])
        # Second terminal notify is a no-op (callbacks were popped).
        admission.notify_task('tcb', terminal=True)
        self.assertEqual(calls, ['tcb'])

    def test_callback_isolated_on_error(self):
        calls = []

        def boom(tid):
            raise RuntimeError('dispose blew up')

        admission.on_terminal('tiso', boom)
        admission.on_terminal('tiso', lambda tid: calls.append(tid))
        admission.notify_task('tiso', terminal=True)
        # The second callback still ran despite the first raising.
        self.assertEqual(calls, ['tiso'])

    def test_non_terminal_notify_does_not_fire_callbacks(self):
        calls = []
        admission.on_terminal('tnt', lambda tid: calls.append(tid))
        admission.notify_task('tnt', terminal=False)
        self.assertEqual(calls, [])
        admission.notify_task('tnt', terminal=True)
        self.assertEqual(calls, ['tnt'])


if __name__ == '__main__':
    unittest.main()
