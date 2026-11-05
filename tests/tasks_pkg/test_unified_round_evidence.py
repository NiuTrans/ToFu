"""Root/child evidence identity and incremental storage specifications."""
import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_identity_restart_and_root_child_collision():
    from lib.tasks_pkg.round_evidence import begin_round_evidence
    root, child = {}, {}
    r = begin_round_evidence(root, 'isolated-root', 1)
    c = begin_round_evidence(child, 'isolated-root#agent:a', 1, 'swarm-agent')
    assert r != c
    assert begin_round_evidence(root, 'isolated-root', 2)['turn'] == r['turn']
    assert begin_round_evidence(root, 'isolated-root', 1)['turn'] != r['turn']
    assert begin_round_evidence({}, c['taskId'], 1)['turn'] != c['turn']


def test_shared_builder_exact_request_and_labeled_fallback(monkeypatch):
    from lib.tasks_pkg.round_evidence import begin_round_evidence, build_round_evidence
    import lib.tasks_pkg.wire_messages as wire
    monkeypatch.setattr(wire, 'apply_wire_sanitize', lambda messages, **kw: messages)
    task = {}
    ref = begin_round_evidence(task, 'isolated-request', 1)
    messages = [{'role': 'user', 'content': 'exact'}]
    request = build_round_evidence(task, [], ref=ref, kind='request', model='m', prepared_messages=messages)
    assert request['messages'] == messages
    assert not request.get('degraded')
    fallback = build_round_evidence(task, messages, ref=ref, kind='request', model='m')
    assert fallback['degradedReason'] == 'request-build-fallback'
    messages = messages + [{'role': 'tool', 'tool_call_id': 'call-a', 'content': 'MODEL RESULT'}]
    state = build_round_evidence(task, messages, ref=ref, kind='state', model='m')
    assert state['messages'][-1]['content'] == 'MODEL RESULT'
    assert state['inspectorRef'] == request['inspectorRef']




def test_round_reference_survives_cold_projection():
    from lib.orchestration.chat_flow_projection import project_flow_tool_rounds
    ref = {'taskId': 'isolated#agent:a', 'roundNum': 1, 'turn': 'swarm-agent:invocation'}
    row = {'tool': 'read_files', 'round': 1, 'inspectorRef': ref, 'status': 'done'}
    assert project_flow_tool_rounds([row])[0]['inspectorRef'] == ref


def test_incremental_storage_budget(monkeypatch):
    from lib.tasks_pkg.round_evidence import begin_round_evidence, build_round_evidence
    from lib.tasks_pkg.snapshot_delta import SnapshotProjector
    import lib.tasks_pkg.wire_messages as wire
    monkeypatch.setattr(wire, 'apply_wire_sanitize', lambda messages, **kw: messages)
    task, messages, projector = {}, [], SnapshotProjector()
    stored = 0
    for number in range(1, 51):
        ref = begin_round_evidence(task, 'isolated-budget', number)
        messages.append({'role': 'user', 'content': 'x' * 1000})
        for kind in ('request', 'state'):
            event = build_round_evidence(task, list(messages), ref=ref, kind=kind,
                model='m', prepared_messages=list(messages))
            stored += len(json.dumps(projector.project('isolated-budget', event)))
    assert stored < 160_000  # full repeated history exceeds 2.5 MB


def test_snapshots_share_settled_twins_of_unchanged_messages(monkeypatch):
    from lib.conversation_sync.settled_rows import SettledDict
    from lib.tasks_pkg.round_evidence import begin_round_evidence, build_round_evidence
    from lib.tasks_pkg.manager._persist import _HEAVY_TERMINAL_FIELDS
    from lib.tasks_pkg.manager._events import _strip_base64_for_snapshot
    task = {}
    history = [{'role': 'system', 'content': 'system'},
               {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {
                   'url': 'data:image/png;base64,' + 'A' * 64}}]},
               {'role': 'assistant', 'content': '', 'tool_calls': [{
                   'id': 'c1', 'type': 'function',
                   'function': {'name': 'read_file', 'arguments': '{"path": "a"}'}}]}]
    snapshots = []
    for number, extra in enumerate(([], [{'role': 'tool', 'tool_call_id': 'c1',
                                          'content': '结果'}]), start=1):
        ref = begin_round_evidence(task, 'isolated-settled', number)
        # The request builder hands over a fresh deep copy every round.
        prepared = json.loads(json.dumps(history + extra))
        snapshots.append(build_round_evidence(task, [], ref=ref, kind='request',
                                              model='m', prepared_messages=prepared))
        assert json.dumps(snapshots[-1]['messages']) == json.dumps(
            _strip_base64_for_snapshot(prepared))
    first, second = (snapshot['messages'] for snapshot in snapshots)
    assert all(type(message) is SettledDict for message in second)
    assert all(old is new for old, new in zip(first, second[:len(first)]))
    assert task['_snapshotMessages'] is second
    assert '_snapshotMessages' in _HEAVY_TERMINAL_FIELDS


def test_explicit_turn_never_selects_other_invocation(monkeypatch):
    import lib.tasks_pkg.request_inspector as inspector
    events = [{'type': 'messages_snapshot', 'ts_ms': 1, 'payload': {
        'kind': 'state', 'roundNum': 1, 'turn': turn,
        'messages': [{'role': 'tool', 'content': turn}]}}
        for turn in ('root:old', 'root:new')]
    monkeypatch.setattr(inspector, '_read_events', lambda task, **kwargs: (events if task == 'isolated-owner' else [], True))
    assert inspector.get_request_payload('isolated-owner', 1, 'root:old', 'state')['messages'][0]['content'] == 'root:old'
    assert inspector.get_request_payload('other-owner', 1, 'root:old', 'state') is None
    assert inspector.get_request_payload('isolated-owner', 1, 'missing', 'state') is None


@pytest.mark.parametrize('mismatch', ['round', 'task', 'missing'])
def test_diagnostic_identity_never_borrows_stale_reference(mismatch):
    from lib.tasks_pkg.round_evidence import begin_round_evidence, round_evidence_turn
    task = {'id': 'current', '_flow_phase': 'worker'}
    ref = begin_round_evidence(task, 'current', 2, 'worker')
    assert round_evidence_turn(task, 2) == ref['turn']
    if mismatch == 'round':
        round_num = 1
    elif mismatch == 'task':
        task['_inspectorRef']['taskId'] = 'other'
        round_num = 2
    else:
        task.pop('_inspectorRef')
        round_num = 2
    assert round_evidence_turn(task, round_num) == 'worker'


def test_diagnostic_identity_uses_declared_inspector_task():
    from lib.tasks_pkg.round_evidence import begin_round_evidence, round_evidence_turn
    task = {'id': 'child', '_inspectorTaskId': 'parent#agent:a'}
    ref = begin_round_evidence(task, 'parent#agent:a', 1)
    assert round_evidence_turn(task, 1) == ref['turn']
    restarted = begin_round_evidence(task, 'parent#agent:a', 1)
    assert restarted['turn'] != ref['turn']
    assert round_evidence_turn(task, 1) == restarted['turn']
