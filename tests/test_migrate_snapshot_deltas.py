"""Migration safety contract for tests/_migrate_snapshot_deltas.py.

Design: docs/DEBUG_PANEL_REDESIGN.md §11. The owner's hard requirement is
"迁移脚本自带校验,不许迁完就删" — so the properties that MUST hold are:

  1. VERIFY-BEFORE-WRITE — a task whose rebuild does not reproduce the
     original byte-for-byte is reported FAILED and its rows are left
     COMPLETELY untouched (all-or-nothing per task).
  2. A healthy task migrates, and its rows read back byte-identical
     through the real rebuild path.
  3. IDEMPOTENT — re-running over already-migrated rows is a no-op.
  4. dry-run writes nothing.

NEUTER: make the verification always pass (skip the byte compare) → the
corruption test stops being caught, proving the compare is load-bearing.
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

_SCRIPT = os.path.join(_ROOT, 'tests', '_migrate_snapshot_deltas.py')


def _load_script():
    import importlib.util
    spec = importlib.util.spec_from_file_location('_migrate_snapshot_deltas', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _canon(o):
    return json.dumps(o, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _full_snapshot(round_num, messages, tools):
    return {'type': 'messages_snapshot', 'kind': 'request', 'roundNum': round_num,
            'model': 'm-x', 'params': {'maxTokens': 100},
            'label': f'Round {round_num}', 'messages': messages, 'tools': tools}


def _seed(tid, payloads):
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


def _grow(n_rounds=4):
    """Realistic growing conversation + constant tools."""
    tools = [{'type': 'function', 'function': {'name': 't1', 'description': 'd' * 200}}]
    msgs = [{'role': 'system', 'content': 'S' * 300}]
    out = []
    for r in range(1, n_rounds + 1):
        out.append(_full_snapshot(r, [dict(m) for m in msgs], tools))
        msgs = msgs + [{'role': 'assistant', 'content': f'A{r}' * 50},
                       {'role': 'tool', 'content': f'T{r}' * 50}]
    return out


def test_healthy_task_migrates_and_reads_back_identical():
    mod = _load_script()
    tid = f'mig-ok-{uuid.uuid4().hex[:8]}'
    originals = _grow(5)
    db = _seed(tid, originals)
    try:
        rep = mod.migrate_task(db, tid)
        assert rep['status'] == 'ok', rep
        assert rep['ratio'] > 1
        stored = _stored(tid)
        assert all('prefixLen' in s for s in stored), 'rows not converted to delta'
        # Read back through the REAL rebuild path.
        from lib.tasks_pkg.snapshot_delta import rebuild_snapshots
        rebuilt = rebuild_snapshots(
            [{'type': 'messages_snapshot', 'payload': s} for s in stored])
        for orig, got in zip(originals, rebuilt):
            assert not got.get('degraded')
            assert _canon(got['messages']) == _canon(orig['messages'])
            assert _canon(got['tools']) == _canon(orig['tools'])
    finally:
        _cleanup(tid)


def test_verification_failure_leaves_rows_untouched():
    """★ THE SAFETY CONTRACT: if the rebuild can't reproduce the original,
    the task is reported FAILED and NOTHING is written."""
    mod = _load_script()
    tid = f'mig-bad-{uuid.uuid4().hex[:8]}'
    originals = _grow(4)
    db = _seed(tid, originals)
    before = _stored(tid)
    try:
        # Corrupt the projector so the rebuild cannot match: drop the tail.
        real_project = mod.SnapshotProjector.project

        def _lossy(self, task_id, payload):
            out = real_project(self, task_id, payload)
            if isinstance(out, dict) and out.get('newMessages'):
                out = dict(out)
                out['newMessages'] = out['newMessages'][:-1]   # lose a message
            return out

        mod.SnapshotProjector.project = _lossy
        try:
            rep = mod.migrate_task(db, tid)
        finally:
            mod.SnapshotProjector.project = real_project

        assert rep['status'] == 'FAILED', f'corruption was NOT caught: {rep}'
        assert rep.get('reason'), 'no reason recorded'
        after = _stored(tid)
        assert _canon(after) == _canon(before), (
            'FAILED task must leave its rows byte-identical — data was written '
            'on unverified output')
        assert all('prefixLen' not in s for s in after), 'partial write leaked'
    finally:
        _cleanup(tid)


def test_dry_run_writes_nothing():
    mod = _load_script()
    tid = f'mig-dry-{uuid.uuid4().hex[:8]}'
    db = _seed(tid, _grow(3))
    before = _stored(tid)
    try:
        rep = mod.migrate_task(db, tid, dry_run=True)
        assert rep['status'] == 'dry-run'
        assert _canon(_stored(tid)) == _canon(before), 'dry-run mutated rows'
    finally:
        _cleanup(tid)


def test_idempotent_second_run_is_noop():
    mod = _load_script()
    tid = f'mig-idem-{uuid.uuid4().hex[:8]}'
    db = _seed(tid, _grow(4))
    try:
        assert mod.migrate_task(db, tid)['status'] == 'ok'
        first = _stored(tid)
        rep2 = mod.migrate_task(db, tid)
        assert rep2['status'] == 'already-delta', rep2
        assert _canon(_stored(tid)) == _canon(first), 're-run mutated rows'
    finally:
        _cleanup(tid)


def test_neuter_verification_lets_corruption_through():
    """NC: make the rebuild ECHO the originals so the byte-compare always
    passes → the same corruption that was caught above now lands in the DB,
    proving the verification step is the only thing protecting the data."""
    mod = _load_script()
    tid = f'mig-nc-{uuid.uuid4().hex[:8]}'
    originals = _grow(4)
    db = _seed(tid, originals)
    try:
        real_project = mod.SnapshotProjector.project
        real_rebuild = mod.rebuild_snapshots

        def _lossy(self, task_id, payload):
            out = real_project(self, task_id, payload)
            if isinstance(out, dict) and out.get('newMessages'):
                out = dict(out)
                out['newMessages'] = out['newMessages'][:-1]
            return out

        # NEUTER: verification echoes the ORIGINALS → always matches.
        mod.SnapshotProjector.project = _lossy
        mod.rebuild_snapshots = lambda rows: [
            {'messages': o['messages'], 'tools': o['tools']} for o in originals]
        try:
            rep = mod.migrate_task(db, tid)
        finally:
            mod.SnapshotProjector.project = real_project
            mod.rebuild_snapshots = real_rebuild

        assert rep['status'] == 'ok', (
            f'NC did not take effect — expected the lossy write to pass the '
            f'neutered verification, got {rep}')
        # And the damage is real: the stored rows no longer rebuild correctly.
        from lib.tasks_pkg.snapshot_delta import rebuild_snapshots
        rebuilt = rebuild_snapshots(
            [{'type': 'messages_snapshot', 'payload': s} for s in _stored(tid)])
        lost = [i for i, (o, g) in enumerate(zip(originals, rebuilt))
                if _canon(o['messages']) != _canon(g.get('messages'))]
        assert lost, (
            'NC wrote data that still round-trips — the corruption injection '
            'is not actually lossy, so this control proves nothing')
    finally:
        _cleanup(tid)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
