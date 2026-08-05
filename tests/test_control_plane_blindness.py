"""pt_a21cd6eb ③-2/③-3 — control-plane blindness guards.

2026-08-01 evaporation family: TWO live tasks (a VU carrier and a plain
worker) vanished from the in-memory registry while their threads kept
running. Consequences measured that afternoon:

  * abort by id → 404; abort-conv → aborted:0 (③-3's tombstone channel);
  * every /api/v1/chat/poll then flipped the LIVE tasks' DB rows to
    'interrupted' via the "absent = crashed" heuristic (③-2's liveness gate).

These guards pin:
  A. the poll liveness gate — a task with a FRESH event log is reported
     running (never flipped); a stale/absent one keeps the legacy verdict;
  B. the abort tombstone channel — plant on registry-miss (running row
     only), consume from make_task_abort_check via BOTH the in-memory set
     and the throttled DB read-back, end-to-end for a vanished task;
  C. endpoint wiring (source pins).
"""

import json
import time

import pytest

import lib.tasks_pkg.event_log as evlog
import routes.chat_poll_abort as cpa
from lib.tasks_pkg.manager import _registry as reg
from lib.tasks_pkg.manager import _state as mstate


# ── fakes ────────────────────────────────────────────────────────────

class _FakeRow(dict):
    pass


class _FakeDB:
    """Answers the handful of SQL shapes the tombstone/gate code issues."""

    def __init__(self, *, meta_rows=None, running_ids=None, max_ts=None):
        self.meta_rows = meta_rows or {}       # task_id -> (metadata, status)
        self.running_ids = running_ids or []   # conv scan result
        self.max_ts = max_ts
        self.updates = []

    def execute(self, sql, params=()):
        s = ' '.join(sql.split()).lower()
        if 'max(ts_ms)' in s:
            return self
        if 'select metadata, status from task_results' in s:
            self._pending = ('meta', params[0])
            return self
        if 'select task_id from task_results' in s and "status='running'" in s:
            self._pending = ('convscan', None)
            return self
        if s.startswith('update task_results set metadata'):
            self.updates.append(params)
            return self
        raise AssertionError(f'unexpected SQL: {sql}')

    def fetchone(self):
        kind = getattr(self, '_pending', (None, None))
        if kind[0] == 'meta':
            meta, status = self.meta_rows.get(kind[1], (None, None))
            if meta is None:
                return None
            return _FakeRow(metadata=meta, status=status)
        if 'max' in getattr(self, '_last', ''):
            pass
        return _FakeRow(mx=self.max_ts) if self.max_ts is not None else None

    def fetchall(self):
        return [_FakeRow(task_id=t) for t in self.running_ids]

    def commit(self):
        pass


@pytest.fixture
def clean_tombstones():
    with mstate._abort_tombstones_lock:
        mstate._abort_tombstones.clear()
    yield
    with mstate._abort_tombstones_lock:
        mstate._abort_tombstones.clear()


# ── A. poll liveness gate (③-2) ──────────────────────────────────────

@pytest.mark.unit
class TestPollLivenessGate:

    def test_fresh_event_log_means_alive(self, monkeypatch):
        monkeypatch.setattr('lib.tasks_pkg.event_log.latest_event_ts',
                            lambda tid: int(time.time() * 1000) - 5_000)
        assert cpa._live_unregistered_gate('task-x') is True

    def test_stale_event_log_falls_to_legacy_verdict(self, monkeypatch):
        monkeypatch.setattr('lib.tasks_pkg.event_log.latest_event_ts',
                            lambda tid: int(time.time() * 1000) - 600_000)
        assert cpa._live_unregistered_gate('task-x') is False

    def test_no_events_falls_to_legacy_verdict(self, monkeypatch):
        monkeypatch.setattr('lib.tasks_pkg.event_log.latest_event_ts',
                            lambda tid: None)
        assert cpa._live_unregistered_gate('task-x') is False

    def test_probe_failure_is_fail_safe_to_legacy(self, monkeypatch):
        def _boom(tid):
            raise RuntimeError('db down')
        monkeypatch.setattr('lib.tasks_pkg.event_log.latest_event_ts', _boom)
        assert cpa._live_unregistered_gate('task-x') is False

    def test_latest_event_ts_accessor(self, monkeypatch):
        fake = _FakeDB(max_ts=1720000000000)
        monkeypatch.setattr(evlog, 'get_thread_db', lambda domain: fake)
        assert evlog.latest_event_ts('task-x') == 1720000000000


# ── B. abort tombstone channel (③-3) ─────────────────────────────────

@pytest.mark.unit
class TestAbortTombstone:

    def test_plant_requires_running_row(self, monkeypatch, clean_tombstones):
        fake = _FakeDB(meta_rows={'t-dead': ('{}', 'done')})
        monkeypatch.setattr('lib.database.get_thread_db', lambda domain: fake)
        assert reg.plant_abort_tombstone('t-dead', source='test') is False
        assert not reg.has_abort_tombstone('t-dead')

    def test_plant_writes_metadata_and_memory(self, monkeypatch,
                                              clean_tombstones):
        fake = _FakeDB(meta_rows={'t-live': ('{}', 'running')})
        monkeypatch.setattr('lib.database.get_thread_db', lambda domain: fake)
        assert reg.plant_abort_tombstone('t-live', source='test') is True
        assert reg.has_abort_tombstone('t-live')
        assert fake.updates, 'tombstone metadata was not written to the row'
        meta = json.loads(fake.updates[0][0])
        assert meta['_abort_requested'] > 0
        assert meta['_abort_source'] == 'test'

    def test_abort_check_consumes_memory_tombstone(self, clean_tombstones):
        task = {'id': 't-ghost', 'aborted': False}
        with mstate._abort_tombstones_lock:
            mstate._abort_tombstones.add('t-ghost')
        check = reg.make_task_abort_check(task)
        assert check() is True
        assert check() is True  # hit latched

    def test_abort_check_db_channel(self, monkeypatch, clean_tombstones):
        monkeypatch.setattr(reg, '_db_abort_tombstoned', lambda tid: True)
        task = {'id': 't-ghost2', 'aborted': False}
        check = reg.make_task_abort_check(task)
        assert check() is True  # first call reads DB (last_db=0.0)

    def test_abort_check_plain_flag_and_negative(self, monkeypatch,
                                                 clean_tombstones):
        monkeypatch.setattr(reg, '_db_abort_tombstoned', lambda tid: False)
        assert reg.make_task_abort_check({'id': 'a', 'aborted': True})() is True
        assert reg.make_task_abort_check({'id': 'b', 'aborted': False})() is False

    def test_end_to_end_vanished_task(self, monkeypatch, clean_tombstones):
        """create → vanish from registry → plant → worker's check sees it."""
        fake = _FakeDB(meta_rows={'t-vanished': ('{}', 'running')})
        monkeypatch.setattr('lib.database.get_thread_db', lambda domain: fake)
        task = {'id': 't-vanished', 'aborted': False, 'convId': 'c1',
                'status': 'running'}
        with reg.tasks_lock:
            reg.tasks['t-vanished'] = task
        try:
            with reg.tasks_lock:
                reg.tasks.pop('t-vanished', None)  # the evaporation
            assert reg.plant_abort_tombstone('t-vanished',
                                             source='test') is True
            assert reg.make_task_abort_check(task)() is True
        finally:
            with reg.tasks_lock:
                reg.tasks.pop('t-vanished', None)

    def test_conv_sweep_only_tombstones_registry_lost(self, monkeypatch,
                                                      clean_tombstones):
        fake = _FakeDB(running_ids=['a', 'b'])
        monkeypatch.setattr('lib.database.get_thread_db', lambda domain: fake)
        monkeypatch.setattr(reg, '_write_abort_tombstone_row',
                            lambda tid, src: True)
        with reg.tasks_lock:
            reg.tasks['a'] = {'id': 'a', 'status': 'running'}
        try:
            n = reg.plant_abort_tombstones_for_conv('c1', source='test')
            assert n == 1
            assert reg.has_abort_tombstone('b')
            assert not reg.has_abort_tombstone('a')
        finally:
            with reg.tasks_lock:
                reg.tasks.pop('a', None)


# ── C. endpoint wiring (source pins) ─────────────────────────────────

@pytest.mark.unit
class TestEndpointWiring:

    def test_abort_by_id_plants_on_miss(self):
        src = open(cpa.__file__, encoding='utf-8').read()
        assert "_plant(task_id, source='api_chat_abort')" in src

    def test_abort_conv_sweeps_tombstones(self):
        src = open(cpa.__file__, encoding='utf-8').read()
        assert "_plant_conv(conv_id, source='api_chat_abort_conv')" in src

    def test_poll_handler_consults_liveness_gate(self):
        src = open(cpa.__file__, encoding='utf-8').read()
        assert "_fresh_gate = _live_unregistered_gate(task_id)" in src, (
            'regression: the poll DB branch no longer consults the liveness '
            'gate — the "absent = crashed" flip is unguarded again and will '
            'corrupt live-but-unregistered tasks to interrupted.')

    def test_stream_wires_tombstone_abort_check(self):
        import lib.tasks_pkg.manager._stream as stream_mod
        src = open(stream_mod.__file__, encoding='utf-8').read()
        assert 'make_task_abort_check' in src
        assert 'abort_check=_abort_check' in src
