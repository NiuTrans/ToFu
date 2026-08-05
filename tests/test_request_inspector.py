"""Request Inspector server fold (P2) — pytest suite.

Design: docs/DEBUG_PANEL_REDESIGN.md §3.3 (frozen row schemas). Verifies
``lib/tasks_pkg/request_inspector.py`` against REAL seeded ``task_events``
rows (unique task ids in the dev DB, cleaned up after):

  1. Request rows are METADATA-ONLY (no ``messages``/``tools`` bulk) and
     come ONLY from request-kind snapshots — state snapshots (post-tool /
     final / fallback) route to ``states``.
  2. Legacy rows (no ``kind``) classify via the roundNum/label shim and are
     flagged ``legacy:true``.
  3. ``round_usage`` events join as ``attempts`` per round — MULTIPLE per
     round (R1 + R1-FALLBACK = 2 real HTTP calls, the fallback case).
  4. ``coverage`` flips to ``'partial'`` when the task drove endpoint mode
     (Planner/Critic calls not captured — the honest chip).
  5. Unknown/expired task → ``eventsAvailable:false``, empty lists.
  6. ``get_request_payload`` serves the full payload per round, last
     re-emitted snapshot wins, 404 (None) for state-only/unknown rounds.
  7. ``list_conv_tasks`` returns task rows with EXACT kind-counted tallies.
  8. Route registration pins the three endpoints on the v1 blueprint.

NEUTER: make ``_snapshot_kind`` classify everything as 'request' → the
state-separation assertions flip red (proving the split is load-bearing).
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_TARGET = os.path.join(ROOT, 'lib', 'tasks_pkg', 'request_inspector.py')


def _seed(task_id, events):
    """Persist (type, payload) events with sequential ids; returns task_id."""
    from lib.tasks_pkg.event_log import append_persistent_event
    for eid, (etype, payload) in enumerate(events):
        append_persistent_event(task_id, eid, payload | {'type': etype})
    return task_id


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


def _snap(kind=None, round_num=1, label='', n_msgs=3, tools=2):
    p = {
        'roundNum': round_num,
        'label': label or f'Round {round_num} 请求前 · {n_msgs}条',
        'messages': [{'role': 'user', 'content': 'x' * 100}] * n_msgs,
        'model': 'm-test',
        'params': {'maxTokens': 1000, 'temperature': 1},
    }
    if tools:
        p['tools'] = [{'function': {'name': 't%d' % i}} for i in range(tools)]
    if kind:
        p['kind'] = kind
    return p


def _usage_event(round_num, tag, model='m-test', trace='trace-abc'):
    return ('round_usage', {
        'roundNum': round_num, 'model': model, 'tag': tag,
        'tokensIn': 500, 'tokensOut': 120,
        'usage': {'trace_id': trace, 'stream_elapsed_ms': 2300,
                  'prompt_tokens': 500, 'completion_tokens': 120},
    })


@pytest.fixture()
def task_a():
    tid = f'ri-a-{uuid.uuid4().hex[:8]}'
    _seed(tid, [
        ('messages_snapshot', _snap('request', 1, n_msgs=3)),
        _usage_event(1, 'R1', trace='trace-r1'),
        _usage_event(1, 'R1-FALLBACK', model='m-fb', trace='trace-r1fb'),
        ('messages_snapshot', _snap('request', 2, n_msgs=5)),
        _usage_event(2, 'R2', trace='trace-r2'),
        ('messages_snapshot', _snap('state', 'final', label='最终回复后 · 6条',
                                    n_msgs=6, tools=0)),
    ])
    yield tid
    _cleanup(tid)


@pytest.fixture()
def task_legacy():
    tid = f'ri-l-{uuid.uuid4().hex[:8]}'
    _seed(tid, [
        ('messages_snapshot', _snap(None, 1, label='Round 1 请求前 · 2条',
                                    n_msgs=2)),
        ('messages_snapshot', _snap(None, 1, label='Round 1 工具结果后 · 4条',
                                    n_msgs=4)),
        ('messages_snapshot', _snap(None, 'final', label='最终回复后 · 5条',
                                    n_msgs=5, tools=0)),
    ])
    yield tid
    _cleanup(tid)


@pytest.fixture()
def task_endpoint():
    tid = f'ri-e-{uuid.uuid4().hex[:8]}'
    _seed(tid, [
        ('endpoint_iteration', {'iteration': 1, 'phase': 'working'}),
        ('messages_snapshot', _snap('request', 1, n_msgs=3)),
    ])
    yield tid
    _cleanup(tid)


def test_request_rows_metadata_only_and_split(task_a):
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_a)
    assert fold['eventsAvailable'] is True
    assert fold['requestCount'] == 2
    reqs = fold['requests']
    assert [r['roundNum'] for r in reqs] == [1, 2]
    for r in reqs:
        # METADATA-ONLY: the payload must NOT ride the list rows.
        assert 'messages' not in r, f'payload leaked into row: {r.keys()}'
        assert 'tools' not in r
        assert r['model'] == 'm-test'
        assert r['params'].get('maxTokens') == 1000
        assert r['approxTokens'] > 0
        assert r['ts'] > 0
    # state snapshot routed to states, never into requests
    assert len(fold['states']) == 1
    assert fold['states'][0]['roundNum'] == 'final'
    assert fold['coverage'] == 'full'


def test_attempts_join_multi_call_round(task_a):
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_a)
    r1 = fold['requests'][0]
    assert len(r1['attempts']) == 2, (
        'R1 primary + R1-FALLBACK = two real HTTP calls, both must join')
    tags = [a['tag'] for a in r1['attempts']]
    assert tags == ['R1', 'R1-FALLBACK']
    fb = r1['attempts'][1]
    assert fb['model'] == 'm-fb' and fb['traceId'] == 'trace-r1fb'
    assert fb['tokensIn'] == 500 and fb['streamElapsedMs'] == 2300
    r2 = fold['requests'][1]
    assert [a['tag'] for a in r2['attempts']] == ['R2']


def test_legacy_rows_classified_by_shim(task_legacy):
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_legacy)
    assert fold['requestCount'] == 1
    assert fold['requests'][0]['legacy'] is True
    assert fold['requests'][0]['messageCount'] == 2
    state_labels = [s['label'] for s in fold['states']]
    assert any('工具结果后' in lb for lb in state_labels)
    assert any('最终回复后' in lb for lb in state_labels)
    assert all(s['legacy'] for s in fold['states'])


def test_coverage_partial_for_endpoint_task(task_endpoint):
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_endpoint)
    assert fold['coverage'] == 'partial'
    assert fold['requestCount'] == 1


def test_unknown_task_honest_empty():
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(f'ri-none-{uuid.uuid4().hex[:8]}')
    assert fold['eventsAvailable'] is False
    assert fold['requests'] == [] and fold['states'] == []
    assert fold['requestCount'] == 0


def test_streaming_noise_never_hides_recent_rounds():
    """2026-08-04 incident: every SSE delta is persisted as its own
    task_events row (exact-cursor cold replay), so a long task's log is
    dominated by streaming noise — measured on a real 51,754-row task, the
    inspector's first-10000-rows read cut EVERY snapshot past round 6 and
    rounds 7+ all reported 'mirror expired'. The read must filter to the
    structural slice it renders (snapshots / round_usage / endpoint_*);
    with the filter, the same cap spans thousands of rounds.

    Seeds 10,300 noise rows BEFORE the structural rows of a late round
    (their event_ids sit beyond the first-10000 window): unfiltered, the
    round vanishes; filtered, both axes resolve."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.event_log import append_persistent_event
    from lib.tasks_pkg.request_inspector import (
        _read_events, fold_request_log, get_request_payload)
    tid = f'ri-n-{uuid.uuid4().hex[:8]}'
    db = get_thread_db(DOMAIN_CHAT)
    noise = [(tid, i, 1, 'delta', '{}') for i in range(10300)]
    db.executemany(
        'INSERT INTO task_events (task_id, event_id, ts_ms, type, payload) '
        'VALUES (?, ?, ?, ?, ?)', noise)
    db.commit()
    base = 10300
    append_persistent_event(
        tid, base,
        _snap('request', 88, n_msgs=3) | {'type': 'messages_snapshot'})
    append_persistent_event(
        tid, base + 1,
        _snap('state', 88, label='Round 88 工具结果后 · 5条', n_msgs=5,
              tools=0) | {'type': 'messages_snapshot'})
    append_persistent_event(
        tid, base + 2,
        _usage_event(88, 'R88')[1] | {'type': 'round_usage'})
    try:
        rows = _read_events(tid)
        assert rows, 'no rows returned'
        leaked = sorted({r['type'] for r in rows} -
                        {'messages_snapshot', 'round_usage', 'round_start',
                         'round_end'} - {r['type'] for r in rows
                                         if r['type'].startswith('endpoint_')})
        assert not leaked, (
            f'streaming noise leaked into the inspector read: {leaked}')
        fold = fold_request_log(tid)
        assert fold['requestCount'] == 1
        assert fold['requests'][0]['roundNum'] == 88
        assert [a['tag'] for a in fold['requests'][0]['attempts']] == ['R88']
        assert len(fold['states']) == 1
        assert fold['states'][0]['roundNum'] == 88
        p_req = get_request_payload(tid, 88)
        assert p_req is not None and len(p_req['messages']) == 3
        p_state = get_request_payload(tid, 88, kind='state')
        assert p_state is not None and len(p_state['messages']) == 5
    finally:
        _cleanup(tid)


def test_payload_on_demand_last_wins(task_a):
    from lib.tasks_pkg.request_inspector import get_request_payload
    p2 = get_request_payload(task_a, 2)
    assert p2 is not None
    assert len(p2['messages']) == 5
    assert len(p2['tools']) == 2
    assert p2['params'].get('maxTokens') == 1000
    # string roundNum also resolves (frontend passes strings)
    p2s = get_request_payload(task_a, '2')
    assert p2s is not None and len(p2s['messages']) == 5
    # 'final' is a STATE round → no request payload
    assert get_request_payload(task_a, 'final') is None
    # unknown round → None
    assert get_request_payload(task_a, 99) is None


def test_payload_kind_state_same_round_axis():
    """kind='state' serves the post-tool / final mirrors via the SAME
    roundNum axis (design §3.1: post-tool mirror of loop round N carries
    roundNum=N+1) — the in-chat state inspector's fetch contract."""
    from lib.tasks_pkg.request_inspector import get_request_payload
    tid = f'ri-s-{uuid.uuid4().hex[:8]}'
    req = _snap('request', 2, n_msgs=5)
    req['messages'] = [{'role': 'user', 'content': 'pre-request'}] * 5
    state = _snap('state', 2, label='Round 2 工具结果后 · 7条', n_msgs=7,
                  tools=0)
    state['messages'] = [{'role': 'tool', 'content': 'post-tool'}] * 7
    fin = _snap('state', 'final', label='最终回复后 · 8条', n_msgs=8, tools=0)
    fin['messages'] = [{'role': 'assistant', 'content': 'final'}] * 8
    _seed(tid, [
        ('messages_snapshot', req),
        ('messages_snapshot', state),
        ('messages_snapshot', fin),
    ])
    try:
        # default kind stays the pre-request snapshot
        p_req = get_request_payload(tid, 2)
        assert p_req is not None and p_req['kind'] == 'request'
        assert p_req['messages'][0]['content'] == 'pre-request'
        # kind='state' at the SAME round number returns the post-tool mirror
        p_state = get_request_payload(tid, 2, kind='state')
        assert p_state is not None and p_state['kind'] == 'state'
        assert p_state['messages'][0]['content'] == 'post-tool'
        assert len(p_state['messages']) == 7
        assert p_state['label'] == 'Round 2 工具结果后 · 7条'
        # string round labels address the final / fallback mirrors
        p_fin = get_request_payload(tid, 'final', kind='state')
        assert p_fin is not None and p_fin['messages'][0]['content'] == 'final'
        # cross-kind misses stay misses
        assert get_request_payload(tid, 'final') is None
        assert get_request_payload(tid, 1, kind='state') is None
        # an unknown kind is refused, never silently reclassified
        assert get_request_payload(tid, 2, kind='bogus') is None
    finally:
        _cleanup(tid)


def test_list_conv_tasks_exact_tallies(task_a, task_legacy):
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database._core_schema import TASK_RESULTS, upsert
    conv = f'ri-conv-{uuid.uuid4().hex[:8]}'
    db = get_thread_db(DOMAIN_CHAT)
    now = int(time.time() * 1000)
    for tid, ts in ((task_a, now), (task_legacy, now - 1000)):
        upsert(db, TASK_RESULTS,
               {'task_id': tid, 'conv_id': conv, 'content': '',
                'status': 'done', 'created_at': ts, 'completed_at': ts},
               conflict_cols=['task_id'],
               insert_cols=['task_id', 'conv_id', 'content', 'status',
                            'created_at', 'completed_at'],
               update_cols=[], commit=True, retry=False)
    try:
        from lib.tasks_pkg.request_inspector import list_conv_tasks
        out = list_conv_tasks(conv)
        rows = {t['taskId']: t for t in out['tasks']}
        assert task_a in rows and task_legacy in rows
        ra = rows[task_a]
        assert ra['requestCount'] == 2 and ra['stateCount'] == 1
        assert ra['legacyCount'] == 0 and ra['hasEvents'] is True
        assert ra['status'] == 'done' and ra['live'] is False
        rl = rows[task_legacy]
        assert rl['legacyCount'] == 3 and rl['requestCount'] == 0
        # newest first
        assert out['tasks'][0]['taskId'] == task_a
        # unknown conv → empty
        assert list_conv_tasks(f'ri-noconv-{uuid.uuid4().hex[:6]}')['tasks'] == []
    finally:
        _cleanup(task_a, task_legacy)


def test_routes_registered_on_v1_blueprint():
    from quart import Quart

    from routes.api_v1.tasks import api_v1_tasks_bp
    app = Quart(__name__)
    app.register_blueprint(api_v1_tasks_bp)
    rules = {str(r) for r in app.url_map.iter_rules()}
    assert '/api/v1/tasks/by-conv/<conv_id>' in rules
    assert '/api/v1/tasks/<task_id>/requests' in rules
    assert '/api/v1/tasks/<task_id>/requests/<round_num>' in rules
    # the payload route passes kind through (state mirrors ride the same URL)
    src = open(os.path.join(ROOT, 'routes', 'api_v1', 'tasks.py'),
               encoding='utf-8').read()
    assert "kind=request.args.get('kind', 'request')" in src
    # merge-artifact guard: one stream route, one api_response import
    assert src.count(
        "@api_v1_tasks_bp.route('/api/v1/tasks/<task_id>/stream'") == 1
    assert src.count('from lib.api_response import (') == 1


# ─────────────────────────────────────────────────────────────────────────
#  P4 (epic pt_e3dc7198e7e34bb1): turn tags + swarm sub-agent rows
# ─────────────────────────────────────────────────────────────────────────

def _snap_turn(turn, round_num=1, n_msgs=3, content='x' * 100, **extra):
    p = _snap('request', round_num, n_msgs=n_msgs, tools=0)
    p['turn'] = turn
    p['messages'] = [{'role': 'user', 'content': content}] * n_msgs
    p.update(extra)
    return p


@pytest.fixture()
def task_turns():
    """Endpoint-shaped task: same-numbered rounds across two phases."""
    tid = f'ri-t-{uuid.uuid4().hex[:8]}'
    _seed(tid, [
        ('endpoint_iteration', {'iteration': 0, 'phase': 'planning'}),
        ('messages_snapshot', _snap_turn('working', 1, content='worker-body')),
        ('round_usage', {'roundNum': 1, 'model': 'm-w', 'tag': 'R1',
                         'turn': 'working', 'tokensIn': 100, 'tokensOut': 10,
                         'usage': {'trace_id': 'tr-w',
                                   'stream_elapsed_ms': 500}}),
        ('messages_snapshot', _snap_turn('reviewing', 1, content='critic-body')),
        ('round_usage', {'roundNum': 1, 'model': 'm-c', 'tag': 'R1',
                         'turn': 'reviewing', 'tokensIn': 200, 'tokensOut': 20,
                         'usage': {'trace_id': 'tr-c',
                                   'stream_elapsed_ms': 700}}),
    ])
    yield tid
    _cleanup(tid)


def test_turn_tagged_rounds_stay_distinct(task_turns):
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_turns)
    assert fold['requestCount'] == 2
    turns = sorted(r['turn'] for r in fold['requests'])
    assert turns == ['reviewing', 'working']
    # attempts join per (turn, roundNum) — no cross-phase leakage
    by_turn = {r['turn']: r for r in fold['requests']}
    assert [a['traceId'] for a in by_turn['working']['attempts']] == ['tr-w']
    assert [a['traceId'] for a in by_turn['reviewing']['attempts']] == ['tr-c']
    # endpoint + turn tags → fully covered, chip removed
    assert fold['coverage'] == 'full'
    assert 'coverageReason' not in fold


def test_endpoint_untagged_is_ambiguous_not_uncovered(task_endpoint):
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_endpoint)
    assert fold['coverage'] == 'partial'
    assert fold['coverageReason'] == 'endpoint-untagged'


def test_payload_turn_disambiguation(task_turns):
    from lib.tasks_pkg.request_inspector import get_request_payload
    critic = get_request_payload(task_turns, 1, turn='reviewing')
    assert critic is not None and critic['turn'] == 'reviewing'
    assert critic['messages'][0]['content'] == 'critic-body'
    worker = get_request_payload(task_turns, 1, turn='working')
    assert worker is not None and worker['messages'][0]['content'] == 'worker-body'
    # no turn → last-wins (the critic snapshot, emitted second)
    last = get_request_payload(task_turns, 1)
    assert last is not None and last['messages'][0]['content'] == 'critic-body'
    # unknown turn → None
    assert get_request_payload(task_turns, 1, turn='planning') is None


def test_swarm_agent_emission_end_to_end():
    """The agent.py helper persists under '{parent}#agent:{id}' with
    kind='request' + turn='swarm-agent' — and the PARENT's own log stays
    clean (suppression contract intact)."""
    from types import SimpleNamespace

    from lib.swarm.agent import _emit_request_snapshot
    parent_id = f'ri-p-{uuid.uuid4().hex[:8]}'
    agent = SimpleNamespace(
        parent_task={'id': parent_id, 'convId': 'c1', 'provider_id': ''},
        spec=SimpleNamespace(role='research', id='x1'),
        agent_id='agent-research-x1',
        model='m-agent',
        thinking_enabled=True,
        messages=[
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'objective'},
        ],
    )
    iid = _emit_request_snapshot(agent, 1)
    assert iid == f'{parent_id}#agent:agent-research-x1'
    try:
        from lib.tasks_pkg.request_inspector import fold_request_log
        fold = fold_request_log(iid)
        assert fold['requestCount'] == 1
        row = fold['requests'][0]
        assert row['turn'] == 'swarm-agent'
        assert row['agentId'] == 'agent-research-x1'
        assert row['agentRole'] == 'research'
        assert row['model'] == 'm-agent'
        assert row['params']['maxTokens'] == 64000
        # parent log untouched — no snapshot leaked to the parent stream
        from lib.tasks_pkg.event_log import read_events
        assert read_events(parent_id) == []
        # no parent id → helper no-ops cleanly
        agent2 = SimpleNamespace(**{**agent.__dict__,
                                    'parent_task': {'id': ''}})
        assert _emit_request_snapshot(agent2, 1) == ''
    finally:
        _cleanup(iid, parent_id)


def test_list_conv_tasks_includes_swarm_agents():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database._core_schema import TASK_RESULTS, upsert
    conv = f'ri-sc-{uuid.uuid4().hex[:8]}'
    parent = f'ri-sp-{uuid.uuid4().hex[:8]}'
    db = get_thread_db(DOMAIN_CHAT)
    now = int(time.time() * 1000)
    upsert(db, TASK_RESULTS,
           {'task_id': parent, 'conv_id': conv, 'content': '',
            'status': 'done', 'created_at': now, 'completed_at': now},
           conflict_cols=['task_id'],
           insert_cols=['task_id', 'conv_id', 'content', 'status',
                        'created_at', 'completed_at'],
           update_cols=[], commit=True, retry=False)
    agent_tid = f'{parent}#agent:agent-research-x1'
    _seed(parent, [('messages_snapshot', _snap('request', 1, n_msgs=2))])
    _seed(agent_tid, [
        ('messages_snapshot',
         _snap_turn('swarm-agent', 1, n_msgs=2,
                    agentId='agent-research-x1', agentRole='research')),
    ])
    try:
        from lib.tasks_pkg.request_inspector import list_conv_tasks
        out = list_conv_tasks(conv)
        rows = {t['taskId']: t for t in out['tasks']}
        assert agent_tid in rows, f'swarm agent row missing: {list(rows)}'
        arow = rows[agent_tid]
        assert arow['isSwarmAgent'] is True
        assert arow['agentId'] == 'agent-research-x1'
        assert arow['parentTaskId'] == parent
        assert arow['requestCount'] == 1 and arow['hasEvents'] is True
        # parent row still present with its own tally
        assert rows[parent]['requestCount'] == 1
    finally:
        _cleanup(parent, agent_tid)


def test_neuter_state_split_is_load_bearing(task_a):
    """NC: classify EVERYTHING as 'request' → the states bucket empties and
    the request list gets polluted — proving the split is load-bearing."""
    from tests._nc_harness import neutered_source
    fixed = "    kind = payload.get('kind')\n    if kind in ('request', 'state'):"
    broken = ("    kind = payload.get('kind')\n    if True:  # NC-RI-SPLIT\n"
              "        return 'request'\n    if kind in ('request', 'state'):")
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert fixed in src, 'NC anchor drifted — update the neuter'
    with neutered_source(_TARGET, fixed, broken) as mod:
        # Drive the NEUTERED module object directly (importlib.reload would
        # re-read the un-neutered file and defeat the neuter — see
        # tests/_nc_harness.py docstring).
        fold = mod.fold_request_log(task_a)
        assert fold['states'] == [] and any(
            r['roundNum'] == 'final' for r in fold['requests']), (
            f'expected state rows to pollute requests under NC: {fold}')
    # Post-restore: the canonical module (never mutated) splits again.
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_a)
    assert len(fold['states']) == 1 and fold['requestCount'] == 2
    with open(_TARGET, encoding='utf-8') as f:
        assert 'NC-RI-SPLIT' not in f.read(), (
            'shipped request_inspector.py must be byte-identical')


def test_neuter_structural_filter_is_load_bearing():
    """NC: make the WHERE clause always-true (keep the parameter list valid)
    → the seeded streaming-noise rows leak back into the inspector read and
    the no-leak assertion flips red, proving the structural filter — not
    the fold's type routing — is what keeps delta noise out."""
    from tests._nc_harness import neutered_source
    fixed = "f'WHERE task_id=? AND (type IN ({_struct_ph}) '"
    broken = "f'WHERE task_id=? AND (type=type OR type IN ({_struct_ph}) '"
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert fixed in src, 'NC anchor drifted — update the neuter'
    tid = f'ri-nc-{uuid.uuid4().hex[:8]}'
    _seed(tid, [
        ('delta', {'text': 'chunk'}),
        ('messages_snapshot', _snap('request', 1, n_msgs=2)),
    ])
    try:
        with neutered_source(_TARGET, fixed, broken) as mod:
            rows = mod._read_events(tid)
            assert any(r['type'] == 'delta' for r in rows), (
                'expected delta noise to leak into the read under NC')
        # Post-restore: the canonical module filters again.
        from lib.tasks_pkg.request_inspector import _read_events
        assert all(r['type'] != 'delta' for r in _read_events(tid))
    finally:
        _cleanup(tid)
    with open(_TARGET, encoding='utf-8') as f:
        assert 'type=type' not in f.read(), (
            'shipped request_inspector.py must be byte-identical')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-q']))
