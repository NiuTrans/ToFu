"""Executable state matrix for provider-stream and durable turn semantics."""

from __future__ import annotations

import pytest

from lib.llm.stream_result import (
    ProviderStreamResult,
    ProviderStreamState,
    UnverifiedProviderStreamError,
    require_verified_provider_stream_result,
)
from lib.tasks_pkg.stream_handler._decision import (
    RecoveryAction,
    RecoveryDecision,
)
from lib.turn_verdict import (
    TurnOutcome,
    TurnStatus,
    TurnTerminalEvidence,
    TurnTerminationEvidence,
    TurnVerdict,
    derive_provider_stream_verdict,
    derive_task_verdict,
    derive_turn_verdict,
    normalize_turn_settlement,
    terminal_finish_reason,
)


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ('stream_state', 'finish_reason', 'has_error', 'aborted',
     'expected_status', 'expected_evidence'),
    [
        (ProviderStreamState.PROVIDER_FINISHED, 'stop', False, False,
         'completed', TurnTerminationEvidence.PROVIDER_FINISH),
        (ProviderStreamState.PROVIDER_FINISHED, 'tool_calls', False, False,
         'completed', TurnTerminationEvidence.PROVIDER_FINISH),
        (None, 'stop', False, False,
         'completed', TurnTerminationEvidence.LEGACY_FINISH),
        (None, 'tool_use', False, False,
         'completed', TurnTerminationEvidence.LEGACY_FINISH),
        (ProviderStreamState.PREMATURE_CLOSE, 'stop', False, False,
         'failed', TurnTerminationEvidence.PROVIDER_STREAM_FAILURE),
        (ProviderStreamState.MALFORMED_STREAM, 'stop', False, False,
         'failed', TurnTerminationEvidence.PROVIDER_STREAM_FAILURE),
        (ProviderStreamState.EMPTY_RESPONSE, 'stop', False, False,
         'failed', TurnTerminationEvidence.PROVIDER_STREAM_FAILURE),
        (ProviderStreamState.UNKNOWN, 'stop', False, False,
         'failed', TurnTerminationEvidence.MISSING_COMPLETION_EVIDENCE),
        (ProviderStreamState.UNKNOWN, 'error', True, False,
         'failed', TurnTerminationEvidence.GENERATION_ERROR),
        (ProviderStreamState.PROVIDER_FINISHED, 'length', False, False,
         'truncated', TurnTerminationEvidence.PROVIDER_LIMIT),
        (ProviderStreamState.PROVIDER_FINISHED, 'stop', True, False,
         'failed', TurnTerminationEvidence.GENERATION_ERROR),
        (ProviderStreamState.PROVIDER_FINISHED, 'stop', False, True,
         'interrupted', TurnTerminationEvidence.USER_ABORT),
        (None, '', False, False,
         'failed', TurnTerminationEvidence.MISSING_COMPLETION_EVIDENCE),
    ],
)
def test_turn_verdict_state_matrix(
    stream_state,
    finish_reason,
    has_error,
    aborted,
    expected_status,
    expected_evidence,
):
    verdict = derive_turn_verdict(TurnTerminalEvidence(
        event_type='done',
        finish_reason=finish_reason,
        stream_state=stream_state,
        has_error=has_error,
        aborted=aborted,
        task_status='done',
    ))

    assert verdict.status.value == expected_status
    assert verdict.outcome.value == expected_status
    assert verdict.evidence is expected_evidence


def test_bare_done_event_cannot_default_to_completed_settlement():
    from lib.turn_lifecycle import _settlement

    status, settlement = _settlement(
        {'status': 'done', 'content': 'looks complete', 'config': {}},
        {'type': 'done'},
        {'content': 'looks complete', 'toolRounds': []},
    )

    assert status == 'failed'
    assert settlement['outcome'] == 'failed'
    assert settlement['cause'] == 'completion_evidence_missing'
    assert settlement['evidence'] == 'missing_completion_evidence'
    assert settlement['streamState'] is None
    assert settlement['error']['kind'] == 'internal'


def test_explicit_empty_event_evidence_cannot_borrow_task_success():
    """Persistence and exporters must share event-over-task precedence."""
    from lib.turn_lifecycle import _settlement

    status, settlement = _settlement(
        {
            'status': 'done',
            'finishReason': 'stop',
            'streamState': 'provider_finished',
            'content': 'looks complete',
            'config': {},
        },
        {'type': 'done', 'finishReason': '', 'streamState': ''},
        {'content': 'looks complete', 'toolRounds': []},
    )

    assert status == 'failed'
    assert settlement['cause'] == 'completion_evidence_missing'
    assert settlement['providerFinishReason'] is None
    assert settlement['streamState'] is None


@pytest.mark.parametrize(
    ('task', 'expected'),
    [
        ({'status': 'done', 'finishReason': 'stop'}, 'stop'),
        ({'status': 'done'}, 'error'),
        ({'status': 'error'}, 'error'),
        ({'status': 'aborted'}, 'aborted'),
        ({'status': 'truncated'}, 'incomplete'),
    ],
)
def test_terminal_finish_reason_never_defaults_missing_evidence_to_stop(
    task,
    expected,
):
    assert terminal_finish_reason(task) == expected


def test_explicit_stream_state_survives_into_settlement():
    from lib.turn_lifecycle import _settlement

    status, settlement = _settlement(
        {
            'status': 'done',
            'finishReason': 'premature_close',
            'streamState': 'malformed_stream',
            'content': 'safe prefix',
            'model': 'gpt-4o',
            'config': {'model': 'gpt-4o'},
        },
        {
            'type': 'done',
            'finishReason': 'premature_close',
            'streamState': 'malformed_stream',
        },
        {'content': 'safe prefix', 'toolRounds': []},
    )

    assert status == 'failed'
    assert settlement['streamState'] == 'malformed_stream'
    assert settlement['evidence'] == 'provider_stream_failure'
    assert settlement['error']['kind'] == 'premature_close'


def test_historical_settlement_projection_has_closed_public_shape():
    settlement = normalize_turn_settlement(
        {'outcome': 'completed', 'cause': 'ingested'},
        status='completed',
    )

    assert settlement == {
        'outcome': 'completed',
        'cause': 'ingested',
        'providerFinishReason': None,
        'error': None,
        'resumeOptions': [],
        'streamState': None,
        'evidence': 'external_authority',
    }


def test_historical_completed_premature_close_is_repaired_fail_closed():
    settlement = normalize_turn_settlement(
        {
            'outcome': 'completed',
            'cause': 'provider_finished',
            'providerFinishReason': 'premature_close',
            'resumeOptions': [{'action': 'regenerate'}],
        },
        status='completed',
    )

    assert settlement['outcome'] == 'failed'
    assert settlement['cause'] == 'provider_stream_error'
    assert settlement['streamState'] == 'premature_close'
    assert settlement['evidence'] == 'provider_stream_failure'
    assert settlement['error']['kind'] == 'premature_close'
    assert settlement['resumeOptions'] == [{'action': 'regenerate'}]


@pytest.mark.parametrize(
    ('settlement_input', 'expected_cause', 'expected_evidence'),
    [
        (
            {'outcome': 'completed', 'cause': 'generation_error',
             'error': {'kind': 'generic', 'message': 'provider failed'}},
            'generation_error',
            'generation_error',
        ),
        (
            {'outcome': 'completed', 'cause': 'provider_finished',
             'streamState': 'unknown'},
            'completion_evidence_missing',
            'missing_completion_evidence',
        ),
    ],
)
def test_any_explicit_historical_failure_overrides_completed(
    settlement_input,
    expected_cause,
    expected_evidence,
):
    settlement = normalize_turn_settlement(
        settlement_input,
        status='completed',
    )

    assert settlement['outcome'] == 'failed'
    assert settlement['cause'] == expected_cause
    assert settlement['evidence'] == expected_evidence
    assert settlement['error'] is not None


def test_legacy_stream_adapter_accepts_typed_state_in_usage():
    result = ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': 'safe prefix'},
        'stop',
        {'_stream_state': ProviderStreamState.PREMATURE_CLOSE},
    )

    assert result.state is ProviderStreamState.PREMATURE_CLOSE
    assert result.is_verified_complete is False


def test_legacy_stream_adapter_projects_its_inferred_closed_state():
    result = ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': 'complete'},
        'stop',
        {'_missing_done': True},
    )

    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.usage['_stream_state'] == 'provider_finished'
    assert result.usage['_missing_done'] is True


def test_stream_result_keeps_tuple_compatibility_without_faking_evidence():
    result = ProviderStreamResult(
        message={'role': 'assistant', 'content': 'safe prefix'},
        compatibility_finish_reason='stop',
        usage={'_stream_state': 'premature_close'},
        state=ProviderStreamState.PREMATURE_CLOSE,
        provider_finish_reason=None,
    )

    message, finish_reason, usage = result
    assert message['content'] == 'safe prefix'
    assert finish_reason == 'stop'
    assert usage['_stream_state'] == 'premature_close'
    assert result.provider_finish_reason is None
    assert result.is_verified_complete is False


def test_done_event_projects_typed_stream_state():
    from lib.tasks_pkg.orchestrator._finalize import _build_done_event_base

    stream_result = ProviderStreamResult(
        message={'role': 'assistant', 'content': 'safe prefix'},
        compatibility_finish_reason='stop',
        usage={},
        state=ProviderStreamState.MALFORMED_STREAM,
        malformed_frame_count=1,
    )
    task = {'id': 'task-stream-state', 'preset': 'medium'}

    event = _build_done_event_base(
        task,
        last_finish_reason='premature_close',
        last_stream_result=stream_result,
        accumulated_usage={},
        last_usage={},
        model='gpt-4o',
        thinking_depth=None,
    )

    assert event['type'] == 'done'
    assert event['finishReason'] == 'premature_close'
    assert event['streamState'] == 'malformed_stream'


def test_done_event_keeps_plan_advisory_and_preserves_explicit_wait():
    from lib.tasks_pkg.orchestrator._finalize import _build_done_event_base

    snapshot = {
        'version': 3, 'update_count': 1, 'explanation': '',
        'todos': [{'content': 'Deferred verification', 'status': 'pending'}],
    }
    task = {
        'id': 'task-advisory-plan', '_todoState': snapshot,
        '_todo_blocked': {'reason': 'historical-checklist-gate'},
        '_waiting_on': {'kind': 'swarm', 'swarmKey': 'owned-wave'},
    }
    event = _build_done_event_base(
        task, last_finish_reason='stop', last_stream_result=None,
        accumulated_usage={}, last_usage={}, model='test', thinking_depth=None,
    )
    assert event['finishReason'] == 'stop'
    assert event['todoState'] == snapshot
    assert event['waitingOn'] == task['_waiting_on']
    assert 'todoBlocked' not in event
    event['todoState']['todos'].clear()
    assert task['_todoState']['todos']


def test_stream_result_rejects_internally_contradictory_evidence():
    with pytest.raises(ValueError, match='observed provider finish'):
        ProviderStreamResult(
            message={'role': 'assistant', 'content': 'looks complete'},
            compatibility_finish_reason='stop',
            usage={},
            state=ProviderStreamState.PROVIDER_FINISHED,
        )
    with pytest.raises(ValueError, match='at least one malformed frame'):
        ProviderStreamResult(
            message={'role': 'assistant', 'content': 'partial'},
            compatibility_finish_reason='stop',
            usage={},
            state=ProviderStreamState.MALFORMED_STREAM,
        )
    with pytest.raises(ValueError, match='cannot carry an observed'):
        ProviderStreamResult(
            message={'role': 'assistant', 'content': 'partial'},
            compatibility_finish_reason='stop',
            usage={},
            state=ProviderStreamState.PREMATURE_CLOSE,
            provider_finish_reason='stop',
            saw_finish_reason=True,
        )


def test_with_usage_keeps_typed_stream_state_authoritative():
    result = ProviderStreamResult(
        message={'role': 'assistant', 'content': 'complete'},
        compatibility_finish_reason='stop',
        usage={},
        state=ProviderStreamState.PROVIDER_FINISHED,
        provider_finish_reason='stop',
        saw_finish_reason=True,
    )

    updated = result.with_usage({'_stream_state': 'premature_close'})

    assert updated.state is ProviderStreamState.PROVIDER_FINISHED
    assert updated.usage['_stream_state'] == 'provider_finished'


def test_closed_semantic_results_reject_untyped_enum_values():
    with pytest.raises(TypeError, match='ProviderStreamState'):
        ProviderStreamResult(
            message={'role': 'assistant', 'content': 'partial'},
            compatibility_finish_reason='stop',
            usage={},
            state='premature_close',  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match='TurnStatus'):
        TurnVerdict(
            status='failed',  # type: ignore[arg-type]
            outcome=TurnOutcome.FAILED,
            cause='provider_stream_error',
            evidence=TurnTerminationEvidence.PROVIDER_STREAM_FAILURE,
        )
    with pytest.raises(TypeError, match='TurnOutcome'):
        TurnVerdict(
            status=TurnStatus.FAILED,
            outcome='failed',  # type: ignore[arg-type]
            cause='provider_stream_error',
            evidence=TurnTerminationEvidence.PROVIDER_STREAM_FAILURE,
        )
    with pytest.raises(TypeError, match='RecoveryAction'):
        RecoveryDecision(
            action='continue',  # type: ignore[arg-type]
            loop_exit_reason=None,
            abort_detected_phase=None,
            premature_retry_count=1,
            last_finish_reason='premature_close',
            stream_state=ProviderStreamState.PREMATURE_CLOSE,
        )
    with pytest.raises(TypeError, match='ProviderStreamState'):
        RecoveryDecision(
            action=RecoveryAction.CONTINUE,
            loop_exit_reason=None,
            abort_detected_phase=None,
            premature_retry_count=1,
            last_finish_reason='premature_close',
            stream_state='premature_close',  # type: ignore[arg-type]
        )


def test_task_terminal_adapter_uses_event_evidence_and_fails_closed():
    task = {
        'status': 'done',
        'finishReason': 'stop',
        'streamState': 'provider_finished',
    }
    verdict = derive_task_verdict(task, {
        'type': 'done',
        'finishReason': 'premature_close',
        'streamState': 'malformed_stream',
    })

    assert verdict.status is TurnStatus.FAILED
    assert verdict.cause == 'provider_stream_error'


def test_task_terminal_adapter_never_invents_missing_stop():
    verdict = derive_task_verdict({'status': 'done', 'content': 'looks done'})

    assert verdict.status is TurnStatus.FAILED
    assert verdict.cause == 'completion_evidence_missing'


def test_provider_result_adoption_requires_positive_finish_evidence():
    partial = ProviderStreamResult(
        message={'role': 'assistant', 'content': 'safe prefix'},
        compatibility_finish_reason='stop',
        usage={},
        state=ProviderStreamState.PREMATURE_CLOSE,
    )

    with pytest.raises(UnverifiedProviderStreamError, match='premature_close'):
        require_verified_provider_stream_result(
            partial, context='durable consumer')
    assert derive_provider_stream_verdict(partial).status is TurnStatus.FAILED

    complete = ProviderStreamResult(
        message={'role': 'assistant', 'content': 'complete'},
        compatibility_finish_reason='stop',
        usage={},
        state=ProviderStreamState.PROVIDER_FINISHED,
        provider_finish_reason='stop',
        saw_finish_reason=True,
    )
    assert require_verified_provider_stream_result(complete) is complete


@pytest.mark.parametrize(
    'fence_reason',
    [
        'storage_authority_integrity',
        'storage_frame_overflow',
        'turn_attempt_stale_fence',
        'conversation_attempt_stale_fence',
    ],
)
def test_internal_fence_abort_is_never_a_user_abort(fence_reason):
    verdict = derive_turn_verdict(TurnTerminalEvidence(
        event_type='done',
        finish_reason='aborted',
        stream_state=ProviderStreamState.PROVIDER_FINISHED,
        has_error=False,
        aborted=True,
        task_status='done',
        abort_reason=fence_reason,
    ))

    assert verdict.status is TurnStatus.FAILED
    assert verdict.cause == 'internal_fence_abort'
    assert verdict.evidence is TurnTerminationEvidence.EXTERNAL_AUTHORITY


def test_non_fence_abort_reason_keeps_user_abort_verdict():
    verdict = derive_turn_verdict(TurnTerminalEvidence(
        event_type='done',
        finish_reason='aborted',
        stream_state=None,
        has_error=False,
        aborted=True,
        task_status='done',
        abort_reason='superseded_by_new_task',
    ))

    assert verdict.status is TurnStatus.INTERRUPTED
    assert verdict.cause == 'user_abort'


def test_task_terminal_adapter_carries_internal_fence_stamp():
    verdict = derive_task_verdict({
        'status': 'done',
        'aborted': True,
        'finishReason': 'aborted',
        '_abort_reason': 'storage_authority_integrity',
    })

    assert verdict.status is TurnStatus.FAILED
    assert verdict.cause == 'internal_fence_abort'


def test_internal_fence_settlement_publishes_storage_fence_error():
    from lib.turn_lifecycle import _settlement

    status, settlement = _settlement(
        {
            'status': 'done',
            'aborted': True,
            'finishReason': 'error',
            '_abort_reason': 'storage_authority_integrity',
            'config': {},
        },
        {'type': 'done'},
        {'content': 'partial answer', 'toolRounds': []},
    )

    assert status == 'failed'
    assert settlement['outcome'] == 'failed'
    assert settlement['cause'] == 'internal_fence_abort'
    assert settlement['evidence'] == 'external_authority'
    assert settlement['error']['kind'] == 'storage_fence'
    assert settlement['error']['retryable'] is True


@pytest.mark.parametrize('reason', ['stalled', 'deadline', 'execution_deadline'])
def test_root_internal_invocation_stop_is_failed_not_user_abort(reason):
    import json
    import time
    from threading import Event
    from lib.tasks_pkg.invocation import invocation_abort_requested
    from lib.swarm.liveness import ProgressBeacon
    from lib.turn_lifecycle import _settlement

    task = {'id': 'root-guard', 'abort_event': Event(), 'config': {},
            'content': 'Safe partial output', 'usage': {'total_tokens': 23},
            'toolRounds': [{'status': 'done', 'result': 'committed receipt'}]}
    if reason == 'stalled':
        beacon = ProgressBeacon(stall_timeout=0)
        beacon.touch(task['id'], 'secret provider note must not escape')
        task['_invocationProgressBeacon'] = beacon
    elif reason == 'execution_deadline':
        task['config']['executionLimits'] = {'deadline': time.time() - 1}
    else:
        task['_invocationDeadline'] = time.monotonic() - 1
    assert invocation_abort_requested(task)
    assert task['abort_event'].is_set() and task['aborted']
    task.update(status='aborted', finishReason='aborted', streamState='client_aborted')
    event = {'type': 'aborted', 'finishReason': 'aborted', 'streamState': 'client_aborted'}
    verdict = derive_task_verdict(task, event)
    cause = 'invocation_stalled' if reason == 'stalled' else 'invocation_deadline'
    assert verdict.status.value == 'failed' and verdict.cause == cause
    status, settlement = _settlement(task, event, {
        'content': task['content'], 'toolRounds': task['toolRounds']})
    assert status == 'failed' and settlement['cause'] == cause
    assert settlement['streamState'] == 'client_aborted'
    assert settlement['providerFinishReason'] == 'aborted'
    assert settlement['error']
    cold = json.loads(json.dumps(settlement))
    assert cold['error'] == settlement['error']
    assert 'secret provider note' not in json.dumps(cold)
    assert task['usage']['total_tokens'] == 23
    assert task['toolRounds'][0]['result'] == 'committed receipt'
    # Repeated abort polls cannot change cause or recapture diagnostics.
    error = task['error']
    assert invocation_abort_requested(task) and task['error'] is error


@pytest.mark.parametrize('inherited', [True, False])
def test_explicit_stop_wins_when_guard_deadline_is_already_due(inherited):
    import time
    from threading import Event
    from lib.tasks_pkg.invocation import invocation_abort_requested
    task = {'id': 'user-stop', 'aborted': not inherited, 'abort_event': Event(),
            '_invocationAbortCheck': lambda: inherited,
            '_invocationDeadline': time.monotonic() - 1}
    assert invocation_abort_requested(task)
    assert '_invocationStopReason' not in task and not task.get('error')
    assert derive_task_verdict(task).cause == 'user_abort'
    assert derive_task_verdict(task).status.value == 'interrupted'
