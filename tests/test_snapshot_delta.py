"""Snapshot delta storage (Request Inspector P5) — pytest suite.

Design: docs/FRONTEND_ARCHITECTURE.md §10 (format FROZEN). The owner's
acceptance criteria drive these tests:

  1. ROUNDTRIP FIDELITY — rebuild(project(x)) == x byte-for-byte
     (canonical JSON), for a realistic multi-round task. This is the
     criterion the migration must satisfy before deleting any old row.
  2. COMPRESSION — the measured redundancies actually go away: constant
     `tools` stored ONCE, repeated rounds stored as empty records, and the
     total delta bytes are an order of magnitude below full storage.
  3. HONEST DEGRADATION — a missing baseline / mismatched prefix hash is
     reported as degraded=True with a reason, never silently truncated.
  4. IDEMPOTENCE — projecting an already-projected row is a no-op, and a
     partially-migrated row list still rebuilds correctly.
  5. TURN ISOLATION — Flow node turns re-number rounds from 1, so their
     baseline chains must not cross-contaminate.

NEUTER: disable the tools dedup, and the "tools stored once" assertion
flips red while roundtrip still passes — proving the dedup is load-bearing
for the compression criterion specifically.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.tasks_pkg.snapshot_delta import (  # noqa: E402
    SnapshotProjector,
    prefix_hash,
    rebuild_snapshots,
    shared_prefix_len,
)


def _canon(o):
    return json.dumps(o, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _tools(n=6):
    """A realistic (large, CONSTANT across rounds) tool array."""
    return [{'type': 'function',
             'function': {'name': f'tool_{i}',
                          'description': 'x' * 400,
                          'parameters': {'type': 'object',
                                         'properties': {'a': {'type': 'string'}}}}}
            for i in range(n)]


def _task_rounds(n_rounds=8, turn=''):
    """Build n rounds of FULL snapshot payloads the way the orchestrator emits
    them: messages grow by 2 per round, tools byte-identical every round."""
    tools = _tools()
    messages = [{'role': 'system', 'content': 'S' * 2000},
                {'role': 'user', 'content': 'U' * 500}]
    out = []
    for r in range(1, n_rounds + 1):
        payload = {
            'type': 'messages_snapshot', 'kind': 'request',
            'roundNum': r, 'model': 'm-x',
            'params': {'maxTokens': 1000, 'temperature': 1},
            'label': f'Round {r} 请求前 · {len(messages)}条',
            'messages': [dict(m) for m in messages],
            'tools': tools,
        }
        if turn:
            payload['turn'] = turn
        out.append(payload)
        messages = messages + [
            {'role': 'assistant', 'content': f'A{r}' * 300,
             'tool_calls': [{'id': f'call_{r}', 'type': 'function',
                             'function': {'name': 'tool_1', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': f'call_{r}', 'content': f'T{r}' * 800},
        ]
    return out


def _project_all(payloads, task_id='t1'):
    """Project a list of full payloads → the stored row list."""
    proj = SnapshotProjector()
    rows = []
    for p in payloads:
        delta = proj.project(task_id, p)
        rows.append({'type': delta['type'], 'payload': delta})
    return rows


def test_roundtrip_is_byte_identical():
    """★ THE MIGRATION GATE: rebuild(project(x)) == x, canonical-JSON exact."""
    originals = _task_rounds(8)
    rows = _project_all(originals)
    rebuilt = rebuild_snapshots(rows)
    assert len(rebuilt) == len(originals)
    for orig, got in zip(originals, rebuilt):
        assert not got.get('degraded'), f'unexpected degradation: {got.get("degradedReason")}'
        assert _canon(got.get('messages')) == _canon(orig['messages']), (
            f'round {orig["roundNum"]} messages diverged')
        assert _canon(got.get('tools')) == _canon(orig['tools']), (
            f'round {orig["roundNum"]} tools diverged')
        for k in ('kind', 'roundNum', 'model', 'params', 'label'):
            assert got.get(k) == orig.get(k), f'metadata {k} lost'


def test_tools_stored_once_and_messages_are_deltas():
    """Compression criterion: constant tools stored ONCE (inline on the first
    carrier, by-hash reference thereafter); per-round rows carry only the new
    tail, not the whole array."""
    originals = _task_rounds(8)
    rows = _project_all(originals)
    snaps = [r['payload'] for r in rows]
    carriers = [s for s in snaps if isinstance(s.get('tools'), list)]
    assert len(carriers) == 1, (
        f'constant tools must be stored exactly once, got {len(carriers)}')
    assert all(s.get('toolsHash') for s in snaps), 'every row must reference the hash'
    assert len({s['toolsHash'] for s in snaps}) == 1, 'hash must be stable'
    # Round 1 has no baseline → full tail; later rounds carry exactly 2 new msgs.
    assert snaps[0]['prefixLen'] == 0
    for s in snaps[1:]:
        assert len(s.get('newMessages') or []) == 2, (
            f'round {s["roundNum"]} stored {len(s.get("newMessages") or [])} '
            f'messages, expected the 2 new ones')


def test_total_bytes_drop_by_an_order_of_magnitude():
    """Owner's numeric gate: >=20x. Full storage is O(n^2) in rounds (every
    round re-stores the whole array) while delta is O(n), so the ratio grows
    with round count — the real task measured 65.7x at 167 rounds. We assert
    at a round count representative of a real long task, AND that the ratio
    genuinely grows with length (which is what makes the real-data gate hold)."""
    def _ratio(n):
        originals = _task_rounds(n)
        full_bytes = sum(len(_canon(p)) for p in originals)
        rows = _project_all(originals)
        delta_bytes = sum(len(_canon(r['payload'])) for r in rows)
        return full_bytes / max(delta_bytes, 1)

    r60 = _ratio(60)
    assert r60 >= 20, (
        f'compression only {r60:.1f}x at 60 rounds — owner gate is >=20x')
    assert r60 > _ratio(20), 'ratio must grow with round count (O(n^2) → O(n))'


def test_repeat_round_stores_empty_record():
    """§10.2 item 3 — a duplicate emission of the same round must NOT re-store
    the payload."""
    originals = _task_rounds(3)
    dup = dict(originals[1])          # same round, re-emitted verbatim
    seq = [originals[0], originals[1], dup, originals[2]]
    rows = _project_all(seq)
    snaps = [r['payload'] for r in rows]
    assert 'newMessages' not in snaps[2], 'duplicate round re-stored a payload'
    assert snaps[2]['prefixLen'] == snaps[2]['messageCount']
    rebuilt = rebuild_snapshots(rows)
    assert _canon(rebuilt[2]['messages']) == _canon(dup['messages']), (
        'the empty duplicate record must still rebuild to the same payload')


def test_missing_baseline_degrades_honestly():
    """§10.3 — dropping a baseline row must yield degraded=True + reason,
    NEVER a silently truncated payload."""
    originals = _task_rounds(4)
    rows = _project_all(originals)
    del rows[1]                              # prune the round-2 baseline
    rebuilt = rebuild_snapshots(rows)
    assert rebuilt, 'rebuild returned nothing'
    degraded = [r for r in rebuilt if r.get('degraded')]
    assert degraded, 'missing baseline was NOT flagged'
    assert all(d.get('degradedReason') for d in degraded), 'degradation carries no reason'


def test_corrupted_prefix_hash_degrades():
    originals = _task_rounds(3)
    rows = _project_all(originals)
    rows[1]['payload']['prefixHash'] = 'deadbeefdeadbeef'
    rebuilt = rebuild_snapshots(rows)
    assert rebuilt[1].get('degraded') is True
    assert 'hash' in (rebuilt[1].get('degradedReason') or '').lower()


def test_missing_tools_carrier_degrades():
    """If the row that carried the tools array inline is gone, later rows that
    only reference its hash must degrade honestly."""
    originals = _task_rounds(3)
    rows = _project_all(originals)
    rows[0]['payload'].pop('tools', None)   # carrier lost its array
    rebuilt = rebuild_snapshots(rows)
    assert any(r.get('degraded') for r in rebuilt)
    assert any('tools' in (r.get('degradedReason') or '').lower() for r in rebuilt)


def test_projection_is_idempotent():
    """Already-projected rows pass through untouched — the migration can be
    re-run / batched safely."""
    originals = _task_rounds(3)
    proj = SnapshotProjector()
    delta = proj.project('t1', originals[0])
    again = SnapshotProjector().project('t1', delta)
    assert again is delta, 'a delta row must pass through unchanged'


def test_legacy_full_rows_still_rebuild():
    """A partially-migrated table (legacy full rows mixed with deltas) must
    rebuild correctly — legacy rows also re-establish the baseline."""
    originals = _task_rounds(4)
    rows = _project_all(originals)
    rows[0] = {'type': 'messages_snapshot', 'payload': originals[0]}
    rebuilt = rebuild_snapshots(rows)
    for orig, got in zip(originals, rebuilt):
        assert not got.get('degraded')
        assert _canon(got['messages']) == _canon(orig['messages'])


def test_turn_chains_do_not_cross_contaminate():
    """Flow node turns re-number rounds from 1; their baseline chains are
    independent, so a planner R1 must never be diffed against a worker R1."""
    planner = _task_rounds(3, turn='planning')
    worker = _task_rounds(3, turn='working')
    for w in worker:                     # make worker content distinct
        for m in w['messages']:
            if m['role'] == 'system':
                m['content'] = 'W' * 2000
    interleaved = [planner[0], worker[0], planner[1], worker[1],
                   planner[2], worker[2]]
    rows = _project_all(interleaved)
    rebuilt = rebuild_snapshots(rows)
    for orig, got in zip(interleaved, rebuilt):
        assert not got.get('degraded'), got.get('degradedReason')
        assert _canon(got['messages']) == _canon(orig['messages']), (
            f'turn={orig.get("turn")} round={orig["roundNum"]} cross-contaminated')


def test_shared_prefix_helpers():
    a = [{'x': 1}, {'y': 2}, {'z': 3}]
    assert shared_prefix_len(a, a) == 3
    assert shared_prefix_len(a, a[:2] + [{'z': 99}]) == 2
    assert shared_prefix_len([], a) == 0
    assert prefix_hash(a, 0) == prefix_hash([], 0)


def test_neuter_tools_dedup_breaks_compression_not_roundtrip():
    """NC: disable the tools dedup (emit the array on every round) → the
    'stored once' criterion FAILS while roundtrip still passes, proving the
    dedup is load-bearing for compression specifically."""
    from tests._nc_harness import neutered_source
    target = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'lib', 'tasks_pkg', 'snapshot_delta.py')
    fixed = "            if th not in seen:\n                seen.add(th)"
    broken = "            if True:  # NC-TOOLS-DEDUP\n                seen.add(th)"
    with open(target, encoding='utf-8') as f:
        assert fixed in f.read(), 'NC anchor drifted'
    originals = _task_rounds(6)
    with neutered_source(target, fixed, broken) as mod:
        proj = mod.SnapshotProjector()
        rows = []
        for p in originals:
            delta = proj.project('t1', p)
            rows.append({'type': delta['type'], 'payload': delta})
        carriers = len([r for r in rows if isinstance(r['payload'].get('tools'), list)])
        assert carriers == len(originals), (
            f'NC did not take effect (got {carriers} tools carriers)')
        # Roundtrip is unaffected — the neuter costs bytes, not correctness.
        rebuilt = mod.rebuild_snapshots(rows)
        assert _canon(rebuilt[-1]['messages']) == _canon(originals[-1]['messages'])
    # Post-restore: dedup works again.
    rows = _project_all(originals)
    assert len([r for r in rows if isinstance(r['payload'].get('tools'), list)]) == 1


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
