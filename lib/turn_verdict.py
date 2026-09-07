"""Pure, fail-closed turn verdict derivation.

Responsibility
--------------
Convert explicit terminal evidence into one closed turn status/outcome.  This
module has no storage or event side effects; ``lib.turn_lifecycle`` owns error
normalization, resume options, and persistence around the derived verdict.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState


class TurnStatus(str, Enum):
    COMPLETED = 'completed'
    INTERRUPTED = 'interrupted'
    TRUNCATED = 'truncated'
    FAILED = 'failed'


class TurnOutcome(str, Enum):
    COMPLETED = 'completed'
    INTERRUPTED = 'interrupted'
    TRUNCATED = 'truncated'
    FAILED = 'failed'


class TurnTerminationEvidence(str, Enum):
    PROVIDER_FINISH = 'provider_finish'
    LEGACY_FINISH = 'legacy_finish'
    PROVIDER_LIMIT = 'provider_limit'
    USER_ABORT = 'user_abort'
    PROVIDER_STREAM_FAILURE = 'provider_stream_failure'
    GENERATION_ERROR = 'generation_error'
    MISSING_COMPLETION_EVIDENCE = 'missing_completion_evidence'
    EXTERNAL_AUTHORITY = 'external_authority'
    SYSTEM_RECOVERY = 'system_recovery'


_SUCCESS_FINISH_REASONS = frozenset({
    'stop', 'done', 'completed', 'end_turn',
    'tool_calls', 'tool_use', 'function_call',
})
_TRUNCATED_FINISH_REASONS = frozenset({
    'length', 'max_tokens', 'context_length', 'content_filter',
    'budget_exceeded', 'incomplete', 'max_turns',
})
_INTERRUPTED_FINISH_REASONS = frozenset({'aborted', 'interrupted'})
_FAILED_FINISH_REASONS = frozenset({
    'error', 'timeout', 'premature_close', 'abnormal_stop',
})
_FAILED_STREAM_STATES = frozenset({
    ProviderStreamState.PREMATURE_CLOSE,
    ProviderStreamState.MALFORMED_STREAM,
    ProviderStreamState.SEMANTIC_PROGRESS_TIMEOUT,
    ProviderStreamState.NO_ACTIONABLE_OUTPUT,
    ProviderStreamState.EMPTY_RESPONSE,
    ProviderStreamState.TOOL_PAYLOAD_MISSING,
})
_FAILED_SETTLEMENT_CAUSES = frozenset({
    'provider_stream_error',
    'generation_error',
    'completion_evidence_missing',
})
_FAILED_SETTLEMENT_EVIDENCE = frozenset({
    TurnTerminationEvidence.PROVIDER_STREAM_FAILURE.value,
    TurnTerminationEvidence.GENERATION_ERROR.value,
    TurnTerminationEvidence.MISSING_COMPLETION_EVIDENCE.value,
})


@dataclass(frozen=True, slots=True)
class TurnTerminalEvidence:
    event_type: str
    finish_reason: str
    stream_state: ProviderStreamState | None
    has_error: bool
    aborted: bool
    task_status: str


@dataclass(frozen=True, slots=True)
class TurnVerdict:
    status: TurnStatus
    outcome: TurnOutcome
    cause: str
    evidence: TurnTerminationEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.status, TurnStatus):
            raise TypeError('status must be a TurnStatus')
        if not isinstance(self.outcome, TurnOutcome):
            raise TypeError('outcome must be a TurnOutcome')
        if not isinstance(self.evidence, TurnTerminationEvidence):
            raise TypeError('evidence must be a TurnTerminationEvidence')
        if self.status.value != self.outcome.value:
            raise ValueError('turn status and outcome must describe one verdict')
        if self.status is TurnStatus.COMPLETED and self.evidence not in {
            TurnTerminationEvidence.PROVIDER_FINISH,
            TurnTerminationEvidence.LEGACY_FINISH,
        }:
            raise ValueError('completed turns require positive finish evidence')


class TerminalTaskFailure(RuntimeError):
    """A terminal consumer attempted to publish a failed task as output."""

    def __init__(self, verdict: TurnVerdict):
        self.verdict = verdict
        super().__init__(
            'The model turn ended without a verified completion '
            f'({verdict.cause}).')


def parse_provider_stream_state(value: object) -> ProviderStreamState | None:
    """Parse an optional wire value; invalid explicit values fail closed."""
    if value is None or value == '':
        return None
    if isinstance(value, ProviderStreamState):
        return value
    try:
        return ProviderStreamState(str(value))
    except ValueError:
        return ProviderStreamState.UNKNOWN


def terminal_finish_reason(task: Mapping[str, object]) -> str:
    """Project a terminal task's finish reason without inventing success.

    Task-native surfaces historically used ``or 'stop'`` when the producer
    omitted its terminal evidence. That turns a contract failure into a
    successful answer. Explicit provider/internal values pass through; only a
    missing value is derived from an unambiguous terminal status, otherwise it
    fails closed as ``error``.
    """
    explicit = task.get('finishReason')
    if explicit is None:
        explicit = task.get('finish_reason')
    if explicit is not None and str(explicit):
        return str(explicit)
    status = str(task.get('status') or '')
    if status in {'aborted', 'interrupted', 'superseded'}:
        return 'aborted'
    if status == 'truncated':
        return 'incomplete'
    return 'error'


def derive_turn_verdict(evidence: TurnTerminalEvidence) -> TurnVerdict:
    """Derive a verdict without any default-success branch."""
    finish = evidence.finish_reason
    stream_state = evidence.stream_state

    if (evidence.event_type == 'aborted' or evidence.aborted
            or evidence.task_status == 'aborted'
            or stream_state is ProviderStreamState.CLIENT_ABORTED
            or finish in _INTERRUPTED_FINISH_REASONS):
        return TurnVerdict(
            TurnStatus.INTERRUPTED,
            TurnOutcome.INTERRUPTED,
            'user_abort',
            TurnTerminationEvidence.USER_ABORT,
        )
    if stream_state in _FAILED_STREAM_STATES or finish in {
            'premature_close', 'abnormal_stop'}:
        return TurnVerdict(
            TurnStatus.FAILED,
            TurnOutcome.FAILED,
            'provider_stream_error',
            TurnTerminationEvidence.PROVIDER_STREAM_FAILURE,
        )
    if (evidence.has_error or evidence.event_type == 'error'
            or evidence.task_status == 'error'
            or finish in _FAILED_FINISH_REASONS):
        return TurnVerdict(
            TurnStatus.FAILED,
            TurnOutcome.FAILED,
            'generation_error',
            TurnTerminationEvidence.GENERATION_ERROR,
        )
    if finish in _TRUNCATED_FINISH_REASONS:
        return TurnVerdict(
            TurnStatus.TRUNCATED,
            TurnOutcome.TRUNCATED,
            finish,
            TurnTerminationEvidence.PROVIDER_LIMIT,
        )
    if (stream_state is ProviderStreamState.PROVIDER_FINISHED
            and finish in _SUCCESS_FINISH_REASONS):
        return TurnVerdict(
            TurnStatus.COMPLETED,
            TurnOutcome.COMPLETED,
            'provider_finished',
            TurnTerminationEvidence.PROVIDER_FINISH,
        )
    if stream_state is None and finish in _SUCCESS_FINISH_REASONS:
        # Explicit compatibility seam for historical/non-stream producers.
        # Unlike the old catch-all branch this still requires a terminal
        # finish reason; a bare ``done`` frame cannot manufacture success.
        return TurnVerdict(
            TurnStatus.COMPLETED,
            TurnOutcome.COMPLETED,
            'provider_finished',
            TurnTerminationEvidence.LEGACY_FINISH,
        )
    return TurnVerdict(
        TurnStatus.FAILED,
        TurnOutcome.FAILED,
        'completion_evidence_missing',
        TurnTerminationEvidence.MISSING_COMPLETION_EVIDENCE,
    )


def derive_provider_stream_verdict(
    result: ProviderStreamResult,
    *,
    has_error: bool = False,
    aborted: bool = False,
    task_status: str = 'done',
) -> TurnVerdict:
    """Project typed provider evidence into the canonical turn verdict."""
    if not isinstance(result, ProviderStreamResult):
        raise TypeError('result must be a ProviderStreamResult')
    return derive_turn_verdict(TurnTerminalEvidence(
        event_type='done',
        finish_reason=result.provider_finish_reason or '',
        stream_state=result.state,
        has_error=has_error,
        aborted=aborted,
        task_status=task_status,
    ))


def task_terminal_evidence(
    task: Mapping[str, object],
    terminal_event: Mapping[str, object] | None = None,
) -> TurnTerminalEvidence:
    """Extract one authoritative evidence record from task/event wire data.

    UI persistence, in-process callers, and SDK compatibility adapters all
    observe the same task but historically interpreted it independently.  In
    particular, several exporters turned a missing finish reason into
    ``stop``.  Keep wire precedence in this function so all consumers feed the
    exact same evidence into :func:`derive_turn_verdict`.

    An explicitly present event field is authoritative even when empty; this
    makes contradictory terminal frames fail closed instead of borrowing a
    more convenient value from the task snapshot.
    """
    event = terminal_event if isinstance(terminal_event, Mapping) else {}

    if 'finishReason' in event:
        finish_reason = str(event.get('finishReason') or '')
    elif 'finish_reason' in event:
        finish_reason = str(event.get('finish_reason') or '')
    else:
        task_finish = task.get('finishReason')
        if task_finish is None:
            task_finish = task.get('finish_reason')
        finish_reason = str(task_finish or '')

    if 'streamState' in event:
        raw_stream_state = event.get('streamState')
    elif 'stream_state' in event:
        raw_stream_state = event.get('stream_state')
    else:
        raw_stream_state = task.get('streamState')
        if raw_stream_state is None:
            raw_stream_state = task.get('stream_state')

    return TurnTerminalEvidence(
        event_type=str(event.get('type') or 'done'),
        finish_reason=finish_reason,
        stream_state=parse_provider_stream_state(raw_stream_state),
        has_error=bool(event.get('error') or task.get('error')),
        aborted=bool(task.get('aborted')),
        task_status=str(task.get('status') or ''),
    )


def derive_task_verdict(
    task: Mapping[str, object],
    terminal_event: Mapping[str, object] | None = None,
) -> TurnVerdict:
    """Derive the canonical verdict for a task-facing terminal consumer."""
    return derive_turn_verdict(task_terminal_evidence(task, terminal_event))


def require_deliverable_task(
    task: Mapping[str, object],
    terminal_event: Mapping[str, object] | None = None,
) -> TurnVerdict:
    """Return the canonical terminal verdict or reject false success.

    Truncated and explicitly interrupted outputs remain representable by
    transport adapters. A failed verdict must use their error channel.
    """
    verdict = derive_task_verdict(task, terminal_event)
    if verdict.status is TurnStatus.FAILED:
        raise TerminalTaskFailure(verdict)
    return verdict


def normalize_turn_settlement(
    value: object,
    *,
    status: str,
) -> dict[str, object]:
    """Backfill the public settlement shape for historical/external rows.

    This is a compatibility projection, not verdict derivation. New live model
    turns are produced by :func:`derive_turn_verdict`; manually ingested and
    historical rows retain their cause while gaining the required closed-shape
    fields used by generated clients.
    """
    settlement = dict(value) if isinstance(value, Mapping) else {}
    default_outcome = (
        TurnOutcome.INTERRUPTED.value if status == 'superseded'
        else status or TurnOutcome.FAILED.value
    )
    outcome = str(settlement.get('outcome') or default_outcome)
    if outcome not in {item.value for item in TurnOutcome}:
        outcome = TurnOutcome.FAILED.value
    cause = str(settlement.get('cause') or 'ingested')
    finish_reason = str(settlement.get('providerFinishReason') or '')
    stream_state = parse_provider_stream_state(settlement.get('streamState'))
    raw_evidence = str(settlement.get('evidence') or '')
    historical_stream_failure = (
        finish_reason in {'premature_close', 'abnormal_stop'}
        or stream_state in _FAILED_STREAM_STATES
        or cause == 'provider_stream_error'
        or raw_evidence == TurnTerminationEvidence.PROVIDER_STREAM_FAILURE.value
    )
    historical_completion_missing = (
        stream_state is ProviderStreamState.UNKNOWN
        or cause == 'completion_evidence_missing'
        or raw_evidence == TurnTerminationEvidence.MISSING_COMPLETION_EVIDENCE.value
    )
    historical_generation_failure = (
        bool(settlement.get('error'))
        or finish_reason in {'error', 'timeout'}
        or cause == 'generation_error'
        or raw_evidence == TurnTerminationEvidence.GENERATION_ERROR.value
    )
    # Compatibility repair for rows written by the old catch-all success
    # branch (the mt9lvcgir9n62o incident shape). The durable bytes remain
    # untouched; the public authority projection stops repeating a verdict
    # that is self-contradictory on its face.
    contradictory_completed = (
        outcome == TurnOutcome.COMPLETED.value
        and (
            historical_stream_failure
            or historical_completion_missing
            or historical_generation_failure
            or cause in _FAILED_SETTLEMENT_CAUSES
            or raw_evidence in _FAILED_SETTLEMENT_EVIDENCE
        )
    )
    if contradictory_completed:
        outcome = TurnOutcome.FAILED.value
        if historical_stream_failure:
            cause = 'provider_stream_error'
            evidence = TurnTerminationEvidence.PROVIDER_STREAM_FAILURE
            if stream_state is None:
                stream_state = ProviderStreamState.PREMATURE_CLOSE
            settlement['streamState'] = stream_state.value
            error_kind = (
                finish_reason
                if finish_reason in {'premature_close', 'abnormal_stop'}
                else 'premature_close'
            )
        elif historical_completion_missing:
            cause = 'completion_evidence_missing'
            evidence = TurnTerminationEvidence.MISSING_COMPLETION_EVIDENCE
            error_kind = 'internal'
        else:
            cause = 'generation_error'
            evidence = TurnTerminationEvidence.GENERATION_ERROR
            error_kind = 'generic'
        settlement['evidence'] = evidence.value
        if not settlement.get('error'):
            from lib.error_envelope import make_envelope
            settlement['error'] = make_envelope(
                error_kind,
                detail=(
                    'A historical turn marked completed carried explicit '
                    'terminal failure evidence '
                    f'({finish_reason or raw_evidence or cause}).'
                ),
                context='settlement-normalization',
                source='turn-verdict',
                raw=(
                    f'outcome=completed cause={settlement.get("cause") or ""} '
                    f'finish={finish_reason} '
                    f'stream_state={stream_state.value if stream_state else ""} '
                    f'evidence={raw_evidence}'
                ),
            )
    settlement['outcome'] = outcome
    settlement['cause'] = cause
    settlement.setdefault('providerFinishReason', None)
    settlement.setdefault('error', None)
    settlement.setdefault('resumeOptions', [])
    settlement.setdefault('streamState', None)
    if not settlement.get('evidence'):
        if cause == 'server_restart' or cause == 'conversation_deleted':
            inferred = TurnTerminationEvidence.SYSTEM_RECOVERY
        elif outcome == TurnOutcome.TRUNCATED.value:
            inferred = TurnTerminationEvidence.PROVIDER_LIMIT
        elif outcome == TurnOutcome.FAILED.value:
            inferred = TurnTerminationEvidence.GENERATION_ERROR
        elif (cause == 'provider_finished'
              and settlement.get('providerFinishReason')):
            inferred = TurnTerminationEvidence.LEGACY_FINISH
        else:
            inferred = TurnTerminationEvidence.EXTERNAL_AUTHORITY
        settlement['evidence'] = inferred.value
    return settlement


__all__ = [
    'TurnOutcome',
    'TurnStatus',
    'TerminalTaskFailure',
    'TurnTerminalEvidence',
    'TurnTerminationEvidence',
    'TurnVerdict',
    'derive_task_verdict',
    'derive_provider_stream_verdict',
    'derive_turn_verdict',
    'normalize_turn_settlement',
    'parse_provider_stream_state',
    'require_deliverable_task',
    'task_terminal_evidence',
    'terminal_finish_reason',
]
