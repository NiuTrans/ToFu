"""Unified activity timeline contracts: folding, bounds, and cold replay."""

from __future__ import annotations

import json
import threading
import uuid

import pytest

from lib.agent_core.events import EventType, Phase, build_event


pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)


def _task(**values):
    return {
        '_attemptId': 'attempt-a',
        'id': 'task-a',
        'model': 'kimi-k3',
        **values,
    }


def test_schema_isolation_model_failure_and_fallback_keep_exact_order():
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task(_activeModelRequestSpan='model:attempt-a:1')
    timeline = None
    for event in (
        {
            'type': EventType.MODEL_REQUEST_START,
            'seq': 1,
            'emittedAt': 1_000,
            'spanId': 'model:attempt-a:1',
            'model': 'kimi-k3',
            'roundNum': 1,
        },
        {
            'type': EventType.TOOL_SCHEMA_REJECTED,
            'seq': 2,
            'emittedAt': 1_010,
            'toolName': 'write_file',
            'reasonCode': 'invalid_schema',
            'detail': '$.function.parameters.required: description is missing',
            'action': 'omitted',
            'parentSpanId': 'model:attempt-a:1',
        },
        {
            'type': EventType.MODEL_REQUEST_COMPLETE,
            'seq': 3,
            'emittedAt': 1_020,
            'spanId': 'model:attempt-a:1',
            'model': 'kimi-k3',
            'status': 'failed',
            'errorKind': 'BadRequestError',
            'errorDetail': 'HTTP 400',
        },
        {
            'type': EventType.MODEL_FALLBACK,
            'seq': 4,
            'emittedAt': 1_030,
            'fallbackFrom': 'kimi-k3',
            'fallbackModel': 'glm-5.3',
            'fallbackKind': 'provider_unavailable',
            'fallbackReason': 'all selected-model slots unavailable',
        },
    ):
        timeline = fold_activity_timeline(timeline, event, task)

    assert timeline is not None
    assert [(entry['kind'], entry['status'], entry['occurredAt'])
            for entry in timeline['entries']] == [
        ('tool', 'skipped', 1_010),
        ('model', 'failed', 1_020),
        ('model', 'switched', 1_030),
    ]
    isolated = timeline['entries'][0]
    assert isolated['toolName'] == 'write_file'
    assert isolated['action'] == 'omitted'
    assert isolated['parentSpanId'] == 'model:attempt-a:1'
    switched = timeline['entries'][2]
    assert (switched['fromModel'], switched['toModel']) == (
        'kimi-k3', 'glm-5.3',
    )


def test_llm_round_stamps_from_request_tag_and_inherits_across_folds():
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task()
    timeline = None
    # A preflight schema rejection before any model request stays unanchored.
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.TOOL_SCHEMA_REJECTED,
        'seq': 1,
        'emittedAt': 1_000,
        'toolName': 'write_file',
        'reasonCode': 'invalid_schema',
        'detail': 'preflight rejection',
        'action': 'omitted',
    }, task)
    # Round 1 opens: the fold tracks the 0-based llmRound for later events.
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.MODEL_REQUEST_START,
        'seq': 2,
        'emittedAt': 1_010,
        'spanId': 'model:attempt-a:1',
        'model': 'kimi-k3',
        'requestTag': 'R1',
        'roundNum': 1,
    }, task)
    assert task['_activityLastLlmRound'] == 0
    # An in-request rejection carries its own 1-based roundNum → llmRound 0.
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.TOOL_SCHEMA_REJECTED,
        'seq': 3,
        'emittedAt': 1_020,
        'toolName': 'edit_file',
        'reasonCode': 'invalid_schema',
        'detail': 'in-request rejection',
        'action': 'omitted',
        'parentSpanId': 'model:attempt-a:1',
        'roundNum': 1,
    }, task)
    # A retry phase without a round inherits the tracked request round.
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.PHASE,
        'seq': 4,
        'emittedAt': 1_030,
        'phase': Phase.RETRYING,
        'detail': 'temporary rate limit',
        'statusCode': 429,
        'model': 'kimi-k3',
    }, task)
    # A failed completion stamps llmRound = roundNum − 1 (R{n} is 1-based).
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.MODEL_REQUEST_COMPLETE,
        'seq': 5,
        'emittedAt': 1_040,
        'spanId': 'model:attempt-a:1',
        'model': 'kimi-k3',
        'status': 'failed',
        'errorKind': 'BadRequestError',
        'errorDetail': 'HTTP 400',
        'roundNum': 1,
    }, task)
    # Fallback and terminal error without a round inherit the failed round.
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.MODEL_FALLBACK,
        'seq': 6,
        'emittedAt': 1_050,
        'fallbackFrom': 'kimi-k3',
        'fallbackModel': 'glm-5.3',
    }, task)
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.ERROR,
        'seq': 7,
        'emittedAt': 1_060,
        'content': 'fatal transport failure',
    }, task)
    # Round 2 opens; a later diagnostic inherits the newer round.
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.MODEL_REQUEST_START,
        'seq': 8,
        'emittedAt': 1_070,
        'spanId': 'model:attempt-a:2',
        'model': 'glm-5.3',
        'requestTag': 'R2',
        'roundNum': 2,
    }, task)
    assert task['_activityLastLlmRound'] == 1
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.PHASE,
        'seq': 9,
        'emittedAt': 1_080,
        'phase': Phase.COMPACTING,
        'detail': 'Compressing context…',
    }, task)

    assert timeline is not None
    entries = timeline['entries']
    assert [(entry['kind'], entry['status']) for entry in entries] == [
        ('tool', 'skipped'),
        ('tool', 'skipped'),
        ('status', 'failed'),  # retry row settled by the terminal error
        ('model', 'failed'),
        ('model', 'switched'),
        ('error', 'failed'),
        ('status', 'running'),
    ]
    assert 'llmRound' not in entries[0]
    assert [entry.get('llmRound') for entry in entries[1:]] == [0, 0, 0, 0, 0, 1]


def test_compaction_event_pair_replaces_phase_with_one_accounted_receipt():
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task()
    timeline = fold_activity_timeline(None, {
        'type': EventType.PHASE,
        'seq': 1,
        'emittedAt': 1_000,
        'phase': Phase.COMPACTING,
        'detail': 'Compressing context…',
        'roundNum': 3,
    }, task)
    assert timeline is not None
    assert timeline['entries'][0]['reasonCode'] == Phase.COMPACTING

    timeline = fold_activity_timeline(timeline, {
        'type': EventType.COMPACTION,
        'seq': 2,
        'emittedAt': 1_010,
        'archiveId': 'archive-a',
        'convId': 'conv-a',
        'trigger': 'working_set',
        'tokensBefore': 180_000,
        'tokenCountKind': 'estimated',
        'msgsBefore': 42,
        'model': 'kimi-k3',
        'reason': 'working-set threshold reached',
        'roundNum': 3,
    }, task)
    assert timeline is not None
    assert len(timeline['entries']) == 1
    started = timeline['entries'][0]
    assert started['reasonCode'] == 'context_compaction'
    assert started['status'] == 'running'
    assert started['archiveId'] == 'archive-a'
    assert started['tokensBefore'] == 180_000

    timeline = fold_activity_timeline(timeline, {
        'type': EventType.COMPACTION_DONE,
        'seq': 3,
        'emittedAt': 1_310,
        'archiveId': 'archive-a',
        'convId': 'conv-a',
        'trigger': 'working_set',
        'tokensBefore': 180_000,
        'tokensAfter': 42_000,
        'tokenCountKind': 'estimated',
        'msgsBefore': 42,
        'msgsAfter': 11,
        'reductionPct': 76.7,
        'roundNum': 3,
        'receipt': {'status': 'completed', 'trigger': 'working_set'},
    }, task)
    assert timeline is not None
    assert len(timeline['entries']) == 1
    settled = timeline['entries'][0]
    assert settled['id'] == started['id']
    assert settled['status'] == 'succeeded'
    assert settled['severity'] == 'info'
    assert settled['summaryKey'] == 'activity.compaction.succeeded'
    assert settled['tokensBefore'] == 180_000
    assert settled['tokensAfter'] == 42_000
    assert settled['messagesBefore'] == 42
    assert settled['messagesAfter'] == 11
    assert settled['reductionPercent'] == 77
    assert settled['durationMs'] == 300
    assert settled['llmRound'] == 2


def test_failed_compaction_receipt_remains_a_visible_warning_fact():
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task()
    timeline = fold_activity_timeline(None, {
        'type': EventType.COMPACTION,
        'archiveId': 'archive-failed',
        'tokensBefore': 90_000,
        'msgsBefore': 18,
        'emittedAt': 2_000,
    }, task)
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.COMPACTION_DONE,
        'archiveId': 'archive-failed',
        'tokensBefore': 90_000,
        'tokensAfter': 90_000,
        'msgsBefore': 18,
        'msgsAfter': 18,
        'reductionPct': 0,
        'receipt': {
            'status': 'failed',
            'trigger': 'force',
            'outcomeReason': 'no safe messages to drop',
        },
        'emittedAt': 2_020,
    }, task)

    assert timeline is not None
    entry = timeline['entries'][0]
    assert entry['status'] == 'failed'
    assert entry['severity'] == 'warning'
    assert entry['summaryKey'] == 'activity.compaction.failed'
    assert entry['detail'] == 'no safe messages to drop'
    assert entry['reductionPercent'] == 0


def test_repeated_schema_rejection_across_dispatches_coalesces():
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task(_activeModelRequestSpan='model:attempt-a:1')
    timeline = None
    for seq, span in ((1, 'model:attempt-a:1'), (2, 'model:attempt-a:2'),
                      (3, 'model:attempt-a:3')):
        task['_activeModelRequestSpan'] = span
        timeline = fold_activity_timeline(timeline, {
            'type': EventType.TOOL_SCHEMA_REJECTED,
            'seq': seq,
            'emittedAt': 1_000 + seq,
            'toolName': 'mcp__hope__get_job_logs',
            'reasonCode': 'invalid_schema',
            'detail': ('$.function.parameters.oneOf[0].required references '
                       'properties not declared at that level: hope_run_id'),
            'action': 'omitted',
            'parentSpanId': span,
        }, task)

    assert timeline is not None
    assert len(timeline['entries']) == 1
    entry = timeline['entries'][0]
    assert entry['count'] == 3
    assert entry['status'] == 'skipped'
    assert entry['occurredAt'] == 1_001
    assert entry['endedAt'] == 1_003
    assert entry['parentSpanId'] == 'model:attempt-a:3'

    # A different rejection detail is a distinct fact and earns its own row.
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.TOOL_SCHEMA_REJECTED,
        'seq': 4,
        'emittedAt': 1_004,
        'toolName': 'mcp__hope__get_job_logs',
        'reasonCode': 'invalid_schema',
        'detail': '$.function.parameters.required must be an array of strings',
        'parentSpanId': 'model:attempt-a:3',
    }, task)
    assert len(timeline['entries']) == 2


def test_model_switch_event_has_an_exact_backend_decision_clock():
    event = build_event(
        EventType.MODEL_FALLBACK,
        fallbackFrom='kimi-k3',
        fallbackModel='glm-5.3',
    )

    assert event['emittedAt'] > 1_000_000_000_000


def test_tool_progress_and_failure_update_one_correlated_row():
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task(_activeModelRequestSpan='model:attempt-a:1')
    timeline = fold_activity_timeline(None, {
        'type': EventType.TOOL_START,
        'seq': 5,
        'toolCallId': 'call-write',
        'toolName': 'write_file',
        'roundNum': 1,
        'tStart': 2_000,
        'emittedAt': 2_001,
    }, task)
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.TOOL_PROGRESS,
        'seq': 6,
        'toolCallId': 'call-write',
        'toolName': 'write_file',
        'detail': 'halfway through',
        'emittedAt': 2_010,
    }, task)
    assert timeline is not None
    assert timeline['entries'][0]['detail'] == 'halfway through'
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.TOOL_RESULT,
        'seq': 7,
        'toolCallId': 'call-write',
        'toolName': 'write_file',
        'status': 'error',
        'isError': True,
        'content': 'permission denied',
        'tEnd': 2_050,
        'emittedAt': 2_051,
    }, task)

    assert timeline is not None
    assert len(timeline['entries']) == 1
    entry = timeline['entries'][0]
    assert entry['toolCallId'] == 'call-write'
    assert entry['status'] == 'failed'
    assert entry['detail'] == 'permission denied'
    assert entry['startedAt'] == 2_000
    assert entry['endedAt'] == 2_050
    assert entry['durationMs'] == 50


def test_tool_content_v2_error_keeps_code_message_and_next_action():
    from lib.tools.result_envelope import typed_tool_error
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task()
    terminal = typed_tool_error(
        'browser_write_authorization_required',
        retryable=False,
        message='Write access is missing for example.test (tab #17).',
        next_action='Verify the tab, then grant access or use a read tool.',
    ).to_model_text()
    timeline = fold_activity_timeline(None, {
        'type': EventType.TOOL_START,
        'seq': 1,
        'toolCallId': 'js-1',
        'toolName': 'browser_execute_js',
        'tStart': 2_000,
    }, task, now_ms=2_000)
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.TOOL_COMPLETE,
        'seq': 2,
        'toolCallId': 'js-1',
        'toolName': 'browser_execute_js',
        'status': 'error',
        'toolContent': terminal,
        'tEnd': 2_050,
    }, task, now_ms=2_050)

    assert timeline is not None
    entry = timeline['entries'][0]
    assert entry['reasonCode'] == 'browser_write_authorization_required'
    assert 'Write access is missing for example.test' in entry['detail']
    assert 'Verify the tab' in entry['detail']
    assert 'contractVersion' not in entry['detail']


def test_execute_tools_validation_failure_projects_child_as_skipped():
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task()
    outer = json.dumps({
        'contractVersion': 'tofu.tool-result/v2',
        'status': 'ok',
        'items': [{
            'status': 'error',
            'errors': [{
                'code': 'missing_required_arguments',
                'name': 'read_tool_artifact',
                'message': 'Missing required arguments: artifact_ref',
                'retry_hint': 'Provide artifact_ref and retry.',
            }],
            'results': [],
        }],
    })
    timeline = fold_activity_timeline(None, {
        'type': EventType.TOOL_START,
        'seq': 1,
        'toolCallId': 'gateway-1',
        'toolName': 'execute_tools',
        'tStart': 3_000,
    }, task, now_ms=3_000)
    assert timeline is None
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.TOOL_COMPLETE,
        'seq': 2,
        'toolCallId': 'gateway-1',
        'toolName': 'execute_tools',
        'status': 'error',
        'toolContent': outer,
        'tStart': 3_000,
        'tEnd': 3_020,
    }, task, now_ms=3_020)

    assert timeline is not None
    assert len(timeline['entries']) == 1
    entry = timeline['entries'][0]
    assert entry['toolName'] == 'read_tool_artifact'
    assert entry['status'] == 'skipped'
    assert entry['severity'] == 'warning'
    assert entry['reasonCode'] == 'missing_required_arguments'
    assert 'Provide artifact_ref and retry' in entry['detail']
    assert all(row.get('toolName') != 'execute_tools'
               for row in timeline['entries'])


def test_execute_tools_child_failure_is_not_duplicated_by_gateway_shell():
    from lib.tools.result_envelope import typed_tool_error
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task()
    child_error = typed_tool_error(
        'browser_write_authorization_required',
        retryable=False,
        message='Write access missing.',
        next_action='Grant access.',
    ).to_model_text()
    timeline = None
    for event in (
        {'type': EventType.TOOL_START, 'seq': 1,
         'toolCallId': 'gateway-1', 'toolName': 'execute_tools'},
        {'type': EventType.TOOL_START, 'seq': 2,
         'toolCallId': 'js-1', 'toolName': 'browser_execute_js'},
        {'type': EventType.TOOL_COMPLETE, 'seq': 3,
         'toolCallId': 'js-1', 'toolName': 'browser_execute_js',
         'status': 'error', 'toolContent': child_error},
        {'type': EventType.TOOL_COMPLETE, 'seq': 4,
         'toolCallId': 'gateway-1', 'toolName': 'execute_tools',
         'status': 'error', 'toolContent': json.dumps({
             'contractVersion': 'tofu.tool-result/v2', 'status': 'ok',
             'items': [{'status': 'partial_failure', 'results': [{
                 'call_id': 'js-1', 'name': 'browser_execute_js',
                 'status': 'error', 'error': child_error,
             }]}],
         })},
    ):
        timeline = fold_activity_timeline(
            timeline, event, task, now_ms=4_000 + event['seq'])

    assert timeline is not None
    assert [(entry['toolName'], entry['reasonCode'])
            for entry in timeline['entries']] == [
        ('browser_execute_js', 'browser_write_authorization_required')]


def test_policy_rejection_projects_as_blocked_with_its_reason():
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task(_activeModelRequestSpan='model:attempt-a:1')
    timeline = fold_activity_timeline(None, {
        'type': EventType.TOOL_START,
        'seq': 1,
        'toolCallId': 'call-command',
        'toolName': 'run_command',
        'roundNum': 1,
        'tStart': 4_000,
        'emittedAt': 4_000,
    }, task)
    reason = 'Project write blocked: approval required by project policy.'
    descriptor = {
        'kind': 'project_write_authorization_required',
        'tool': 'run_command',
        'reason': reason,
        'retryable': False,
    }
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.TOOL_COMPLETE,
        'seq': 2,
        'toolCallId': 'call-command',
        'toolName': 'run_command',
        'status': 'rejected',
        'toolContent': reason,
        'rejection': descriptor,
        '_rejected': descriptor,
        'tEnd': 4_010,
        'emittedAt': 4_010,
    }, task)

    entry = timeline['entries'][0]
    assert entry['status'] == 'skipped'  # stable public status enum
    assert entry['summaryKey'] == 'activity.tool.blocked'
    assert entry['reasonCode'] == 'project_write_authorization_required'
    assert entry['detail'] == reason


def test_hallucinated_rejection_projects_as_unavailable():
    from lib.turn_activity_timeline import fold_activity_timeline

    descriptor = {
        'kind': 'hallucinated',
        'attempted': 'run_magic_command',
        'suggestions': ['run_command'],
    }
    timeline = fold_activity_timeline(None, {
        'type': EventType.TOOL_COMPLETE,
        'seq': 1,
        'toolCallId': 'call-fake',
        'toolName': 'run_magic_command',
        'status': 'rejected',
        'toolContent': 'Tool does not exist and was not executed.',
        'rejection': descriptor,
        'emittedAt': 4_100,
    }, _task())

    entry = timeline['entries'][0]
    assert entry['status'] == 'skipped'
    assert entry['summaryKey'] == 'activity.tool.unavailable'
    assert entry['reasonCode'] == 'hallucinated'


def test_successful_model_requests_leave_no_timeline_row():
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task(_activeModelRequestSpan='model:attempt-a:1')
    timeline = fold_activity_timeline(None, {
        'type': EventType.MODEL_REQUEST_START,
        'seq': 1,
        'spanId': 'model:attempt-a:1',
        'model': 'kimi-k3',
        'roundNum': 1,
        'emittedAt': 3_000,
    }, task)
    assert timeline is None
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.MODEL_REQUEST_COMPLETE,
        'seq': 2,
        'spanId': 'model:attempt-a:1',
        'model': 'kimi-k3',
        'roundNum': 1,
        'status': 'succeeded',
        'emittedAt': 3_010,
    }, task)
    assert timeline is None

    # Without a live request span or a durable model row there is nothing to
    # nest under: the tool renders flat instead of faking a correlation.
    task.pop('_activeModelRequestSpan')
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.TOOL_START,
        'seq': 3,
        'toolCallId': 'call-after-response',
        'toolName': 'search',
        'roundNum': 1,
        'emittedAt': 3_020,
    }, task)

    assert timeline is not None
    assert [entry['kind'] for entry in timeline['entries']] == ['tool']
    assert 'parentSpanId' not in timeline['entries'][0]


def test_wait_heartbeats_are_transient_and_only_real_retry_is_counted():
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task(_activeModelRequestSpan='model:attempt-a:1')
    timeline = None
    for seq, phase in (
            (1, Phase.WAITING_MODEL),
            (2, Phase.STREAM_STALLED),
            (3, Phase.WAITING_MODEL)):
        timeline = fold_activity_timeline(timeline, {
            'type': EventType.PHASE,
            'seq': seq,
            'emittedAt': 3_000 + seq,
            'phase': phase,
            'detail': 'current attempt heartbeat',
        }, task)
    assert timeline is None

    timeline = fold_activity_timeline(timeline, {
        'type': EventType.PHASE,
        'seq': 4,
        'emittedAt': 3_004,
        'phase': Phase.RETRYING,
        'attempt': 1,
        'detail': 'rate limited; retrying',
        'statusCode': 429,
    }, task)
    assert timeline is not None
    assert len(timeline['entries']) == 1
    assert timeline['entries'][0]['phase'] == Phase.RETRYING
    assert timeline['entries'][0]['count'] == 1


def test_model_completion_settles_its_wait_and_retry_rows_immediately():
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task(_activeModelRequestSpan='model:attempt-a:1')
    timeline = None
    for event in (
        {
            'type': EventType.MODEL_REQUEST_START,
            'seq': 1,
            'spanId': 'model:attempt-a:1',
            'model': 'kimi-k3',
            'emittedAt': 4_000,
        },
        {
            'type': EventType.PHASE,
            'seq': 2,
            'phase': Phase.RETRYING,
            'detail': 'rate limited; waiting',
            'detailKey': 'stream.phase.rateLimited',
            'statusCode': 429,
            'emittedAt': 4_010,
        },
        {
            'type': EventType.MODEL_REQUEST_COMPLETE,
            'seq': 3,
            'spanId': 'model:attempt-a:1',
            'model': 'kimi-k3',
            'status': 'succeeded',
            'emittedAt': 4_050,
        },
    ):
        timeline = fold_activity_timeline(timeline, event, task)

    assert timeline is not None
    assert len(timeline['entries']) == 1
    retry = timeline['entries'][0]
    assert retry['kind'] == 'status'
    assert retry['status'] == 'succeeded'
    assert retry['severity'] == 'warning'
    assert retry['endedAt'] == 4_050
    assert retry['durationMs'] == 40


def test_retry_frames_coalesce_and_projection_has_a_hard_size_budget():
    from lib.turn_activity_timeline import (
        ACTIVITY_TIMELINE_MAX_ENTRIES,
        ACTIVITY_TIMELINE_MAX_JSON_BYTES,
        fold_activity_timeline,
    )

    task = _task(_activeModelRequestSpan='model:attempt-a:1')
    timeline = None
    for seq in range(1, 400):
        timeline = fold_activity_timeline(timeline, {
            'type': EventType.PHASE,
            'seq': seq,
            'phase': Phase.RETRYING,
            'detail': f'Rate limited; waiting cycle {seq}',
            'detailKey': 'stream.phase.rateLimited',
            'detailArgs': {'model': 'Kimi K3', 'attempt': seq},
            'attempt': seq,
            'statusCode': 429,
            'model': 'kimi-k3',
        }, task, now_ms=10_000 + seq)

    assert timeline is not None
    assert len(timeline['entries']) == 1
    assert timeline['entries'][0]['count'] == 399
    assert timeline['entries'][0]['startedAt'] == 10_001
    assert timeline['entries'][0]['endedAt'] == 10_399
    assert timeline['entries'][0]['durationMs'] == 398

    # Routine working/thinking beats are stream status text, never rows.
    timeline = fold_activity_timeline(timeline, {
        'type': EventType.PHASE,
        'seq': 10_000,
        'phase': Phase.WORKING,
        'detail': 'preparing workspace',
        'detailKey': 'stream.phase.startupTools',
    }, task, now_ms=20_000)
    assert len(timeline['entries']) == 1

    # Distinct retry cycles force rows until the hard cap. Critical error
    # rows remain preferred while low-priority status history is reclaimed.
    for seq in range(400, 700):
        timeline = fold_activity_timeline(timeline, {
            'type': EventType.PHASE,
            'seq': seq,
            'phase': Phase.RETRYING,
            'detail': 'x' * 1_000,
            'detailKey': f'activity.test.phase.{seq}',
            'detailArgs': {'value': 'y' * 1_000},
            'statusCode': 429,
        }, task, now_ms=10_000 + seq)
    assert len(timeline['entries']) == ACTIVITY_TIMELINE_MAX_ENTRIES
    assert timeline['droppedCount'] > 0
    assert len(json.dumps(
        timeline, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8')) <= ACTIVITY_TIMELINE_MAX_JSON_BYTES


def test_separate_recovery_incidents_sum_backoff_not_wall_envelope():
    from lib.turn_activity_timeline import fold_activity_timeline

    # Semantic stream retries happen after a model request has closed, so they
    # can be minutes apart. Coalescing keeps cognition low (one row ×3), while
    # duration must remain the 0.8+1.3+2.1s recovery delay rather than 28m.
    task = _task()
    timeline = None
    for seq, now, backoff in (
            (1, 10_000, 0.8),
            (2, 550_000, 1.3),
            (3, 1_697_000, 2.1)):
        timeline = fold_activity_timeline(timeline, {
            'type': EventType.PHASE,
            'seq': seq,
            'phase': Phase.RETRYING,
            'detail': 'stream ended; retrying',
            'detailKey': 'stream.phase.streamInterruptedRetryDirect',
            'detailArgs': {'model': 'Kimi K3', 'attempt': seq, 'max': 16},
            'attempt': seq,
            'backoff_s': backoff,
            'model': 'kimi-k3',
        }, task, now_ms=now)

    assert timeline is not None
    assert len(timeline['entries']) == 1
    recovery = timeline['entries'][0]
    assert recovery['count'] == 3
    assert recovery['timingMode'] == 'occurrences'
    assert recovery['startedAt'] == 10_000
    assert recovery['endedAt'] == 1_697_000
    assert recovery['durationMs'] == 4_200


def test_normalization_enforces_byte_budget_and_preserves_newest_error():
    from lib.turn_activity_timeline import (
        ACTIVITY_TIMELINE_MAX_JSON_BYTES,
        normalize_activity_timeline,
    )

    entries = []
    for seq in range(128):
        critical = seq == 127
        entries.append({
            'id': f'activity:{seq}',
            'spanId': f'span:{seq}',
            'seq': seq,
            'occurredAt': 10_000 + seq,
            'kind': 'error' if critical else 'status',
            'status': 'failed' if critical else 'running',
            'severity': 'error' if critical else 'info',
            'summary': 's' * 180,
            'detail': 'd' * 400,
            'summaryArgs': {f'summary-{index}': 'x' * 160
                            for index in range(12)},
            'detailArgs': {f'detail-{index}': 'y' * 160
                           for index in range(12)},
            'count': 1,
        })

    timeline = normalize_activity_timeline({'entries': entries})

    assert timeline is not None
    assert timeline['entries'][-1]['id'] == 'activity:127'
    assert timeline['droppedCount'] > 0
    assert len(json.dumps(
        timeline, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8')) <= ACTIVITY_TIMELINE_MAX_JSON_BYTES

    # A new low-priority heartbeat must not evict older critical history just
    # because it happens to be the newest row.
    critical_entries = [{
        'id': f'error:{seq}',
        'spanId': f'error-span:{seq}',
        'seq': seq,
        'occurredAt': seq,
        'kind': 'error',
        'status': 'failed',
        'severity': 'error',
        'count': 1,
    } for seq in range(128)]
    normalized = normalize_activity_timeline({'entries': [
        *critical_entries,
        {
            'id': 'new-heartbeat',
            'spanId': 'new-heartbeat',
            'seq': 128,
            'occurredAt': 128,
            'kind': 'status',
            'status': 'running',
            'severity': 'info',
            'count': 1,
        },
    ]})
    assert normalized is not None
    assert len(normalized['entries']) == 128
    assert normalized['entries'] == critical_entries

    finite_json = normalize_activity_timeline({'entries': [{
        'id': 'non-finite',
        'spanId': 'non-finite',
        'seq': 129,
        'occurredAt': 129,
        'kind': 'status',
        'status': 'running',
        'severity': 'info',
        'summaryArgs': {
            'nan': float('nan'),
            'infinity': float('inf'),
            'api_token': 'provider-secret-value',
        },
        'count': 1,
    }]})
    assert finite_json is not None
    json.dumps(finite_json, allow_nan=False)
    assert finite_json['entries'][0]['summaryArgs']['api_token'] == '<redacted>'

    bounded_counters = normalize_activity_timeline({
        'droppedCount': 10 ** 100,
        'entries': [{
            'id': 'bounded-counters',
            'spanId': 'bounded-counters',
            'seq': 10 ** 100,
            'occurredAt': 10 ** 100,
            'kind': 'status',
            'status': 'running',
            'severity': 'info',
            'count': 10 ** 100,
            'reductionPercent': 10 ** 100,
        }],
    })
    assert bounded_counters is not None
    assert bounded_counters['droppedCount'] == 2_147_483_647
    assert bounded_counters['entries'][0]['count'] == 2_147_483_647
    assert bounded_counters['entries'][0]['seq'] == 9_007_199_254_740_991
    assert bounded_counters['entries'][0]['reductionPercent'] == 100


def test_terminal_error_closes_open_rows_and_success_done_resolves_warnings():
    from lib.turn_activity_timeline import fold_activity_timeline

    task = _task(_activeModelRequestSpan='model:attempt-a:1')
    timeline = None
    for event in (
        {
            'type': EventType.MODEL_REQUEST_START,
            'seq': 1,
            'emittedAt': 20_000,
            'spanId': 'model:attempt-a:1',
            'model': 'kimi-k3',
        },
        {
            'type': EventType.PHASE,
            'seq': 2,
            'emittedAt': 20_010,
            'phase': Phase.RETRYING,
            'detail': 'temporary transport problem',
            'statusCode': 429,
        },
        {
            'type': EventType.ERROR,
            'seq': 3,
            'emittedAt': 20_020,
            'content': 'one recoverable frame failed',
        },
    ):
        timeline = fold_activity_timeline(timeline, event, task)

    assert timeline is not None
    assert [entry['kind'] for entry in timeline['entries']] == [
        'status', 'error',
    ]
    assert [entry['status'] for entry in timeline['entries']] == [
        'failed', 'failed',
    ]

    success_timeline = fold_activity_timeline(None, {
        'type': EventType.MODEL_REQUEST_START,
        'seq': 4,
        'emittedAt': 30_000,
        'spanId': 'model:attempt-a:2',
        'model': 'kimi-k3',
    }, task)
    task['_activeModelRequestSpan'] = 'model:attempt-a:2'
    success_timeline = fold_activity_timeline(success_timeline, {
        'type': EventType.PHASE,
        'seq': 5,
        'emittedAt': 30_010,
        'phase': Phase.RETRYING,
        'detail': 'temporary rate limit',
        'statusCode': 429,
    }, task)
    success_timeline = fold_activity_timeline(success_timeline, {
        'type': EventType.DONE,
        'seq': 6,
        'emittedAt': 30_020,
        'finishReason': 'stop',
    }, task)

    assert success_timeline is not None
    assert len(success_timeline['entries']) == 1
    settled = success_timeline['entries'][0]
    assert settled['kind'] == 'status'
    assert settled['status'] == 'succeeded'
    assert settled['severity'] == 'warning'


def test_durable_error_detail_is_redacted_before_frontend_projection():
    from lib.turn_activity_timeline import fold_activity_timeline

    timeline = fold_activity_timeline(None, {
        'type': EventType.ERROR,
        'seq': 1,
        'emittedAt': 40_000,
        'content': 'Authorization: Bearer provider-secret-value',
    }, _task())

    assert timeline is not None
    detail = timeline['entries'][0]['detail']
    assert 'provider-secret-value' not in detail
    assert '<redacted>' in detail


def test_turn_authority_persists_and_replays_activity_timeline(chat_sidecar):
    from lib.turn_lifecycle import (
        bind_task,
        create_turn_pair,
        get_turn,
        read_events,
        record_task_event,
    )

    token = uuid.uuid4().hex[:10]
    conversation_id = f'activity-conv-{token}'
    task_id = f'activity-task-{token}'
    created = create_turn_pair(
        conversation_id,
        command_id=f'activity-pair-{token}',
        input_projection={'content': 'hello'},
        config={'model': 'kimi-k3'},
        user_id=1,
        conversation_defaults={
            'allowCreate': True,
            'title': 'activity replay',
            'settings': {},
        },
    )
    attempt_id = created['attempt']['attemptId']
    bind_task(attempt_id, task_id, user_id=1)
    task = {
        '_attemptId': attempt_id,
        '_userId': 1,
        '_activeModelRequestSpan': f'model:{attempt_id}:1',
        'id': task_id,
        'status': 'running',
        'content': '',
        'thinking': '',
        'toolRounds': [],
        'model': 'kimi-k3',
        'config': {'model': 'kimi-k3'},
    }
    start = {
        'type': EventType.MODEL_REQUEST_START,
        'seq': 0,
        'spanId': f'model:{attempt_id}:1',
        'model': 'kimi-k3',
        'emittedAt': 50_000,
    }
    rejected = {
        'type': EventType.TOOL_SCHEMA_REJECTED,
        'seq': 1,
        'toolName': 'write_file',
        'reasonCode': 'invalid_schema',
        'detail': '$.required description missing',
        'parentSpanId': f'model:{attempt_id}:1',
        'emittedAt': 50_010,
    }
    assert record_task_event(task, start) is True
    assert record_task_event(task, rejected) is True

    turn = get_turn(
        conversation_id, created['turn']['turnId'], user_id=1,
    )
    timeline = turn['projection']['activityTimeline']
    assert timeline['blockId'] == 'activity-timeline'
    assert [entry['status'] for entry in timeline['entries']] == ['skipped']

    replay = read_events(attempt_id, user_id=1)
    hydrated_tail = replay[-1]['payload']['projection']
    assert hydrated_tail['activityTimeline'] == timeline


def test_stream_boundary_emits_model_span_and_schema_isolation(monkeypatch):
    import lib.tasks_pkg.manager._stream as stream_module

    emitted = []
    monkeypatch.setattr(
        stream_module, 'append_event',
        lambda _task_value, event: emitted.append(event),
    )

    def dispatch(body, **_kwargs):
        body['_request_activity_sink']({
            'toolName': 'write_file',
            'stage': 'wire_preflight',
            'reasonCode': 'invalid_schema',
            'detail': '$.required description missing',
            'action': 'omitted',
        })
        return ({'role': 'assistant', 'content': 'ok'}, 'stop', {
            '_dispatch': {'provider_id': 'moonshot', 'model': 'kimi-k3'},
            '_network_route': {
                'routeId': 'pool:hk', 'routeMode': 'proxy',
                'decisionReason': 'proxy_pool',
            },
        })

    monkeypatch.setattr(stream_module, 'dispatch_stream', dispatch)
    task = {
        '_attemptId': 'attempt-stream',
        '_userId': 1,
        'id': 'task-stream',
        'status': 'running',
        'content': '',
        'thinking': '',
        'content_lock': threading.Lock(),
        'events_lock': threading.Lock(),
        'model': 'kimi-k3',
        'config': {},
    }
    body = {
        'model': 'kimi-k3',
        'messages': [{'role': 'user', 'content': 'hi'}],
    }

    message, finish_reason, _usage = stream_module.stream_llm_response(
        task, body, tag='R1',
    )

    assert message['content'] == 'ok'
    assert finish_reason == 'stop'
    assert [event['type'] for event in emitted] == [
        EventType.MODEL_REQUEST_START,
        EventType.PHASE,
        EventType.TOOL_SCHEMA_REJECTED,
        EventType.MODEL_REQUEST_COMPLETE,
    ]
    start, _waiting, isolated, complete = emitted
    assert start['spanId'] == isolated['parentSpanId'] == complete['spanId']
    assert isolated['toolName'] == 'write_file'
    assert complete['providerId'] == 'moonshot'
    assert complete['status'] == 'succeeded'
    assert complete['routeId'] == 'pool:hk'
    assert complete['routeMode'] == 'proxy'
    assert complete['routeDecision'] == 'proxy_pool'
    assert '_request_activity_sink' not in body


def test_stream_boundary_marks_returned_truncated_stream_failed(monkeypatch):
    import lib.tasks_pkg.manager._stream as stream_module

    emitted = []
    monkeypatch.setattr(
        stream_module, 'append_event',
        lambda _task_value, event: emitted.append(event),
    )
    monkeypatch.setattr(
        stream_module, 'dispatch_stream',
        lambda _body, **_kwargs: (
            {'role': 'assistant', 'content': '',
             'reasoning_content': 'unfinished'},
            'stop',
            {
                '_dispatch': {'provider_id': 'sankuai', 'model': 'kimi-k3'},
                '_missing_done': True,
                '_stream_anomaly': True,
                '_failure_stage': 'midstream_close',
                '_network_route': {
                    'routeId': 'direct:configured-bypass',
                    'routeMode': 'direct',
                    'decisionReason': 'configured_bypass',
                },
            },
        ),
    )
    task = {
        '_attemptId': 'attempt-stream-truncated', '_userId': 1,
        'id': 'task-stream-truncated', 'status': 'running',
        'content': '', 'thinking': '', 'content_lock': threading.Lock(),
        'events_lock': threading.Lock(), 'model': 'kimi-k3', 'config': {},
    }

    stream_module.stream_llm_response(task, {
        'model': 'kimi-k3',
        'messages': [{'role': 'user', 'content': 'hi'}],
    }, tag='R3')

    complete = emitted[-1]
    assert complete['type'] == EventType.MODEL_REQUEST_COMPLETE
    assert complete['status'] == 'failed'
    assert complete['errorKind'] == 'PrematureStreamClose'
    assert complete['routeId'] == 'direct:configured-bypass'
    assert complete['failureStage'] == 'midstream_close'


def test_stream_boundary_closes_failed_span_with_redacted_detail(monkeypatch):
    import lib.tasks_pkg.manager._stream as stream_module

    class BadRequestError(RuntimeError):
        status_code = 400

    emitted = []
    monkeypatch.setattr(
        stream_module, 'append_event',
        lambda _task_value, event: emitted.append(event),
    )

    def dispatch(_body, **_kwargs):
        raise BadRequestError(
            'Authorization: Bearer provider-secret-value request rejected',
        )

    monkeypatch.setattr(stream_module, 'dispatch_stream', dispatch)
    task = {
        '_attemptId': 'attempt-stream-failed',
        '_userId': 1,
        'id': 'task-stream-failed',
        'status': 'running',
        'content': '',
        'thinking': '',
        'content_lock': threading.Lock(),
        'events_lock': threading.Lock(),
        'model': 'kimi-k3',
        'config': {},
    }

    with pytest.raises(BadRequestError):
        stream_module.stream_llm_response(task, {
            'model': 'kimi-k3',
            'messages': [{'role': 'user', 'content': 'hi'}],
        }, tag='R2')

    complete = emitted[-1]
    assert complete['type'] == EventType.MODEL_REQUEST_COMPLETE
    assert complete['status'] == 'failed'
    assert complete['statusCode'] == 400
    assert complete['errorKind'] == 'BadRequestError'
    assert 'provider-secret-value' not in complete['errorDetail']
    assert '<redacted>' in complete['errorDetail']
    assert '_activeModelRequestSpan' not in task


def test_stream_boundary_cleans_activity_state_when_start_event_fails(monkeypatch):
    import lib.tasks_pkg.manager._stream as stream_module

    def reject_start(_task_value, event):
        if event.get('type') == EventType.MODEL_REQUEST_START:
            raise RuntimeError('turn authority rejected start')

    monkeypatch.setattr(stream_module, 'append_event', reject_start)
    monkeypatch.setattr(
        stream_module,
        'dispatch_stream',
        lambda *_args, **_kwargs: pytest.fail('dispatch must not start'),
    )
    task = {
        '_attemptId': 'attempt-start-rejected',
        '_userId': 1,
        'id': 'task-start-rejected',
        'status': 'running',
        'content': '',
        'thinking': '',
        'content_lock': threading.Lock(),
        'events_lock': threading.Lock(),
        'model': 'kimi-k3',
        'config': {},
    }
    body = {
        'model': 'kimi-k3',
        'messages': [{'role': 'user', 'content': 'hi'}],
    }

    with pytest.raises(RuntimeError, match='authority rejected start'):
        stream_module.stream_llm_response(task, body, tag='R1')

    assert '_activeModelRequestSpan' not in task
    assert '_request_activity_sink' not in body


def test_activity_timeline_never_enters_model_messages():
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages

    messages = _transform_messages([
        {'role': 'user', 'content': 'first request'},
        {
            'role': 'assistant',
            'content': 'visible answer',
            'activityTimeline': {
                'blockId': 'activity-timeline',
                'version': 1,
                'entries': [{
                    'id': 'secret-diagnostic',
                    'spanId': 'model:1',
                    'seq': 1,
                    'occurredAt': 1,
                    'kind': 'error',
                    'status': 'failed',
                    'severity': 'error',
                    'detail': 'provider-only diagnostic must stay display-only',
                    'count': 1,
                }],
            },
        },
        {'role': 'user', 'content': 'follow up'},
    ], {})

    wire = json.dumps(messages, ensure_ascii=False)
    assert 'visible answer' in wire
    assert 'activityTimeline' not in wire
    assert 'secret-diagnostic' not in wire
    assert 'provider-only diagnostic' not in wire


def test_normalize_whitelist_stays_declared_in_the_sync_contract():
    """Normalize-surviving fields must exist in the closed-world sync schema.

    TurnActivityEntry is additionalProperties:false and the snapshot route
    validates before responding — an undeclared field makes every sync 500.
    """
    from pathlib import Path

    import yaml

    from lib import turn_activity_timeline as timeline

    contract = yaml.safe_load(
        (Path(__file__).resolve().parents[1]
         / 'contracts/conversation_sync_v3.yaml').read_text(encoding='utf-8')
    )
    properties = (
        contract['components']['schemas']['TurnActivityEntry']['properties']
    )
    missing = sorted(set(timeline._ENTRY_FIELDS) - set(properties))
    assert not missing, f'TurnActivityEntry schema missing fields: {missing}'
    mismatched = {
        field: (limit, properties[field].get('maxLength'))
        for field, limit in timeline._STRING_LIMITS.items()
        if field in properties
        and properties[field].get('maxLength') is not None
        and properties[field]['maxLength'] != limit
    }
    assert not mismatched, f'maxLength drift normalize vs contract: {mismatched}'
