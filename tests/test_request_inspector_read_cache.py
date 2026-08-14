"""Request Inspector read-path caching (P6 follow-up).

Symptom that motivated this: opening round after round in the drawer took
~1.6 s per click on a real 126-round task (206 s to walk them all), because
every ``get_request_payload`` call re-read AND re-rebuilt the task's ENTIRE
event log — O(rounds^2) work for a linear UI action. With delta storage the
rebuild is not free, so this got worse, not better.

Properties pinned here:
  1. A fold + full round walk issues ONE database read, not one per round.
  2. Correctness is unchanged: cached reads return the same payloads.
  3. TTL is SHORT and real — a live task appending rounds must become
     visible, so the cache must expire rather than pin a stale list.
  4. The cache is bounded (a browsing session must not grow it forever).
  5. Cache is keyed per task — one task's rows never leak into another's.

NEUTER: give the cache an infinite TTL → the "live task sees new rounds"
test fails, proving the expiry is load-bearing (not just a nicety).
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from contextlib import contextmanager

import pytest

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


@pytest.fixture(autouse=True, scope='module')
def _schema():
    """Direct-DB suite: make sure this worker's isolated SQLite file has the
    schema. conftest does this best-effort at session start, but a suite that
    only ever touches the DB directly (never builds the app) can land on a
    worker where it did not run."""
    try:
        from lib.database import init_db
        init_db()
    except Exception as e:                                  # pragma: no cover
        pytest.skip(f'cannot initialise the test schema: {e}')
    yield


def _seed(tid, payloads, start_eid=0):
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import TASK_EVENTS, upsert
    db = get_thread_db(DOMAIN_CHAT)
    for i, p in enumerate(payloads):
        upsert(db, TASK_EVENTS,
               {'task_id': tid, 'event_id': start_eid + i,
                'ts_ms': 1700000000000 + start_eid + i,
                'type': 'messages_snapshot', 'payload': json_dumps_pg(p)},
               conflict_cols=['task_id', 'event_id'],
               insert_cols=['task_id', 'event_id', 'ts_ms', 'type', 'payload'],
               update_cols=[], commit=True, retry=False)
    return db


def _cleanup(*tids):
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        for t in tids:
            db.execute('DELETE FROM task_events WHERE task_id=?', (t,))
        db.commit()
    except Exception:
        pass


def _snap(round_num, n_msgs):
    return {'type': 'messages_snapshot', 'kind': 'request',
            'roundNum': round_num, 'model': 'm-x', 'params': {'maxTokens': 10},
            'label': f'Round {round_num}',
            'messages': [{'role': 'user', 'content': f'r{round_num}-{i}'}
                         for i in range(n_msgs)],
            'tools': [{'type': 'function', 'function': {'name': 't1'}}]}


class _CountingDB:
    """Wraps the real thread DB, counting task_events SELECTs."""

    def __init__(self, real):
        self._real = real
        self.reads = 0

    def execute(self, sql, params=()):
        if 'FROM task_events' in sql and sql.strip().upper().startswith('SELECT'):
            self.reads += 1
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _clear_cache():
    import lib.tasks_pkg.request_inspector as ri
    ri._EVENTS_CACHE.clear()


def test_round_walk_issues_one_read_not_one_per_round(monkeypatch):
    """★ The fix: walking every round is ONE read, not N."""
    import lib.tasks_pkg.request_inspector as ri
    from lib.database import DOMAIN_CHAT, get_thread_db
    tid = f'ri-cache-{uuid.uuid4().hex[:8]}'
    _seed(tid, [_snap(i, i + 1) for i in range(1, 13)])
    _clear_cache()
    counting = _CountingDB(get_thread_db(DOMAIN_CHAT))

    @contextmanager
    def _counting_lease(*_a, **_k):
        yield counting

    monkeypatch.setattr(ri, 'pooled_db', _counting_lease)
    try:
        fold = ri.fold_request_log(tid)
        assert fold['requestCount'] == 12
        reads_after_fold = counting.reads
        for r in fold['requests']:
            p = ri.get_request_payload(tid, r['roundNum'])
            assert p is not None and p['messages'], f'round {r["roundNum"]} empty'
        assert counting.reads == reads_after_fold, (
            f'walking 12 rounds issued {counting.reads - reads_after_fold} extra '
            f'reads — the per-round re-read is back (this is the O(n^2) bug)')
    finally:
        _clear_cache()
        _cleanup(tid)


def test_cached_reads_return_identical_payloads():
    """Caching must not change WHAT is returned."""
    import lib.tasks_pkg.request_inspector as ri
    tid = f'ri-same-{uuid.uuid4().hex[:8]}'
    _seed(tid, [_snap(i, i + 2) for i in range(1, 6)])
    try:
        _clear_cache()
        first = [ri.get_request_payload(tid, i) for i in range(1, 6)]
        second = [ri.get_request_payload(tid, i) for i in range(1, 6)]   # cached
        _clear_cache()
        third = [ri.get_request_payload(tid, i) for i in range(1, 6)]    # cold again
        cn = lambda o: json.dumps(o, sort_keys=True, ensure_ascii=False)
        assert cn(first) == cn(second) == cn(third), 'cache changed the payload'
        assert all(p and p['messages'] for p in first)
    finally:
        _clear_cache()
        _cleanup(tid)


def test_live_task_new_rounds_become_visible_after_ttl(monkeypatch):
    """★ CORRECTNESS BOUND: a live task keeps appending rounds. The cache
    must EXPIRE so the drawer sees them — a stale pin would hide the newest
    request, which is exactly what the user is usually looking for."""
    import lib.tasks_pkg.request_inspector as ri
    tid = f'ri-live-{uuid.uuid4().hex[:8]}'
    _seed(tid, [_snap(1, 2), _snap(2, 3)])
    try:
        _clear_cache()
        assert ri.fold_request_log(tid)['requestCount'] == 2
        # A new round lands while the entry is cached.
        _seed(tid, [_snap(3, 4)], start_eid=2)
        assert ri.fold_request_log(tid)['requestCount'] == 2, (
            'precondition: the cache should still be serving the old list')
        # ...and after the TTL elapses it must be visible.
        real_time = ri.__dict__.get('time')
        import time as _t
        base = _t.time()

        class _Clock:
            @staticmethod
            def time():
                return base + ri._EVENTS_CACHE_TTL_S + 1

        monkeypatch.setitem(sys.modules, 'time', _Clock)
        try:
            got = ri.fold_request_log(tid)['requestCount']
        finally:
            monkeypatch.setitem(sys.modules, 'time', _t)
        assert got == 3, (
            f'after the TTL the new round must be visible, got {got} — a live '
            f'task would appear frozen in the drawer')
    finally:
        _clear_cache()
        _cleanup(tid)


def test_cache_is_bounded():
    import lib.tasks_pkg.request_inspector as ri
    tids = [f'ri-b{i}-{uuid.uuid4().hex[:6]}' for i in range(ri._EVENTS_CACHE_MAX + 4)]
    try:
        _clear_cache()
        for t in tids:
            _seed(t, [_snap(1, 1)])
            ri.fold_request_log(t)
        assert len(ri._EVENTS_CACHE) <= ri._EVENTS_CACHE_MAX, (
            f'cache grew to {len(ri._EVENTS_CACHE)} entries, cap is '
            f'{ri._EVENTS_CACHE_MAX}')
    finally:
        _clear_cache()
        _cleanup(*tids)


def test_cache_is_keyed_per_task():
    import lib.tasks_pkg.request_inspector as ri
    a = f'ri-ka-{uuid.uuid4().hex[:8]}'
    b = f'ri-kb-{uuid.uuid4().hex[:8]}'
    _seed(a, [_snap(1, 2)])
    _seed(b, [_snap(1, 9)])
    try:
        _clear_cache()
        pa = ri.get_request_payload(a, 1)
        pb = ri.get_request_payload(b, 1)
        assert len(pa['messages']) == 2 and len(pb['messages']) == 9, (
            'one task\'s cached rows leaked into another')
    finally:
        _clear_cache()
        _cleanup(a, b)


def test_neuter_infinite_ttl_hides_live_rounds(monkeypatch):
    """NC: make the TTL effectively infinite → the live-task test's property
    breaks (new rounds never appear), proving the expiry is load-bearing."""
    import lib.tasks_pkg.request_inspector as ri
    tid = f'ri-nc-{uuid.uuid4().hex[:8]}'
    _seed(tid, [_snap(1, 2)])
    try:
        _clear_cache()
        monkeypatch.setattr(ri, '_EVENTS_CACHE_TTL_S', 10 ** 9)
        assert ri.fold_request_log(tid)['requestCount'] == 1
        _seed(tid, [_snap(2, 3)], start_eid=1)
        import time as _t
        base = _t.time()

        class _Clock:
            @staticmethod
            def time():
                return base + 3600      # an hour later

        monkeypatch.setitem(sys.modules, 'time', _Clock)
        try:
            got = ri.fold_request_log(tid)['requestCount']
        finally:
            monkeypatch.setitem(sys.modules, 'time', _t)
        assert got == 1, (
            'NC did not take effect — with an infinite TTL the new round '
            'should have stayed hidden')
    finally:
        _clear_cache()
        _cleanup(tid)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
