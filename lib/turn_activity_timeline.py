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

import re
import time
from collections.abc import Mapping
from typing import Any

from lib.agent_core.events import EventType, Phase
from lib.logging_pkg.log_record_values import text_value
from lib.tool_results.tool_rejection import (
    is_unavailable_tool_rejection,
    tool_rejection_descriptor,
)
from lib.tool_results.tool_round_replay import SUPERSEDED_PROVIDER_ATTEMPT_FIELD
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
    'keyHint': 64,
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

    'taskId': 256,
    'attemptId': 256,
    'anchorBlockId': 320,

    'anchorPosition': 24,
    'taskModel': 160,
    'summaryModel': 160,
    'summarySelectedModel': 160,
    'summaryProviderId': 160,
    'requestLevel': 24,
    'attemptScope': 24,
}
_INTEGER_FIELDS = frozenset({
    'seq', 'occurredAt', 'startedAt', 'endedAt', 'roundNum', 'llmRound',
    'anchorToolRoundNum',
    'count', 'durationMs', 'statusCode', 'tokensBefore', 'tokensAfter',
    'messagesBefore', 'messagesAfter', 'reductionPercent', 'attempt',
})
_ARG_FIELDS = frozenset({'summaryArgs', 'detailArgs'})
_ENTRY_FIELDS = frozenset({*_STRING_LIMITS, *_INTEGER_FIELDS, *_ARG_FIELDS, 'summaryCalls'})

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
        return text_value(value, max_chars=_ARG_STRING_MAX_CHARS)[
            :_ARG_STRING_MAX_CHARS
        ]
    if isinstance(value, (list, tuple)):
        return [
            _safe_arg(item) for item in list(value)[:8]
            if item is None or isinstance(item, (bool, int, float, str))
        ]
    return text_value(
        _text(value, _ARG_STRING_MAX_CHARS),
        max_chars=_ARG_STRING_MAX_CHARS,
    )[:_ARG_STRING_MAX_CHARS]


def _safe_args(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:_ARG_MAX_ITEMS]:
        safe_key = str(key)[:80]
        result[safe_key] = _safe_arg(item)
    return result


def _diagnostic_text(value: Any, limit: int = _DETAIL_MAX_CHARS) -> str:
    """Bound and redact any durable user-visible diagnostic detail."""
    if value is None:
        return ''
    return _text(text_value(value, max_chars=limit), limit)


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


def _identity_int(value: Any) -> int | None:
    """Identity counters cannot be rounded or clamped into another occurrence."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    if isinstance(value, str) and not value.isdecimal():
        return None
    if isinstance(value, str) and len(value) > 10:
        return None
    parsed = int(value)
    return parsed if 0 <= parsed <= _INTEGER_MAX else None


def _compaction_fact_text(value: Any, limit: int = 160) -> str:
    """New attribution never stringifies containers or discloses credential prose."""
    if not isinstance(value, str):
        return ''
    if re.search(
        r'''(?ix)(?:authorization(?:\\?["'])?\s*:|bearer\s+|
        (?:api[_-]?(?:key|secret)|(?:access|refresh)[_-]?token|token|password|secret)
        (?:\\?["'])?\s*[:=]|https?://[^/\s:@]+:[^/\s@]+@)''', value,
    ):
        return '<redacted>'
    return _text(value, limit)


def _summary_calls(value: Any) -> list[dict[str, Any]]:
    """Allow only recorded receipt provenance, never arbitrary dispatch secrets."""
    if not isinstance(value, list):
        return []
    allowed = {
        'model', 'providerId', 'status', 'costUsd', 'known', 'reason',
        'costBasis', 'pricingSource', 'pricingSnapshot', 'source', 'currency',
        'provider', 'selectedPromptTokens', 'tierId', 'maxPromptTokens',
        'usdToCny', 'rates', 'input', 'output', 'cacheReadMul', 'cacheWriteMul',
    }

    def clean(row: Mapping[str, Any], depth: int = 0) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in row.items():
            if key not in allowed:
                continue

            if key in {'model', 'providerId', 'provider'} and not isinstance(item, str):
                continue
            if isinstance(item, Mapping) and key in {'pricingSnapshot', 'rates'} and depth < 2:
                result[key] = clean(item, depth + 1)
            elif isinstance(item, str):
                result[key] = _compaction_fact_text(item)
            elif item is None or isinstance(item, bool):
                result[key] = item
            elif (isinstance(item, (int, float)) and abs(item) <= _INTEGER_MAX
                  and math.isfinite(item)):
                result[key] = item
        return result

    return [clean(row) for row in value[:16] if isinstance(row, Mapping)]


def compaction_attribution_from_receipt(receipt: Any, observed_calls: Any = None) -> dict[str, Any]:
    """Pure bounded projection seam for events or explicit offline evidence.

    Never read an archive, reprice costs or graft the task model onto a summary.
    Legacy receipt call identities can be selected-route fallbacks; they do
    not alone prove actual dispatch. Callers must already have matching receipt
    and optional observed dispatch evidence, never fetch or guess it here.
    """
    if not isinstance(receipt, Mapping):
        return {}
    summary = receipt.get('summary')
    if not isinstance(summary, Mapping):
        return {}
    route = summary.get('route')
    route = route if isinstance(route, Mapping) else {}
    result: dict[str, Any] = {}
    selected = _compaction_fact_text(route.get('modelId'))
    if selected:
        result['summarySelectedModel'] = selected
    cost = summary.get('cost')
    cost = cost if isinstance(cost, Mapping) else {}
    recorded_raw = cost.get('calls')
    call_count = cost.get('callCount')
    # Establish completeness BEFORE filtering/truncation. Otherwise a hidden
    # seventeenth route or malformed row can turn partial evidence into a lie.
    complete_ledger = (
        type(call_count) is int and 0 < call_count <= 16
        and isinstance(recorded_raw, list) and len(recorded_raw) == call_count
        and isinstance(observed_calls, list) and len(observed_calls) == call_count
        and all(isinstance(row, Mapping) for row in recorded_raw)
        and all(isinstance(row, Mapping) for row in observed_calls)
    )
    recorded = _summary_calls(recorded_raw)
    observed = _summary_calls(observed_calls) if complete_ledger else []
    calls = []
    for index in range(max(len(recorded), len(observed))):
        row = dict(recorded[index]) if index < len(recorded) else {}
        # Price provenance remains intact, but identity must be observed.
        row.pop('model', None)
        row.pop('providerId', None)
        if index < len(observed):
            row.update({key: observed[index][key] for key in ('model', 'providerId')
                        if key in observed[index]})
        calls.append(row)
    if calls:
        result['summaryCalls'] = calls
        # Do not hide incomplete/mixed attempts behind one selected model.
        for source, target in (('model', 'summaryModel'), ('providerId', 'summaryProviderId')):
            identities = {row.get(source) for row in calls}
            if (complete_ledger and len(identities) == 1
                    and all(isinstance(v, str) and v for v in identities)):
                result[target] = next(iter(identities))
    return result


def _normalize_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    entry: dict[str, Any] = {}

    # Never erase a malformed exact selector and then enable legacy guessing.
    for key in ('taskId', 'attemptId', 'anchorBlockId'):
        if key in raw and raw[key] is not None:
            value = raw[key]
            if (not isinstance(value, str) or not value.strip()
                    or len(value) > _STRING_LIMITS[key]):
                return None
    if (raw.get('anchorToolRoundNum') is not None
            and _identity_int(raw['anchorToolRoundNum']) is None):
        return None
    position = raw.get('anchorPosition')
    if position is not None and (not isinstance(position, str)
                                 or position not in {'before', 'after', 'producer-start'}):
        return None
    for key, value in raw.items():
        if key not in _ENTRY_FIELDS or value is None:
            continue
        if key in _STRING_LIMITS:
            rendered = (value if key in {'taskId', 'attemptId', 'anchorBlockId'}
                        else _compaction_fact_text(value, _STRING_LIMITS[key])
                        if key in {'taskModel', 'summaryModel', 'summarySelectedModel', 'summaryProviderId'}
                        else _text(value, _STRING_LIMITS[key]))
            if rendered:
                entry[key] = rendered
        elif key in _INTEGER_FIELDS:
            parsed = (_identity_int(value) if key in {'llmRound', 'anchorToolRoundNum'}
                      else _nonnegative_int(
                          value, maximum=100 if key == 'reductionPercent' else _INTEGER_MAX,
                      ))
            if parsed is not None:
                entry[key] = parsed
        elif key == 'summaryCalls':
            calls = _summary_calls(value)
            if calls:
                entry[key] = calls
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
    source = json.dumps([_text(part, 300) for part in parts], ensure_ascii=False)
    digest = hashlib.sha256(source.encode('utf-8')).hexdigest()[:18]
    return f'{prefix}:{digest}'


def _producer_identity(
    raw_event: Mapping[str, Any], task: Mapping[str, Any],
) -> dict[str, str]:
    """Keep a relayed producer distinct from its current display carrier."""
    from lib.tool_results.tool_round_identity import execution_identity

    attempt_id, task_id = execution_identity(raw_event)
    owner_attempt, _ = execution_identity(task)
    owner_task = _text(task.get('id'), 256)
    if ((not task_id or task_id == owner_task)
            and (not attempt_id or attempt_id == owner_attempt)):
        task_id = task_id or owner_task
        attempt_id = attempt_id or owner_attempt
    return {key: value for key, value in (
        ('taskId', task_id), ('attemptId', attempt_id),
    ) if value}


def _same_producer(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(not left.get(key) or not right.get(key)
               or left[key] == right[key] for key in ('taskId', 'attemptId'))


def _entry_id(
    task: Mapping[str, Any], seq: int, suffix: str = '', *,
    raw_event: Mapping[str, Any],
) -> str:
    identity = _producer_identity(raw_event, task)
    return _stable_span(
        'activity', identity.get('taskId'), identity.get('attemptId'), seq,
    ) + suffix


def _find_span(
    entries: list[dict[str, Any]], span_id: str,
    identity: Mapping[str, Any],
) -> int | None:
    matches = [index for index, entry in enumerate(entries)
               if entry.get('spanId') == span_id and _same_producer(entry, identity)]
    return matches[0] if len(matches) == 1 else None


def _append_or_update(
    entries: list[dict[str, Any]],
    entry: dict[str, Any],
    *,
    coalesce: bool = False,
) -> None:
    index = _find_span(entries, str(entry.get('spanId') or ''), entry)
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
        parsed = _identity_int(raw_event.get(key))
        if parsed is not None:
            return parsed
    return None


def _llm_round_anchor(
    raw_event: Mapping[str, Any], task: Mapping[str, Any],
) -> int | None:
    """Model request roundNum is 1-based; tool roundNum is NOT a model round."""
    explicit = _identity_int(raw_event.get('llmRound'))
    if explicit is not None:
        return explicit
    if raw_event.get('type') in {
        EventType.MODEL_REQUEST_START, EventType.MODEL_REQUEST_COMPLETE,
        EventType.TOOL_SCHEMA_REJECTED,
    }:
        round_num = _round_number(raw_event)
        if round_num is not None:
            return max(0, round_num - 1)
    if raw_event.get('type') in {EventType.COMPACTION, EventType.COMPACTION_DONE}:
        return _round_number(raw_event)  # Already zero-based at every producer.
    if raw_event.get('type') in {
        EventType.TOOL_START, EventType.TOOL_PROGRESS,
        EventType.TOOL_RESULT, EventType.TOOL_COMPLETE,
    }:
        # A terminal event can settle an earlier parallel round; retain its
        # start's explicit llmRound rather than borrowing the current request.
        return None
    if _producer_identity(raw_event, task) == _producer_identity({}, task):
        return _nonnegative_int(task.get('_activityLastLlmRound'))
    return None


def capture_compaction_boundary(raw_event: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
    """Capture the nearest existing occurrence once, including prior tool output."""
    scope: dict[str, Any] = _producer_identity(raw_event, task)
    if raw_event.get('anchorPosition'):
        return {**scope, **{key: raw_event[key] for key in ('anchorPosition', 'anchorBlockId') if key in raw_event}}
    segments = task.get('segments')
    for segment in reversed(segments if isinstance(segments, list) else []):
        if (not isinstance(segment, Mapping) or segment.get('terminal')
                or segment.get('type') not in {'text', 'thinking', 'tool_use', 'program'}
                or not _same_producer(segment, scope)
                or not any(scope.get(key) and segment.get(key) == scope[key] for key in ('taskId', 'attemptId'))):
            continue
        block_id = segment.get('blockId')
        if (isinstance(block_id, str) and block_id
                and not block_id.endswith(':terminal') and not block_id.startswith('terminal:')):
            return {**scope, 'anchorBlockId': block_id, 'anchorPosition': 'after'}
    return {**scope, 'anchorPosition': 'producer-start'}


def _parent_request(raw_event: Mapping[str, Any], task: Mapping[str, Any]) -> str:
    explicit = _text(raw_event.get('parentSpanId'), 160)
    if explicit:
        return explicit
    if _same_producer(_producer_identity(raw_event, task), _producer_identity({}, task)):
        return _text(task.get('_activeModelRequestSpan'), 160)
    return ''


def _activity_anchors(
    raw_event: Mapping[str, Any], task: Mapping[str, Any],
) -> dict[str, Any]:
    """Bounded producer evidence only; never count rows in the trimmed ledger."""
    anchors: dict[str, Any] = _producer_identity(raw_event, task)
    llm_round = _llm_round_anchor(raw_event, task)
    anchors['llmRound'] = llm_round
    tool_occurrence = raw_event.get('type') in {
        EventType.TOOL_START, EventType.TOOL_PROGRESS,
        EventType.TOOL_RESULT, EventType.TOOL_COMPLETE,
    }
    anchors['anchorToolRoundNum'] = _identity_int(
        raw_event.get('anchorToolRoundNum'))
    if tool_occurrence and anchors['anchorToolRoundNum'] is None:
        anchors['anchorToolRoundNum'] = _round_number(raw_event)
    block_id = raw_event.get('anchorBlockId')
    if not block_id and not tool_occurrence and llm_round is not None:
        # Capture only an existing durable block. Terminal aliases are live
        # placeholders which may later name a different execution entirely.
        segments = task.get('segments')
        for segment in reversed(segments if isinstance(segments, list) else []):
            if (not isinstance(segment, Mapping) or segment.get('terminal')
                    or segment.get('type') not in {'text', 'thinking'}
                    or segment.get('llmRound') != llm_round
                    or not _same_producer(segment, anchors)
                    or not any(segment.get(key) == anchors.get(key)
                               and anchors.get(key) for key in ('taskId', 'attemptId'))):
                continue
            block_id = segment.get('blockId')
            if block_id:
                break
    if block_id is not None and (not isinstance(block_id, str)
            or (not block_id.endswith(':terminal') and not block_id.startswith('terminal:'))):
        anchors['anchorBlockId'] = block_id
    return anchors


def _track_llm_round(
    raw_event: Mapping[str, Any], task: Mapping[str, Any],
) -> None:
    """Remember the round of the latest model request for later inherits."""
    round_num = _round_number(raw_event)
    if (round_num is not None and isinstance(task, dict)
            and _producer_identity(raw_event, task) == _producer_identity({}, task)):
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
    # Older MCP adapters and gateway envelopes nest the public error in
    # summary strings. Peel complete bounded documents, never JSON in prose.
    current = value
    for _ in range(5):
        if isinstance(current, str) and current.startswith('MCP Error: '):
            current = current[len('MCP Error: '):]
        if isinstance(current, str) and len(current) > _DETAIL_MAX_CHARS * 8:
            break
        decoded = _json_mapping(current)
        if decoded is None:
            break
        typed = tool_result_error(decoded)
        if typed is not None:
            return _tool_failure_diagnostic(decoded)
        if decoded.get('ok') is False and isinstance(decoded.get('error_code'), str):
            return (_text(decoded['error_code'], _IDENTIFIER_MAX_CHARS),
                    _diagnostic_text(decoded.get('error') or decoded['error_code']))
        if decoded.get('status') == 'error' and isinstance(decoded.get('summary'), str):
            current = decoded['summary']
            continue
        reason_code, detail = _error_parts(decoded)
        if isinstance(decoded.get('message'), str) and decoded['message'].strip():
            detail = _diagnostic_text(decoded['message'])
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
    # Sparse model delivery can wrap the gateway batch more than once.
    # Descend only a single-item envelope, preserving multi-item batches.
    for _ in range(4):
        items = payload.get('items')
        if (payload.get('status') in {'ok', 'error', 'partial'}
                and isinstance(items, list) and len(items) == 1
                and isinstance(items[0], Mapping)):
            payload = items[0]
        else:
            break

    diagnostics: list[dict[str, str]] = []
    errors = payload.get('errors')
    for error in (errors if isinstance(errors, list) else [])[:8]:
        if not isinstance(error, Mapping):
            continue
        reason_code, detail = _tool_failure_diagnostic(error)
        tool_name = _text(
            error.get('name') or error.get('attempted') or 'tool request',
            _IDENTIFIER_MAX_CHARS,
        )
        diagnostics.append({
            'toolName': tool_name,
            'toolCallId': '',
            'status': 'skipped',
            'severity': 'warning',
            'reasonCode': reason_code,
            'detail': detail,
            'summary': f'{tool_name} not run: argument validation failed',
            'summaryKey': 'activity.tool.validationRejected',
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
            'toolName': 'execute_tools',
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
    ended_at: int, identity: Mapping[str, Any],
) -> None:
    for index, source in enumerate(entries):
        if not _same_producer(source, identity):
            continue
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
    identity: Mapping[str, Any],
) -> None:
    """Settle retry/wait rows when their owning producer request settles."""
    for index, source in enumerate(entries):
        if not _same_producer(source, identity):
            continue
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
    index = _find_span(entries, span_id, _producer_identity(raw_event, task))
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
            'id': _entry_id(task, seq, raw_event=raw_event),
            **_activity_anchors(raw_event, task),
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
            'keyHint': _text(raw_event.get('keyHint'), 64),
            'requestTag': _text(raw_event.get('requestTag'), 80),
            'routeId': _text(raw_event.get('routeId'), 160),
            'routeMode': _text(raw_event.get('routeMode'), 24),
            'routeDecision': _text(raw_event.get('routeDecision'), 80),
            'failureStage': _text(raw_event.get('failureStage'), 80),

            'requestLevel': _text(raw_event.get('requestLevel'), 24),
            'attemptScope': _text(raw_event.get('attemptScope'), 24),
            'attempt': _nonnegative_int(raw_event.get('attempt')),
            'parentSpanId': _text(raw_event.get('parentSpanId'), 160),
            # An abort row's class name is always AbortedError — constant,
            # zero-diagnostic English noise in the facts line. Keep the kind
            # only for genuine failures.
            'reasonCode': ('' if aborted
                           else _text(raw_event.get('errorKind'), 160)),
            'detail': _diagnostic_text(raw_event.get('errorDetail')),
            'statusCode': _nonnegative_int(raw_event.get('statusCode')),
            'roundNum': _round_number(raw_event),
            # R{n} tag convention: roundNum is 1-based, llmRound is 0-based.
            'llmRound': _llm_round_anchor(raw_event, task),
        }
        if aborted:
            abort_url = _text(raw_event.get('errorUrl'), 400)
            entry['detailKey'] = (
                'activity.model.abortedWaiting' if abort_url
                else 'activity.model.aborted')
            if abort_url:
                entry['detailArgs'] = {'url': abort_url}
        _append_or_update(entries, entry)
    _close_request_status_entries(
        entries,
        parent_span_id=span_id,
        identity=_producer_identity(raw_event, task),
        status='failed' if failed else ('aborted' if aborted else 'succeeded'),
        severity='error' if failed else ('warning' if aborted else None),
        ended_at=now,
    )


def _tool_occurrence_span(
    raw_event: Mapping[str, Any], identity: Mapping[str, Any],
    round_num: int | None,
) -> str:
    return _text(raw_event.get('spanId'), 160) or _stable_span(
        'tool', identity.get('taskId'), identity.get('attemptId'),
        raw_event.get('anchorBlockId') or 'round', round_num,
    )


def _gateway_parent_span(
    raw_event: Mapping[str, Any], task: Mapping[str, Any],
    identity: Mapping[str, Any], occurrence: int | None,
) -> str:
    """Resolve a stamped call edge only through one proven parent occurrence.

    Tool display/dispatch stamps parentToolCallId, not a timeline span. The
    existing tool-round authority can supply that span without a new ledger
    or wire field. A raw id alone, an unstamped parent, or reused parent ids
    cannot authorize hiding a gateway failure.
    """
    parent_call_id = raw_event.get('parentToolCallId')
    if (not isinstance(parent_call_id, str) or not parent_call_id
            or occurrence is None
            or not all(identity.get(key) for key in ('taskId', 'attemptId'))):
        return ''
    rounds = task.get('toolRounds')
    matches = [row for row in (rounds if isinstance(rounds, list) else [])
               if isinstance(row, Mapping)
               and row.get('toolCallId') == parent_call_id
               and _same_producer(row, identity)]
    if len(matches) != 1:
        return ''
    parent = matches[0]
    parent_ordinal = _round_number(parent)
    if (parent.get('toolName') not in _PROTOCOL_ONLY_TOOL_NAMES
            or any(parent.get(key) != identity[key]
                   for key in ('taskId', 'attemptId'))
            or parent_ordinal is None or parent_ordinal >= occurrence):
        return ''
    return _tool_occurrence_span(parent, identity, parent_ordinal)


def _fold_tool_event(
    entries: list[dict[str, Any]], raw_event: Mapping[str, Any],
    task: Mapping[str, Any], seq: int, now: int,
) -> None:
    event_type = str(raw_event.get('type') or '')
    tool_call_id = _text(raw_event.get('toolCallId'), 160)
    tool_name = _text(raw_event.get('toolName'), 160) or 'tool'
    round_num = _identity_int(raw_event.get('anchorToolRoundNum'))
    if round_num is None:
        round_num = _round_number(raw_event)
    identity = _producer_identity(raw_event, task)
    if (_text(raw_event.get('spanId'), 160) or raw_event.get('anchorBlockId')
            or round_num is not None):
        # ``roundNum`` is the backend-owned occurrence identity for a tool
        # row. Provider call ids are only correlation tokens: positional-id
        # models recycle them across rounds, and the execution pipeline may
        # remint one *after* its provisional tool_start was emitted. Anchoring
        # the fallback span on the round keeps both cases occurrence-local:
        # an old recycled id cannot overwrite a prior row, while a terminal
        # event carrying the reminted id still settles its original start.
        span_id = _tool_occurrence_span(raw_event, identity, round_num)
    else:
        # Legacy frames may omit roundNum. Only one scoped occurrence in the
        # entire retained history is proof; a sole running row is not enough
        # when a late terminal could belong to a previously closed occurrence.
        # A start without roundNum always earns a fresh span. Gateways have
        # no retained shell, so a legacy completion cannot borrow a child's
        # span merely because the provider reused its raw id.
        open_matches = [
            entry for entry in entries
            if event_type != EventType.TOOL_START
            and tool_name not in _PROTOCOL_ONLY_TOOL_NAMES
            and entry.get('kind') == 'tool'
            and tool_call_id
            and _text(entry.get('toolCallId'), 160) == tool_call_id
            and _same_producer(entry, identity)
        ]
        if len(open_matches) == 1:
            span_id = _text(open_matches[0].get('spanId'), 160)
        else:
            span_id = _stable_span(
                'tool', identity.get('taskId'), identity.get('attemptId'),
                tool_call_id or 'anonymous', seq,
            )
    index = _find_span(entries, span_id, _producer_identity(raw_event, task))
    if index is not None and tool_name == 'tool':
        tool_name = _text(entries[index].get('toolName'), 160) or tool_name
    if index is not None and not tool_call_id:
        tool_call_id = _text(entries[index].get('toolCallId'), 160)
    parent_span = (
        _text(raw_event.get('parentSpanId'), 160)
        or _gateway_parent_span(raw_event, task, identity, round_num)
        or (_text(entries[index].get('parentSpanId'), 160)
            if index is not None else '')
        or _parent_request(raw_event, task)
    )
    if not parent_span:
        llm_round = _llm_round_anchor(raw_event, task)
        model_matches = [entry for entry in entries
                         if entry.get('kind') == 'model'
                         and llm_round is not None
                         and entry.get('llmRound') == llm_round
                         and _same_producer(entry, identity)]
        if len(model_matches) == 1:
            parent_span = _text(model_matches[0].get('spanId'), 160)
    if tool_name in _PROTOCOL_ONLY_TOOL_NAMES:
        if event_type != EventType.TOOL_COMPLETE:
            return
        # Older tool_result frames omitted toolName and may already have
        # opened a generic row for this exact span. Only a generic/protocol
        # shell can be removed; never consume a named child occurrence.
        if (index is not None and entries[index].get('toolName')
                in _PROTOCOL_ONLY_TOOL_NAMES | {'tool'}):
            entries.pop(index)
        diagnostics = _gateway_failure_diagnostics(
            raw_event.get('toolContent') or raw_event.get('content'))
        started_at = _nonnegative_int(raw_event.get('tStart')) or now
        ended_at = _nonnegative_int(raw_event.get('tEnd')) or now
        for offset, diagnostic in enumerate(diagnostics):
            child_call_id = diagnostic.get('toolCallId') or tool_call_id
            matching_child_entries = [
                entry for entry in entries
                if diagnostic.get('toolCallId')
                and entry.get('kind') == 'tool'
                and entry.get('toolCallId') == diagnostic['toolCallId']
                and entry.get('parentSpanId') == span_id
                and all(identity.get(key) and entry.get(key) == identity[key]
                        for key in ('taskId', 'attemptId'))
                and ':gateway-' not in str(entry.get('id') or '')
            ]
            if (len(matching_child_entries) == 1
                    and matching_child_entries[0].get('toolName') == diagnostic.get('toolName')
                    and matching_child_entries[0].get('status') == diagnostic.get('status')):
                # Only a settled, explicitly linked child owns this diagnostic.
                # A unique raw ID anywhere in retained history can be an old
                # success, not proof that this failure is already represented.
                # Legacy frames without linkage remain visible rather than
                # losing the only diagnostic for a reused child call ID.
                continue
            child_name = diagnostic.get('toolName') or 'tool request'
            child_status = diagnostic.get('status') or 'failed'
            child_span = _stable_span(
                'gateway-tool', task.get('_attemptId'), span_id,
                diagnostic.get('toolCallId') or tool_call_id, offset,
            )
            _append_or_update(entries, {
                'id': _entry_id(task, seq, f':gateway-{offset}', raw_event=raw_event),

                **_activity_anchors(raw_event, task),
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
                'summary': (
                    diagnostic.get('summary')
                    or f'{child_name} {child_status}'),
                'summaryKey': (
                    diagnostic.get('summaryKey')
                    or f'activity.tool.{child_status}'),
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
            'id': _entry_id(task, seq, raw_event=raw_event),
            **_activity_anchors(raw_event, task),
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
    if raw_event.get(SUPERSEDED_PROVIDER_ATTEMPT_FIELD) is True:
        # A discarded provider-attempt announcement settling, not a tool
        # execution: its adopted twin owns the real lifecycle. Close the
        # running span the early tool_start opened instead of surfacing a
        # misleading "{tool} stopped" warning row for a call that was never
        # actually executed or lost.
        if index is not None:
            entries.pop(index)
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
        'id': _entry_id(task, seq, raw_event=raw_event),
        **_activity_anchors(raw_event, task),
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
    parent_span = _parent_request(raw_event, task)
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
        reason_key, status_code, _llm_round_anchor(raw_event, task),
        raw_event.get('anchorBlockId'), raw_event.get('anchorToolRoundNum'),
    )
    warning = phase == Phase.RETRYING or has_http_error
    anchors = (capture_compaction_boundary(raw_event, task)
               if phase == Phase.COMPACTING else _activity_anchors(raw_event, task))
    if phase == Phase.COMPACTING:
        # A retry/resume can compact twice at the same model round.
        span_id = _stable_span('compaction-start', anchors.get('taskId'), anchors.get('attemptId'), seq)

        # Reactive recovery archives before L2 starts. Bind its still-pending,
        # unwitnessed placeholder rather than leave a permanent phase ghost.
        # Witnessed or settled archives cannot consume a later start.
        pending = [entry for entry in entries
                   if _same_producer(entry, anchors)
                   and entry.get('reasonCode') == 'context_compaction'
                   and entry.get('status') == 'running'
                   and entry.get('anchorPosition') == 'producer-start'
                   and not entry.get('anchorBlockId')
                   and entry.get('durationMs') == 0
                   and entry.get('startedAt') == entry.get('occurredAt')]
        if len(pending) == 1:
            pending[0].update({**anchors, 'startedAt': now, 'endedAt': now,
                               'durationMs': 0})
            return
    _append_or_update(entries, {
        'id': _entry_id(task, seq, raw_event=raw_event),
        **anchors,
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
    if not archive_id and not (raw_event.get('type') == EventType.COMPACTION_DONE
                               and (raw_event.get('receipt') or {}).get('status') == 'failed'):
        return
    producer = _producer_identity(raw_event, task)
    span_id = _stable_span(
        'compaction', producer.get('taskId'), producer.get('attemptId'),
        archive_id or f'failed:{seq}',
    )
    prior_index = _find_span(entries, span_id, producer)

    if prior_index is None and archive_id:
        # Older writers used the carrier attempt in the span hash. An already
        # projected owner-scoped archive occurrence still has its stable id.
        matches = [index for index, entry in enumerate(entries)
                   if entry.get('archiveId') == archive_id and _same_producer(entry, producer)]
        if len(matches) == 1:
            prior_index = matches[0]
            span_id = entries[prior_index]['spanId']
    prior = entries[prior_index] if prior_index is not None else {}
    # Archive creation can happen AFTER summarization. A late settlement for
    # an existing archive must not consume a newer same-round start phase.
    phase_rows = [
        entry for entry in entries
        if not prior and _same_producer(entry, producer)
        and entry.get('phase') == Phase.COMPACTING
        and entry.get('reasonCode') == Phase.COMPACTING
        and entry.get('status') in {'started', 'running', 'waiting'}
    ]
    phase_row = phase_rows[0] if len(phase_rows) == 1 else {}
    phase_start = phase_row.get('startedAt')
    if phase_row:
        entries.remove(phase_row)
    receipt = raw_event.get('receipt')

    # Existing start wins: settling cannot move after future same-round output.
    boundary_source = prior if prior.get('anchorPosition') else phase_row
    anchors = _producer_identity(raw_event, task)
    if boundary_source.get('anchorPosition'):
        anchors.update({key: boundary_source[key] for key in ('anchorBlockId', 'anchorPosition') if key in boundary_source})
    elif raw_event.get('anchorPosition'):
        # A preflight refusal has no running phase, but its producer can
        # record the real boundary before checking the budget. Reuse that
        # witness on reload; never derive it from later replay segments.
        anchors.update({key: raw_event[key] for key in ('anchorBlockId', 'anchorPosition') if key in raw_event})
    else:
        # Sparse replay/legacy has no witnessed start. Never inspect future
        # segments to fabricate it; producer-start placement is conservative.
        anchors['anchorPosition'] = 'producer-start'
    receipt = receipt if isinstance(receipt, Mapping) else {}
    receipt_status = _text(receipt.get('status'), 32).lower()
    completed = str(raw_event.get('type') or '') == EventType.COMPACTION_DONE
    started_at = int(prior.get('startedAt') or phase_start or now)
    summary = receipt.get('summary')
    summary_ms = (_nonnegative_int(summary.get('durationMs'))
                  if isinstance(summary, Mapping) else None)
    # A partial replay can lack the earlier phase. Its summary receipt still
    # proves a lower bound; never replace minutes of known work with the short
    # interval between archive insertion and final accounting.
    duration_ms = max(0, now - started_at, summary_ms or 0)
    failed = completed and receipt_status in {
        'failed', 'aborted', 'rejected', 'cancelled',
    }
    archive_failed = receipt.get('implementation') == 'archive_commit_failed'
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
    if archive_failed:
        detail = ''
    _append_or_update(entries, {
        'id': _entry_id(task, seq, raw_event=raw_event),
        **anchors,
        **compaction_attribution_from_receipt(receipt, raw_event.get('summaryDispatchCalls')),
        'spanId': span_id,
        'seq': seq,
        'occurredAt': now,
        'startedAt': started_at,
        'endedAt': now,
        'durationMs': duration_ms,
        'kind': 'status',
        'status': ('failed' if failed else 'succeeded' if completed else 'running'),
        'severity': 'warning' if failed else 'info',
        'summary': ('Could not save compacted context; original context retained.' if archive_failed else
                    'Context compaction made no reduction' if failed else
                    'Context compacted' if completed else
                    'Compacting context'),
        'summaryKey': ('activity.compaction.archiveFailed' if archive_failed else
                       'activity.compaction.failed' if failed else
                       'activity.compaction.succeeded' if completed else
                       'activity.compaction.running'),
        'detail': detail,
        'phase': Phase.COMPACTING,
        'reasonCode': 'context_compaction',
        **({'archiveId': archive_id} if archive_id else {}),
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
        'taskModel': _compaction_fact_text(raw_event.get('taskModel') or raw_event.get('model')
                                          or prior.get('taskModel') or task.get('model')),
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
    parent_span = _parent_request(raw_event, task)
    # The wire boundary re-isolates the same bad schema on EVERY model
    # dispatch of an attempt, each with a fresh per-dispatch parent span.
    # That repeated isolation is one durable fact, so the row span excludes
    # the dispatch span: repeats coalesce into a single counted row instead
    # of minting a duplicate per request.
    span_id = _stable_span(
        'schema', task.get('_attemptId'), tool_name, reason_code, detail,
    )
    _append_or_update(entries, {
        'id': _entry_id(task, seq, raw_event=raw_event),
        **_activity_anchors(raw_event, task),
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
        'id': _entry_id(task, seq, raw_event=raw_event),
        **_activity_anchors(raw_event, task),
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
    parent_span = _parent_request(raw_event, task)
    span_id = _stable_span(
        'error', task.get('_attemptId'), parent_span, reason_code, detail,
    )
    _append_or_update(entries, {
        'id': _entry_id(task, seq, raw_event=raw_event),
        **_activity_anchors(raw_event, task),
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
            entries, identity=_producer_identity(raw_event, task), status='failed', severity='error', ended_at=occurred_at,
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
            entries, identity=_producer_identity(raw_event, task),
            status=('failed' if terminal_error else
                    'aborted' if terminal_aborted else 'succeeded'),
            severity=('error' if terminal_error else
                      'warning' if terminal_aborted else None),
            ended_at=occurred_at,
        )
    elif event_type == 'aborted':
        _close_open_entries(
            entries, identity=_producer_identity(raw_event, task), status='aborted', severity='warning', ended_at=occurred_at,
        )
    # 'budget_warning' is historical: no longer emitted, still rendered for
    # pre-removal transcripts.
    elif event_type in {EventType.RETRY_RESET, 'budget_warning'}:
        detail = _diagnostic_text(
            raw_event.get('detail') or raw_event.get('content') or event_type,
        )
        _append_or_update(entries, {
            'id': _entry_id(task, seq, raw_event=raw_event),
            **_activity_anchors(raw_event, task),
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
    'capture_compaction_boundary',
    'fold_activity_timeline',
    'normalize_activity_timeline',
]
