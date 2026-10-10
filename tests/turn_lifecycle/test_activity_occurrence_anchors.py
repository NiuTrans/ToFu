"""Occurrence-anchor contract: producer identity, not call-ID or retained position."""
from __future__ import annotations

import json

import pytest

from lib.agent_core.events import EventType
from lib.turn_activity_timeline import (
    ACTIVITY_TIMELINE_MAX_ENTRIES,
    ACTIVITY_TIMELINE_MAX_JSON_BYTES,
    fold_activity_timeline,
    normalize_activity_timeline,
)

pytestmark = pytest.mark.unit


def task(**extra):
    return {'id': 'task-a', '_attemptId': 'attempt-a', **extra}


def tool_event(seq, round_num=None, **extra):
    return {'type': EventType.TOOL_START, 'seq': seq, 'emittedAt': 1000 + seq,
            'toolName': 'read_files', 'toolCallId': 'reused',
            **({'roundNum': round_num} if round_num is not None else {}), **extra}


def test_tool_ordinal_is_not_a_model_round_and_survives_terminal_and_reload():
    owner = task(_activityLastLlmRound=500)
    timeline = fold_activity_timeline(None, tool_event(1, 97, llmRound=3), owner)
    timeline = fold_activity_timeline(timeline, tool_event(2, 97,
        type=EventType.TOOL_COMPLETE, toolCallId='reminted', status='error'), owner)
    entry = normalize_activity_timeline(json.loads(json.dumps(timeline)))['entries'][0]
    assert entry['taskId'] == 'task-a'
    assert entry['attemptId'] == 'attempt-a'
    assert entry['anchorToolRoundNum'] == 97
    assert entry['llmRound'] == 3
    assert entry['toolCallId'] == 'reminted'
    assert entry['status'] == 'failed'
    assert len(timeline['entries']) == 1
    assert 'llmRound' not in fold_activity_timeline(None, tool_event(3, 99), owner)['entries'][0]


def test_recycled_call_id_and_same_round_in_different_producers_never_coalesce():
    owner = task()
    timeline = None
    for seq, producer, occurrence in ((1, 'parent', 1), (2, 'parent', 2), (3, 'child', 1)):
        timeline = fold_activity_timeline(timeline, tool_event(seq, occurrence,
            taskId=producer, attemptId=f'{producer}-attempt', llmRound=0), owner)
    assert len(timeline['entries']) == 3
    assert len({entry['spanId'] for entry in timeline['entries']}) == 3
    assert [(entry['taskId'], entry['anchorToolRoundNum']) for entry in timeline['entries']] == [
        ('parent', 1), ('parent', 2), ('child', 1)]
    assert len({entry['id'] for entry in timeline['entries']}) == 3


def test_legacy_unique_open_tool_join_is_scoped_and_keeps_start_anchor():
    owner = task()
    timeline = fold_activity_timeline(None, tool_event(1, 1), owner)
    timeline = fold_activity_timeline(timeline, tool_event(2, 1, taskId='child', attemptId='child-a'), owner)
    timeline = fold_activity_timeline(timeline, tool_event(3,
        type=EventType.TOOL_COMPLETE, status='error', taskId='child', attemptId='child-a'), owner)
    assert len(timeline['entries']) == 2
    assert timeline['entries'][0]['status'] == 'running'
    assert timeline['entries'][1]['status'] == 'failed'
    assert timeline['entries'][1]['anchorToolRoundNum'] == 1
    ambiguous = fold_activity_timeline(None, tool_event(1, 1), owner)
    ambiguous = fold_activity_timeline(ambiguous, tool_event(2, 2), owner)
    ambiguous = fold_activity_timeline(ambiguous, tool_event(3,
        type=EventType.TOOL_COMPLETE, status='error'), owner)
    assert len(ambiguous['entries']) == 3
    assert all(entry['status'] == 'running' for entry in ambiguous['entries'][:2])


def test_content_anchor_captures_existing_block_not_mutable_terminal_alias():
    owner = task(_activityLastLlmRound=2, segments=[
        {'type': 'text', 'blockId': 'task:task-a:text:2:0', 'llmRound': 2,
         'taskId': 'task-a', 'attemptId': 'attempt-a'},
        {'type': 'text', 'blockId': 'task:task-a:text:terminal', 'terminal': True,
         'llmRound': 2, 'taskId': 'task-a', 'attemptId': 'attempt-a'},
    ])
    event = {'type': EventType.TOOL_SCHEMA_REJECTED, 'seq': 4, 'toolName': 'read',
             'reasonCode': 'bad_schema'}
    entry = fold_activity_timeline(None, event, owner)['entries'][0]
    assert entry['anchorBlockId'] == 'task:task-a:text:2:0'
    assert entry['llmRound'] == 2
    child = fold_activity_timeline(None, {**event, 'taskId': 'child'}, owner)['entries'][0]
    assert child['taskId'] == 'child'
    assert 'attemptId' not in child
    assert 'llmRound' not in child
    assert 'anchorBlockId' not in child


def test_model_request_round_remains_one_based_and_explicit_block_is_preserved():
    entry = fold_activity_timeline(None, {'type': EventType.MODEL_REQUEST_COMPLETE,
        'seq': 1, 'roundNum': 4, 'status': 'failed', 'anchorBlockId': 'known-block'}, task())['entries'][0]
    assert entry['llmRound'] == 3
    assert entry['anchorBlockId'] == 'known-block'
    assert 'anchorToolRoundNum' not in entry


def test_anchor_fields_remain_bounded_after_trimming_and_replay():
    source = {'entries': [{'id': f'entry:{index}', 'spanId': f'span:{index}',
        'seq': index, 'occurredAt': index,
        'kind': 'tool', 'status': 'failed', 'severity': 'error', 'summary': 'failed',
        'taskId': 'task-' + 't' * 250, 'attemptId': 'a' * 256,
        'anchorBlockId': 'b' * 320, 'anchorToolRoundNum': 9000 + index,
        } for index in range(ACTIVITY_TIMELINE_MAX_ENTRIES + 10)]}
    timeline = normalize_activity_timeline(source)
    assert len(timeline['entries']) <= ACTIVITY_TIMELINE_MAX_ENTRIES
    assert timeline['droppedCount'] > 0
    assert len(json.dumps(timeline, ensure_ascii=False, separators=(',', ':')).encode()) <= ACTIVITY_TIMELINE_MAX_JSON_BYTES
    assert timeline['entries'][-1]['anchorToolRoundNum'] == 9000 + ACTIVITY_TIMELINE_MAX_ENTRIES + 9
    assert len(timeline['entries'][-1]['anchorBlockId']) <= 320
    assert normalize_activity_timeline(json.loads(json.dumps(timeline))) == timeline


@pytest.mark.parametrize('selector', [
    {'taskId': 't' * 257}, {'attemptId': 'a' * 257},
    {'anchorBlockId': 'b' * 321}, {'anchorToolRoundNum': True},
    {'anchorToolRoundNum': 0.5}, {'anchorToolRoundNum': 9_007_199_254_740_992},
])
def test_invalid_exact_selector_is_not_coerced_to_a_legacy_join(selector):
    entry = {'id': 'entry', 'spanId': 'span', 'seq': 1, 'occurredAt': 1,
             'kind': 'tool', 'status': 'failed', 'severity': 'error',
             'toolCallId': 'reused', **selector}
    assert normalize_activity_timeline({'entries': [entry]}) is None


def test_real_tool_producer_stamps_attempt_and_thin_segment_retains_ordinal():
    from lib.tasks_pkg.tool_display._dispatch import _build_tool_round_entry
    from lib.tasks_pkg.segments._assemble import tool_use_segment_from_round
    from lib.tasks_pkg.segments._serde import segments_to_json

    ordinal, round_entry, payload = _build_tool_round_entry(
        'read_files', {}, 'reused', '{}', 91, False, task=task())
    assert ordinal == 92
    for value in (round_entry, payload):
        assert value['taskId'] == 'task-a'
        assert value['attemptId'] == 'attempt-a'
        assert value['roundNum'] == 92
    segment = tool_use_segment_from_round(round_entry, 0)
    wire = segments_to_json([segment])[0]
    assert wire['roundNum'] == 92
    assert wire['taskId'] == 'task-a'
    assert wire['attemptId'] == 'attempt-a'
    assert '_round' not in wire


@pytest.mark.parametrize('old_status', ['done', 'error'])
def test_old_child_occurrence_cannot_suppress_new_gateway_failure(old_status):
    owner = task()
    timeline = fold_activity_timeline(None, tool_event(1, 1,
        type=EventType.TOOL_COMPLETE, status=old_status), owner)
    previous_entries = timeline['entries']
    timeline = fold_activity_timeline(timeline, tool_event(2, 2,
        type=EventType.TOOL_COMPLETE, toolName='execute_tools',
        toolCallId='gateway-new', status='error',
        toolContent=json.dumps({'results': [{
            'name': 'read_files', 'call_id': 'reused', 'status': 'error',
            'error': 'new occurrence failed',
        }]})), owner)
    failures = [row for row in timeline['entries'] if ':gateway-' in row['id']]
    assert len(failures) == 1
    assert failures[0]['status'] == 'failed'
    assert failures[0]['anchorToolRoundNum'] == 2
    assert failures[0]['toolCallId'] == 'reused'
    assert 'new occurrence failed' in failures[0]['detail']
    assert len(timeline['entries']) == 2
    assert timeline['entries'][:1] == previous_entries


def gateway_round(round_num=1, call_id='gateway-1', **extra):
    return {'toolName': 'execute_tools', 'toolCallId': call_id,
            'roundNum': round_num, 'taskId': 'task-a', 'attemptId': 'attempt-a',
            **extra}


def gateway_failure(seq, round_num=1, call_id='gateway-1', **extra):
    return tool_event(seq, round_num, type=EventType.TOOL_COMPLETE,
        toolName='execute_tools', toolCallId=call_id, status='error',
        toolContent=json.dumps({'results': [{
            'name': 'read_files', 'call_id': 'reused', 'status': 'error',
            'error': 'new occurrence failed',
        }]}), **extra)


@pytest.mark.parametrize('old_status', ['done', 'error'])
@pytest.mark.parametrize('reuse_gateway_id', [False, True])
def test_old_linked_child_cannot_hide_a_new_gateway_occurrence(
    old_status, reuse_gateway_id,
):
    owner = task(toolRounds=[gateway_round()])
    timeline = fold_activity_timeline(None, tool_event(1, 2,
        type=EventType.TOOL_COMPLETE, status=old_status,
        parentToolCallId='gateway-1'), owner)
    assert timeline['entries'][0].get('parentSpanId')
    previous_entries = timeline['entries']
    next_call_id = 'gateway-1' if reuse_gateway_id else 'gateway-2'
    owner['toolRounds'].append(gateway_round(3, next_call_id))
    timeline = fold_activity_timeline(
        timeline, gateway_failure(2, 3, next_call_id), owner)

    assert timeline['entries'][:-1] == previous_entries
    assert timeline['entries'][-1]['status'] == 'failed'
    assert timeline['entries'][-1]['anchorToolRoundNum'] == 3
    assert timeline['entries'][-1]['detail'] == 'new occurrence failed'


@pytest.mark.parametrize('unsafe_parent', [
    'missing', 'ambiguous', 'unstamped', 'wrong-task', 'wrong-attempt',
    'missing-ordinal', 'future-ordinal', 'not-gateway',
])
def test_parent_call_link_requires_a_unique_proven_gateway_occurrence(unsafe_parent):
    parent = gateway_round()
    owner = task(toolRounds=[parent])
    if unsafe_parent == 'missing':
        owner['toolRounds'] = []
    elif unsafe_parent == 'ambiguous':
        owner['toolRounds'].append(gateway_round(0))
    elif unsafe_parent == 'unstamped':
        parent.pop('attemptId')
    elif unsafe_parent == 'wrong-task':
        parent['taskId'] = 'other-task'
    elif unsafe_parent == 'wrong-attempt':
        parent['attemptId'] = 'other-attempt'
    elif unsafe_parent == 'missing-ordinal':
        parent.pop('roundNum')
    elif unsafe_parent == 'future-ordinal':
        parent['roundNum'] = 3
    else:
        parent['toolName'] = 'read_files'
    timeline = fold_activity_timeline(None, tool_event(1, 2,
        type=EventType.TOOL_COMPLETE, status='error',
        parentToolCallId='gateway-1'), owner)
    assert 'parentSpanId' not in timeline['entries'][0]
    previous_entries = timeline['entries']
    timeline = fold_activity_timeline(timeline, gateway_failure(2), owner)

    assert timeline['entries'][:-1] == previous_entries
    assert timeline['entries'][-1]['detail'] == 'new occurrence failed'


@pytest.mark.parametrize('unsafe_child', [
    'running', 'succeeded', 'wrong-task', 'wrong-attempt', 'missing-producer',
    'wrong-tool', 'ambiguous',
])
def test_exact_parent_span_requires_a_scoped_settled_unique_child(unsafe_child):
    owner = task()
    child = tool_event(1, 2, type=EventType.TOOL_COMPLETE, status='error',
                       parentSpanId='gateway-span')
    if unsafe_child == 'running':
        child['type'] = EventType.TOOL_START
    elif unsafe_child == 'succeeded':
        child['status'] = 'done'
    elif unsafe_child == 'wrong-task':
        child['taskId'] = 'other-task'
    elif unsafe_child == 'wrong-attempt':
        child['attemptId'] = 'other-attempt'
    elif unsafe_child == 'wrong-tool':
        child['toolName'] = 'write_file'
    timeline = fold_activity_timeline(None, child, owner)
    if unsafe_child == 'missing-producer':
        timeline['entries'][0].pop('attemptId')
    elif unsafe_child == 'ambiguous':
        timeline = fold_activity_timeline(timeline, tool_event(2, 3,
            type=EventType.TOOL_COMPLETE, status='done',
            parentSpanId='gateway-span'), owner)
    previous_entries = timeline['entries']
    timeline = fold_activity_timeline(
        timeline, gateway_failure(3, spanId='gateway-span'), owner)

    assert timeline['entries'][:-1] == previous_entries
    assert timeline['entries'][-1]['detail'] == 'new occurrence failed'


def test_real_artifact_child_stamp_deduplicates_after_terminal_and_reload():
    from lib.tasks_pkg.tool_display._dispatch import _build_tool_round_entry
    from lib.tool_result_artifacts import register_artifact_provenance

    owner = task(toolRounds=[gateway_round()])
    ref = 'tool-result:' + 'd' * 64
    register_artifact_provenance(
        owner, ref, tool_name='execute_tools', display='Tool execution batch',
        llm_round=0, tool_call_id='gateway-1')
    ordinal, round_entry, payload = _build_tool_round_entry(
        'read_tool_artifact', {'artifact_ref': ref}, 'reused', '{}',
        1, False, task=owner)
    for value in (round_entry, payload):
        assert value['parentToolCallId'] == 'gateway-1'
        assert value['taskId'] == 'task-a'
        assert value['attemptId'] == 'attempt-a'
        assert 'parentSpanId' not in value
    timeline = fold_activity_timeline(None, {**payload, 'seq': 1}, owner)
    assert timeline['entries'][0].get('parentSpanId')
    timeline = normalize_activity_timeline(json.loads(json.dumps(timeline)))
    timeline = fold_activity_timeline(timeline, tool_event(2, ordinal,
        type=EventType.TOOL_COMPLETE, toolName='read_tool_artifact',
        status='error', toolContent='child failed'), owner)
    previous_entries = timeline['entries']
    terminal = gateway_failure(3)
    terminal['toolContent'] = json.dumps({'results': [{
        'name': 'read_tool_artifact', 'call_id': 'reused', 'status': 'error',
        'error': 'child failed',
    }]})
    timeline = fold_activity_timeline(timeline, terminal, owner)

    assert timeline['entries'] == previous_entries
    assert len(timeline['entries']) == 1
    assert timeline['entries'][0]['status'] == 'failed'
    assert timeline['entries'][0]['anchorToolRoundNum'] == ordinal


@pytest.mark.parametrize('old_status', ['done', 'error'])
def test_legacy_gateway_id_collision_does_not_consume_an_old_child(old_status):
    owner = task()
    timeline = fold_activity_timeline(None, tool_event(1, 1,
        type=EventType.TOOL_COMPLETE, status=old_status), owner)
    previous_entries = timeline['entries']
    timeline = fold_activity_timeline(
        timeline, gateway_failure(2, None, 'reused'), owner)

    assert timeline['entries'][:-1] == previous_entries
    assert timeline['entries'][-1]['detail'] == 'new occurrence failed'
    assert timeline['entries'][-1]['spanId'] != previous_entries[0]['spanId']


@pytest.mark.parametrize('prior_tool_name', [None, 'read_files'])
def test_gateway_cleanup_only_consumes_an_exact_generic_shell(prior_tool_name):
    owner = task()
    timeline = fold_activity_timeline(None, tool_event(1, 1,
        type=EventType.TOOL_RESULT, toolName=prior_tool_name, status='error'), owner)
    previous_entries = timeline['entries']
    timeline = fold_activity_timeline(
        timeline, gateway_failure(2, 1, 'reused'), owner)

    expected_prior = [] if prior_tool_name is None else previous_entries
    assert timeline['entries'][:-1] == expected_prior
    assert timeline['entries'][-1]['detail'] == 'new occurrence failed'
