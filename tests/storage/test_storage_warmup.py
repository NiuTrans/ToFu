"""Storage warm-up keeper: bounded, fail-soft, and explicit about coverage."""
from __future__ import annotations

import threading
import time
import unittest

import pytest

from lib.storage import warmup
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _drain_shared_warmup_executor():
    """The passes share one long-lived executor (C10 hardening): stragglers
    from an earlier test must never bleed into the next test's pass."""
    yield
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with warmup._pool_lock:
            pending = [f for f in warmup._inflight.values() if not f.done()]
        if not pending:
            break
        for future in pending:
            try:
                future.result(timeout=1)
            except Exception:
                pass


class _FakeClient:
    def __init__(self, *, fail: set[str] | None = None, delay: float = 0.0):
        self.calls: list[tuple[str, dict, float]] = []
        self._fail = fail or set()
        self._delay = delay

    def query(self, operation, payload, deadline=None):
        self.calls.append((operation, dict(payload or {}), deadline))
        if operation in self._fail:
            raise StorageError('database_timeout', 'deadline_elapsed')
        return {}


class RunWarmupPassTests(unittest.TestCase):
    def test_touches_every_hot_family_with_bounded_deadline(self):
        client = _FakeClient()
        stats = warmup.run_warmup_pass(
            client, deadline_seconds=180.0, budget_seconds=600.0)
        operations = [call[0] for call in client.calls]
        self.assertEqual(
            operations,
            ['conversation.count', 'conversation.list', 'record.list',
             'task_results.recovery_pending', 'timer.active.count',
             'credential.list'])
        self.assertEqual(stats['failed'], [])
        self.assertEqual(stats['skipped'], [])
        self.assertEqual(len(stats['warmed']), len(operations))
        for _, payload, deadline in client.calls:
            self.assertLessEqual(deadline, 180.0)
            self.assertIsInstance(payload, dict)
        record_payload = client.calls[2][1]
        # A generic list's limit does not bound full document hydration on
        # every backend. Warm metadata only through the catalog page contract.
        conversation_payload = client.calls[1][1]
        self.assertTrue(conversation_payload['catalog_page'])
        self.assertEqual(conversation_payload['limit'], 1)
        self.assertFalse(conversation_payload['include_messages'])
        self.assertEqual(conversation_payload['settings_keys'], [])
        self.assertTrue(record_payload['prefix'].endswith(':'))
        credential_payload = client.calls[5][1]
        self.assertGreater(credential_payload['owner_user_id'], 0)

    def test_transient_failure_is_fail_soft_per_operation(self):
        client = _FakeClient(fail={'record.list', 'timer.active.count'})
        stats = warmup.run_warmup_pass(
            client, deadline_seconds=10.0, budget_seconds=600.0)
        self.assertEqual(sorted(stats['failed']),
                         ['record.list', 'timer.active.count'])
        self.assertEqual(len(stats['warmed']), 4)
        self.assertEqual(stats['skipped'], [])

    def test_total_budget_skips_unfinished_operations(self):
        # Concurrent contract: every operation is submitted at once and the
        # budget bounds the WAIT — operations that cannot finish inside the
        # budget are honestly reported as skipped, never faked as warm.
        class _SlowClient(_FakeClient):
            def query(self, operation, payload, deadline=None):
                if operation != 'conversation.count':
                    time.sleep(0.4)
                return super().query(operation, payload, deadline=deadline)

        client = _SlowClient()
        stats = warmup.run_warmup_pass(
            client, deadline_seconds=180.0, budget_seconds=0.1)
        self.assertEqual(stats['warmed'], ['conversation.count'])
        self.assertEqual(
            sorted(stats['skipped']),
            ['conversation.list', 'credential.list', 'record.list',
             'task_results.recovery_pending', 'timer.active.count'])


class KeepWarmTests(unittest.TestCase):
    def tearDown(self):
        warmup.stop_keep_warm()

    def test_disabled_interval_starts_nothing(self):
        self.assertIsNone(warmup.start_keep_warm(
            client_factory=lambda: _FakeClient(), interval_seconds=0))
        self.assertIsNone(warmup._keep_warm_thread)

    def test_keeper_runs_passes_and_stops(self):
        passes = threading.Event()
        clients = []

        class _Client(_FakeClient):
            def query(self, operation, payload, deadline=None):
                result = super().query(operation, payload, deadline=deadline)
                if operation == 'credential.list':
                    passes.set()
                return result

        def factory():
            client = _Client()
            clients.append(client)
            return client

        thread = warmup.start_keep_warm(
            client_factory=factory, interval_seconds=0.05, deadline_seconds=5.0)
        self.assertIsNotNone(thread)
        self.assertTrue(passes.wait(timeout=5.0))
        # Repeated starts reuse the live thread instead of duplicating it.
        self.assertIs(warmup.start_keep_warm(
            client_factory=factory, interval_seconds=0.05), thread)
        warmup.stop_keep_warm(timeout=5.0)
        self.assertFalse(thread.is_alive())

    def test_keeper_survives_client_factory_failure(self):
        calls = []

        def factory():
            calls.append(1)
            raise StorageError('database_unavailable', 'sidecar restarting')

        thread = warmup.start_keep_warm(
            client_factory=factory, interval_seconds=0.05, deadline_seconds=5.0)
        try:
            self.assertIsNotNone(thread)
            deadline = threading.Event()
            self.assertFalse(deadline.wait(timeout=0.2) and not calls)
            self.assertTrue(thread.is_alive())
        finally:
            warmup.stop_keep_warm(timeout=5.0)


class EnvParsingTests(unittest.TestCase):
    def test_invalid_env_falls_back_to_default(self):
        import os
        os.environ['TOFU_STORAGE_WARMUP_DEADLINE_SECONDS'] = 'not-a-number'
        try:
            self.assertEqual(warmup.warmup_deadline_seconds(), 180.0)
        finally:
            del os.environ['TOFU_STORAGE_WARMUP_DEADLINE_SECONDS']

    def test_env_floor_is_enforced(self):
        import os
        os.environ['TOFU_STORAGE_WARMUP_DEADLINE_SECONDS'] = '0.0001'
        try:
            self.assertEqual(warmup.warmup_deadline_seconds(), 1.0)
        finally:
            del os.environ['TOFU_STORAGE_WARMUP_DEADLINE_SECONDS']


if __name__ == '__main__':
    unittest.main()


def test_initial_pass_runs_off_caller_with_periodic_warming_disabled(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocking_pass(client, **kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return {'failed': [], 'elapsed_seconds': 0.0}

    monkeypatch.setattr(warmup, 'run_warmup_pass', blocking_pass)
    thread = warmup.start_keep_warm(
        client_factory=object, interval_seconds=0, initial_pass=True)
    try:
        assert entered.wait(2)
        assert thread.is_alive()  # startup returned before the pass finished
        warmup.stop_keep_warm(timeout=0)
        assert warmup._keep_warm_thread is thread
        assert warmup.start_keep_warm(
            client_factory=object, interval_seconds=0, initial_pass=True) is thread
    finally:
        release.set()
        warmup.stop_keep_warm()
    assert not thread.is_alive()
    assert len(calls) == 1
    assert calls[0]['budget_seconds'] == warmup.warmup_boot_budget_seconds()
    assert warmup._keep_warm_thread is None
