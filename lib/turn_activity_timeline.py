"""Durable, bounded activity timeline for one conversation Turn.

Responsibility
--------------
Fold durable execution facts (tool lifecycle, retry/compaction cycles,
schema isolation, model fallback, and errors) into the public Turn projection.
Routine stream phase text and per-round model-request bookkeeping are NOT
folded: the phase channel already owns live status text and the turn trace
owns timing, so persisting them here would render the same fact twice.  The
raw event registry remains the source of truth; this module owns only the
replay-safe UI projection.  Timeline entries never enter the LLM transcript or
grant tool execution authority.

The projection is deliberately bounded.  Repeated retry/phase frames update a
single span row, tool progress updates its tool row, and at most 128 rows are
retained.  This keeps a multi-hour wait or long tool loop from turning a useful
diagnostic surface into unbounded durable state.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from typing import Any

from lib.agent_core.events import EventType, Phase
from lib.log_redaction import redact_text, sensitive_field_name
from lib.tool_rejection import (
    is_unavailable_tool_rejection,
    tool_rejection_descriptor,
)
from lib.tools.result_envelope import tool_result_error


ACTIVITY_TIMELINE_VERSION = 1
ACTIVITY_TIMELINE_MAX_ENTRIES = 128
ACTIVITY_TIMELINE_MAX_JSON_BYTES = 96 * 1024

_SUMMARY_MAX_CHARS = 180
_DETAIL_MAX_CHARS = 400
_IDENTIFIER_MAX_CHARS = 160
_ARG_STRING_MAX_CHARS = 160
_ARG_MAX_ITEMS = 12
_INTEGER_MAX = 9_007_199_254_740_991  # JavaScript Number.MAX_SAFE_INTEGER
_COUNT_MAX = 2_147_483_647

_KINDS = frozenset({'model', 'tool', 'status', 'error', 'system'})
_STATUSES = frozenset({
    'started', 'running', 'waiting', 'succeeded', 'failed', 'skipped',
    'switched', 'aborted',
})
_SEVERITIES = frozenset({'info', 'warning', 'error'})

_STRING_LIMITS: dict[str, int] = {
    'id': _IDENTIFIER_MAX_CHARS,
    'spanId': _IDENTIFIER_MAX_CHARS,
    'parentSpanId': _IDENTIFIER_MAX_CHARS,
    'kind': 24,
    'status': 24,
    'severity': 16,
    'summary': _SUMMARY_MAX_CHARS,
    'summaryKey': _IDENTIFIER_MAX_CHARS,
    'detail': _DETAIL_MAX_CHARS,
    'detailKey': _IDENTIFIER_MAX_CHARS,
    'model': _IDENTIFIER_MAX_CHARS,
    'providerId': _IDENTIFIER_MAX_CHARS,
    'toolName': _IDENTIFIER_MAX_CHARS,
    'toolCallId': _IDENTIFIER_MAX_CHARS,
    'reasonCode': _IDENTIFIER_MAX_CHARS,
    'action': 48,
    'fromModel': _IDENTIFIER_MAX_CHARS,
    'toModel': _IDENTIFIER_MAX_CHARS,
    'requestTag': 80,
    'phase': 80,
    'archiveId': _IDENTIFIER_MAX_CHARS,
    'trigger': 80,
    'tokenCountKind': 24,
    'timingMode': 24,
    'routeId': _IDENTIFIER_MAX_CHARS,
    'routeMode': 24,
    'routeDecision': 80,
    'failureStage': 80,
}
_INTEGER_FIELDS = frozenset({
    'seq', 'occurredAt', 'startedAt', 'endedAt', 'roundNum', 'llmRound',
    'count', 'durationMs', 'statusCode', 'tokensBefore', 'tokensAfter',
    'messagesBefore', 'messagesAfter', 'reductionPercent',
})
_ARG_FIELDS = frozenset({'summaryArgs', 'detailArgs'})
_ENTRY_FIELDS = frozenset({*_STRING_LIMITS, *_INTEGER_FIELDS, *_ARG_FIELDS})

_TOOL_TERMINAL_ERROR_STATUSES = frozenset({
    'error', 'failed', 'timeout', 'timed_out', 'cancelled',
})
_TOOL_TERMINAL_SKIP_STATUSES = frozenset({
    'rejected', 'skipped', 'not_run', 'not-run',
})
_TOOL_TERMINAL_ABORT_STATUSES = frozenset({'abort', 'aborted', 'interrupted'})
_PROTOCOL_ONLY_TOOL_NAMES = frozenset({'execute_tools'})


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        rendered = value
    elif isinstance(value, Mapping):
        for key in ('message', 'detail', 'content', 'error', 'reason'):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                rendered = candidate
                break
        else:
            rendered = json.dumps(
                dict(value), ensure_ascii=False, separators=(',', ':'),
                default=str,
            )
    elif isinstance(value, (list, tuple)):
        rendered = json.dumps(
            list(value), ensure_ascii=False, separators=(',', ':'),
            default=str,
        )
    else:
        rendered = str(value)
    rendered = ' '.join(rendered.split())
    return rendered[:limit]


def _safe_arg(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return redact_text(value, max_chars=_ARG_STRING_MAX_CHARS)[
            :_ARG_STRING_MAX_CHARS
        ]
    if isinstance(value, (list, tuple)):
        return [
            _safe_arg(item) for item in list(value)[:8]
            if item is None or isinstance(item, (bool, int, float, str))
        ]
    return redact_text(
        _text(value, _ARG_STRING_MAX_CHARS),
        max_chars=_ARG_STRING_MAX_CHARS,
    )[:_ARG_STRING_MAX_CHARS]


def _safe_args(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:_ARG_MAX_ITEMS]:
        safe_key = str(key)[:80]
        result[safe_key] = (
            '<redacted>' if sensitive_field_name(safe_key)
            else _safe_arg(item)
        )
    return result


def _diagnostic_text(value: Any, limit: int = _DETAIL_MAX_CHARS) -> str:
    """Bound and redact any durable user-visible diagnostic detail."""
    if value is None:
        return ''
    return _text(redact_text(value, max_chars=limit), limit)


def _nonnegative_int(
    value: Any, *, maximum: int = _INTEGER_MAX,
) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return min(parsed, maximum) if parsed >= 0 else None


def _normalize_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    entry: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _ENTRY_FIELDS or value is None:
            continue
        if key in _STRING_LIMITS:
            rendered = _text(value, _STRING_LIMITS[key])
            if rendered:
                entry[key] = rendered
        elif key in _INTEGER_FIELDS:
            parsed = _nonnegative_int(
                value, maximum=100 if key == 'reductionPercent' else _INTEGER_MAX,
            )
            if parsed is not None:
                entry[key] = parsed
        elif key in _ARG_FIELDS:
            args = _safe_args(value)
            if args:
                entry[key] = args
    if not entry.get('id') or not entry.get('spanId'):
        return None
    if entry.get('kind') not in _KINDS:
        return None
    if entry.get('status') not in _STATUSES:
        return None
    if entry.get('severity') not in _SEVERITIES:
        return None
    if 'occurredAt' not in entry:
        return None
    entry['count'] = min(
        _COUNT_MAX,
        max(1, int(entry.get('count') or 1)),
    )
    return entry


def _trim_entries(
    entries: list[dict[str, Any]], dropped_count: int,
) -> tuple[list[dict[str, Any]], int]:
    """Apply hard row/byte budgets, retaining diagnostics preferentially."""

    def serialized_size() -> int:
        document = {
            'blockId': 'activity-timeline',
            'version': ACTIVITY_TIMELINE_VERSION,
            'entries': entries,
            **({'droppedCount': dropped_count} if dropped_count else {}),
        }
        return len(json.dumps(
            document, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8'))

    def removal_index() -> int:
        removable = next((
            index for index, entry in enumerate(entries)
            if entry.get('severity') == 'info'
            and entry.get('kind') in {'status', 'model'}
            and entry.get('status') not in {'failed', 'switched'}
        ), None)
        if removable is not None:
            return removable
        return next((
            index for index, entry in enumerate(entries)
            if entry.get('severity') != 'error'
            and entry.get('status') not in {'failed', 'switched'}
        ), 0)

    while entries and (
        len(entries) > ACTIVITY_TIMELINE_MAX_ENTRIES
        or serialized_size() > ACTIVITY_TIMELINE_MAX_JSON_BYTES
    ):
        removable = removal_index()
        entries.pop(removable)
        dropped_count = min(_COUNT_MAX, dropped_count + 1)
    return entries, dropped_count


def normalize_activity_timeline(raw: Any) -> dict[str, Any] | None:
    """Return the public, bounded activity document or ``None`` when empty."""
    if not isinstance(raw, Mapping):
        return None
    entries = [
        normalized for item in (raw.get('entries') or [])
        if (normalized := _normalize_entry(item)) is not None
    ] if isinstance(raw.get('entries'), list) else []
    if not entries:
        return None
    dropped_count = _nonnegative_int(
        raw.get('droppedCount'), maximum=_COUNT_MAX,
    ) or 0
    entries, dropped_count = _trim_entries(entries, dropped_count)
    return {
        'blockId': 'activity-timeline',
        'version': ACTIVITY_TIMELINE_VERSION,
        'entries': entries,
        **({'droppedCount': dropped_count} if dropped_count else {}),
    }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _event_time(raw_event: Mapping[str, Any], fallback_ms: int) -> int:
    for key in ('emittedAt', 'tEnd', 'tStart'):
        value = raw_event.get(key)
        try:
            parsed = int(float(value))
        except (TypeError, ValueError, OverflowError):
            continue
        if parsed >= 0:
            return parsed
    return fallback_ms


def _stable_span(prefix: str, *parts: Any) -> str:
    source = '|'.join(_text(part, 300) for part in parts)
    digest = hashlib.sha256(source.encode('utf-8')).hexdigest()[:18]
    return f'{prefix}:{digest}'


def _entry_id(task: Mapping[str, Any], seq: int, suffix: str = '') -> str:
    attempt_id = _text(
        task.get('_attemptId') or task.get('attemptId') or 'attempt', 80,
    )
    return f'activity:{attempt_id}:{seq}{suffix}'


def _find_span(entries: list[dict[str, Any]], span_id: str) -> int | None:
    return next((
        index for index in range(len(entries) - 1, -1, -1)
        if entries[index].get('spanId') == span_id
    ), None)


def _append_or_update(
    entries: list[dict[str, Any]],
    entry: dict[str, Any],
    *,
    coalesce: bool = False,
) -> None:
    index = _find_span(entries, str(entry.get('spanId') or ''))
    if index is None:
        normalized = _normalize_entry(entry)
        if normalized is not None:
            entries.append(normalized)
        return
    previous = dict(entries[index])
    occurrence_timing = (
        entry.get('timingMode') == 'occurrences'
        or previous.get('timingMode') == 'occurrences')
    previous_duration = int(previous.get('durationMs') or 0)
    incoming_duration = int(entry.get('durationMs') or 0)
    previous.update({
        key: value for key, value in entry.items()
        if value is not None and value != ''
    })
    previous['id'] = entries[index]['id']
    previous['occurredAt'] = entries[index]['occurredAt']
    if entries[index].get('startedAt') is not None:
        previous['startedAt'] = entries[index]['startedAt']
    if occurrence_timing:
        # These are separated recovery incidents folded into one counted row.
        # Their meaningful duration is the sum of declared backoffs, never the
        # wall-clock envelope from the first incident to the last (which made
        # three short retries look like one 28-minute wait).
        previous['timingMode'] = 'occurrences'
        previous['durationMs'] = min(
            _INTEGER_MAX, previous_duration + incoming_duration)
    elif previous.get('startedAt') is not None \
            and previous.get('endedAt') is not None \
            and (entry.get('status') in {'started', 'running', 'waiting'}
                 or entry.get('durationMs') is None):
        previous['durationMs'] = max(
            0,
            int(previous['endedAt']) - int(previous['startedAt']),
        )
    if coalesce:
        previous['count'] = min(
            _COUNT_MAX,
            int(entries[index].get('count') or 1) + 1,
        )
    normalized = _normalize_entry(previous)
    if normalized is not None:
        entries[index] = normalized


def _round_number(raw_event: Mapping[str, Any]) -> int | None:
    for key in ('roundNum', 'round'):
        parsed = _nonnegative_int(raw_event.get(key))
        if parsed is not None:
            return parsed
    return None


def _llm_round_anchor(
    raw_event: Mapping[str, Any], task: Mapping[str, Any],
) -> int | None:
    """0-based model-round anchor for one diagnostic event.

    Model-dispatch events carry the 1-based ``R{n}`` request-tag round as
    ``roundNum`` (``lib/tasks_pkg/manager/_stream.py`` dispatches with
    ``tag=f'R{round_num+1}'``), so the 0-based ``llmRound`` used by content
    segments and tool rounds is ``roundNum - 1``.  Events without their own
    round inherit the most recent model-request round tracked on the task;
    diagnostics before any request (e.g. preflight schema rejection) stay
    unanchored and render at the turn start.
    """
    round_num = _round_number(raw_event)
    if round_num is not None:
        return max(0, round_num - 1)
    return _nonnegative_int(task.get('_activityLastLlmRound'))


def _track_llm_round(
    raw_event: Mapping[str, Any], task: Mapping[str, Any],
) -> None:
    """Remember the round of the latest model request for later inherits."""
    round_num = _round_number(raw_event)
    if round_num is not None and isinstance(task, dict):
        task['_activityLastLlmRound'] = max(0, round_num - 1)


def _error_parts(value: Any) -> tuple[str, str]:
    reason_code = ''
    if isinstance(value, Mapping):
        for key in ('kind', 'code', 'errorType', 'type'):
            if value.get(key):
                reason_code = _text(value[key], _IDENTIFIER_MAX_CHARS)
                break
    return reason_code, _diagnostic_text(value)


def _json_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _tool_failure_diagnostic(value: Any) -> tuple[str, str]:
    """Extract a stable code plus actionable copy from a bounded tool error."""
    typed = tool_result_error(value)
    if typed is not None:
        message = typed.message or typed.code
        if typed.next_action and typed.next_action not in message:
            message = f'{message} Next: {typed.next_action}'
        return typed.code, _diagnostic_text(message)
    decoded = _json_mapping(value)
    if decoded is not None:
        reason_code, detail = _error_parts(decoded)
        next_action = _text(
            decoded.get('retry_hint') or decoded.get('next_action'),
            _DETAIL_MAX_CHARS,
        )
        if next_action and next_action not in detail:
            detail = _diagnostic_text(f'{detail} Next: {next_action}')
        return reason_code, detail
    return '', _diagnostic_text(value)


def _gateway_failure_diagnostics(value: Any) -> list[dict[str, str]]:
    """Project protocol-gateway failures as their attempted child tools.

    ``execute_tools`` is a transport adapter, not user work. Validation errors
    have no child lifecycle event, so they become accurate skipped rows here;
    executed children retain their own events and these descriptors are only a
    replay fallback when such an event is absent.
    """
    document = _json_mapping(value)
    if document is None:
        return []
    payload: Mapping[str, Any] = document
    if document.get('contractVersion') == 'tofu.tool-result/v2':
        items = document.get('items')
        payload = next((
            item for item in (items if isinstance(items, list) else [])
            if isinstance(item, Mapping)
        ), document)

    diagnostics: list[dict[str, str]] = []
    errors = payload.get('errors')
    for error in (errors if isinstance(errors, list) else [])[:8]:
        if not isinstance(error, Mapping):
            continue
        reason_code, detail = _tool_failure_diagnostic(error)
        diagnostics.append({
            'toolName': _text(
                error.get('name') or error.get('attempted') or 'tool request',
                _IDENTIFIER_MAX_CHARS,
            ),
            'toolCallId': '',
            'status': 'skipped',
            'severity': 'warning',
            'reasonCode': reason_code,
            'detail': detail,
        })

    results = payload.get('results')
    for result in (results if isinstance(results, list) else [])[:8]:
        if not isinstance(result, Mapping):
            continue
        raw_status = str(result.get('status') or '').lower()
        if raw_status not in (
                _TOOL_TERMINAL_ERROR_STATUSES
                | _TOOL_TERMINAL_SKIP_STATUSES
                | _TOOL_TERMINAL_ABORT_STATUSES):
            continue
        reason_code, detail = _tool_failure_diagnostic(
            result.get('error') or result.get('output') or result)
        if raw_status in _TOOL_TERMINAL_SKIP_STATUSES:
            status, severity = 'skipped', 'warning'
        elif raw_status in _TOOL_TERMINAL_ABORT_STATUSES:
            status, severity = 'aborted', 'warning'
        else:
            status, severity = 'failed', 'error'
        diagnostics.append({
            'toolName': _text(
                result.get('name') or 'tool request', _IDENTIFIER_MAX_CHARS),
            'toolCallId': _text(
                result.get('call_id') or result.get('callId'),
                _IDENTIFIER_MAX_CHARS,
            ),
            'status': status,
            'severity': severity,
            'reasonCode': reason_code or raw_status,
            'detail': detail,
        })

    program = payload.get('program')
    if not diagnostics and isinstance(program, Mapping) \
            and str(program.get('status') or '').lower() == 'error':
        reason_code, detail = _tool_failure_diagnostic(
            program.get('error') or program)
        diagnostics.append({
            'toolName': 'ToolScript',
            'toolCallId': '',
            'status': 'failed',
            'severity': 'error',
            'reasonCode': reason_code or 'program_error',
            'detail': detail,
        })

    if not diagnostics and str(payload.get('status') or '').lower() == 'error':
        reason_code, detail = _tool_failure_diagnostic(payload)
        diagnostics.append({
            'toolName': 'tool request',
            'toolCallId': '',
            'status': 'failed',
            'severity': 'error',
            'reasonCode': reason_code or 'gateway_error',
            'detail': detail,
        })
    return diagnostics[:8]


def _close_open_entries(
    entries: list[dict[str, Any]], *, status: str, severity: str | None,
    ended_at: int,
) -> None:
    for index, source in enumerate(entries):
        if source.get('status') not in {'started', 'running', 'waiting'}:
            continue
        entry = dict(source)
        entry['status'] = status
        if severity is not None:
            entry['severity'] = severity
        entry['endedAt'] = ended_at
        if (entry.get('startedAt') is not None
                and entry.get('timingMode') != 'occurrences'):
            entry['durationMs'] = max(
                0, ended_at - int(entry.get('startedAt') or ended_at),
            )
        normalized = _normalize_entry(entry)
        if normalized is not None:
            entries[index] = normalized


def _close_request_status_entries(
    entries: list[dict[str, Any]], *, parent_span_id: str,
    status: str, severity: str | None, ended_at: int,
) -> None:
    """Settle retry/wait rows when their owning model request settles."""
    for index, source in enumerate(entries):
        if source.get('kind') != 'status' \
                or source.get('parentSpanId') != parent_span_id \
                or source.get('status') not in {'started', 'running', 'waiting'}:
            continue
        entry = dict(source)
        entry['status'] = status
        if severity is not None:
            entry['severity'] = severity
        entry['endedAt'] = ended_at
        if (entry.get('startedAt') is not None
                and entry.get('timingMode') != 'occurrences'):
            entry['durationMs'] = max(
                0, ended_at - int(entry.get('startedAt') or ended_at),
            )
        normalized = _normalize_entry(entry)
        if normalized is not None:
            entries[index] = normalized


def _fold_model_complete(
    entries: list[dict[str, Any]], raw_event: Mapping[str, Any],
    task: Mapping[str, Any], seq: int, now: int,
) -> None:
    raw_status = str(raw_event.get('status') or '').lower()
    failed = raw_status == 'failed'
    aborted = raw_status == 'aborted'
    model = _text(raw_event.get('model') or task.get('model') or '?', 160)
    span_id = _text(raw_event.get('spanId'), 160) or _stable_span(
        'model', task.get('_attemptId'), raw_event.get('requestTag'), model,
    )
    index = _find_span(entries, span_id)
    _track_llm_round(raw_event, task)
    if failed or aborted or index is not None:
        # Only a failed/aborted request earns its own row; a success merely
        # settles a span a legacy projection may already carry.
        duration_ms = _nonnegative_int(raw_event.get('durationMs'))
        if index is not None:
            started_at = int(entries[index].get('startedAt') or now)
        elif duration_ms is not None:
            started_at = max(0, now - duration_ms)
        else:
            started_at = now
        entry = {
            'id': _entry_id(task, seq),
            'spanId': span_id,
            'seq': seq,
            'occurredAt': now,
            'startedAt': started_at,
            'endedAt': now,
            'durationMs': duration_ms
            if duration_ms is not None else max(0, now - started_at),
            'kind': 'model',
            'status': ('failed' if failed
                       else 'aborted' if aborted else 'succeeded'),
            'severity': ('error' if failed
                         else 'warning' if aborted else 'info'),
            'summary': (
                f'{model} request failed' if failed
                else f'{model} request stopped' if aborted
                else f'{model} responded'
            ),
            'summaryKey': (
                'activity.model.requestFailed' if failed
                else 'activity.model.requestAborted' if aborted
                else 'activity.model.requestSucceeded'
            ),
            'summaryArgs': {'model': model},
            'model': model,
            'providerId': _text(raw_event.get('providerId'), 160),
            'requestTag': _text(raw_event.get('requestTag'), 80),
            'routeId': _text(raw_event.get('routeId'), 160),
            'routeMode': _text(raw_event.get('routeMode'), 24),
            'routeDecision': _text(raw_event.get('routeDecision'), 80),
            'failureStage': _text(raw_event.get('failureStage'), 80),
            'reasonCode': _text(raw_event.get('errorKind'), 160),
            'detail': _diagnostic_text(raw_event.get('errorDetail')),
            'statusCode': _nonnegative_int(raw_event.get('statusCode')),
            'roundNum': _round_number(raw_event),
            # R{n} tag convention: roundNum is 1-based, llmRound is 0-based.
            'llmRound': _llm_round_anchor(raw_event, task),
        }
        _append_or_update(entries, entry)
    _close_request_status_entries(
        entries,
        parent_span_id=span_id,
        status='failed' if failed else ('aborted' if aborted else 'succeeded'),
        severity='error' if failed else ('warning' if aborted else None),
        ended_at=now,
    )


def _fold_tool_event(
    entries: list[dict[str, Any]], raw_event: Mapping[str, Any],
    task: Mapping[str, Any], seq: int, now: int,
) -> None:
    event_type = str(raw_event.get('type') or '')
    tool_call_id = _text(raw_event.get('toolCallId'), 160)
    tool_name = _text(raw_event.get('toolName'), 160) or 'tool'
    span_id = _text(raw_event.get('spanId'), 160) or _stable_span(
        'tool', task.get('_attemptId'), tool_call_id or seq,
    )
    index = _find_span(entries, span_id)
    if index is not None and tool_name == 'tool':
        tool_name = _text(entries[index].get('toolName'), 160) or tool_name
    if index is not None and not tool_call_id:
        tool_call_id = _text(entries[index].get('toolCallId'), 160)
    round_num = _round_number(raw_event)
    parent_span = _text(
        raw_event.get('parentSpanId') or task.get('_activeModelRequestSpan'), 160,
    )
    if not parent_span and round_num is not None:
        parent_span = next((
            _text(entry.get('spanId'), 160)
            for entry in reversed(entries)
            if entry.get('kind') == 'model'
            and entry.get('roundNum') == round_num
        ), '')
    if tool_name in _PROTOCOL_ONLY_TOOL_NAMES:
        if event_type != EventType.TOOL_COMPLETE:
            return
        # Older tool_result frames omitted toolName and may already have
        # opened a generic row for this span. The named terminal frame is the
        # first point where it can be removed without guessing.
        if index is not None:
            entries.pop(index)
        diagnostics = _gateway_failure_diagnostics(
            raw_event.get('toolContent') or raw_event.get('content'))
        started_at = _nonnegative_int(raw_event.get('tStart')) or now
        ended_at = _nonnegative_int(raw_event.get('tEnd')) or now
        for offset, diagnostic in enumerate(diagnostics):
            child_call_id = diagnostic.get('toolCallId') or tool_call_id
            if diagnostic.get('toolCallId') and any(
                    entry.get('toolCallId') == diagnostic['toolCallId']
                    for entry in entries):
                # The actual child lifecycle is more authoritative and already
                # contains its own timing/result envelope.
                continue
            child_name = diagnostic.get('toolName') or 'tool request'
            child_status = diagnostic.get('status') or 'failed'
            child_span = _stable_span(
                'tool', task.get('_attemptId'),
                diagnostic.get('toolCallId') or f'{tool_call_id}:{offset}',
            )
            _append_or_update(entries, {
                'id': _entry_id(task, seq, f':gateway-{offset}'),
                'spanId': child_span,
                'parentSpanId': parent_span,
                'seq': seq,
                'occurredAt': now,
                'startedAt': started_at,
                'endedAt': ended_at,
                'durationMs': max(0, ended_at - started_at),
                'kind': 'tool',
                'status': child_status,
                'severity': diagnostic.get('severity') or 'error',
                'summary': f'{child_name} {child_status}',
                'summaryKey': f'activity.tool.{child_status}',
                'summaryArgs': {'tool': child_name},
                'detail': diagnostic.get('detail'),
                'reasonCode': diagnostic.get('reasonCode'),
                'toolName': child_name,
                'toolCallId': child_call_id,
                **({'roundNum': round_num} if round_num is not None else {}),
            })
        return
    if event_type == EventType.TOOL_START:
        started_at = _nonnegative_int(raw_event.get('tStart')) or now
        _append_or_update(entries, {
            'id': _entry_id(task, seq),
            'spanId': span_id,
            'parentSpanId': parent_span,
            'seq': seq,
            'occurredAt': started_at,
            'startedAt': started_at,
            'kind': 'tool',
            'status': 'running',
            'severity': 'info',
            'summary': tool_name,
            'summaryKey': 'activity.tool.started',
            'summaryArgs': {'tool': tool_name},
            'toolName': tool_name,
            'toolCallId': tool_call_id,
            **({'roundNum': round_num} if round_num is not None else {}),
        })
        return
    if event_type == EventType.TOOL_PROGRESS:
        if index is not None:
            entry = dict(entries[index])
            entry['status'] = 'running'
            entry['endedAt'] = now
            progress_detail = _diagnostic_text(raw_event.get('detail'))
            if progress_detail:
                entry['detail'] = progress_detail
            _append_or_update(entries, entry)
        return
    raw_status = str(raw_event.get('status') or '').lower()
    rejection = tool_rejection_descriptor(raw_event)
    is_error = bool(raw_event.get('isError')) \
        or raw_status in _TOOL_TERMINAL_ERROR_STATUSES
    if raw_status in _TOOL_TERMINAL_SKIP_STATUSES:
        status, severity = 'skipped', 'warning'
    elif raw_status in _TOOL_TERMINAL_ABORT_STATUSES:
        status, severity = 'aborted', 'warning'
    elif is_error:
        status, severity = 'failed', 'error'
    else:
        status, severity = 'succeeded', 'info'
    started_at = (
        int(entries[index].get('startedAt') or now)
        if index is not None else (_nonnegative_int(raw_event.get('tStart')) or now)
    )
    ended_at = _nonnegative_int(raw_event.get('tEnd')) or now
    reason_code = (
        str((rejection or {}).get('kind') or '')
        if status != 'succeeded' else '')
    detail = ''
    if status in {'failed', 'skipped', 'aborted'}:
        diagnostic_source = (
            raw_event.get('content') or raw_event.get('toolContent')
            or raw_event.get('detail') or (rejection or {}).get('reason'))
        typed_reason, detail = _tool_failure_diagnostic(diagnostic_source)
        reason_code = reason_code or typed_reason or raw_status
    summary_status = status
    summary_key = f'activity.tool.{status}'
    if status == 'skipped' and rejection is not None:
        if is_unavailable_tool_rejection(raw_event):
            summary_status = 'unavailable'
            summary_key = 'activity.tool.unavailable'
        else:
            summary_status = 'blocked'
            summary_key = 'activity.tool.blocked'
    _append_or_update(entries, {
        'id': _entry_id(task, seq),
        'spanId': span_id,
        'parentSpanId': parent_span,
        'seq': seq,
        'occurredAt': now,
        'startedAt': started_at,
        'endedAt': ended_at,
        'durationMs': max(0, ended_at - started_at),
        'kind': 'tool',
        'status': status,
        'severity': severity,
        'summary': f'{tool_name} {summary_status}',
        'summaryKey': summary_key,
        'summaryArgs': {'tool': tool_name},
        'detail': detail,
        'reasonCode': reason_code,
        'toolName': tool_name,
        'toolCallId': tool_call_id,
        **({'roundNum': round_num} if round_num is not None else {}),
    })


def _fold_phase(
    entries: list[dict[str, Any]], raw_event: Mapping[str, Any],
    task: Mapping[str, Any], seq: int, now: int,
) -> None:
    phase = _text(raw_event.get('phase'), 80)
    status_code = _nonnegative_int(raw_event.get('statusCode'))
    has_http_error = bool(status_code and status_code >= 400)
    if phase not in {Phase.RETRYING, Phase.COMPACTING} and not has_http_error:
        # The phase channel is the stream's live status text (startup beats,
        # thinking, waiting-for-model): transient by contract and already
        # shown by the live-status surface. Only retry/compaction cycles and
        # HTTP error beats are durable timeline facts.
        return
    parent_span = _text(task.get('_activeModelRequestSpan'), 160)
    detail = _diagnostic_text(raw_event.get('detail') or phase)
    detail_key = _text(raw_event.get('detailKey'), 160)
    detail_args = _safe_args(raw_event.get('detailArgs'))
    model = _text(raw_event.get('model') or task.get('model'), 160)
    reason_key = _text(detail_args.get('reasonKey'), 160)
    occurrence_duration_ms = None
    if raw_event.get('backoff_s') is not None:
        try:
            occurrence_duration_ms = max(
                0, round(float(raw_event.get('backoff_s') or 0) * 1000))
        except (TypeError, ValueError, OverflowError):
            occurrence_duration_ms = 0
    span_id = _stable_span(
        'status', task.get('_attemptId'), parent_span, phase, detail_key,
        reason_key, status_code,
    )
    warning = phase == Phase.RETRYING or has_http_error
    _append_or_update(entries, {
        'id': _entry_id(task, seq),
        'spanId': span_id,
        'parentSpanId': parent_span,
        'seq': seq,
        'occurredAt': now,
        'startedAt': now,
        'endedAt': now,
        **({'durationMs': occurrence_duration_ms,
            'timingMode': 'occurrences'}
           if occurrence_duration_ms is not None else {}),
        'kind': 'status',
        'status': 'waiting' if phase == Phase.RETRYING else 'running',
        'severity': 'warning' if warning else 'info',
        'summary': detail,
        'summaryKey': detail_key,
        'summaryArgs': detail_args,
        'phase': phase,
        'model': model,
        'reasonCode': reason_key or phase,
        'statusCode': status_code,
        'roundNum': _round_number(raw_event),
        'llmRound': _llm_round_anchor(raw_event, task),
    }, coalesce=True)


def _compaction_percent(value: Any) -> int | None:
    """Return the user-facing whole-percent receipt without fabricating one."""
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return min(100, round(numeric))


def _fold_compaction_event(
    entries: list[dict[str, Any]], raw_event: Mapping[str, Any],
    task: Mapping[str, Any], seq: int, now: int,
) -> None:
    """Fold the archive/start + receipt/done pair into one durable Turn row.

    ``phase=compacting`` remains transient progress.  The archive event earns
    the inspectable row because it has a stable archive identity; the done
    event settles that same row with final token/message accounting.  Removing
    the earlier generic phase row keeps one compaction fact per archive.
    """
    archive_id = _text(raw_event.get('archiveId'), _IDENTIFIER_MAX_CHARS)
    if not archive_id:
        return
    entries[:] = [
        entry for entry in entries
        if not (
            entry.get('phase') == Phase.COMPACTING
            and entry.get('reasonCode') == Phase.COMPACTING
            and entry.get('status') in {'started', 'running', 'waiting'}
        )
    ]
    span_id = _stable_span(
        'compaction', task.get('_attemptId'), archive_id,
    )
    prior_index = _find_span(entries, span_id)
    prior = entries[prior_index] if prior_index is not None else {}
    receipt = raw_event.get('receipt')
    receipt = receipt if isinstance(receipt, Mapping) else {}
    receipt_status = _text(receipt.get('status'), 32).lower()
    completed = str(raw_event.get('type') or '') == EventType.COMPACTION_DONE
    failed = completed and receipt_status in {
        'failed', 'aborted', 'rejected', 'cancelled',
    }
    tokens_before = _nonnegative_int(raw_event.get('tokensBefore'))
    tokens_after = _nonnegative_int(raw_event.get('tokensAfter'))
    messages_before = _nonnegative_int(raw_event.get('msgsBefore'))
    messages_after = _nonnegative_int(raw_event.get('msgsAfter'))
    reduction_percent = _compaction_percent(raw_event.get('reductionPct'))
    trigger = _text(
        raw_event.get('trigger') or receipt.get('trigger'), 80,
    )
    detail = _diagnostic_text(
        raw_event.get('reason') or receipt.get('outcomeReason'),
    )
    _append_or_update(entries, {
        'id': _entry_id(task, seq),
        'spanId': span_id,
        'seq': seq,
        'occurredAt': now,
        'startedAt': int(prior.get('startedAt') or now),
        'endedAt': now,
        'kind': 'status',
        'status': ('failed' if failed else 'succeeded' if completed else 'running'),
        'severity': 'warning' if failed else 'info',
        'summary': ('Context compaction made no reduction' if failed else
                    'Context compacted' if completed else
                    'Compacting context'),
        'summaryKey': ('activity.compaction.failed' if failed else
                       'activity.compaction.succeeded' if completed else
                       'activity.compaction.running'),
        'detail': detail,
        'phase': Phase.COMPACTING,
        'reasonCode': 'context_compaction',
        'archiveId': archive_id,
        'trigger': trigger,
        'tokenCountKind': _text(
            raw_event.get('tokenCountKind')
            or prior.get('tokenCountKind') or 'estimated', 24,
        ),
        'tokensBefore': (tokens_before if tokens_before is not None
                         else prior.get('tokensBefore')),
        'tokensAfter': (tokens_after if completed and tokens_after is not None
                        else prior.get('tokensAfter')),
        'messagesBefore': (
            messages_before if messages_before is not None
            else prior.get('messagesBefore')
        ),
        'messagesAfter': (
            messages_after if completed and messages_after is not None
            else prior.get('messagesAfter')
        ),
        'reductionPercent': (
            reduction_percent if reduction_percent is not None
            else prior.get('reductionPercent')
        ),
        'model': _text(raw_event.get('model') or prior.get('model'), 160),
        'roundNum': _round_number(raw_event),
        'llmRound': _llm_round_anchor(raw_event, task),
    })


def _fold_schema_rejection(
    entries: list[dict[str, Any]], raw_event: Mapping[str, Any],
    task: Mapping[str, Any], seq: int, now: int,
) -> None:
    tool_name = _text(raw_event.get('toolName'), 160) or 'unknown tool'
    reason_code = _text(raw_event.get('reasonCode'), 160) or 'invalid_schema'
    detail = _diagnostic_text(raw_event.get('detail'))
    parent_span = _text(
        raw_event.get('parentSpanId') or task.get('_activeModelRequestSpan'), 160,
    )
    # The wire boundary re-isolates the same bad schema on EVERY model
    # dispatch of an attempt, each with a fresh per-dispatch parent span.
    # That repeated isolation is one durable fact, so the row span excludes
    # the dispatch span: repeats coalesce into a single counted row instead
    # of minting a duplicate per request.
    span_id = _stable_span(
        'schema', task.get('_attemptId'), tool_name, reason_code, detail,
    )
    _append_or_update(entries, {
        'id': _entry_id(task, seq),
        'spanId': span_id,
        'parentSpanId': parent_span,
        'seq': seq,
        'occurredAt': now,
        'startedAt': now,
        'endedAt': now,
        'kind': 'tool',
        'status': 'skipped',
        'severity': 'warning',
        'summary': f'{tool_name} schema rejected; tool isolated',
        'summaryKey': 'activity.tool.schemaRejected',
        'summaryArgs': {'tool': tool_name},
        'detail': detail,
        'model': _text(raw_event.get('model') or task.get('model'), 160),
        'toolName': tool_name,
        'reasonCode': reason_code,
        'action': _text(raw_event.get('action') or 'omitted', 48),
        'roundNum': _round_number(raw_event),
        'llmRound': _llm_round_anchor(raw_event, task),
    }, coalesce=True)


def _fold_model_fallback(
    entries: list[dict[str, Any]], raw_event: Mapping[str, Any],
    task: Mapping[str, Any], seq: int, now: int,
) -> None:
    from_model = _text(raw_event.get('fallbackFrom'), 160) or '?'
    to_model = _text(raw_event.get('fallbackModel'), 160) or '?'
    _append_or_update(entries, {
        'id': _entry_id(task, seq),
        'spanId': _stable_span(
            'fallback', task.get('_attemptId'), seq, from_model, to_model,
        ),
        'seq': seq,
        'occurredAt': now,
        'startedAt': now,
        'endedAt': now,
        'kind': 'model',
        'status': 'switched',
        'severity': 'warning',
        'summary': f'Model switched: {from_model} → {to_model}',
        'summaryKey': 'activity.model.switched',
        'summaryArgs': {'from': from_model, 'to': to_model},
        'detail': _diagnostic_text(raw_event.get('fallbackReason')),
        'reasonCode': _text(raw_event.get('fallbackKind'), 160),
        'fromModel': from_model,
        'toModel': to_model,
        'action': 'fallback',
        'llmRound': _llm_round_anchor(raw_event, task),
    })


def _fold_error(
    entries: list[dict[str, Any]], raw_event: Mapping[str, Any],
    task: Mapping[str, Any], seq: int, now: int,
) -> None:
    raw_error = (
        raw_event.get('error') or raw_event.get('detail')
        or raw_event.get('content') or task.get('error')
    )
    reason_code, detail = _error_parts(raw_error)
    if not detail and not reason_code:
        return
    parent_span = _text(task.get('_activeModelRequestSpan'), 160)
    span_id = _stable_span(
        'error', task.get('_attemptId'), parent_span, reason_code, detail,
    )
    _append_or_update(entries, {
        'id': _entry_id(task, seq),
        'spanId': span_id,
        'parentSpanId': parent_span,
        'seq': seq,
        'occurredAt': now,
        'startedAt': now,
        'endedAt': now,
        'kind': 'error',
        'status': 'failed',
        'severity': 'error',
        'summary': 'Turn failed',
        'summaryKey': 'activity.error.failed',
        'detail': detail,
        'reasonCode': reason_code or 'error',
        'model': _text(task.get('model'), 160),
        'llmRound': _llm_round_anchor(raw_event, task),
    }, coalesce=True)


def fold_activity_timeline(
    previous: Any,
    raw_event: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    """Fold one registered runtime fact into the durable timeline projection."""
    existing = normalize_activity_timeline(previous)
    entries = [dict(item) for item in (existing or {}).get('entries', [])]
    dropped_count = int((existing or {}).get('droppedCount') or 0)
    event_type = str(raw_event.get('type') or '')
    seq = _nonnegative_int(raw_event.get('seq'))
    if seq is None:
        seq = min(
            _INTEGER_MAX,
            (entries[-1].get('seq', -1) + 1) if entries else 0,
        )
    fallback_time = _now_ms() if now_ms is None else int(now_ms)
    occurred_at = _event_time(raw_event, fallback_time)

    if event_type == EventType.MODEL_REQUEST_START:
        # Request bookkeeping lives in the event registry and turn trace; a
        # timeline row is earned only by a failed/aborted completion.  The
        # round is still tracked so later diagnostics inherit their anchor.
        _track_llm_round(raw_event, task)
        return existing
    if event_type == EventType.MODEL_REQUEST_COMPLETE:
        _fold_model_complete(entries, raw_event, task, seq, occurred_at)
    elif event_type in {
        EventType.TOOL_START, EventType.TOOL_PROGRESS,
        EventType.TOOL_RESULT, EventType.TOOL_COMPLETE,
    }:
        _fold_tool_event(entries, raw_event, task, seq, occurred_at)
    elif event_type == EventType.TOOL_SCHEMA_REJECTED:
        _fold_schema_rejection(entries, raw_event, task, seq, occurred_at)
    elif event_type == EventType.PHASE:
        _fold_phase(entries, raw_event, task, seq, occurred_at)
    elif event_type in {EventType.COMPACTION, EventType.COMPACTION_DONE}:
        _fold_compaction_event(entries, raw_event, task, seq, occurred_at)
    elif event_type == EventType.MODEL_FALLBACK:
        _fold_model_fallback(entries, raw_event, task, seq, occurred_at)
    elif event_type == EventType.ERROR:
        # The chat Turn authority treats the legacy ERROR frame as terminal
        # (newer fatal paths normally use DONE + error). Keep the projection
        # aligned with that boundary so a settled Turn has no open spans.
        _fold_error(entries, raw_event, task, seq, occurred_at)
        _close_open_entries(
            entries, status='failed', severity='error', ended_at=occurred_at,
        )
    elif event_type == EventType.DONE:
        finish_reason = str(raw_event.get('finishReason') or '').lower()
        terminal_error = bool(
            raw_event.get('error') or task.get('error')
            or finish_reason == 'error'
        )
        if terminal_error:
            _fold_error(entries, raw_event, task, seq, occurred_at)
        terminal_aborted = finish_reason in {'abort', 'aborted', 'interrupted'}
        _close_open_entries(
            entries,
            status=('failed' if terminal_error else
                    'aborted' if terminal_aborted else 'succeeded'),
            severity=('error' if terminal_error else
                      'warning' if terminal_aborted else None),
            ended_at=occurred_at,
        )
    elif event_type == 'aborted':
        _close_open_entries(
            entries, status='aborted', severity='warning', ended_at=occurred_at,
        )
    elif event_type in {EventType.RETRY_RESET, EventType.BUDGET_WARNING}:
        detail = _diagnostic_text(
            raw_event.get('detail') or raw_event.get('content') or event_type,
        )
        _append_or_update(entries, {
            'id': _entry_id(task, seq),
            'spanId': _stable_span(
                'system', task.get('_attemptId'), event_type,
                raw_event.get('kind'), raw_event.get('limit'),
            ),
            'seq': seq,
            'occurredAt': occurred_at,
            'startedAt': occurred_at,
            'endedAt': occurred_at,
            'kind': 'system',
            'status': 'waiting' if event_type == EventType.RETRY_RESET
            else 'running',
            'severity': 'warning',
            'summary': detail,
            'reasonCode': _text(
                raw_event.get('kind') or raw_event.get('limit') or event_type,
                160,
            ),
        }, coalesce=True)
    else:
        return existing

    entries, dropped_count = _trim_entries(entries, dropped_count)
    return normalize_activity_timeline({
        'entries': entries,
        'droppedCount': dropped_count,
    })


__all__ = [
    'ACTIVITY_TIMELINE_MAX_ENTRIES',
    'ACTIVITY_TIMELINE_MAX_JSON_BYTES',
    'ACTIVITY_TIMELINE_VERSION',
    'fold_activity_timeline',
    'normalize_activity_timeline',
]
