"""tests/test_api_billing_terminal_settle.py — billing is bound to the
TERMINAL event, not the HTTP request lifecycle.

Root-cause regression suite for two confirmed reservation-leak paths on
the headless Agent API (``/api/v1/agent/run`` + ``/api/v1/chat/completions``):

  (a) BLOCKING TIMEOUT — the client's ``timeout_s`` elapses and the route
      returns 500 while the task is still running. The task finishes a
      moment later. The pre-flight credit reservation MUST be settled
      against actual usage when the task reaches terminal (via the task's
      ``on_terminal`` callback), NOT stranded until the 30-min janitor.

  (b) STREAM CLIENT DISCONNECT — the SSE consumer hangs up mid-generation
      (``GeneratorExit``). The task keeps running; settlement MUST happen
      on the terminal event, again via the ``on_terminal`` callback — the
      generator itself never settles (so a disconnect can't skip it).

The invariant asserted throughout is the one the janitor uses to detect a
leak: after the task is terminal there is NO ``reserve`` ledger row for
this task without a matching ``reserve_release`` / ``debit``. i.e. the
ledger is BALANCED.

Semantics chosen (and documented in the fix): on disconnect / late-finish
we ``settle`` against the task's ACTUAL final usage (not a blanket
refund), because the work really ran — a blanket refund under-bills
consumed tokens. A task that genuinely never terminates falls to the
admission-slot TTL + the billing janitor (the crash backstop).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest


def _install_shim():
    import quart
    sys.modules['flask'] = quart
    for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
        qs = f'quart.{attr}'
        if qs in sys.modules:
            sys.modules[f'flask.{attr}'] = sys.modules[qs]
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    from quart.wrappers import Request as _QR
    import inspect
    if inspect.iscoroutinefunction(_QR.get_json):
        _orig = _QR.get_json

        def _sync_get_json(self, *a, **kw):
            import asyncio as _a
            coro = _orig(self, *a, **kw)
            return _a.run(coro)
        _sync_get_json._genuine_async_get_json = _orig
        _QR.get_json = _sync_get_json


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _TerminalSettleBase(unittest.TestCase):

    USER_ID = 'usr_billtest'

    @classmethod
    def setUpClass(cls):
        _install_shim()
        cls._tmp = tempfile.TemporaryDirectory()

        # Fresh SQLite DB with the full schema (incl. billing_ledger).
        from lib.database import reset_sqlite_for_tests
        cls._db_snapshot = reset_sqlite_for_tests(
            os.path.join(cls._tmp.name, 'tofu.db'))

        # Isolate pricing.json + api_keys / byo stores.
        from unittest.mock import patch
        cls._pricing_path = os.path.join(cls._tmp.name, 'pricing.json')
        cls._pricing_patch = patch('lib.billing.pricing._PRICING_PATH',
                                   cls._pricing_path)
        cls._pricing_patch.start()
        from lib.billing import pricing as _p
        _p.reload_pricing()

        from lib import api_keys, byo_providers
        cls._orig_keys = api_keys._STORE_PATH
        cls._orig_byo = byo_providers._STORE_PATH
        api_keys._STORE_PATH = os.path.join(cls._tmp.name, 'api_keys.json')
        byo_providers._STORE_PATH = os.path.join(cls._tmp.name, 'byo.json')
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        byo_providers._cache.clear()
        byo_providers._cache_loaded = False

        os.environ['TUNNEL_TOKEN'] = 'test-no-real'
        # Turn billing ON for these tests (env wins over relay.json).
        cls._orig_billing = os.environ.get('TOFU_RELAY_BILLING')
        os.environ['TOFU_RELAY_BILLING'] = '1'
        cls._orig_preflight = os.environ.get('TOFU_EPHEMERAL_PREFLIGHT')
        os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = '0'

        from quart import Quart
        cls.app = Quart(__name__)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)
        from routes.api_v1.agent_run import api_v1_agent_run_bp
        from routes.api_v1.chat import api_v1_chat_bp
        cls.app.register_blueprint(api_v1_agent_run_bp)
        cls.app.register_blueprint(api_v1_chat_bp)

        # A key bound to a billing user (so auth.user_id is populated →
        # the reserve/settle path is live rather than a personal no-op).
        from lib.api_keys import create_key
        _row, cls.token = create_key(
            name='bill-bot', scopes=['agents:run', 'chat'],
            user_id=cls.USER_ID)

        # Fund the wallet generously so reserve() never 402s.
        from lib.billing import deposit
        deposit(cls.USER_ID, 10_000_000, kind='topup', ref_id='boot_fund')

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys, byo_providers
        api_keys._STORE_PATH = cls._orig_keys
        byo_providers._STORE_PATH = cls._orig_byo
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        byo_providers._cache.clear()
        byo_providers._cache_loaded = False
        cls._pricing_patch.stop()
        from lib.database import restore_db_state
        restore_db_state(getattr(cls, '_db_snapshot', None))
        for name, val in (('TOFU_RELAY_BILLING', cls._orig_billing),
                          ('TOFU_EPHEMERAL_PREFLIGHT', cls._orig_preflight)):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val
        cls._tmp.cleanup()

    def setUp(self):
        import lib.runtime_state_store as rss
        rss.reset_for_test()
        from lib.idempotency import _cache as _id_cache
        _id_cache.clear()

    def tearDown(self):
        import lib.runtime_state_store as rss
        rss.reset_for_test()

    # ── helpers ─────────────────────────────────────────────────────

    def _ref_is_stranded(self, ref_id: str) -> bool:
        """True when the ledger has a ``reserve`` for ``ref_id`` with NO
        matching ``reserve_release`` / ``debit`` — the exact leak the
        janitor sweeps. A large cutoff makes every reserve age-eligible so
        the query reflects settle-state, not wall-clock."""
        from lib.billing.janitor import _stale_reservations
        cutoff = int(time.time()) + 3600
        return any(r[1] == ref_id for r in _stale_reservations(cutoff))

    def _wait_balanced(self, ref_id: str, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._ref_is_stranded(ref_id):
                return True
            time.sleep(0.05)
        return False


class BlockingTimeoutSettleTest(_TerminalSettleBase):
    """A slow task that outran the client's timeout must still settle its
    reservation when it terminates (via the terminal callback)."""

    def _run_late_finish(self, path: str, model: str):
        import threading
        import lib.tasks_pkg as pkg
        from lib.tasks_pkg.manager import append_event

        captured = {}

        def _deferred_spawn(task):
            captured['id'] = task['id']

            def _worker():
                time.sleep(1.0)  # finish AFTER the route's timeout_s
                task['content'] = 'late hello'
                task['status'] = 'done'
                task['finishReason'] = 'stop'
                task['usage'] = {'input_tokens': 50, 'output_tokens': 20,
                                 'total_tokens': 70}
                append_event(task, {'type': 'done', 'finishReason': 'stop',
                                    'usage': task['usage']})
            threading.Thread(target=_worker, daemon=True).start()

        orig = pkg.spawn_task
        pkg.spawn_task = _deferred_spawn
        try:
            async def go():
                cli = self.app.test_client()
                r = await cli.post(
                    path,
                    headers={'Authorization': f'Bearer {self.token}'},
                    json={'model': model,
                          'messages': [{'role': 'user', 'content': 'hi'}],
                          'timeout_s': 0.4})
                # The client-side call timed out (task still running).
                self.assertEqual(r.status_code, 500,
                                 await r.get_data(as_text=True))
            _new_loop_run(go())
        finally:
            pkg.spawn_task = orig

        ref_id = captured.get('id')
        self.assertIsNotNone(ref_id)
        # A reservation WAS placed (billing is on + nonzero-priced model).
        # Once the deferred worker finishes, the terminal callback must
        # settle it → ledger balanced (no stranded reserve).
        self.assertTrue(
            self._wait_balanced(ref_id),
            f'reservation for {ref_id} was left stranded after the task '
            f'terminated (settle not bound to the terminal event)')

    def test_agent_run_blocking_timeout_late_finish_settles(self):
        self._run_late_finish('/api/v1/agent/run', 'gpt-4o-mini')

    def test_chat_blocking_timeout_late_finish_settles(self):
        self._run_late_finish('/api/v1/chat/completions', 'gpt-4o-mini')


class StreamDisconnectSettleTest(_TerminalSettleBase):
    """A stream that disconnects mid-generation must NOT strand the hold.

    Driven END-TO-END through the real route (real ``_on_done``): POST with
    ``stream=true``, receive one chunk, DISCONNECT the client while the task
    is still running, then let the task terminate. Settlement is owned by the
    route's ``on_terminal`` callback — the SSE generator itself never settles
    (a disconnect would skip it) — so the ledger must balance once the task
    reaches terminal, NOT wait for the 30-min janitor."""

    def _run_disconnect(self, path: str, model: str):
        import threading
        import lib.tasks_pkg as pkg
        from lib.tasks_pkg.manager import append_event

        captured = {}
        terminate = threading.Event()

        def _spawn(task):
            captured['id'] = task['id']
            # Emit a partial delta so the first client receive returns data.
            append_event(task, {'type': 'delta', 'content': 'partial'})

            def _worker():
                # Stay running until the test signals (post-disconnect).
                terminate.wait(3.0)
                task['content'] = 'partial done'
                task['status'] = 'done'
                task['finishReason'] = 'stop'
                task['usage'] = {'input_tokens': 10, 'output_tokens': 4,
                                 'total_tokens': 14}
                append_event(task, {'type': 'done', 'finishReason': 'stop',
                                    'usage': task['usage']})
            threading.Thread(target=_worker, daemon=True).start()

        orig = pkg.spawn_task
        pkg.spawn_task = _spawn
        try:
            async def go():
                cli = self.app.test_client()
                async with cli.request(
                        path, method='POST',
                        headers={'Authorization': f'Bearer {self.token}',
                                 'Content-Type': 'application/json'}) as conn:
                    import json as _json
                    await conn.send(_json.dumps({
                        'model': model,
                        'messages': [{'role': 'user', 'content': 'hi'}],
                        'stream': True, 'timeout_s': 5}).encode())
                    await conn.send_complete()
                    # Receive at least one streamed chunk (task is running).
                    chunk = await conn.receive()
                    self.assertTrue(chunk)
                    # Mid-flight the reservation must still be OPEN — no
                    # premature settle/refund while work is ongoing.
                    self.assertTrue(
                        self._ref_is_stranded(captured['id']),
                        'reservation must stay open while task runs')
                    # Client hangs up mid-generation.
                    await conn.disconnect()
            _new_loop_run(go())
        finally:
            # Let the still-running task terminate → fires the real _on_done.
            terminate.set()
            pkg.spawn_task = orig

        ref_id = captured.get('id')
        self.assertIsNotNone(ref_id)
        self.assertTrue(
            self._wait_balanced(ref_id),
            f'reservation for {ref_id} stranded after terminal — the SSE '
            f'disconnect skipped settlement (settle not bound to terminal)')

    def test_agent_run_stream_disconnect_settles_on_terminal(self):
        self._run_disconnect('/api/v1/agent/run', 'gpt-4o-mini')

    def test_chat_stream_disconnect_settles_on_terminal(self):
        self._run_disconnect('/api/v1/chat/completions', 'gpt-4o-mini')


class SettleIdempotencyTest(_TerminalSettleBase):
    """settle_task is safe to call multiple times (terminal callback + the
    happy-path route settle can both fire) — the wallet is debited once."""

    def test_double_settle_debits_once(self):
        from lib.tasks_pkg import create_task
        from lib.billing import get_balance
        from lib.billing.request_flow import reserve_for_task, settle_task

        task = create_task('conv-idem',
                           [{'role': 'user', 'content': 'hi'}],
                           {'model': 'gpt-4o-mini'})
        reserve_for_task(task, user_id=self.USER_ID, model='gpt-4o-mini',
                         prompt_tokens=100, max_completion_tokens=100)
        task['usage'] = {'input_tokens': 100, 'output_tokens': 50,
                         'total_tokens': 150}

        first = settle_task(task, user_id=self.USER_ID, model='gpt-4o-mini')
        bal_after_first = get_balance(self.USER_ID)
        second = settle_task(task, user_id=self.USER_ID, model='gpt-4o-mini')
        bal_after_second = get_balance(self.USER_ID)

        self.assertIsNotNone(first)
        # Second call is idempotent: balance unchanged, same result echoed.
        self.assertEqual(bal_after_first, bal_after_second)
        self.assertEqual(first, second)
        self.assertFalse(self._ref_is_stranded(task['id']))


class ReaperSettleTest(_TerminalSettleBase):
    """A wedged task force-finalized by the reaper must settle its
    reservation too. ``_finalize_reaped_stuck_task`` emits a terminal
    ``DONE(error)`` via ``append_event`` → ``notify_task(terminal=True)`` →
    ``fire_terminal_callbacks`` → the route's ``_on_done`` → settle. This
    proves the terminal-callback binding subsumes the reaper path (no
    billing code belongs in the reaper, which has no billing context)."""

    def test_reaped_task_settles_via_terminal_callback(self):
        import threading
        import lib.tasks_pkg as pkg

        captured = {}
        block = threading.Event()

        def _spawn(task):
            captured['id'] = task['id']
            # Simulate a wedged worker: register the terminal callback path is
            # already set by the route; just never terminate on our own.
            def _worker():
                block.wait(3.0)
            threading.Thread(target=_worker, daemon=True).start()

        orig = pkg.spawn_task
        pkg.spawn_task = _spawn
        try:
            async def go():
                cli = self.app.test_client()
                r = await cli.post(
                    '/api/v1/agent/run',
                    headers={'Authorization': f'Bearer {self.token}'},
                    json={'model': 'gpt-4o-mini',
                          'messages': [{'role': 'user', 'content': 'hi'}],
                          'timeout_s': 0.4})
                self.assertEqual(r.status_code, 500)
            _new_loop_run(go())

            ref_id = captured['id']
            self.assertTrue(self._ref_is_stranded(ref_id),
                            'precondition: reservation open while wedged')

            # Now force the reaper's finalize on the wedged task.
            from lib.tasks_pkg import tasks, tasks_lock
            from lib.tasks_pkg.manager import _finalize_reaped_stuck_task
            with tasks_lock:
                t = tasks.get(ref_id)
            self.assertIsNotNone(t)
            t['aborted'] = True
            t['_abort_reason'] = 'stuck_no_progress'
            t['status'] = 'error'
            t['finishReason'] = 'error'
            t['error'] = {'kind': 'internal', 'detail': 'stuck'}
            _finalize_reaped_stuck_task(t)

            self.assertTrue(
                self._wait_balanced(ref_id),
                f'reaped task {ref_id} left its reservation stranded — the '
                f'terminal-callback chain did not settle on reaper finalize')
        finally:
            block.set()
            pkg.spawn_task = orig


class BaseExceptionEmitsTerminalTest(unittest.TestCase):
    """run_task must emit a terminal DONE(error) even when the fatal is a
    BaseException (KeyboardInterrupt / SystemExit / CancelledError), then
    re-raise — so the terminal-callback chain (release slot + settle) still
    fires. Without this the task stays non-terminal and leaks its slot +
    reservation until the TTL/janitor."""

    def test_base_exception_fires_terminal_then_reraises(self):
        import threading
        import lib.tasks_pkg.orchestrator as orch
        from lib.tasks_pkg.manager import create_task
        from lib.agent_core.admission import (
            on_terminal, fire_terminal_callbacks,
        )

        # Force the very first thing run_task does inside its try-body to
        # raise a BaseException (KeyboardInterrupt).
        orig_reset = None
        try:
            import lib.swarm.integration as si
            orig_reset = si.reset_autocontinue_chain

            def _boom(*a, **k):
                raise KeyboardInterrupt('simulated cancel')
            si.reset_autocontinue_chain = _boom
        except Exception:
            self.skipTest('cannot install swarm.integration boom hook')

        task = create_task('conv-be',
                           [{'role': 'user', 'content': 'hi'}],
                           {'model': 'test-model'})
        fired = {'terminal': False}
        # Mirror the route: register a terminal callback; append_event on the
        # DONE event should fire it.
        on_terminal(task['id'], lambda _tid: fired.__setitem__('terminal', True))

        try:
            with self.assertRaises(KeyboardInterrupt):
                orch.run_task(task)
        finally:
            if orig_reset is not None:
                si.reset_autocontinue_chain = orig_reset

        # The task reached a terminal status and a terminal event was emitted.
        self.assertEqual(task['status'], 'error')
        self.assertEqual(task['finishReason'], 'error')
        types = [e.get('type') for e in task.get('events', [])]
        self.assertIn('done', types,
                      'BaseException path must emit a terminal DONE event so '
                      'on_terminal callbacks (slot release + settle) fire')
        # The terminal callback fired (proving slot/settle would be reached).
        self.assertTrue(fired['terminal'])


if __name__ == '__main__':
    unittest.main()
