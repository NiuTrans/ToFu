"""Turn-trace evidence contract — dependency-light shared core.

Responsibility: the versioned trace document budget constants plus the pure
merge/append/compaction functions that fold authoritative server evidence with
content-free browser perception receipts.

Entry points: ``append_client_trace_observation``,
``merge_client_trace_evidence``, ``compact_trace_document`` and the
``TRACE_*`` budget constants.

Dependencies: standard library only, deliberately. Both the task-execution
fold (``lib/tasks_pkg/turn_trace.py``) and the persistence sidecar
(``lib/storage_sidecar/operations_pkg/``) consume this module; the sidecar is
a separate process and must never import the task-execution graph. Keep this
module free of imports from ``lib.agent_core``, ``lib.orchestration``,
``lib.tasks_pkg`` and any other execution owner — ``make architecture-check``
rejects new heavy dependencies here.

Callers that historically imported these names from
``lib.tasks_pkg.turn_trace`` keep working: that module re-exports this
contract's symbols unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re

TRACE_CONTRACT_VERSION = 1

# The trace is durable user state once it is folded into its generation attempt.
# Keep that evidence useful on an 8 GiB personal computer without allowing a
# pathological agent loop to turn one conversation row into an unbounded event
# archive.  Exact aggregate clocks remain in ``summary`` when row-level detail
# is compacted.
TRACE_MAX_SPANS = 256
TRACE_MAX_GAPS = 128
TRACE_MAX_STATUS_ENTRIES = 128
TRACE_MAX_CLIENT_OBSERVATIONS = 64
TRACE_MAX_PERSISTED_BYTES = 96 * 1024
TRACE_MAX_COUNTER = 2_147_483_647

# One advisory policy for both live executor warnings and persisted turn traces.
# Human waits, model generation and arbitrary shell workloads are unbounded.
TOOL_EXECUTION_BUDGETS_MS = {
    'read_files': 5_000,
    'write_file': 2_000,
    'edit_file': 2_000,
    'apply_diff': 2_000,
    'apply_diffs': 2_000,
    'insert_content': 2_000,
    'insert_contents': 2_000,
    'grep_search': 20_000,
    'find_files': 10_000,
    'glob_search': 10_000,
    'list_dir': 5_000,
    'web_search': 60_000,
    'fetch_url': 60_000,
    'run_command': None,
}


CLIENT_OBSERVATION_KINDS = frozenset({
    'phase_painted',
    'terminal_painted',
    'transport_degraded',
    'transport_recovered',
})
_CLIENT_OBSERVATION_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:~-]{0,159}$')
_CLIENT_OBSERVATION_TEXT_LIMITS = {
    'observationId': 160,
    'attemptId': 128,
    'clientId': 64,
    'phase': 80,
    'detailKey': 160,
    'reason': 160,
    'healthState': 32,
    'visibility': 16,
}
_CLIENT_OBSERVATION_NUMERIC_LIMITS = {
    'serverEmittedAt': 9_007_199_254_740_991,
    'receivedAt': 9_007_199_254_740_991,
    'paintedAt': 9_007_199_254_740_991,
    'observedAt': 9_007_199_254_740_991,
    'durationMs': 9_007_199_254_740_991,
    'generation': 2_147_483_647,
    'projectionRevision': 9_007_199_254_740_991,
    'retryCount': 2_147_483_647,
    'clientDroppedBefore': 2_147_483_647,
}
_CLIENT_OBSERVATION_ALLOWED_FIELDS = frozenset({
    'kind', *_CLIENT_OBSERVATION_TEXT_LIMITS,
    *_CLIENT_OBSERVATION_NUMERIC_LIMITS,
})


def _bounded_trace_text(value, maximum=400):
    if value is None:
        return ''
    return str(value)[:maximum]


def _sanitize_trace_value(value, *, depth=0):
    """Return a small JSON-safe diagnostic value without content payloads."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:400]
    if depth >= 5:
        return _bounded_trace_text(value, 200)
    if isinstance(value, Mapping):
        out = {}
        for key, child in list(value.items())[:32]:
            name = _bounded_trace_text(key, 100)
            if not name or name.startswith('_'):
                continue
            out[name] = _sanitize_trace_value(child, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_trace_value(child, depth=depth + 1)
            for child in list(value)[:32]
        ]
    return _bounded_trace_text(value, 200)


def _bounded_trace_rows(rows, maximum, *, keep_recent=False):
    clean = [
        _sanitize_trace_value(row)
        for row in (rows or [])
        if isinstance(row, Mapping)
    ]
    if len(clean) <= maximum:
        return clean, 0
    if keep_recent:
        return clean[-maximum:], len(clean) - maximum
    head = maximum // 2
    tail = maximum - head
    return clean[:head] + clean[-tail:], len(clean) - maximum


def compact_trace_document(document, *, source=None):
    """Bound one live or terminal trace while preserving aggregate truth.

    Row detail is diagnostic/reconstructible; ``summary`` and top-level clocks
    remain exact.  When compaction is necessary the dropped counts make the
    loss explicit instead of presenting a deceptively complete waterfall.
    """
    if not isinstance(document, Mapping):
        return {}
    scalar_fields = (
        'version', 'taskId', 'eventsAvailable', 'eventLogAvailable', 'status',
        'running', 'coverage', 'coverageReason', 'clockSource', 'tStart', 'tEnd', 'totalMs',
    )
    out = {
        key: _sanitize_trace_value(document.get(key))
        for key in scalar_fields if key in document
    }
    if source or document.get('source'):
        out['source'] = _bounded_trace_text(
            source or document.get('source'), 40)
    if document.get('detailCompacted'):
        out['detailCompacted'] = True
    if document.get('compacted'):
        out['compacted'] = True
    dropped_over_budget = 0
    if isinstance(document.get('summary'), Mapping):
        out['summary'] = _sanitize_trace_value(document['summary'])
        if isinstance(document['summary'].get('overBudget'), list):
            over_budget_rows, dropped_over_budget = _bounded_trace_rows(
                document['summary']['overBudget'], TRACE_MAX_SPANS)
            out['summary']['overBudget'] = over_budget_rows

    spans, dropped_spans = _bounded_trace_rows(
        document.get('spans'), TRACE_MAX_SPANS)
    gaps, dropped_gaps = _bounded_trace_rows(
        document.get('gaps'), TRACE_MAX_GAPS)
    statuses, dropped_statuses = _bounded_trace_rows(
        document.get('statusHistory'), TRACE_MAX_STATUS_ENTRIES,
        keep_recent=True)
    observations, dropped_observations = _bounded_trace_rows(
        document.get('clientObservations'), TRACE_MAX_CLIENT_OBSERVATIONS,
        keep_recent=True)
    if spans or 'spans' in document:
        out['spans'] = spans
    if gaps or 'gaps' in document:
        out['gaps'] = gaps
    if statuses or 'statusHistory' in document:
        out['statusHistory'] = statuses
    if observations or 'clientObservations' in document:
        out['clientObservations'] = observations

    dropped_fields = (
        ('droppedSpans', dropped_spans),
        ('droppedGaps', dropped_gaps),
        ('statusDroppedCount', dropped_statuses),
        ('clientObservationDroppedCount', dropped_observations),
        ('overBudgetDroppedCount', dropped_over_budget),
    )
    for key, newly_dropped in dropped_fields:
        total = min(
            TRACE_MAX_COUNTER,
            max(0, int(document.get(key) or 0)) + newly_dropped,
        )
        if total:
            out[key] = total
            out['compacted'] = True

    def encoded_size():
        return len(json.dumps(
            out, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8'))

    # The shape caps normally stay well below 96 KiB. This final byte guard is
    # deliberately deterministic for unusually rich attempt diagnostics.
    for key, floor, dropped_key in (
        ('spans', 24, 'droppedSpans'),
        ('statusHistory', 16, 'statusDroppedCount'),
        ('gaps', 16, 'droppedGaps'),
        ('clientObservations', 16, 'clientObservationDroppedCount'),
    ):
        while encoded_size() > TRACE_MAX_PERSISTED_BYTES \
                and len(out.get(key) or []) > floor:
            rows = out[key]
            remove = max(1, len(rows) // 4)
            if key in {'statusHistory', 'clientObservations'}:
                del rows[:remove]
            else:
                middle = max(1, (len(rows) - remove) // 2)
                del rows[middle:middle + remove]
            out[dropped_key] = min(
                TRACE_MAX_COUNTER,
                int(out.get(dropped_key) or 0) + remove,
            )
            out['compacted'] = True

    if encoded_size() > TRACE_MAX_PERSISTED_BYTES:
        # Preserve identities/verdicts but shed verbose attributes before any
        # remaining incident receipt. This is evidence degradation, never an
        # authority or completion change.
        for span in out.get('spans') or []:
            if isinstance(span, dict) and span.get('attrs'):
                span['attrs'] = {}
        out['detailCompacted'] = True
        out['compacted'] = True

    # The normal floors favor a useful cross-section of each evidence lane,
    # but all lanes can simultaneously contain maximum-length strings. Continue
    # deterministically below those preference floors until the byte contract
    # is actually true. Aggregate clocks remain untouched; each loss is counted.
    for key, floor, dropped_key in (
        ('gaps', 0, 'droppedGaps'),
        ('spans', 1, 'droppedSpans'),
        ('statusHistory', 1, 'statusDroppedCount'),
        ('clientObservations', 1, 'clientObservationDroppedCount'),
    ):
        while encoded_size() > TRACE_MAX_PERSISTED_BYTES \
                and len(out.get(key) or []) > floor:
            rows = out[key]
            remove = max(1, min(len(rows) - floor, len(rows) // 4))
            if key in {'statusHistory', 'clientObservations'}:
                del rows[:remove]
            else:
                middle = max(0, (len(rows) - remove) // 2)
                del rows[middle:middle + remove]
            out[dropped_key] = min(
                TRACE_MAX_COUNTER,
                int(out.get(dropped_key) or 0) + remove,
            )
            out['detailCompacted'] = True
            out['compacted'] = True

    # ``summary.overBudget`` is a diagnostic worklist, not part of the disjoint
    # aggregate clocks. Trim it only after row evidence; exact bucket totals are
    # retained. This also protects the hard cap from 256 maximum-length names.
    summary = out.get('summary')
    over_budget = (
        summary.get('overBudget')
        if isinstance(summary, dict) else None
    )
    while encoded_size() > TRACE_MAX_PERSISTED_BYTES \
            and isinstance(over_budget, list) and over_budget:
        del over_budget[max(0, len(over_budget) // 2)]
        out['overBudgetDroppedCount'] = min(
            TRACE_MAX_COUNTER,
            int(out.get('overBudgetDroppedCount') or 0) + 1,
        )
        out['detailCompacted'] = True
        out['compacted'] = True

    if encoded_size() > TRACE_MAX_PERSISTED_BYTES:
        # Absolute fail-safe for corrupt/future over-wide detail. Preserve only
        # the closed aggregate-clock contract and top-level identity/verdicts.
        for key, dropped_key in (
            ('gaps', 'droppedGaps'),
            ('spans', 'droppedSpans'),
            ('statusHistory', 'statusDroppedCount'),
            ('clientObservations', 'clientObservationDroppedCount'),
        ):
            removed = len(out.get(key) or [])
            if removed:
                out[key] = []
                out[dropped_key] = min(
                    TRACE_MAX_COUNTER,
                    int(out.get(dropped_key) or 0) + removed,
                )
        if isinstance(summary, dict):
            remaining_over_budget = len(summary.get('overBudget') or [])
            if remaining_over_budget:
                out['overBudgetDroppedCount'] = min(
                    TRACE_MAX_COUNTER,
                    int(out.get('overBudgetDroppedCount') or 0)
                    + remaining_over_budget,
                )
            aggregate_keys = (
                'totalMs', 'llmMs', 'toolMs', 'waitMs', 'compactionMs',
                'approvalWaitMs', 'unattributedMs', 'ttftMs',
            )
            out['summary'] = {
                key: summary[key]
                for key in aggregate_keys if key in summary
            }
            out['summary']['overBudget'] = []
        out['detailCompacted'] = True
        out['compacted'] = True
    if any(out.get(key) for key in (
        'droppedSpans', 'droppedGaps', 'statusDroppedCount',
        'clientObservationDroppedCount', 'overBudgetDroppedCount',
        'detailCompacted',
    )):
        out['compacted'] = True
    return out


def append_client_trace_observation(
    timing_trace,
    observation,
    *,
    task_id,
    attempt_id,
    recorded_at_ms,
):
    """Append one content-free browser perception receipt, idempotently."""
    if not isinstance(observation, Mapping):
        raise ValueError('observation must be an object')
    unknown_fields = set(observation) - _CLIENT_OBSERVATION_ALLOWED_FIELDS
    if unknown_fields:
        raise ValueError(
            f'unsupported perception fields: {sorted(unknown_fields)!r}')
    for field, maximum in _CLIENT_OBSERVATION_TEXT_LIMITS.items():
        value = observation.get(field)
        if field in {'observationId', 'attemptId', 'clientId'} and not value:
            raise ValueError(f'{field} is required')
        if value is not None and (
            not isinstance(value, str) or len(value) > maximum
        ):
            raise ValueError(f'{field} must be a string of at most {maximum}')
    observation_id = str(observation.get('observationId') or '')
    if _CLIENT_OBSERVATION_ID.fullmatch(observation_id) is None:
        raise ValueError('observationId has an invalid format')
    if str(observation.get('attemptId') or '') != str(attempt_id or ''):
        raise ValueError('attemptId does not match the authoritative attempt')
    if observation.get('visibility') not in {None, 'visible', 'hidden'}:
        raise ValueError('invalid perception visibility')
    if observation.get('healthState') not in {
        None, 'idle', 'connecting', 'live', 'recovering', 'degraded',
        'offline', 'closed',
    }:
        raise ValueError('invalid perception healthState')
    for field, maximum in _CLIENT_OBSERVATION_NUMERIC_LIMITS.items():
        value = observation.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(float(value)) \
                or float(value) != int(value) \
                or not 0 <= int(value) <= maximum:
            raise ValueError(f'{field} must be a nonnegative safe integer')
    kind = str(observation.get('kind') or '')
    if kind not in CLIENT_OBSERVATION_KINDS:
        raise ValueError('invalid perception observation kind')

    trace = dict(timing_trace) if isinstance(timing_trace, Mapping) else {}
    observations = [
        dict(item) for item in trace.get('clientObservations') or []
        if isinstance(item, Mapping)
    ]
    if any(item.get('observationId') == observation_id
           for item in observations):
        return compact_trace_document(trace), False

    row = {
        'observationId': observation_id,
        'kind': kind,
        'taskId': _bounded_trace_text(task_id, 256),
        'attemptId': _bounded_trace_text(attempt_id, 128),
        'clientId': _bounded_trace_text(observation.get('clientId'), 64),
        'recordedAt': max(0, int(recorded_at_ms or 0)),
    }
    for source, maximum in (
        ('phase', 80), ('detailKey', 160), ('reason', 160),
        ('healthState', 32),
        ('visibility', 16),
    ):
        value = _bounded_trace_text(observation.get(source), maximum)
        if value:
            row[source] = value
    for field in _CLIENT_OBSERVATION_NUMERIC_LIMITS:
        value = observation.get(field)
        if value is None:
            continue
        row[field] = int(value)

    received_at = row.get('receivedAt')
    painted_at = row.get('paintedAt')
    if received_at is not None and painted_at is not None:
        row['renderMs'] = max(0, min(600_000, painted_at - received_at))
    emitted_at = row.get('serverEmittedAt')
    if emitted_at is not None and received_at is not None:
        transport_ms = received_at - emitted_at
        if -60_000 <= transport_ms <= 24 * 60 * 60 * 1000:
            row['transportMs'] = max(0, transport_ms)
            if transport_ms < 0:
                row['clockSkewSuspected'] = True
        else:
            row['clockSkewSuspected'] = True

    observations.append(row)
    overflow = max(0, len(observations) - TRACE_MAX_CLIENT_OBSERVATIONS)
    if overflow:
        del observations[:overflow]
    trace.update({
        'version': TRACE_CONTRACT_VERSION,
        'taskId': _bounded_trace_text(task_id, 256),
        'clientObservations': observations,
    })
    if overflow:
        trace['clientObservationDroppedCount'] = min(
            TRACE_MAX_COUNTER,
            max(0, int(trace.get('clientObservationDroppedCount') or 0))
            + overflow,
        )
    return compact_trace_document(trace, source=trace.get('source')), True


def merge_client_trace_evidence(
    server_trace,
    receipt_trace,
    *,
    task_id,
):
    """Overlay the authoritative per-attempt browser receipt lane.

    The attempt row serializes receipt writes with terminal settlement.  When
    it contains a receipt list, that list is therefore the complete bounded
    browser lane and replaces any older copy carried by a Turn projection.
    Server spans/status remain owned by ``server_trace``.
    """
    merged = dict(server_trace) if isinstance(server_trace, Mapping) else {}
    receipts = dict(receipt_trace) if isinstance(receipt_trace, Mapping) else {}
    receipt_task_id = str(receipts.get('taskId') or '')
    if receipt_task_id and receipt_task_id != str(task_id or ''):
        return compact_trace_document(merged, source=merged.get('source'))
    if 'clientObservations' in receipts:
        merged['clientObservations'] = receipts.get('clientObservations') or []
        if receipts.get('clientObservationDroppedCount'):
            merged['clientObservationDroppedCount'] = max(
                0, int(receipts.get('clientObservationDroppedCount') or 0))
        else:
            merged.pop('clientObservationDroppedCount', None)
    if merged:
        merged['version'] = TRACE_CONTRACT_VERSION
        merged['taskId'] = _bounded_trace_text(task_id, 256)
    return compact_trace_document(merged, source=merged.get('source'))
