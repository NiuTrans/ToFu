"""Boot-recovery orphan probe: short-circuit the settle when nothing is orphaned.

The two recovery scans (``recover_running_attempts`` over
``storage_generation_attempts.status`` and the ``task_results.recover_running``
keyspace walk) run on UNINDEXED status columns, so on a large or
network-resident authority they overrun the writer's transaction watchdog even
when the answer is zero. Every boot used to pay a ~5s interrupt plus a retry
for nothing. ``task_results.recovery_pending`` is a LIMIT-1 existence probe
that lets ``recover_stale_tasks_on_startup`` skip both scans entirely in the
overwhelmingly common no-orphan case, while a probe failure still falls back
to the authoritative settle (never skip a real orphan on a hiccup).
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_recover_stale_tasks_short_circuits_when_no_orphans(monkeypatch):
    """No orphans → neither the turn sweep nor the task keyspace walk runs."""
    import lib.storage
    import lib.tasks_pkg.manager._recovery as recovery

    calls = []

    class _Client:
        def query(self, op, payload, deadline=None):
            assert op == 'task_results.recovery_pending'
            return {'pending': False}

        def command(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError('task scan must be skipped when no orphans')

    monkeypatch.setattr(
        lib.storage, 'get_storage_client', lambda write=False: _Client())
    monkeypatch.setattr(
        'lib.turn_lifecycle.recover_running_attempts',
        lambda *a, **k: calls.append('turn-sweep') or 0)

    result = recovery.recover_stale_tasks_on_startup(
        prev_shutdown={'verdict': 'clean'})

    assert result['recoveredAttemptCount'] == 0
    assert result['recoveredTaskCount'] == 0
    assert result['conversationIds'] == []
    assert result['interruptedReason'] == 'manual_restart'
    assert calls == []  # the heavy turn sweep was never invoked


def test_recover_stale_tasks_falls_back_to_settle_on_probe_error(monkeypatch):
    """A probe hiccup must never skip a real orphan: fall back to the full settle."""
    import lib.storage
    import lib.tasks_pkg.manager._recovery as recovery

    class _Client:
        def query(self, op, payload, deadline=None):
            raise RuntimeError('probe transport hiccup')

        def command(self, op, payload, _receipt, deadline=None):
            assert op == 'task_results.recover_running'
            return {'recovered': [], 'remaining': False}

    monkeypatch.setattr(
        lib.storage, 'get_storage_client', lambda write=False: _Client())
    monkeypatch.setattr(
        'lib.turn_lifecycle.recover_running_attempts', lambda *a, **k: 0)

    result = recovery.recover_stale_tasks_on_startup(prev_shutdown=None)
    # The settle path ran (returned zero) instead of being short-circuited.
    assert result['recoveredTaskCount'] == 0
    assert result['interruptedReason'] == 'server_restart'
