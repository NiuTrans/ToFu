"""Request Inspector server fold (P2) — pytest suite.

Design: docs/FRONTEND_ARCHITECTURE.md §3.3 (frozen row schemas). Verifies
``lib/tasks_pkg/request_inspector.py`` against REAL seeded ``task_events``
rows (unique task ids in the dev DB, cleaned up after):

  1. Request rows are METADATA-ONLY (no ``messages``/``tools`` bulk) and
     come ONLY from request-kind snapshots — state snapshots (post-tool /
     final / fallback) never enter the round list (served per-round via
     ``get_request_payload(kind='state')``). Each row's ``toolNames`` rides
     the new-message tail of the NEXT snapshot (§3.1 attribution).
  2. Legacy rows (no ``kind``) classify via the roundNum/label shim and are
     flagged ``legacy:true``.
  3. ``round_usage`` events join as ``attempts`` per round — MULTIPLE per
     round (R1 + R1-FALLBACK = 2 real HTTP calls, the fallback case).
  4. ``coverage`` flips to ``'partial'`` when the task drove Flow execution
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

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_TARGET = os.path.join(ROOT, 'lib', 'tasks_pkg', 'request_inspector.py')


def _seed(task_id, events):
    """Persist (type, payload) events with sequential ids; returns task_id."""
    from lib.tasks_pkg.event_log import append_persistent_event, flush_pending
    for eid, (etype, payload) in enumerate(events):
        append_persistent_event(task_id, eid, payload | {'type': etype})
    flush_pending(task_id)  # write-behind lane: drain before asserting
    return task_id


def _cleanup(*task_ids):
    try:
        from lib.storage import get_storage_client
        client = get_storage_client(write=True)
        for task_id in task_ids:
            client.command(
                'record.delete', {
                    'namespace': 'task_results', 'key': task_id,
                }, f'request-inspector-cleanup:{task_id}')
    except Exception:
        pass


def _seed_task_result(task_id, conv_id, created_at, *, user_id=1):
    from lib.storage import get_storage_client
    get_storage_client(write=True).command(
        'task_results.checkpoint', {
            'key': task_id,
            'expected_version': 0,
            'value': {
                'task_id': task_id,
                'conv_id': conv_id,
                'user_id': user_id,
                'content': '',
                'status': 'done',
                'created_at': created_at,
                'completed_at': created_at,
            },
        }, None)


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
def task_flow():
    tid = f'ri-e-{uuid.uuid4().hex[:8]}'
    _seed(tid, [
        ('flow_iteration', {'iteration': 1, 'phase': 'working'}),
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
        assert r['ts'] > 0
        # no tool calls anywhere in this fixture's tails
        assert r['toolNames'] == []
    # state snapshots never enter the round list (served per-round via
    # get_request_payload(kind='state')) — the fold carries no states bucket
    assert 'states' not in fold
    assert fold['coverage'] == 'full'


def test_final_wire_projection_replaces_assembled_tool_count_honestly():
    tid = f'ri-wire-{uuid.uuid4().hex[:8]}'
    _seed(tid, [
        ('messages_snapshot', _snap('request', 1, n_msgs=2, tools=46)),
        ('tool_wire_projection', {
            'roundNum': 1,
            'model': 'kimi-k3',
            'backend': 'local',
            'toolNames': ['read_files', 'edit_file', 'run_command',
                          'search_tools', 'execute_tools'],
            'toolCount': 5,
            'schemaTokens': 3978,
            'schemaFingerprint': 'wire-schema-fingerprint-1',
            'schemaBudgetTokens': 4000,
            'budgetDroppedNames': ['write_file'],
            'compactedNames': ['execute_tools'],
            'executableToolCount': 46,
        }),
    ])
    try:
        from lib.tasks_pkg.request_inspector import (
            fold_request_log, get_request_payload)

        row = fold_request_log(tid)['requests'][0]
        assert row['toolsCount'] == 46
        assert row['wireToolsCount'] == 5
        assert row['wireSchemaTokens'] == 3978
        assert row['wireSchemaFingerprint'] == 'wire-schema-fingerprint-1'
        assert row['budgetDroppedCount'] == 1

        payload = get_request_payload(tid, 1)
        assert len(payload['tools']) == 46
        assert payload['wireProjection'] == {
            'backend': 'local',
            'toolNames': ['read_files', 'edit_file', 'run_command',
                          'search_tools', 'execute_tools'],
            'toolCount': 5,
            'schemaTokens': 3978,
            'schemaFingerprint': 'wire-schema-fingerprint-1',
            'schemaBudgetTokens': 4000,
            'budgetDroppedNames': ['write_file'],
            'compactedNames': ['execute_tools'],
            'executableToolCount': 46,
        }
    finally:
        _cleanup(tid)


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


def test_tool_names_attributed_from_next_snapshot_tail():
    """The round list reads like the chat timeline's turn blocks: each
    request row names the tools that round's response INVOKED. Names ride
    the new-message tail of the NEXT snapshot (the post-tool mirror of
    loop round N carries roundNum=N+1, §3.1) — never a count."""
    tid = f'ri-tn-{uuid.uuid4().hex[:8]}'
    base = [{'role': 'system', 'content': 's'},
            {'role': 'user', 'content': 'u'}]
    r1 = _snap('request', 1, n_msgs=0, tools=0)
    r1['messages'] = base
    r2 = _snap('request', 2, n_msgs=0, tools=0)
    r2['messages'] = base + [
        {'role': 'assistant', 'tool_calls': [
            {'id': 'call-1', 'function': {'name': 'web_search',
                                          'arguments': '{}'}},
        ]},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'done'},
    ]
    r3 = _snap('request', 3, n_msgs=0, tools=0)
    r3['messages'] = r2['messages'] + [
        {'role': 'assistant', 'tool_calls': [
            {'id': 'call-2', 'function': {'name': 'read_files',
                                          'arguments': '{}'}},
            {'id': 'call-3', 'function': {'name': 'read_files',
                                          'arguments': '{}'}},
            {'id': 'call-4', 'function': {'name': 'edit_file',
                                          'arguments': '{}'}},
        ]},
        {'role': 'tool', 'tool_call_id': 'call-2', 'content': 'a'},
    ]
    _seed(tid, [
        ('messages_snapshot', r1),
        ('messages_snapshot', r2),
        ('messages_snapshot', r3),
    ])
    try:
        from lib.tasks_pkg.request_inspector import fold_request_log
        rows = {r['roundNum']: r
                for r in fold_request_log(tid)['requests']}
        assert rows[1]['toolNames'] == ['web_search']
        # ordered unique: a repeated name collapses, order is call order
        assert rows[2]['toolNames'] == ['read_files', 'edit_file']
        # no later snapshot yet → nothing attributable to round 3
        assert rows[3]['toolNames'] == []
    finally:
        _cleanup(tid)


def test_tool_names_supports_anthropic_tool_use_blocks():
    tid = f'ri-tn-a-{uuid.uuid4().hex[:8]}'
    base = [{'role': 'user', 'content': 'u'}]
    r1 = _snap('request', 1, n_msgs=0, tools=0)
    r1['messages'] = base
    r2 = _snap('request', 2, n_msgs=0, tools=0)
    r2['messages'] = base + [
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'toolu-1', 'name': 'search',
             'input': {}},
            {'type': 'text', 'text': 'thinking out loud'},
            {'type': 'tool_use', 'id': 'toolu-2', 'name': 'fetch',
             'input': {}},
        ]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'toolu-1',
             'content': 'done'},
        ]},
    ]
    _seed(tid, [
        ('messages_snapshot', r1),
        ('messages_snapshot', r2),
    ])
    try:
        from lib.tasks_pkg.request_inspector import fold_request_log
        rows = {r['roundNum']: r
                for r in fold_request_log(tid)['requests']}
        assert rows[1]['toolNames'] == ['search', 'fetch']
    finally:
        _cleanup(tid)


def test_tool_names_from_state_mirror_final_never_attributes():
    """A kind='state' post-tool mirror of loop round N carries roundNum=N+1
    and attributes its tail to N; 'final' / 'fallback' labels attribute
    nowhere."""
    tid = f'ri-tn-s-{uuid.uuid4().hex[:8]}'
    r1 = _snap('request', 1, n_msgs=0, tools=0)
    r1['messages'] = [{'role': 'user', 'content': 'u'}]
    mirror = _snap('state', 2, label='Round 1 工具结果后 · 3条',
                   n_msgs=0, tools=0)
    mirror['messages'] = r1['messages'] + [
        {'role': 'assistant', 'tool_calls': [
            {'id': 'call-1', 'function': {'name': 'run_command',
                                          'arguments': '{}'}},
        ]},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'ok'},
    ]
    fin = _snap('state', 'final', label='最终回复后 · 5条', n_msgs=0, tools=0)
    fin['messages'] = mirror['messages'] + [
        {'role': 'assistant', 'tool_calls': [
            {'id': 'call-9', 'function': {'name': 'ghost_tool',
                                          'arguments': '{}'}},
        ]},
    ]
    _seed(tid, [
        ('messages_snapshot', r1),
        ('messages_snapshot', mirror),
        ('messages_snapshot', fin),
    ])
    try:
        from lib.tasks_pkg.request_inspector import fold_request_log
        fold = fold_request_log(tid)
        assert fold['requests'][0]['toolNames'] == ['run_command']
        # 'final' tails never leak into any round row
        assert all('ghost_tool' not in r['toolNames']
                   for r in fold['requests'])
    finally:
        _cleanup(tid)


def test_tool_names_legacy_state_attributes_to_own_round():
    """Pre-contract rows numbered their post-tool state with the round that
    just ran ('Round N 工具结果后'), so a LEGACY state tail attributes to
    roundNum itself — not roundNum-1."""
    tid = f'ri-tn-l-{uuid.uuid4().hex[:8]}'
    req = _snap(None, 1, label='Round 1 请求前 · 1条', n_msgs=0, tools=0)
    req['messages'] = [{'role': 'user', 'content': 'u'}]
    state = _snap(None, 1, label='Round 1 工具结果后 · 3条',
                  n_msgs=0, tools=0)
    state['messages'] = req['messages'] + [
        {'role': 'assistant', 'tool_calls': [
            {'id': 'call-1', 'function': {'name': 'legacy_tool',
                                          'arguments': '{}'}},
        ]},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'ok'},
    ]
    _seed(tid, [
        ('messages_snapshot', req),
        ('messages_snapshot', state),
    ])
    try:
        from lib.tasks_pkg.request_inspector import fold_request_log
        fold = fold_request_log(tid)
        assert fold['requestCount'] == 1
        row = fold['requests'][0]
        assert row['legacy'] is True
        assert row['toolNames'] == ['legacy_tool']
    finally:
        _cleanup(tid)


def test_legacy_rows_classified_by_shim(task_legacy):
    from lib.tasks_pkg.request_inspector import (
        fold_request_log, get_request_payload)
    fold = fold_request_log(task_legacy)
    assert fold['requestCount'] == 1
    assert fold['requests'][0]['legacy'] is True
    assert fold['requests'][0]['messageCount'] == 2
    # legacy STATE rows (工具结果后 / 最终回复后) never enter the round
    # list; they stay fetchable per round via the payload endpoint
    assert 'states' not in fold
    post = get_request_payload(task_legacy, 1, kind='state')
    assert post is not None and '工具结果后' in post['label']
    fin = get_request_payload(task_legacy, 'final', kind='state')
    assert fin is not None and '最终回复后' in fin['label']


def test_coverage_partial_for_flow_task(task_flow):
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_flow)
    assert fold['coverage'] == 'partial'
    assert fold['requestCount'] == 1


def test_unknown_task_honest_empty():
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(f'ri-none-{uuid.uuid4().hex[:8]}')
    assert fold['eventsAvailable'] is False
    assert fold['requests'] == []
    assert fold['requestCount'] == 0


def test_streaming_noise_never_hides_recent_rounds():
    """2026-08-04 incident: every SSE delta is persisted as its own
    task_events row (exact-cursor cold replay), so a long task's log is
    dominated by streaming noise — measured on a real 51,754-row task, the
    inspector's first-10000-rows read cut EVERY snapshot past round 6 and
    rounds 7+ all reported 'mirror expired'. The read must filter to the
    structural slice it renders (snapshots / round_usage / flow_*);
    with the filter, the same cap spans thousands of rounds.

    Seeds 10,300 noise rows BEFORE the structural rows of a late round
    (their event_ids sit beyond the first-10000 window): unfiltered, the
    round vanishes; filtered, both axes resolve."""
    from lib.storage import get_storage_client
    from lib.tasks_pkg.event_log import append_persistent_event
    from lib.tasks_pkg.request_inspector import (
        _read_events, fold_request_log, get_request_payload)
    tid = f'ri-n-{uuid.uuid4().hex[:8]}'
    client = get_storage_client(write=True)
    noise = [
        {'task_id': tid, 'sequence': i,
         'event': {'type': 'delta', 'content': 'x'}}
        for i in range(10300)
    ]
    for offset in range(0, len(noise), 500):
        client.command(
            'event.append_batch', {'events': noise[offset:offset + 500]},
            None, priority='event')
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
    from lib.tasks_pkg.event_log import flush_pending
    flush_pending(tid)
    try:
        rows, read_ok = _read_events(tid)
        assert read_ok, 'successful read must report ok=True'
        assert rows, 'no rows returned'
        leaked = sorted({r['type'] for r in rows} -
                        {'messages_snapshot', 'round_usage', 'round_start',
                         'round_end'} - {r['type'] for r in rows
                                         if r['type'].startswith('flow_')})
        assert not leaked, (
            f'streaming noise leaked into the inspector read: {leaked}')
        fold = fold_request_log(tid)
        assert fold['requestCount'] == 1
        assert fold['requests'][0]['roundNum'] == 88
        assert [a['tag'] for a in fold['requests'][0]['attempts']] == ['R88']
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
    conv = f'ri-conv-{uuid.uuid4().hex[:8]}'
    now = int(time.time() * 1000)
    for tid, ts in ((task_a, now), (task_legacy, now - 1000)):
        _seed_task_result(tid, conv, ts)
    try:
        from lib.tasks_pkg.request_inspector import list_conv_tasks
        out = list_conv_tasks(conv, user_id=1)
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
        assert list_conv_tasks(
            f'ri-noconv-{uuid.uuid4().hex[:6]}', user_id=1)['tasks'] == []
    finally:
        _cleanup(task_a, task_legacy)


def test_routes_registered_on_v1_blueprint():
    from quart import Quart
    from werkzeug.datastructures import ImmutableDict

    from routes.api_v1.tasks import api_v1_tasks_bp
    # Quart 0.19's defaults predate the Flask 3.1 key read by
    # ``add_url_rule``. Production's app factory supplies it; this bare test
    # app needs the same compatibility default before construction.
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = ImmutableDict({**Quart.default_config,
                                              'PROVIDE_AUTOMATIC_OPTIONS': True})
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
    """Flow-shaped task: same-numbered rounds across two phases."""
    tid = f'ri-t-{uuid.uuid4().hex[:8]}'
    _seed(tid, [
        ('flow_iteration', {'iteration': 0, 'phase': 'planning'}),
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
    # Flow events + turn tags → fully covered, chip removed
    assert fold['coverage'] == 'full'
    assert 'coverageReason' not in fold


def test_flow_untagged_is_ambiguous_not_uncovered(task_flow):
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_flow)
    assert fold['coverage'] == 'partial'
    assert fold['coverageReason'] == 'flow-untagged'


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


def test_swarm_agent_emission_end_to_end(chat_sidecar):
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


def test_swarm_agent_emission_continues_after_persisted_tail(chat_sidecar):
    """A rehydrated agent must append, not restart at event id zero."""
    from types import SimpleNamespace

    from lib.swarm.agent import _emit_request_snapshot
    from lib.tasks_pkg.event_log import flush_pending, read_events
    from lib.tasks_pkg.request_inspector import get_request_payload

    parent_id = f'ri-resume-{uuid.uuid4().hex[:8]}'
    iid = f'{parent_id}#agent:agent-browser-visual'
    _seed(iid, [('messages_snapshot', _snap('request', 1, n_msgs=2))])
    agent = SimpleNamespace(
        parent_task={'id': parent_id, 'convId': 'c1', 'provider_id': ''},
        spec=SimpleNamespace(role='browser', id='visual'),
        agent_id='agent-browser-visual',
        model='m-agent',
        thinking_enabled=True,
        messages=[{'role': 'user', 'content': 'resumed'}],
    )
    try:
        assert _emit_request_snapshot(agent, 1) == iid
        flush_pending(iid)
        rows = read_events(iid)
        assert [r['event_id'] for r in rows] == [0, 1]
        # Storage may delta-project the raw second row. The public inspector
        # read must rebuild it and apply last-write-wins for this resumed
        # logical round.
        payload = get_request_payload(iid, 1, turn='swarm-agent')
        assert payload['messages'][0]['content'] == 'resumed'
    finally:
        _cleanup(iid, parent_id)


def test_list_conv_tasks_includes_swarm_agents():
    conv = f'ri-sc-{uuid.uuid4().hex[:8]}'
    parent = f'ri-sp-{uuid.uuid4().hex[:8]}'
    now = int(time.time() * 1000)
    _seed_task_result(parent, conv, now)
    agent_tid = f'{parent}#agent:agent-research-x1'
    _seed(parent, [('messages_snapshot', _snap('request', 1, n_msgs=2))])
    _seed(agent_tid, [
        ('messages_snapshot',
         _snap_turn('swarm-agent', 1, n_msgs=2,
                    agentId='agent-research-x1', agentRole='research')),
    ])
    try:
        from lib.tasks_pkg.request_inspector import list_conv_tasks
        out = list_conv_tasks(conv, user_id=1)
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
    """NC: classify EVERYTHING as 'request' → state mirrors pollute the
    round list — proving the request-only gate is load-bearing."""
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
        assert any(r['roundNum'] == 'final' for r in fold['requests']), (
            f'expected state rows to pollute requests under NC: {fold}')
    # Post-restore: the canonical module (never mutated) gates again.
    from lib.tasks_pkg.request_inspector import fold_request_log
    fold = fold_request_log(task_a)
    assert fold['requestCount'] == 2
    assert all(r['roundNum'] != 'final' for r in fold['requests'])
    with open(_TARGET, encoding='utf-8') as f:
        assert 'NC-RI-SPLIT' not in f.read(), (
            'shipped request_inspector.py must be byte-identical')


class _FakeSidecarClient:
    """Minimal `event.list` server semantics: sequence > after, ASC, limit."""

    def __init__(self, rows):
        self._rows = rows  # [{'sequence', 'event', 'created_at_ms'}]

    def query(self, operation, payload):
        assert operation == 'event.list'
        after = int(payload.get('after_sequence', -1))
        limit = int(payload.get('limit', 500))
        out = [r for r in self._rows if int(r['sequence']) > after]
        return out[:limit]


def test_sidecar_read_rebuilds_delta_snapshots_and_paginates(monkeypatch):
    """Sidecar-mode regression (2026-08-17): `_read_events_uncached`'s sidecar
    branch returned stored rows RAW — but snapshots persist in delta form
    (§10: prefixLen + newMessages, no messages array), so every fold row
    rendered messageCount=0 / approxTokens=0 ("技术详情全是0条"). It also read
    a single 1000-row page, so streaming deltas (one row per SSE token) cut
    every round past the first few. The fold must rebuild deltas AND paginate.
    """
    from lib.tasks_pkg.snapshot_delta import SnapshotProjector
    tid = f'ri-sc-{uuid.uuid4().hex[:8]}'
    projector = SnapshotProjector()
    rows = []

    def _push(etype, payload):
        projected = projector.project(tid, payload | {'type': etype})
        rows.append({'sequence': len(rows) + 1, 'event': projected,
                     'created_at_ms': 1_700_000_000_000 + len(rows)})

    # Round 1 snapshot FIRST, then >1000 rows of streaming noise, then
    # round 2: the single-page read used to drop round 2 entirely.
    _push('messages_snapshot', _snap('request', 1, n_msgs=3))
    for i in range(1500):
        _push('delta', {'content': f'chunk-{i}'})
    _push('messages_snapshot', _snap('request', 2, n_msgs=5))
    _push('messages_snapshot', _snap('state', 'final', label='最终回复后 · 6条',
                                     n_msgs=6, tools=0))

    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda *, write=False: _FakeSidecarClient(rows))
    from lib.tasks_pkg import request_inspector as ri
    ri._EVENTS_CACHE.pop(tid, None)
    try:
        fold = ri.fold_request_log(tid)
        assert fold['requestCount'] == 2
        by_round = {r['roundNum']: r for r in fold['requests']}
        assert by_round[1]['messageCount'] == 3
        assert by_round[1]['toolsCount'] == 2
        # Round 2 lives past the first raw page — pagination must reach it.
        assert by_round[2]['messageCount'] == 5
        # The on-demand payload endpoint serves rebuilt messages, not deltas.
        payload = ri.get_request_payload(tid, 2)
        assert payload is not None and len(payload['messages']) == 5
        assert len(payload['tools']) == 2
        state = ri.get_request_payload(tid, 'final', kind='state')
        assert state is not None and len(state['messages']) == 6
    finally:
        ri._EVENTS_CACHE.pop(tid, None)


def test_sidecar_list_conv_tasks_probe_sets_has_events(monkeypatch):
    """The compact Sidecar summary distinguishes structural logs from noise."""
    conv = f'ri-sc-conv-{uuid.uuid4().hex[:8]}'
    tid_full = f'ri-sc-full-{uuid.uuid4().hex[:8]}'
    tid_noise = f'ri-sc-noise-{uuid.uuid4().hex[:8]}'
    tid_none = f'ri-sc-none-{uuid.uuid4().hex[:8]}'

    def ev_rows(task_id, events):
        return [{'sequence': i + 1, 'event': e,
                 'created_at_ms': 1_700_000_000_000 + i}
                for i, e in enumerate(events)]

    class _Client:
        def __init__(self):
            self._events = {
                tid_full: ev_rows(tid_full, [
                    {'type': 'delta', 'content': 'x'},
                    {'type': 'round_start', 'roundNum': 1},
                ]),
                # streaming noise only — never ran a structural round
                tid_noise: ev_rows(tid_noise,
                                   [{'type': 'delta', 'content': 'y'}] * 3),
                tid_none: [],
            }

        def query(self, operation, payload, deadline=None):
            if operation == 'task_results.summary_list':
                assert payload.get('conv_id') == conv
                now = 1_700_000_000_000
                return {'records': [
                    {'key': tid_full,
                     'conv_id': conv, 'status': 'done',
                     'created_at': now, 'completed_at': now},
                    {'key': tid_noise,
                     'conv_id': conv, 'status': 'done',
                     'created_at': now - 1, 'completed_at': now},
                    {'key': tid_none,
                     'conv_id': conv, 'status': 'done',
                     'created_at': now - 2, 'completed_at': now},
                ], 'capped': False}
            if operation == 'event.inspector_summary':
                assert set(payload['task_ids']) == {
                    tid_full, tid_noise, tid_none}
                return {'records': [{
                    'task_id': tid_full,
                    'request_count': 0,
                    'state_count': 0,
                    'legacy_count': 0,
                    'event_count': 1,
                    'first_event_at_ms': 1_700_000_000_001,
                }]}
            raise AssertionError(f'unexpected op {operation}')

    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda *, write=False: _Client())
    from lib.tasks_pkg import request_inspector as ri
    out = ri.list_conv_tasks(conv, user_id=1)
    by_id = {t['taskId']: t for t in out['tasks']}
    assert set(by_id) == {tid_full, tid_noise, tid_none}
    assert by_id[tid_full]['hasEvents'] is True
    assert by_id[tid_noise]['hasEvents'] is False
    assert by_id[tid_none]['hasEvents'] is False


def test_list_conv_tasks_discovers_durable_attempt_when_task_scan_is_capped(
        monkeypatch):
    """A retained trace stays discoverable after hot/task-result indexes fail.

    The attempt query is owner scoped, metadata only, and server-paged; the
    legacy global scan is no longer the only path to the diagnostic task row.
    """
    conv = f'ri-trace-conv-{uuid.uuid4().hex[:8]}'
    tid = f'ri-trace-task-{uuid.uuid4().hex[:8]}'

    class _Client:
        def query(self, operation, payload, deadline=None):
            if operation == 'turn.timing_trace.list':
                assert payload == {
                    'conversation_id': conv, 'user_id': 7, 'limit': 30,
                }
                return {'records': [{
                    'attempt_id': 'attempt-1', 'task_id': tid,
                    'status': 'completed', 'turn_id': 'turn-1',
                    'created_at': 1_700_000_000_000,
                    'settled_at': 1_700_000_001_000,
                }], 'has_more': False}
            if operation == 'task_results.summary_list':
                return {'records': [], 'capped': True}
            if operation == 'event.inspector_summary':
                assert payload == {'task_ids': [tid]}
                return {'records': []}
            raise AssertionError(f'unexpected op {operation}')

    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda *, write=False: _Client())
    from lib.tasks_pkg import request_inspector as ri
    out = ri.list_conv_tasks(conv, user_id=7)
    assert out['tasks'] == [{
        'taskId': tid,
        'status': 'completed',
        'createdAt': 1_700_000_000_000,
        'completedAt': 1_700_000_001_000,
        'turnId': 'turn-1',
        'live': False,
        'requestCount': 0,
        'stateCount': 0,
        'legacyCount': 0,
        'hasEvents': False,
    }]
    assert out['hasMore'] is False
    assert 'readError' not in out


def test_fold_read_error_is_distinct_from_expired(monkeypatch):
    """A FAILED event read must surface readError:true — never the honest
    'records cleaned up' empty state, which is reserved for a successful
    but empty read."""
    from lib.tasks_pkg import request_inspector as ri

    class _BrokenClient:
        def query(self, operation, payload, *, deadline=None):
            raise RuntimeError('sidecar unreachable')

    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda *, write=False: _BrokenClient())
    tid = f'ri-err-{uuid.uuid4().hex[:8]}'
    ri._EVENTS_CACHE.pop(tid, None)
    try:
        fold = ri.fold_request_log(tid)
        assert fold['eventsAvailable'] is False
        assert fold['readError'] is True
        # A successful-but-empty read (unknown task) must NOT set the flag.
        monkeypatch.undo()
        ok_tid = f'ri-ok-{uuid.uuid4().hex[:8]}'
        ri._EVENTS_CACHE.pop(ok_tid, None)
        ok_fold = ri.fold_request_log(ok_tid)
        assert ok_fold['eventsAvailable'] is False
        assert 'readError' not in ok_fold
    finally:
        ri._EVENTS_CACHE.pop(tid, None)


def test_list_conv_tasks_before_cursor_and_has_more():
    """``before`` pages OLDER persisted rows exclusively; hasMore tracks
    whether another page exists."""
    from lib.tasks_pkg.request_inspector import list_conv_tasks
    conv = f'ri-page-{uuid.uuid4().hex[:8]}'
    now = int(time.time() * 1000)
    tids = [f'ri-page-t{i}-{uuid.uuid4().hex[:6]}' for i in range(3)]
    for i, tid in enumerate(tids):
        _seed_task_result(tid, conv, now - i * 10_000)
    try:
        page1 = list_conv_tasks(conv, user_id=1, limit=2)
        assert [t['taskId'] for t in page1['tasks']] == [tids[0], tids[1]]
        assert page1['hasMore'] is True
        assert 'readError' not in page1
        cursor = page1['tasks'][-1]['createdAt']
        page2 = list_conv_tasks(conv, user_id=1, limit=2, before=cursor)
        assert [t['taskId'] for t in page2['tasks']] == [tids[2]]
        assert page2['hasMore'] is False
        # Cursor is exclusive: the boundary row must not repeat.
        assert tids[1] not in {t['taskId'] for t in page2['tasks']}
    finally:
        _cleanup(*tids)


def test_list_conv_tasks_read_error_flag(monkeypatch):
    """A failed task_results read is readError:true, NOT an empty list
    presented as 'no tasks'."""
    from lib.tasks_pkg import request_inspector as ri

    class _BrokenClient:
        def query(self, operation, payload, *, deadline=None):
            raise RuntimeError('sidecar unreachable')

    monkeypatch.setattr('lib.storage.get_storage_client',
                        lambda *, write=False: _BrokenClient())
    out = ri.list_conv_tasks(f'ri-errc-{uuid.uuid4().hex[:8]}', user_id=1)
    assert out['readError'] is True
    assert out['tasks'] == []
    assert out['hasMore'] is False


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-q']))
