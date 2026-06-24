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

from lib.agent_core import admission


class AdmissionControllerTest(unittest.TestCase):

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
