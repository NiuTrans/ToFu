"""Self-healing delta compaction of leftover FULL snapshot rows.

Why this exists
===============
The write-path projection (``append_persistent_event``) only shrinks rows
that the CURRENT process writes. In a deployment where an older process is
still serving, full rows keep arriving; and the one-shot migration is a
point-in-time sweep, so the gap re-opens the moment it finishes — measured
+519 MB of new full rows accumulating between two checks while the old
process kept running.

``_opportunistic_compact`` runs on the same dedicated maintenance daemon as
TTL pruning, so ANY process running this build heals the backlog continuously
without stalling an SSE producer.

Properties pinned here:
  1. A full row gets compacted to delta form, and reads still return the
     SAME payload (compaction is invisible to every consumer).
  2. Already-delta rows are left alone (idempotent, no churn).
  3. It is BOUNDED — one pass touches at most _COMPACT_MAX_TASKS tasks.
  4. ★ SAFETY: verification failure must leave rows byte-identical. The
     compactor reuses the migration's verify-then-write contract, so a
     projection that cannot round-trip is REFUSED, not written.

NEUTER: make the compactor skip its verification (rebuild echoes the
originals) → the corruption-refusal test fails, proving the byte-compare is
what protects live data.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


@pytest.fixture(autouse=True, scope='module')
def _schema():
    try:
        from lib.database import init_db
        init_db()
    except Exception as e:                                  # pragma: no cover
        pytest.skip(f'cannot initialise the test schema: {e}')
    yield


def _canon(o):
    return json.dumps(o, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _full_snapshot(round_num, messages, tools):
    return {'type': 'messages_snapshot', 'kind': 'request', 'roundNum': round_num,
            'model': 'm-x', 'params': {'maxTokens': 100},
            'label': f'Round {round_num}', 'messages': messages, 'tools': tools}


def _grow(n_rounds=5):
    tools = [{'type': 'function',
              'function': {'name': 't1', 'description': 'd' * 400}}]
    msgs = [{'role': 'system', 'content': 'S' * 800}]
    out = []
    for r in range(1, n_rounds + 1):
        out.append(_full_snapshot(r, [dict(m) for m in msgs], tools))
        msgs = msgs + [{'role': 'assistant', 'content': f'A{r}' * 120},
                       {'role': 'tool', 'content': f'T{r}' * 120}]
    return out


def _seed_full(tid, payloads):
    """Insert FULL (un-projected) snapshot rows, bypassing the write-path
    projection so we reproduce exactly what an old process leaves behind."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import TASK_EVENTS, upsert
    db = get_thread_db(DOMAIN_CHAT)
    for i, p in enumerate(payloads):
        upsert(db, TASK_EVENTS,
               {'task_id': tid, 'event_id': i, 'ts_ms': 1700000000000 + i,
                'type': 'messages_snapshot', 'payload': json_dumps_pg(p)},
               conflict_cols=['task_id', 'event_id'],
               insert_cols=['task_id', 'event_id', 'ts_ms', 'type', 'payload'],
               update_cols=[], commit=True, retry=False)
    return db


def _stored(tid):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    rows = db.execute(
        'SELECT event_id, payload FROM task_events WHERE task_id=? ORDER BY event_id',
        (tid,)).fetchall()
    out = []
    for r in rows:
        p = r[1]
        out.append(p if isinstance(p, dict) else json.loads(p))
    return out


def _cleanup(*tids):
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        for t in tids:
            db.execute('DELETE FROM task_events WHERE task_id=?', (t,))
        db.commit()
    except Exception:
        pass


def test_leftover_full_rows_are_compacted_and_read_back_identical():
    """★ The convergence property: a full row left by an OLD process gets
    healed, and every consumer still sees the same payload."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.event_log import _opportunistic_compact
    from lib.tasks_pkg.request_inspector import (
        get_request_payload, invalidate_task_cache,
    )
    tid = f'cmp-ok-{uuid.uuid4().hex[:8]}'
    originals = _grow(6)
    db = _seed_full(tid, originals)
    try:
        invalidate_task_cache(tid)
        before = [get_request_payload(tid, i) for i in range(1, 7)]
        assert all(p and p['messages'] for p in before)
        assert all('prefixLen' not in s for s in _stored(tid)), 'precondition'

        _opportunistic_compact(db)

        stored = _stored(tid)
        assert any('prefixLen' in s for s in stored), (
            'leftover full rows were NOT compacted — the table would keep '
            'growing on a deployment that has not restarted')
        invalidate_task_cache(tid)
        after = [get_request_payload(tid, i) for i in range(1, 7)]
        for b, a in zip(before, after):
            assert _canon(b['messages']) == _canon(a['messages']), (
                'compaction changed a payload')
            assert _canon(b['tools']) == _canon(a['tools'])
            assert not a.get('degraded')
    finally:
        _cleanup(tid)


def test_already_delta_rows_are_left_alone():
    """Idempotent: a second pass must not churn rows that are already delta."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.event_log import _opportunistic_compact
    tid = f'cmp-idem-{uuid.uuid4().hex[:8]}'
    db = _seed_full(tid, _grow(4))
    try:
        _opportunistic_compact(db)
        first = _stored(tid)
        _opportunistic_compact(db)
        assert _canon(_stored(tid)) == _canon(first), (
            'a second compaction pass rewrote already-delta rows')
    finally:
        _cleanup(tid)


def test_pass_is_bounded():
    """Runs on an SSE delta's thread — one pass must touch at most
    _COMPACT_MAX_TASKS tasks, however big the backlog is."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import event_log as el
    tids = [f'cmp-b{i}-{uuid.uuid4().hex[:6]}' for i in range(el._COMPACT_MAX_TASKS + 3)]
    try:
        for t in tids:
            _seed_full(t, _grow(3))
        db = get_thread_db(DOMAIN_CHAT)
        el._opportunistic_compact(db)
        healed = sum(1 for t in tids
                     if any('prefixLen' in s for s in _stored(t)))
        assert healed <= el._COMPACT_MAX_TASKS, (
            f'one pass compacted {healed} tasks, cap is {el._COMPACT_MAX_TASKS} '
            f'— an unbounded pass would stall an SSE delta')
        assert healed >= 1, 'the pass did nothing at all'
    finally:
        _cleanup(*tids)


def test_verification_failure_leaves_rows_untouched(monkeypatch):
    """★ SAFETY: this rewrites LIVE data, so a projection that cannot
    round-trip must be refused, leaving the rows byte-identical."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.event_log import _opportunistic_compact
    import lib.tasks_pkg.snapshot_delta as sd
    tid = f'cmp-bad-{uuid.uuid4().hex[:8]}'
    db = _seed_full(tid, _grow(5))
    before = _stored(tid)
    try:
        real = sd.SnapshotProjector.project

        def _lossy(self, task_id, payload):
            out = real(self, task_id, payload)
            if isinstance(out, dict) and out.get('newMessages'):
                out = dict(out)
                out['newMessages'] = out['newMessages'][:-1]   # drop a message
            return out

        monkeypatch.setattr(sd.SnapshotProjector, 'project', _lossy)
        _opportunistic_compact(db)
        after = _stored(tid)
        assert _canon(after) == _canon(before), (
            'a lossy projection was WRITTEN to live rows — the verify-then-'
            'write contract is not being honoured by the compactor')
        assert all('prefixLen' not in s for s in after), 'partial write leaked'
    finally:
        _cleanup(tid)


def test_compaction_is_wired_into_maintenance_not_append():
    """Static pin: compaction converges without blocking an SSE append."""
    src = open(os.path.join(_ROOT, 'lib', 'tasks_pkg', 'event_log.py'),
               encoding='utf-8').read()
    loop = src[src.index('def _maintenance_loop'):]
    loop = loop[:loop.index('\ndef ')]
    assert '_opportunistic_compact' in loop
    hook = src[src.index('def append_persistent_event'):]
    hook = hook[:hook.index('\ndef ')]
    assert '_opportunistic_compact' not in hook


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
