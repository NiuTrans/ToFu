"""Typed semantic result for one provider streaming attempt.

Responsibility
--------------
Translate transport evidence into one closed provider-stream state and carry
that state through dispatch/orchestration without making downstream code infer
completion from a default ``finish_reason`` or a bag of ``usage`` flags.

The object intentionally remains tuple-compatible during migration: legacy
callers may still unpack ``message, finish_reason, usage``.  New code must read
``state`` / ``is_verified_complete`` instead.  ``from_legacy`` is the only
place allowed to reconstruct semantics from the historical tuple contract.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterator


class ProviderStreamState(str, Enum):
    """Closed outcome vocabulary for a provider stream."""

    PROVIDER_FINISHED = 'provider_finished'
    CLIENT_ABORTED = 'client_aborted'
    PREMATURE_CLOSE = 'premature_close'
    MALFORMED_STREAM = 'malformed_stream'
    # Historical read/plugin compatibility; live transports no longer arm a
    # semantic-progress termination condition.
    SEMANTIC_PROGRESS_TIMEOUT = 'semantic_progress_timeout'
    # Historical read compatibility; new attempts never emit this state.
    NO_ACTIONABLE_OUTPUT = 'no_actionable_output'
    EMPTY_RESPONSE = 'empty_response'
    TOOL_PAYLOAD_MISSING = 'tool_payload_missing'
    UNKNOWN = 'unknown'


_RETRYABLE_STATES = frozenset({
    ProviderStreamState.PREMATURE_CLOSE,
    ProviderStreamState.MALFORMED_STREAM,
    ProviderStreamState.SEMANTIC_PROGRESS_TIMEOUT,
    ProviderStreamState.NO_ACTIONABLE_OUTPUT,
    ProviderStreamState.EMPTY_RESPONSE,
    ProviderStreamState.TOOL_PAYLOAD_MISSING,
})


_MAX_EVIDENCE_DIAGNOSTICS = 4
_MAX_EVIDENCE_DIAGNOSTIC_CHARS = 240
_EVIDENCE_PROJECTED_USAGE_KEYS = frozenset({
    '_semantic_progress_timeout',
    '_no_actionable_timeout',
    '_semantic_idle_timeout_ms',
    '_no_actionable_timeout_s',
    '_semantic_progress_idle_ms',
    '_no_actionable_stall_elapsed_s',
    '_no_actionable_request_elapsed_s',
    '_no_actionable_reasoning_chars',
    '_no_actionable_reasoning_chunks',
    '_malformed_stream',
    '_malformed_frames',
    '_missing_done',
    '_missing_finish_reason',
    '_stream_anomaly',
    '_empty_stop',
    '_tool_calls_void',
})


@dataclass(frozen=True, slots=True)
class ProviderStreamEvidence:
    """Fixed-shape, bounded observations from one provider attempt.

    The fields deliberately distinguish transport liveness from semantic
    progress.  No raw provider body is retained; parser diagnostics are short,
    redacted descriptions capped both by count and length.
    """

    request_elapsed_ms: int = 0
    response_headers_seen: bool = False
    transport_byte_count: int = 0
    sse_event_count: int = 0
    reasoning_chars: int = 0
    reasoning_chunks: int = 0
    content_chars: int = 0
    content_chunks: int = 0
    tool_call_count: int = 0
    tool_argument_chars: int = 0
    tool_argument_chunks: int = 0
    provider_finish_seen: bool = False
    done_seen: bool = False
    malformed_frame_count: int = 0
    semantic_progress_timeout: bool = False
    semantic_idle_timeout_ms: int = 0
    empty_response: bool = False
    tool_payload_missing: bool = False
    tool_payload_missing_cause: str = ''
    client_aborted: bool = False
    last_semantic_progress_age_ms: int = 0
    last_transport_activity_age_ms: int = 0
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        numeric_fields = (
            'request_elapsed_ms', 'transport_byte_count', 'sse_event_count',
            'reasoning_chars', 'reasoning_chunks', 'content_chars',
            'content_chunks', 'tool_call_count', 'tool_argument_chars',
            'tool_argument_chunks', 'malformed_frame_count',
            'semantic_idle_timeout_ms',
            'last_semantic_progress_age_ms',
            'last_transport_activity_age_ms',
        )
        for field_name in numeric_fields:
            if getattr(self, field_name) < 0:
                raise ValueError(f'{field_name} must be non-negative')
        if len(self.diagnostics) > _MAX_EVIDENCE_DIAGNOSTICS:
            raise ValueError('stream evidence diagnostics exceed bound')
        if any(
                not isinstance(value, str)
                or len(value) > _MAX_EVIDENCE_DIAGNOSTIC_CHARS
                for value in self.diagnostics):
            raise ValueError('stream evidence diagnostic is invalid or too long')
        if self.tool_payload_missing_cause not in {
                '', 'gateway_no_payload', 'filtered'}:
            raise ValueError('invalid tool payload missing cause')

    @property
    def has_reasoning(self) -> bool:
        return self.reasoning_chars > 0

    @property
    def has_content(self) -> bool:
        return self.content_chars > 0

    @property
    def has_valid_tool_call(self) -> bool:
        return self.tool_call_count > 0


def _legacy_evidence(
    *,
    state: ProviderStreamState,
    usage: Any,
    saw_done: bool,
    saw_finish_reason: bool,
    malformed_frame_count: int,
) -> ProviderStreamEvidence:
    """Build bounded evidence once at the historical tuple boundary."""
    values = usage if isinstance(usage, dict) else {}

    def _non_negative_int(key: str, fallback: int = 0) -> int:
        try:
            return max(0, int(values.get(key, fallback) or 0))
        except (TypeError, ValueError, OverflowError):
            return max(0, int(fallback or 0))

    def _seconds_to_ms(key: str) -> int:
        try:
            return max(0, round(float(values.get(key) or 0) * 1000))
        except (TypeError, ValueError, OverflowError):
            return 0

    elapsed_ms = _non_negative_int('stream_elapsed_ms')
    semantic_timeout = bool(
        values.get('_semantic_progress_timeout')
        or values.get('_no_actionable_timeout')
        or state in {
            ProviderStreamState.SEMANTIC_PROGRESS_TIMEOUT,
            ProviderStreamState.NO_ACTIONABLE_OUTPUT,
        }
    )
    stall_age_ms = _non_negative_int(
        '_semantic_progress_idle_ms',
        _seconds_to_ms('_no_actionable_stall_elapsed_s'),
    )
    raw_tool_payload_cause = values.get('_tool_calls_void')
    tool_payload_cause = (
        raw_tool_payload_cause
        if raw_tool_payload_cause in {'gateway_no_payload', 'filtered'}
        else ('gateway_no_payload' if raw_tool_payload_cause else '')
    )
    return ProviderStreamEvidence(
        request_elapsed_ms=elapsed_ms,
        response_headers_seen=bool(values.get('_response_headers_seen')),
        transport_byte_count=_non_negative_int('_transport_bytes_received'),
        sse_event_count=_non_negative_int(
            '_sse_events_received', _non_negative_int('_chunks_received')),
        reasoning_chars=_non_negative_int(
            '_reasoning_chars',
            _non_negative_int('_no_actionable_reasoning_chars')),
        reasoning_chunks=_non_negative_int(
            '_reasoning_chunks',
            _non_negative_int('_no_actionable_reasoning_chunks')),
        content_chars=_non_negative_int('_content_chars'),
        content_chunks=_non_negative_int('_content_chunks'),
        tool_call_count=_non_negative_int('_valid_tool_calls'),
        tool_argument_chars=_non_negative_int('_tool_argument_chars'),
        tool_argument_chunks=_non_negative_int('_tool_argument_chunks'),
        provider_finish_seen=saw_finish_reason,
        done_seen=saw_done,
        malformed_frame_count=malformed_frame_count,
        semantic_progress_timeout=semantic_timeout,
        semantic_idle_timeout_ms=_non_negative_int(
            '_semantic_idle_timeout_ms',
            _seconds_to_ms('_no_actionable_timeout_s')),
        empty_response=bool(
            values.get('_empty_stop')
            or state is ProviderStreamState.EMPTY_RESPONSE),
        tool_payload_missing=bool(
            raw_tool_payload_cause
            or state is ProviderStreamState.TOOL_PAYLOAD_MISSING),
        tool_payload_missing_cause=tool_payload_cause,
        client_aborted=(state is ProviderStreamState.CLIENT_ABORTED),
        last_semantic_progress_age_ms=stall_age_ms,
        last_transport_activity_age_ms=_non_negative_int(
            '_transport_idle_ms'),
    )


def project_legacy_stream_usage(
    usage: dict[str, Any] | None,
    *,
    state: ProviderStreamState,
    evidence: ProviderStreamEvidence,
) -> dict[str, Any]:
    """Compatibility projection derived only from typed evidence.

    Legacy consumers may continue reading underscore-prefixed markers, but
    they no longer participate in deciding the result.  Every marker here is
    a projection of ``ProviderStreamEvidence`` plus the already-closed state.
    """
    values = dict(usage) if isinstance(usage, dict) else {}
    for key in _EVIDENCE_PROJECTED_USAGE_KEYS:
        values.pop(key, None)
    values['_stream_state'] = state.value
    values['stream_elapsed_ms'] = evidence.request_elapsed_ms
    values['_response_headers_seen'] = evidence.response_headers_seen
    values['_transport_bytes_received'] = evidence.transport_byte_count
    values['_sse_events_received'] = evidence.sse_event_count
    # Historically ``_chunks_received`` counted provider payload objects and
    # excluded the terminal ``[DONE]`` sentinel.  Keep that exact projection
    # while the typed evidence exposes the complete SSE event count.
    values['_chunks_received'] = max(
        0,
        evidence.sse_event_count - (1 if evidence.done_seen else 0),
    )
    values['_reasoning_chars'] = evidence.reasoning_chars
    values['_reasoning_chunks'] = evidence.reasoning_chunks
    values['_content_chars'] = evidence.content_chars
    values['_content_chunks'] = evidence.content_chunks
    values['_valid_tool_calls'] = evidence.tool_call_count
    values['_tool_argument_chars'] = evidence.tool_argument_chars
    values['_tool_argument_chunks'] = evidence.tool_argument_chunks
    values['_transport_idle_ms'] = evidence.last_transport_activity_age_ms

    if evidence.semantic_progress_timeout:
        if state is ProviderStreamState.SEMANTIC_PROGRESS_TIMEOUT:
            values['_semantic_progress_timeout'] = True
            # Retain the historical usage marker for tuple/usage consumers.
            # The typed evidence above remains the sole authority for both
            # projections; callers must not infer the stream state from it.
            values['_no_actionable_timeout'] = True
        values['_semantic_progress_idle_ms'] = (
            evidence.last_semantic_progress_age_ms)
        values['_semantic_idle_timeout_ms'] = evidence.semantic_idle_timeout_ms
        values['_no_actionable_timeout_s'] = round(
            evidence.semantic_idle_timeout_ms / 1000, 3)
        if state is ProviderStreamState.NO_ACTIONABLE_OUTPUT:
            values['_no_actionable_timeout'] = True
        values['_no_actionable_stall_elapsed_s'] = round(
            evidence.last_semantic_progress_age_ms / 1000, 3)
        values['_no_actionable_request_elapsed_s'] = round(
            evidence.request_elapsed_ms / 1000, 3)
        values['_no_actionable_reasoning_chars'] = evidence.reasoning_chars
        values['_no_actionable_reasoning_chunks'] = evidence.reasoning_chunks
    if evidence.malformed_frame_count:
        values['_malformed_stream'] = True
        values['_malformed_frames'] = evidence.malformed_frame_count
    if state is not ProviderStreamState.CLIENT_ABORTED:
        if not evidence.done_seen:
            values['_missing_done'] = True
        if not evidence.provider_finish_seen:
            values['_missing_finish_reason'] = True
    if state is ProviderStreamState.EMPTY_RESPONSE:
        values['_empty_stop'] = True
    if state is ProviderStreamState.TOOL_PAYLOAD_MISSING:
        values['_tool_calls_void'] = (
            evidence.tool_payload_missing_cause or 'gateway_no_payload')
    if state not in {
            ProviderStreamState.PROVIDER_FINISHED,
            ProviderStreamState.CLIENT_ABORTED,
    }:
        values['_stream_anomaly'] = True
    return values


class UnverifiedProviderStreamError(RuntimeError):
    """Raised when a consumer tries to adopt an unverified provider result."""

    def __init__(self, result: 'ProviderStreamResult', *, context: str = ''):
        self.result = result
        self.context = context
        prefix = f'{context}: ' if context else ''
        super().__init__(
            f'{prefix}provider stream is not verified complete '
            f'(state={result.state.value})')


def classify_provider_stream_state(
    *,
    aborted: bool,
    saw_finish_reason: bool,
    malformed_frame_count: int,
    empty_response: bool,
    tool_payload_missing: bool,
    semantic_progress_timeout: bool = False,
    no_actionable_timeout: bool = False,
) -> ProviderStreamState:
    """Derive exactly one state from parser evidence, in failure priority.

    A provider finish frame is positive completion evidence only when no frame
    was dropped and the response shape is usable.  ``[DONE]`` is deliberately
    absent from this decision: some compatible providers omit it after an
    otherwise authoritative finish frame, while a ``[DONE]`` with no finish
    reason still proves no semantic completion at all.
    """
    if aborted:
        return ProviderStreamState.CLIENT_ABORTED
    if malformed_frame_count > 0:
        return ProviderStreamState.MALFORMED_STREAM
    if semantic_progress_timeout or no_actionable_timeout:
        return ProviderStreamState.SEMANTIC_PROGRESS_TIMEOUT
    if not saw_finish_reason:
        return ProviderStreamState.PREMATURE_CLOSE
    if tool_payload_missing:
        return ProviderStreamState.TOOL_PAYLOAD_MISSING
    if empty_response:
        return ProviderStreamState.EMPTY_RESPONSE
    return ProviderStreamState.PROVIDER_FINISHED


def _state_from_legacy_usage(
    finish_reason: str | None,
    usage: Any,
) -> ProviderStreamState:
    values = usage if isinstance(usage, dict) else {}
    raw_state = values.get('_stream_state') or values.get('streamState')
    if raw_state:
        if isinstance(raw_state, ProviderStreamState):
            return raw_state
        try:
            return ProviderStreamState(str(raw_state))
        except ValueError:
            return ProviderStreamState.UNKNOWN
    if values.get('_semantic_progress_timeout'):
        return ProviderStreamState.SEMANTIC_PROGRESS_TIMEOUT
    if values.get('_no_actionable_timeout'):
        return ProviderStreamState.NO_ACTIONABLE_OUTPUT
    if values.get('_malformed_stream') or values.get('_malformed_frames'):
        return ProviderStreamState.MALFORMED_STREAM
    if values.get('_tool_calls_void'):
        return ProviderStreamState.TOOL_PAYLOAD_MISSING
    if values.get('_empty_stop'):
        return ProviderStreamState.EMPTY_RESPONSE
    if values.get('_missing_finish_reason') or values.get('_stream_anomaly'):
        return ProviderStreamState.PREMATURE_CLOSE
    if finish_reason in {'premature_close', 'abnormal_stop'}:
        return ProviderStreamState.PREMATURE_CLOSE
    if finish_reason in {'aborted', 'interrupted'}:
        return ProviderStreamState.CLIENT_ABORTED
    if finish_reason in {None, '', 'error', 'timeout'}:
        return ProviderStreamState.UNKNOWN
    # Compatibility boundary only. Production parser results always carry an
    # explicit state; old tests/adapters that return a three-tuple can still
    # prove completion with a non-empty provider finish reason.
    if finish_reason:
        return ProviderStreamState.PROVIDER_FINISHED
    return ProviderStreamState.UNKNOWN


@dataclass(frozen=True, slots=True)
class ProviderStreamResult:
    """One parsed provider attempt with explicit completion evidence.

    ``compatibility_finish_reason`` preserves the historical default ``stop``
    seen by tuple-unpacking callers.  ``provider_finish_reason`` is different:
    it is ``None`` unless a provider finish frame was actually observed.
    """

    message: dict[str, Any]
    compatibility_finish_reason: str
    usage: dict[str, Any] | None
    state: ProviderStreamState
    provider_finish_reason: str | None = None
    saw_done: bool = False
    saw_finish_reason: bool = False
    malformed_frame_count: int = 0
    evidence: ProviderStreamEvidence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ProviderStreamState):
            raise TypeError('state must be a ProviderStreamState')
        if self.malformed_frame_count < 0:
            raise ValueError('malformed_frame_count must be non-negative')
        evidence = self.evidence
        if evidence is None:
            evidence = _legacy_evidence(
                state=self.state,
                usage=self.usage,
                saw_done=self.saw_done,
                saw_finish_reason=self.saw_finish_reason,
                malformed_frame_count=self.malformed_frame_count,
            )
            object.__setattr__(self, 'evidence', evidence)
        elif not isinstance(evidence, ProviderStreamEvidence):
            raise TypeError('evidence must be ProviderStreamEvidence')
        if evidence.done_seen != self.saw_done:
            raise ValueError('evidence.done_seen and saw_done must agree')
        if evidence.provider_finish_seen != self.saw_finish_reason:
            raise ValueError(
                'evidence.provider_finish_seen and saw_finish_reason must agree')
        if evidence.malformed_frame_count != self.malformed_frame_count:
            raise ValueError(
                'evidence malformed count and malformed_frame_count must agree')
        has_provider_finish = bool(self.provider_finish_reason)
        if self.saw_finish_reason != has_provider_finish:
            raise ValueError(
                'saw_finish_reason and provider_finish_reason must agree')
        if (self.state is ProviderStreamState.PROVIDER_FINISHED
                and not self.saw_finish_reason):
            raise ValueError(
                'provider_finished requires an observed provider finish frame')
        if (self.state is ProviderStreamState.MALFORMED_STREAM
                and self.malformed_frame_count < 1):
            raise ValueError(
                'malformed_stream requires at least one malformed frame')
        if (self.state is ProviderStreamState.PREMATURE_CLOSE
                and self.saw_finish_reason):
            raise ValueError(
                'premature_close cannot carry an observed provider finish frame')
        if (self.state in {
                ProviderStreamState.EMPTY_RESPONSE,
                ProviderStreamState.TOOL_PAYLOAD_MISSING,
        } and not self.saw_finish_reason):
            raise ValueError(
                f'{self.state.value} requires an observed provider finish frame')
        if (self.state in {
                ProviderStreamState.SEMANTIC_PROGRESS_TIMEOUT,
                ProviderStreamState.NO_ACTIONABLE_OUTPUT,
        }
                and not evidence.semantic_progress_timeout):
            raise ValueError(
                f'{self.state.value} requires matching typed evidence')

    @property
    def finish_reason(self) -> str:
        return self.compatibility_finish_reason

    @property
    def is_verified_complete(self) -> bool:
        return self.state is ProviderStreamState.PROVIDER_FINISHED

    @property
    def is_retryable(self) -> bool:
        return self.state in _RETRYABLE_STATES

    def require_verified(self, *, context: str = '') -> 'ProviderStreamResult':
        """Return self only when provider terminal evidence is authoritative."""
        if not self.is_verified_complete:
            raise UnverifiedProviderStreamError(self, context=context)
        return self

    def with_usage(self, usage: dict[str, Any] | None) -> 'ProviderStreamResult':
        normalized_usage = project_legacy_stream_usage(
            usage,
            state=self.state,
            evidence=self.evidence,
        )
        return replace(self, usage=normalized_usage)

    def as_legacy_tuple(self) -> tuple[dict[str, Any], str, dict[str, Any] | None]:
        return self.message, self.compatibility_finish_reason, self.usage

    def __iter__(self) -> Iterator[Any]:
        return iter(self.as_legacy_tuple())

    def __len__(self) -> int:
        return 3

    def __getitem__(self, index: int | slice) -> Any:
        return self.as_legacy_tuple()[index]

    @classmethod
    def from_legacy(
        cls,
        message: dict[str, Any] | None,
        finish_reason: str | None,
        usage: dict[str, Any] | None,
    ) -> 'ProviderStreamResult':
        """Adapt one historical three-tuple at the compatibility boundary."""
        state = _state_from_legacy_usage(finish_reason, usage)
        values = dict(usage) if isinstance(usage, dict) else None
        saw_finish = bool(
            finish_reason
            and finish_reason not in {
                'premature_close', 'abnormal_stop', 'aborted', 'interrupted',
                'error', 'timeout',
            }
            and state not in {
                ProviderStreamState.PREMATURE_CLOSE,
                ProviderStreamState.CLIENT_ABORTED,
                ProviderStreamState.SEMANTIC_PROGRESS_TIMEOUT,
                ProviderStreamState.NO_ACTIONABLE_OUTPUT,
                ProviderStreamState.UNKNOWN,
            }
            and not (values or {}).get('_missing_finish_reason')
        )
        raw_malformed_count = (values or {}).get('_malformed_frames') or 0
        try:
            malformed_frame_count = max(0, int(raw_malformed_count))
        except (TypeError, ValueError, OverflowError):
            # Keep an untrusted diagnostic consistent with the fail-closed
            # state selected above instead of crashing the compatibility seam.
            malformed_frame_count = 1 if raw_malformed_count else 0
        if state is ProviderStreamState.MALFORMED_STREAM:
            malformed_frame_count = max(1, malformed_frame_count)
        if values is not None:
            # Once the historical tuple crosses this adapter it has a closed
            # typed state. Keep tuple-unpacking consumers on that same decision
            # instead of making them reconstruct it again from old flags.
            values['_stream_state'] = state.value
        evidence = _legacy_evidence(
            state=state,
            usage=values,
            saw_done=bool(values and not values.get('_missing_done')),
            saw_finish_reason=saw_finish,
            malformed_frame_count=malformed_frame_count,
        )
        return cls(
            message=message or {'role': 'assistant', 'content': ''},
            compatibility_finish_reason=str(finish_reason or 'stop'),
            usage=values,
            state=state,
            provider_finish_reason=(str(finish_reason) if saw_finish else None),
            saw_done=bool(values and not values.get('_missing_done')),
            saw_finish_reason=saw_finish,
            malformed_frame_count=malformed_frame_count,
            evidence=evidence,
        )


def ensure_provider_stream_result(value: Any) -> ProviderStreamResult:
    """Return ``value`` as :class:`ProviderStreamResult`.

    This adapter accepts only the historical three-item return shape.  Any
    other shape is a programmer error rather than an invitation to guess.
    """
    if isinstance(value, ProviderStreamResult):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 3:
        return ProviderStreamResult.from_legacy(value[0], value[1], value[2])
    raise TypeError(
        'provider stream call must return ProviderStreamResult or a 3-item '
        'legacy tuple')


def require_verified_provider_stream_result(
    value: Any,
    *,
    context: str = '',
) -> ProviderStreamResult:
    """Adapt one provider result and reject adoption without finish evidence."""
    return ensure_provider_stream_result(value).require_verified(
        context=context)


__all__ = [
    'ProviderStreamEvidence',
    'ProviderStreamResult',
    'ProviderStreamState',
    'UnverifiedProviderStreamError',
    'classify_provider_stream_state',
    'ensure_provider_stream_result',
    'project_legacy_stream_usage',
    'require_verified_provider_stream_result',
]
