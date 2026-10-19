"""Queued preparation must retain a bounded work budget before any shutdown."""
from contextlib import contextmanager
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from serverctl_pkg import native_engine as engines

pytestmark = [pytest.mark.unit, pytest.mark.skipif(os.name == 'nt', reason='POSIX lock contention')]


@contextmanager
def queued_project(tmp_path, monkeypatch, *, wait_seconds):
    import fcntl
    import runtime_guards

    crate = tmp_path / 'packages/tofu-db'
    crate.mkdir(parents=True)
    (crate / 'Cargo.toml').write_text('[package]\nname="test"\nversion="0.0.0"\n')
    lock_path = crate / 'target/.engine-update.lock'
    lock_path.parent.mkdir()
    now = [100.0]
    monkeypatch.setattr(engines, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=lambda _: None))
    monkeypatch.setattr(engines, 'source_fingerprint', lambda _: 'source')
    monkeypatch.setattr(engines, 'reconcile_project_pin', lambda *_: False)
    monkeypatch.setattr(engines, '_verify_pin', lambda *_: None)
    monkeypatch.setattr(runtime_guards, 'load_deployment_configuration',
                        lambda _: SimpleNamespace(storage_backend='tofudb'))
    with lock_path.open('a+b') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        def finish_competing_preparation(_):
            now[0] += wait_seconds
            fcntl.flock(owner, fcntl.LOCK_UN)
        monkeypatch.setattr(engines.time, 'sleep', finish_competing_preparation)
        yield now


def test_late_lock_acquisition_leaves_full_work_budget_for_real_child(tmp_path, monkeypatch):
    with queued_project(tmp_path, monkeypatch, wait_seconds=495) as now:
        budgets = []
        def verify(binary, project, expected):
            budgets.append(engines._deadline.get() - now[0])
            now[0] += 570
            # A real child must still be allowed after spending almost all of
            # the work budget. The former shared 600s clock expires here.
            assert engines._run([sys.executable, '-c', 'print("verified")'],
                cwd=project, environment=dict(os.environ), timeout=10) == 'verified'
        monkeypatch.setattr(engines, '_verify_generation', verify)
        parent_deadline = engines._deadline.get()
        assert engines.prepare(tmp_path, {}) is False
        assert budgets == [engines.PREPARATION_TOTAL_TIMEOUT_SECONDS - 495]
        assert now[0] == 1165
        assert engines._deadline.get() == parent_deadline


def test_outer_total_and_work_budget_bound_actual_verification(tmp_path, monkeypatch):
    with queued_project(tmp_path, monkeypatch, wait_seconds=495) as now:
        def verify(*_):
            now[0] += engines.PREPARATION_TOTAL_TIMEOUT_SECONDS - 495 + 1
            engines._run(['unused'], cwd=tmp_path, environment={}, timeout=10)
        monkeypatch.setattr(engines, '_verify_generation', verify)
        monkeypatch.setattr(engines.subprocess, 'Popen',
                            lambda *_a, **_k: pytest.fail('expired work must not spawn'))
        monkeypatch.setattr(engines, '_prebuilt', lambda *_: None)
        monkeypatch.setattr(engines, '_build', lambda *_: engines._run(
            ['unused'], cwd=tmp_path, environment={}, timeout=10))
        # The existing repair path retries no expired command and remains loud.
        with pytest.raises(engines.EnginePreparationError, match='timed out'):
            engines.prepare(tmp_path, {})
        assert engines._deadline.get() == float('inf')


def test_contended_lock_times_out_without_starting_preparation(tmp_path, monkeypatch):
    with queued_project(tmp_path, monkeypatch, wait_seconds=1) as now:
        monkeypatch.setattr(engines.time, 'sleep', lambda _: now.__setitem__(0, now[0] + 601))
        monkeypatch.setattr(engines, '_verify_generation',
                            lambda *_: pytest.fail('must not verify without the lock'))
        with pytest.raises(engines.EnginePreparationError, match='Another storage engine update'):
            engines.prepare(tmp_path, {})
        assert engines._deadline.get() == float('inf')


def test_nested_work_scope_never_extends_caller_deadline(monkeypatch):
    monkeypatch.setattr(engines.time, 'monotonic', lambda: 100.0)
    token = engines._deadline.set(150.0)
    try:
        with engines._preparation_budget(600):
            assert engines._deadline.get() == 150.0
        assert engines._deadline.get() == 150.0
    finally:
        engines._deadline.reset(token)


def test_work_failure_releases_lock_and_deadline_scope(tmp_path, monkeypatch):
    path = tmp_path / 'prepare.lock'
    parent = engines._deadline.get()
    with pytest.raises(RuntimeError, match='failed verification'):
        with engines._prepare_lock(path), engines._preparation_budget(600):
            raise RuntimeError('failed verification')
    assert engines._deadline.get() == parent
    with engines._prepare_lock(path):
        pass  # A failed owner must not strand the next preparation.


def test_cli_child_timeout_covers_queue_and_preparation(monkeypatch, tmp_path):
    import runtime_guards
    import serverctl
    monkeypatch.setattr(runtime_guards, 'load_deployment_configuration',
                        lambda _: SimpleNamespace(storage_backend='tofudb'))
    monkeypatch.setattr(engines, 'reconcile_project_pin', lambda *_: False)
    budgets = []
    def child(*_, timeout, **kwargs):
        budgets.append(timeout)
        return ''
    monkeypatch.setattr(engines, '_run', child)
    assert serverctl.prepare_native_engine_artifact('restart', project=str(tmp_path)) == ''
    assert budgets == [engines.PREPARATION_TOTAL_TIMEOUT_SECONDS + 30]


@pytest.mark.parametrize('wait_seconds', [0, 495, 599])
def test_work_uses_unused_queue_budget_without_renewing_total(tmp_path, monkeypatch, wait_seconds):
    with queued_project(tmp_path, monkeypatch, wait_seconds=wait_seconds) as now:
        def verify(*_):
            remaining = engines._deadline.get() - now[0]
            assert remaining == engines.PREPARATION_TOTAL_TIMEOUT_SECONDS - wait_seconds
            # Uncontended preparation may continue beyond the old 600s cap.
            now[0] += remaining - 1
            assert engines._run([sys.executable, '-c', 'print("verified")'],
                cwd=tmp_path, environment=dict(os.environ), timeout=10) == 'verified'
        monkeypatch.setattr(engines, '_verify_generation', verify)
        assert engines.prepare(tmp_path, {}) is False
        assert now[0] == 100 + engines.PREPARATION_TOTAL_TIMEOUT_SECONDS - 1


def test_prepare_never_extends_shorter_caller_deadline(tmp_path, monkeypatch):
    with queued_project(tmp_path, monkeypatch, wait_seconds=0) as now:
        token = engines._deadline.set(now[0] + 50)
        try:
            def verify(*_):
                assert engines._deadline.get() == 150
            monkeypatch.setattr(engines, '_verify_generation', verify)
            assert engines.prepare(tmp_path, {}) is False
            assert engines._deadline.get() == 150
        finally:
            engines._deadline.reset(token)
