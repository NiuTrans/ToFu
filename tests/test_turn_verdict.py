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
