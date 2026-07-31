"""Tiered event retention + SSE non-interference (Request Inspector P5).

Two invariants that the delta-storage change must not break, plus the
retention tiering that makes the inspector's history actually usable.

  1. SSE NON-INTERFERENCE (the load-bearing one): delta projection happens
     at the PERSISTENCE boundary only. The event object handed to
     append_persistent_event must come back out of the caller unchanged —
     the frontend keeps receiving full snapshots, byte-identical.
  2. TIERED TTL (docs/DEBUG_PANEL_REDESIGN.md §10.4): streaming noise
     (delta/phase/...) reaps at 6h; structural events the inspector renders
     (messages_snapshot / round_usage / round_start / round_end) live 30
     days. The old single-tier sweep deleted EVERY row of an eligible task,
     which is why a 2-hour-old task rendered "event log expired".

NEUTER: drop `messages_snapshot` from STRUCTURAL_EVENT_TYPES → the
structural-survival test flips red (proving the tier list is load-bearing).
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TARGET = os.path.join(_ROOT, 'lib', 'tasks_pkg', 'event_log.py')


def _snapshot_event(round_num=1, n_msgs=3):
    return {
        'type': 'messages_snapshot', 'kind': 'request', 'roundNum': round_num,
        'model': 'm-x', 'params': {'maxTokens': 1000},
        'label': f'Round {round_num} 请求前',
        'messages': [{'role': 'user', 'content': 'x' * 200}] * n_msgs,
        'tools': [{'type': 'function', 'function': {'name': 't1',
                                                    'description': 'd' * 300}}],
    }


def _cleanup(*task_ids):
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        for tid in task_ids:
            db.execute('DELETE FROM task_events WHERE task_id=?', (tid,))
            db.execute('DELETE FROM task_results WHERE task_id=?', (tid,))
        db.commit()
    except Exception:
        pass


def test_persist_does_not_mutate_the_sse_event():
    """★ THE INVARIANT: the caller's event object (which is what gets pushed
    to SSE subscribers) must be untouched by persistence."""
    from lib.tasks_pkg.event_log import append_persistent_event
    tid = f'p5-sse-{uuid.uuid4().hex[:8]}'
    ev = _snapshot_event(1)
    before = json.dumps(ev, sort_keys=True, ensure_ascii=False)
    try:
        append_persistent_event(tid, 0, ev)
        after = json.dumps(ev, sort_keys=True, ensure_ascii=False)
        assert after == before, (
            'persistence MUTATED the event object — SSE subscribers would '
            'receive a delta instead of the full snapshot')
        assert isinstance(ev.get('messages'), list) and ev['messages'], \
            'messages stripped from the live event'
        assert isinstance(ev.get('tools'), list) and ev['tools'], \
            'tools stripped from the live event'
    finally:
        _cleanup(tid)


def test_stored_row_is_delta_but_reads_back_full():
    """Storage is incremental; the read path returns the FULL payload, so no
    consumer ever learns about the delta form."""
    from lib.tasks_pkg.event_log import append_persistent_event
    from lib.tasks_pkg.request_inspector import get_request_payload
    from lib.database import DOMAIN_CHAT, get_thread_db
    tid = f'p5-rt-{uuid.uuid4().hex[:8]}'
    try:
        r1 = _snapshot_event(1, n_msgs=2)
        r2 = _snapshot_event(2, n_msgs=4)
        r2['messages'] = r1['messages'] + [
            {'role': 'assistant', 'content': 'A' * 100},
            {'role': 'tool', 'content': 'T' * 100}]
        append_persistent_event(tid, 0, r1)
        append_persistent_event(tid, 1, r2)

        db = get_thread_db(DOMAIN_CHAT)
        raw = db.execute(
            'SELECT payload FROM task_events WHERE task_id=? ORDER BY event_id',
            (tid,)).fetchall()
        stored = []
        for row in raw:
            p = row[0]
            stored.append(p if isinstance(p, dict) else json.loads(p))
        assert 'prefixLen' in stored[1], 'round 2 was not stored as a delta'
        assert len(stored[1].get('newMessages') or []) == 2, (
            'round 2 stored more than its 2 new messages')
        assert 'tools' not in stored[1], 'tools array re-stored on round 2'

        got = get_request_payload(tid, 2)
        assert got is not None
        assert len(got['messages']) == 4, 'read path did not rebuild in full'
        assert got['messages'] == r2['messages']
        assert got['tools'] == r2['tools'], 'tools not resolved by hash'
        assert not got.get('degraded')
    finally:
        _cleanup(tid)


def _seed_aged_task(tid, age_ms, types):
    """Insert rows of the given types with ts_ms aged by age_ms, plus a
    terminal task_results row so the prune's JOIN sees the task."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database._core_schema import TASK_EVENTS, TASK_RESULTS, upsert
    db = get_thread_db(DOMAIN_CHAT)
    now = int(time.time() * 1000)
    ts = now - age_ms
    upsert(db, TASK_RESULTS,
           {'task_id': tid, 'conv_id': 'p5conv', 'content': '',
            'status': 'done', 'created_at': ts, 'completed_at': ts},
           conflict_cols=['task_id'],
           insert_cols=['task_id', 'conv_id', 'content', 'status',
                        'created_at', 'completed_at'],
           update_cols=[], commit=True, retry=False)
    for i, t in enumerate(types):
        upsert(db, TASK_EVENTS,
               {'task_id': tid, 'event_id': i, 'ts_ms': ts, 'type': t,
                'payload': json.dumps({'type': t})},
               conflict_cols=['task_id', 'event_id'],
               insert_cols=['task_id', 'event_id', 'ts_ms', 'type', 'payload'],
               update_cols=[], commit=True, retry=False)


def _types_left(tid):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    return sorted(r[0] for r in db.execute(
        'SELECT type FROM task_events WHERE task_id=?', (tid,)).fetchall())


def test_structural_events_survive_the_6h_sweep():
    """★ The bug the owner hit: a 2-hour-old task read 'expired'. Structural
    events must survive the streaming-noise horizon."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.event_log import _opportunistic_prune
    tid = f'p5-ttl-{uuid.uuid4().hex[:8]}'
    try:
        _seed_aged_task(tid, 12 * 3600 * 1000,          # 12h old: past 6h, under 30d
                        ['delta', 'phase', 'messages_snapshot', 'round_usage'])
        _opportunistic_prune(get_thread_db(DOMAIN_CHAT))
        left = _types_left(tid)
        assert 'messages_snapshot' in left, (
            'messages_snapshot was reaped at the 6h horizon — the inspector '
            'would show "expired" for a same-day task')
        assert 'round_usage' in left, 'round_usage reaped at the 6h horizon'
        assert 'delta' not in left, 'streaming noise was NOT reaped'
        assert 'phase' not in left, 'streaming noise was NOT reaped'
    finally:
        _cleanup(tid)


def test_structural_events_reaped_past_30_days():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.event_log import _opportunistic_prune
    tid = f'p5-old-{uuid.uuid4().hex[:8]}'
    try:
        _seed_aged_task(tid, 40 * 24 * 3600 * 1000,     # 40 days old
                        ['delta', 'messages_snapshot', 'round_usage'])
        _opportunistic_prune(get_thread_db(DOMAIN_CHAT))
        assert _types_left(tid) == [], (
            'structural events must be reaped past the 30-day tier')
    finally:
        _cleanup(tid)


def test_neuter_dropping_snapshot_from_tier_list_reaps_it():
    """NC: remove messages_snapshot from the structural tier → it is reaped at
    6h again (the regression the owner reported), proving the tier list is
    load-bearing."""
    from tests._nc_harness import neutered_source
    fixed = ("STRUCTURAL_EVENT_TYPES = (\n"
             "    'messages_snapshot', 'round_usage', 'round_start', 'round_end',\n"
             ")")
    broken = ("STRUCTURAL_EVENT_TYPES = (\n"
              "    'round_usage', 'round_start', 'round_end',  # NC\n"
              ")")
    with open(_TARGET, encoding='utf-8') as f:
        assert fixed in f.read(), 'NC anchor drifted'
    tid = f'p5-nc-{uuid.uuid4().hex[:8]}'
    try:
        _seed_aged_task(tid, 12 * 3600 * 1000,
                        ['delta', 'messages_snapshot', 'round_usage'])
        from lib.database import DOMAIN_CHAT, get_thread_db
        with neutered_source(_TARGET, fixed, broken) as mod:
            mod._opportunistic_prune(get_thread_db(DOMAIN_CHAT))
        left = _types_left(tid)
        assert 'messages_snapshot' not in left, (
            'NC did not take effect — snapshot survived without being in the '
            'tier list')
        assert 'round_usage' in left, 'unintended blast radius on round_usage'
    finally:
        _cleanup(tid)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
