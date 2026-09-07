"""Turn trace — server-authoritative per-task timing fold (the unified
"where did the time go" interface).

Contract: ``docs/TURN_TRACE_CONTRACT.md``. One pure fold derives the
hierarchical span tree — turn → round → llm / tool / wait / compaction — from
the bounded task event log. The canonical event chokepoint also projects a
small history of the exact status prompts a browser could show, and terminal
settlement freezes both into the durable generation-attempt document (with a
current-Turn mirror) before reconstructible event rows expire. Browser paint
and transport receipts append content-free observations to the attempt document.

Design invariants
=================
1. **Derive, don't instrument.** Every span is folded from the declared
   event vocabulary (``lib/agent_core/events.py``). The phase→span mapping
   is the declared ``_PHASE_TRACE_RULE`` table below; a drift test pins
   every registered chat-domain phase to a rule.
2. **Strict accounting.** The summary buckets are a DISJOINT partition of
   the turn interval (priority allocation), and whatever cannot be
   attributed is an explicit ``unattributedMs`` + ``gaps`` list — never a
   silent hole. Buckets always sum to ``totalMs``.
3. **Budgets are declarations, not verdicts.** ``_TOOL_BUDGETS_MS`` /
   ``_KIND_BUDGETS_MS`` declare the expected upper bound for spans that
   HAVE one (a local file read has an expected cost; an LLM deep-think or
   a user ``run_command`` workload legitimately does not — those are
   declared ``None``, "unbounded, do not flag"). An over-budget span only
   gets ``overBudget: true`` — the optimization worklist is read off the
   summary, the fold never judges.

Row shapes
==========
``fold_task_trace(task_id)`` →
    ``{version, taskId, eventsAvailable, status, coverage, tStart, tEnd,
       totalMs, running, summary, spans, gaps}``
    Span: ``{spanId, parent, depth, kind, name, tStart, tEnd, status,
       attrs, budgetMs?, overBudget?, truncated?}``
    Summary: ``{totalMs, llmMs, toolMs, waitMs, compactionMs,
       approvalWaitMs, unattributedMs, ttftMs, overBudget}``
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re
import time

from lib.agent_core.events import Phase
from lib.log import get_logger
from lib.orchestration_message_compat import is_flow_event_type

logger = get_logger(__name__)

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
TRACE_SOURCES = frozenset({
    'event-log', 'live-projection', 'turn-snapshot', 'attempt-receipts',
})

_TASK_STATUS_HISTORY_KEY = '_timingStatusHistory'
_TASK_STATUS_DROPPED_KEY = '_timingStatusDroppedCount'
_TASK_STATUS_SEQUENCE_KEY = '_timingStatusSequence'
_TRACE_TERMINAL_TYPES = frozenset({'done', 'error', 'aborted', 'interrupted'})
_STATUS_CLEAR_TYPES = frozenset({'delta', *_TRACE_TERMINAL_TYPES})
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

# ── The declared phase → trace-rule mapping (drift-guarded) ──
# Every chat-domain phase in the registry MUST appear here
# (tests/test_turn_trace.py enforces both directions). Rules:
#   'ttft'       — opens the time-to-first-token candidate span for its round
#   'retry_wait' — opens/extends a wait span (same-phase beats coalesce)
#   'compaction' — opens the compaction span (closed by compaction_done)
#   'covered'    — no own span: its time is inside a structural span
#                  (llm_thinking ⊂ llm span; tool_exec ⊂ the tool spans)
#   'ignore'     — momentary notice, not a duration; any time it actually
#                  covers surfaces honestly as a gap (unattributed)
_PHASE_TRACE_RULE: dict[str, str] = {
    Phase.WAITING_MODEL: 'ttft',
    Phase.RETRYING: 'retry_wait',
    Phase.EXECUTOR_QUEUED: 'retry_wait',
    Phase.COMPACTING: 'compaction',
    Phase.LLM_THINKING: 'covered',
    Phase.STREAM_STALLED: 'covered',
    Phase.TOOL_EXEC: 'covered',
    Phase.WORKING: 'ignore',
    Phase.TODO_CONTINUATION: 'ignore',
    Phase.INTENT_STALL_NUDGE: 'ignore',
    Phase.TOOL_HISTORY_RESTORED: 'ignore',
    Phase.TOOL_AUTHORITY: 'ignore',
}

# ── Expected-duration budgets (ms) — declared, advisory, never a verdict ──
# A span whose elapsed exceeds its budget gets budgetMs + overBudget:true;
# the summary lists them as the optimization worklist. ``None`` = DECLARED
# UNBOUNDED (do not flag): LLM deep-think, human approval time, retry waits
# imposed by upstream rate limits, and user run_command workloads can all
# legitimately take any duration — flagging them would be noise (owner
# ruling 2026-08-20: "大模型请求深度思考比较久，我们可以不管").
# LOCAL tools (file/search primitives) have real expectations; a breach
# means something worth investigating (a wedged FS, a cross-DC mount, …).
_TOOL_BUDGETS_MS: dict[str, int | None] = {
    'read_files': 10_000,
    'write_file': 15_000,
    'edit_file': 15_000,
    'grep_search': 20_000,
    'glob_search': 10_000,
    'list_dir': 5_000,
    'web_search': 60_000,
    'fetch_url': 60_000,
    'run_command': None,
}
_KIND_BUDGETS_MS: dict[str, int | None] = {
    'llm': None,
    'llm_ttft': None,
    'retry_wait': None,
    'approval_wait': None,
    'compaction': 120_000,
    'spawn_wait': 10_000,
}

# Payload-bearing event types the fold reads. ``delta`` is deliberately NOT
# here: deltas are the per-token streaming noise (a 50k-row task is mostly
# deltas), so they are read as a ts-only spine for the TTFT boundary.
_TRACE_PAYLOAD_TYPES: frozenset[str] = frozenset({
    'round_start', 'round_end', 'phase',
    'tool_start', 'tool_progress', 'tool_result', 'tool_complete',
    'compaction', 'compaction_done',
    'write_approval_request',
    'round_usage', 'retry_reset', 'model_fallback',
    'model_request_start', 'model_request_complete',
    'done', 'error', 'aborted', 'interrupted',
})

# Bucket priority for the DISJOINT summary allocation — a millisecond is
# attributed to the highest-priority span covering it (an approval wait
# inside a tool span is approval time, not tool time; a 429 retry inside
# the LLM window is wait time, not model time).
_BUCKET_PRIORITY: tuple[tuple[str, str], ...] = (
    ('approval_wait', 'approvalWaitMs'),
    ('compaction', 'compactionMs'),
    ('retry_wait', 'waitMs'),
    ('spawn_wait', 'waitMs'),
    ('tool', 'toolMs'),
    ('llm', 'llmMs'),
    ('llm_ttft', 'llmMs'),
)

_SPAN_OVERLAP_CLOSE_TYPES = frozenset({
    'phase', 'delta', 'tool_start', 'round_start', 'round_end',
    'model_request_start', 'compaction_done', 'done', 'error',
})


# ── Sidecar-authoritative row read ──

_TRACE_CACHE: dict[str, tuple] = {}
_TRACE_CACHE_TTL_S = 3.0
_TRACE_CACHE_MAX = 8


def invalidate_trace_cache(task_id: str) -> None:
    """Drop cached trace rows for ONE task (write-side read-your-writes;
    called from ``event_log.append_persistent_event``)."""
    if task_id:
        _TRACE_CACHE.pop(task_id, None)


def _read_trace_rows(task_id: str) -> list:
    """Return [{event_id, type, payload, ts_ms}] ordered by event_id.

    Payload rows for the structural slice + ts-only spine rows for
    ``delta`` (the TTFT boundary; parsing 50k per-token payloads would be
    pure waste). CACHED with the same short-TTL + write-invalidation
    discipline as the Request Inspector read. Read-only; never throws.
    """
    if not task_id:
        return []
    now = time.time()
    hit = _TRACE_CACHE.get(task_id)
    if hit is not None and (now - hit[0]) < _TRACE_CACHE_TTL_S:
        return hit[1]
    rows = _read_trace_rows_uncached(task_id)
    if len(_TRACE_CACHE) >= _TRACE_CACHE_MAX:
        try:
            oldest = min(_TRACE_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _TRACE_CACHE.pop(oldest, None)
        except ValueError:
            _TRACE_CACHE.clear()
    _TRACE_CACHE[task_id] = (now, rows)
    return rows


def _read_trace_rows_uncached(task_id: str) -> list:
    """Read the bounded trace projection from the Sidecar authority."""
    try:
        from lib.storage import get_storage_client

        out = []
        after = -1
        scanned = 0
        client = get_storage_client()
        while True:
            rows = client.query(
                'event.list', {'task_id': task_id,
                               'after_sequence': after, 'limit': 1000}) or []
            if not rows:
                break
            scanned += len(rows)
            for row in rows:
                payload = row.get('event') or {}
                event_type = payload.get('type') or ''
                projected_payload = {} if event_type == 'delta' else payload
                if (event_type == 'delta'
                        or event_type in _TRACE_PAYLOAD_TYPES
                        or is_flow_event_type(event_type)):
                    out.append({
                        'event_id': int(row.get('sequence', 0)),
                        'type': event_type,
                        'payload': projected_payload,
                        'ts_ms': int(row.get('created_at_ms') or 0),
                    })
            after = int(rows[-1].get('sequence', after))
            if len(rows) < 1000 or scanned >= 200_000:
                break
        out.sort(key=lambda row: row['event_id'])
        return out
    except Exception as e:
        logger.warning('[TurnTrace] Sidecar event read failed task=%s: %s',
                       task_id[:8], e)
        return []


# ── Span helpers ──

def _span(span_id, parent, depth, kind, name, t_start, status='running',
          attrs=None):
    return {
        'spanId': span_id, 'parent': parent, 'depth': depth, 'kind': kind,
        'name': name, 'tStart': int(t_start) if t_start else None,
        'tEnd': None, 'status': status, 'attrs': attrs or {},
    }


def _close(span, t_end, status=None):
    if span.get('tEnd') is None:
        span['tEnd'] = int(t_end) if t_end else None
    if status:
        span['status'] = status


def _elapsed(span):
    if span.get('tStart') is None or span.get('tEnd') is None:
        return None
    return max(0, span['tEnd'] - span['tStart'])


def _safe_nonnegative_int(value, default=0):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return max(0, int(default or 0))


def _safe_seconds_to_ms(value, default=0.0):
    try:
        return max(0, round(float(value or 0) * 1000))
    except (TypeError, ValueError, OverflowError):
        return max(0, round(float(default or 0) * 1000))


def _bounded_trace_text(value, maximum=400):
    if value is None:
        return ''
    return str(value)[:maximum]


def _bounded_status_args(value):
    """Keep only the small primitive interpolation evidence the user saw."""
    if not isinstance(value, Mapping):
        return {}
    out = {}
    for key, child in list(value.items())[:16]:
        name = _bounded_trace_text(key, 80)
        if not name:
            continue
        if isinstance(child, bool) or child is None:
            out[name] = child
        elif isinstance(child, (int, float)):
            out[name] = child
        elif isinstance(child, str):
            out[name] = child[:200]
    return out


def _status_signature(entry):
    args = entry.get('detailArgs') or {}
    return (
        entry.get('phase') or '',
        entry.get('detailKey') or '',
        args.get('reasonKey') or '',
        entry.get('attempt'),
        entry.get('statusCode'),
        entry.get('roundNum'),
    )


def _status_attention(phase):
    if phase == Phase.STREAM_STALLED:
        return 'stall'
    if phase in {
        Phase.WAITING_MODEL, Phase.RETRYING, Phase.EXECUTOR_QUEUED,
        Phase.COMPACTING,
    }:
        return 'wait'
    return 'progress'


def _close_status_history(history, observed_at_ms, *, terminal=False):
    if not history:
        return
    current = history[-1]
    if current.get('tEnd') is not None:
        return
    current['tEnd'] = max(
        int(current.get('tStart') or 0), int(observed_at_ms or 0))
    current.setdefault('lastObservedAt', current['tStart'])
    if terminal:
        current['terminalBoundary'] = True


def observe_task_trace_event(task, event, *, observed_at_ms=None):
    """Fold one canonical task event into a bounded user-visible phase ledger.

    This is a projection at the existing event chokepoint, not a second event
    vocabulary.  It exists because provider-ingress isolation intentionally
    keeps some live phase frames memory-local until the next authoritative
    boundary; the terminal Turn still needs to remember the exact prompt the
    browser could have shown during that window.
    """
    if not isinstance(task, dict) or not isinstance(event, Mapping):
        return
    event_type = str(event.get('type') or '')
    if event_type != 'phase' and event_type not in _STATUS_CLEAR_TYPES \
            and event_type != 'compaction_done':
        return
    now = int(observed_at_ms if observed_at_ms is not None else time.time() * 1000)
    history = task.get(_TASK_STATUS_HISTORY_KEY)
    if not isinstance(history, list):
        history = []
        task[_TASK_STATUS_HISTORY_KEY] = history

    if event_type == 'phase':
        phase = _bounded_trace_text(event.get('phase'), 80)
        if not phase:
            return
        entry = {
            'id': '',
            'phase': phase,
            'attention': _status_attention(phase),
            'tStart': now,
            'lastObservedAt': now,
            'count': 1,
            'detail': _bounded_trace_text(event.get('detail'), 400),
            'detailKey': _bounded_trace_text(event.get('detailKey'), 160),
            'detailArgs': _bounded_status_args(event.get('detailArgs')),
        }
        for source, target, maximum in (
            ('model', 'model', 160),
            ('bucket', 'bucket', 80),
            ('reason', 'reason', 160),
        ):
            value = _bounded_trace_text(event.get(source), maximum)
            if value:
                entry[target] = value
        for source, target in (
            ('attempt', 'attempt'), ('statusCode', 'statusCode'),
            ('roundNum', 'roundNum'), ('queuePosition', 'queuePosition'),
            ('queued', 'queued'), ('active', 'active'),
            ('capacity', 'capacity'), ('waitSeconds', 'waitSeconds'),
        ):
            value = event.get(source)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                entry[target] = value

        current = history[-1] if history else None
        if current is not None and current.get('tEnd') is None \
                and _status_signature(current) == _status_signature(entry):
            current['lastObservedAt'] = now
            current['count'] = max(1, int(current.get('count') or 1)) + 1
            # Heartbeats carry the newest elapsed/queue evidence. Preserve the
            # interval identity while refreshing only bounded presentation data.
            for key in (
                'detail', 'detailKey', 'detailArgs', 'model', 'bucket',
                'reason', 'attempt', 'statusCode', 'roundNum',
                'queuePosition', 'queued', 'active', 'capacity', 'waitSeconds',
            ):
                if key in entry and entry[key] not in ('', {}, None):
                    current[key] = entry[key]
            return

        _close_status_history(history, now)
        sequence = int(task.get(_TASK_STATUS_SEQUENCE_KEY) or 0) + 1
        task[_TASK_STATUS_SEQUENCE_KEY] = sequence
        entry['id'] = f'status.{sequence}'
        history.append(entry)
        if len(history) > TRACE_MAX_STATUS_ENTRIES:
            overflow = len(history) - TRACE_MAX_STATUS_ENTRIES
            del history[:overflow]
            task[_TASK_STATUS_DROPPED_KEY] = int(
                task.get(_TASK_STATUS_DROPPED_KEY) or 0) + overflow
        return

    if event_type == 'compaction_done':
        if history and history[-1].get('phase') == Phase.COMPACTING:
            _close_status_history(history, now)
        return
    _close_status_history(
        history, now, terminal=event_type in _TRACE_TERMINAL_TYPES)


def task_status_history(task):
    """Return a detached bounded status history for a Turn projection."""
    if not isinstance(task, Mapping):
        return [], 0
    history = task.get(_TASK_STATUS_HISTORY_KEY)
    if not isinstance(history, list):
        return [], max(0, int(task.get(_TASK_STATUS_DROPPED_KEY) or 0))
    detached = [
        _sanitize_trace_value(entry)
        for entry in history[-TRACE_MAX_STATUS_ENTRIES:]
        if isinstance(entry, Mapping)
    ]
    dropped = max(0, int(task.get(_TASK_STATUS_DROPPED_KEY) or 0))
    dropped += max(0, len(history) - len(detached))
    return detached, dropped


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
        'running', 'coverage', 'coverageReason', 'tStart', 'tEnd', 'totalMs',
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


def _apply_budget(span):
    """Stamp budgetMs / overBudget from the DECLARED budget tables.

    A tool's budget is looked up by name; an unknown tool has NO declared
    budget (silence is not a verdict — add a row to declare one). Kind
    budgets apply to non-tool spans. ``None`` = declared unbounded.
    """
    if span['kind'] == 'tool':
        tool = span['attrs'].get('toolName') or ''
        if tool not in _TOOL_BUDGETS_MS:
            return
        budget = _TOOL_BUDGETS_MS[tool]
    else:
        if span['kind'] not in _KIND_BUDGETS_MS:
            return
        budget = _KIND_BUDGETS_MS[span['kind']]
    if budget is None:
        return
    span['budgetMs'] = budget
    elapsed = _elapsed(span)
    if elapsed is not None and elapsed > budget:
        span['overBudget'] = True


def _union_length(intervals):
    """Total ms covered by [[tStart, tEnd], ...] (sweep merge)."""
    iv = sorted((a, b) for a, b in intervals
                if a is not None and b is not None and b > a)
    total = 0
    cur_a = cur_b = None
    for a, b in iv:
        if cur_a is None:
            cur_a, cur_b = a, b
        elif a <= cur_b:
            cur_b = max(cur_b, b)
        else:
            total += cur_b - cur_a
            cur_a, cur_b = a, b
    if cur_a is not None:
        total += cur_b - cur_a
    return total


def _disjoint_summary(spans, t0, t1):
    """Priority-allocate every ms of [t0, t1] to exactly ONE bucket.

    Elementary-segment sweep over span boundaries: each segment picks the
    highest-priority span kind covering it (``_BUCKET_PRIORITY``). The
    buckets therefore sum EXACTLY to totalMs — strict accounting, no
    double-count when spans nest (approval ⊂ tool, retry ⊂ llm window).
    """
    prio_kinds = {kind for kind, _b in _BUCKET_PRIORITY}
    points = {t0, t1}
    ivs = []
    for s in spans:
        if s['kind'] not in prio_kinds:
            continue
        a, b = s.get('tStart'), s.get('tEnd')
        if a is None or b is None or b <= a:
            continue
        a, b = max(a, t0), min(b, t1)
        if b > a:
            ivs.append((a, b, s['kind']))
            points.add(a)
            points.add(b)
    out = {bucket: 0 for _k, bucket in _BUCKET_PRIORITY}
    pts = sorted(points)
    for lo, hi in zip(pts, pts[1:]):
        if hi <= lo:
            continue
        # Highest-priority covering kind wins this elementary segment.
        for kind, bucket in _BUCKET_PRIORITY:
            if any(a <= lo and b >= hi and k == kind for a, b, k in ivs):
                out[bucket] += hi - lo
                break
    covered = sum(out.values())
    out['unattributedMs'] = max(0, (t1 - t0) - covered)
    return out


def _fold_status_history(rows, *, settle_ts, running):
    projected_task = {}
    for row in rows:
        payload = row.get('payload') or {'type': row.get('type') or ''}
        observe_task_trace_event(
            projected_task, payload, observed_at_ms=row.get('ts_ms'))
    history = projected_task.get(_TASK_STATUS_HISTORY_KEY) or []
    if history and history[-1].get('tEnd') is None and not running:
        _close_status_history(history, settle_ts, terminal=True)
    return task_status_history(projected_task)


def project_running_trace_status(projection, task):
    """Attach the bounded server phase ledger without disturbing client receipts."""
    next_projection = dict(projection or {})
    task_id = str(task.get('id') or task.get('taskId') or '')
    previous = next_projection.get('timingTrace')
    if isinstance(previous, Mapping) \
            and str(previous.get('taskId') or '') != task_id:
        # A regenerate/continue attempt must never inherit the previous
        # attempt's timing evidence merely because it reuses the Turn row.
        next_projection.pop('timingTrace', None)
        previous = None
    history, dropped = task_status_history(task)
    if not history and not dropped:
        return next_projection
    trace = dict(previous) if isinstance(previous, Mapping) else {}
    trace.update({
        'version': TRACE_CONTRACT_VERSION,
        'taskId': task_id,
        'running': True,
        'status': 'running',
        'statusHistory': history,
    })
    if dropped:
        trace['statusDroppedCount'] = dropped
    next_projection['timingTrace'] = compact_trace_document(
        trace, source=trace.get('source') or 'live-projection')
    return next_projection


def finalize_trace_projection(
    projection,
    task,
    raw_event,
    *,
    now_ms,
    pending_sequence=None,
):
    """Fold and freeze the terminal trace while preserving client evidence."""
    task_id = str(task.get('id') or task.get('taskId') or '')
    history, dropped = task_status_history(task)
    document = fold_task_trace(
        task_id,
        now_ms=now_ms,
        pending_event=raw_event,
        pending_sequence=pending_sequence,
        status_history=history or None,
        status_dropped_count=dropped,
    )
    if not document.get('eventsAvailable'):
        return dict(projection or {})
    previous_trace = (
        projection.get('timingTrace')
        if isinstance(projection, Mapping) else None
    )
    if isinstance(previous_trace, Mapping) \
            and str(previous_trace.get('taskId') or '') == task_id:
        observations = previous_trace.get('clientObservations')
        if isinstance(observations, list):
            document['clientObservations'] = observations
        observation_dropped = previous_trace.get(
            'clientObservationDroppedCount')
        if observation_dropped:
            document['clientObservationDroppedCount'] = max(
                0, int(observation_dropped))
    document['running'] = False
    document['source'] = 'turn-snapshot'
    document['eventLogAvailable'] = True
    next_projection = dict(projection or {})
    next_projection['timingTrace'] = compact_trace_document(
        document, source='turn-snapshot')
    return next_projection


def read_persisted_task_trace(task_id, *, user_id):
    """Read one permanent owner-scoped attempt trace by executor task id."""
    if not task_id:
        return None
    try:
        from lib.storage import get_storage_client
        result = get_storage_client().query(
            'turn.timing_trace.get', {
                'task_id': str(task_id), 'user_id': int(user_id),
            })
    except Exception as exc:
        logger.warning('[TurnTrace] durable trace read failed task=%s: %s',
                       str(task_id)[:8], exc)
        return None
    if not isinstance(result, Mapping):
        return None
    trace = result.get('timingTrace')
    if not isinstance(trace, Mapping):
        return None
    document = dict(trace)
    has_trace_evidence = bool(
        document.get('eventsAvailable')
        or isinstance(document.get('summary'), Mapping)
        or document.get('spans')
        or document.get('statusHistory')
        or document.get('clientObservations')
        or document.get('tStart') is not None
    )
    document['version'] = TRACE_CONTRACT_VERSION
    document['taskId'] = str(task_id)
    document['eventsAvailable'] = has_trace_evidence
    document['eventLogAvailable'] = False
    source = str(document.get('source') or 'attempt-receipts')
    if source not in TRACE_SOURCES:
        source = 'attempt-receipts'
    return compact_trace_document(document, source=source)


# ── The fold ──

def fold_task_trace(
    task_id: str,
    now_ms: int | None = None,
    *,
    pending_event: Mapping | None = None,
    pending_sequence: int | None = None,
    status_history: list | None = None,
    status_dropped_count: int = 0,
) -> dict:
    """Fold a task's persisted event log into the timing span tree.

    Never throws on shape: unknown/expired tasks return
    ``eventsAvailable:false`` (the Request Inspector honesty precedent).
    ``now_ms`` is the server clock used to bound still-running spans;
    injectable for tests.
    """
    rows = list(_read_trace_rows(task_id))
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    if isinstance(pending_event, Mapping):
        event_type = str(pending_event.get('type') or '')
        try:
            sequence = int(pending_sequence) if pending_sequence is not None \
                else (max((int(row.get('event_id') or 0) for row in rows),
                          default=-1) + 1)
        except (TypeError, ValueError, OverflowError):
            sequence = max((int(row.get('event_id') or 0) for row in rows),
                           default=-1) + 1
        if not any(int(row.get('event_id') or -1) == sequence for row in rows):
            rows.append({
                'event_id': sequence,
                'type': event_type,
                'payload': dict(pending_event),
                'ts_ms': now_ms,
            })
            rows.sort(key=lambda row: int(row.get('event_id') or 0))
    base = {
        'version': TRACE_CONTRACT_VERSION,
        'taskId': task_id,
        'eventsAvailable': bool(rows),
        'eventLogAvailable': bool(rows) and pending_event is None,
        'source': 'event-log',
    }
    if not rows:
        return base

    t0 = rows[0]['ts_ms']
    terminal = None
    flow_seen = False
    for r in rows:
        if r['type'] in _TRACE_TERMINAL_TYPES:
            terminal = r
        elif is_flow_event_type(r['type']):
            flow_seen = True
    running = terminal is None
    t_end_turn = terminal['ts_ms'] if terminal else None
    eff_end = t_end_turn if t_end_turn is not None else now_ms
    if eff_end < t0:
        eff_end = t0

    status = 'running'
    if terminal is not None:
        if terminal['type'] == 'error' or terminal['payload'].get('error'):
            status = 'error'
        else:
            fr = (terminal['payload'].get('finishReason') or '')
            status = ('aborted' if terminal['type'] in {'aborted', 'interrupted'}
                      or fr in {'aborted', 'interrupted'} else 'done')

    spans: list[dict] = []
    turn_span = _span('turn', None, 0, 'turn', 'turn', t0,
                      status=('running' if running else status))
    if not running:
        _close(turn_span, t_end_turn, status)
    spans.append(turn_span)

    rounds: dict[str, dict] = {}       # roundNum(str) -> round span
    round_order: list[str] = []
    cur_round_key: str | None = None
    # Call ids are provider correlation tokens, not task-global identities.
    # Keep occurrence-ordered spans so positional ``call_0`` reuse and even a
    # malformed duplicate inside one round cannot overwrite an older span.
    open_tools: list[dict] = []
    tool_span_occurrences: dict[tuple[str, str], int] = {}
    open_model_requests: dict[str, dict] = {}  # diagnostic spanId -> span
    pending_approval: dict[tuple[str, str], list[float]] = {}
    open_phase_span: dict | None = None  # retry_wait / compaction span
    ttft_open: dict | None = None      # {'round': key, 'tStart': ts}
    usage_by_round: dict[str, list] = {}
    usage_orphans: list[dict] = []
    saw_round_markers = False
    counters = {'wait': 0, 'compact': 0, 'flat_tool': 0}

    def _tool_event_round_key(payload: dict) -> str:
        return str(payload.get('roundNum', '')) or (cur_round_key or '')

    def _find_open_tool(round_key: str, call_id: str, *,
                        require_unapproved: bool = False,
                        require_unmarked: bool = False) -> dict | None:
        candidates = [
            span for span in open_tools
            if (not round_key or span['attrs'].get('roundKey') == round_key)
            and (not call_id or span['attrs'].get('toolCallId') == call_id)
            and (not require_unapproved
                 or '_approval' not in span['attrs'])
            and (not require_unmarked
                 or not span['attrs'].get('_execMarked'))
        ]
        # A legacy event without either selector is safe only when exactly one
        # tool is open. Never guess between several concurrent occurrences.
        if not round_key and not call_id and len(candidates) != 1:
            return None
        return candidates[0] if candidates else None

    def _take_open_tool(round_key: str, call_id: str) -> dict | None:
        target = _find_open_tool(round_key, call_id)
        if target is not None:
            open_tools.remove(target)
        return target

    def _close_phase_span(ts, status='done'):
        nonlocal open_phase_span
        if open_phase_span is not None:
            _close(open_phase_span, ts, status)
            _apply_budget(open_phase_span)
            open_phase_span = None

    def _close_ttft(ts):
        nonlocal ttft_open
        if ttft_open is not None:
            sp = ttft_open['span']
            _close(sp, ts, 'done')
            _apply_budget(sp)
            ttft_open = None

    def _close_llm(round_key, ts, status_):
        rk = str(round_key)
        rnd = rounds.get(rk)
        if rnd is None:
            return
        llm = rnd['attrs'].get('_llm')
        if llm is not None and llm.get('tEnd') is None:
            _close(llm, ts, status_)
            _apply_budget(llm)

    def _close_round(round_key, ts, status_):
        nonlocal cur_round_key
        rk = str(round_key)
        rnd = rounds.get(rk)
        if rnd is None or rnd.get('tEnd') is not None:
            return
        if ttft_open is not None and ttft_open.get('round') == rk:
            _close_ttft(ts)
        _close_llm(rk, ts, status_)
        for tsp in list(open_tools):
            if tsp['attrs'].get('roundKey') == rk:
                _close(tsp, ts, 'unknown')
                tsp['truncated'] = True
                _apply_budget(tsp)
                open_tools.remove(tsp)
        for request_id, request_span in list(open_model_requests.items()):
            if request_span['attrs'].get('roundKey') == rk:
                _close(request_span, ts, 'unknown')
                request_span['truncated'] = True
                open_model_requests.pop(request_id, None)
        _close(rnd, ts, status_)
        if cur_round_key == rk:
            cur_round_key = None

    for row in rows:
        et = row['type']
        p = row['payload']
        ts = row['ts_ms']

        # A phase span closes when the next meaningful row arrives (it is a
        # "what is happening NOW" marker; the NEXT thing ends it). Phase
        # rows close/coalesce inside the phase branch itself; tool_progress
        # / round_usage rows don't close it (heartbeats ride along).
        if open_phase_span is not None and et in _SPAN_OVERLAP_CLOSE_TYPES \
                and et != 'phase':
            _close_phase_span(ts)

        if et == 'round_start':
            saw_round_markers = True
            rn = p.get('roundNum')
            key = str(rn)
            # The next model round proves all tools authored by prior rounds
            # have stopped blocking the loop. If their terminal event was
            # lost, close those diagnostic spans honestly instead of allowing
            # a recycled call id to overwrite them later.
            for tsp in list(open_tools):
                if tsp['attrs'].get('roundKey') != key:
                    _close(tsp, ts, 'unknown')
                    tsp['truncated'] = True
                    _apply_budget(tsp)
                    open_tools.remove(tsp)
            if cur_round_key is not None and cur_round_key != key:
                # A new round implicitly closes the previous one; a missing
                # round_end (crash window) shows as truncated, never a
                # silent overlap.
                prev = rounds.get(cur_round_key)
                if prev is not None and prev.get('tEnd') is None:
                    prev['truncated'] = True
                _close_round(cur_round_key, ts, 'unknown')
            rnd = _span(f'r{key}', 'turn', 1, 'round', f'round {key}', ts)
            rnd['attrs']['roundKey'] = key
            llm = _span(f'r{key}.llm', rnd['spanId'], 2, 'llm',
                        'llm', ts)
            rnd['attrs']['_llm'] = llm
            spans.append(rnd)
            spans.append(llm)
            rounds[key] = rnd
            round_order.append(key)
            cur_round_key = key
            continue

        if et == 'round_end':
            saw_round_markers = True
            key = str(p.get('roundNum'))
            reason = p.get('reason') or ''
            st = 'done'
            if reason == 'aborted':
                st = 'aborted'
            elif reason == 'error':
                st = 'error'
            _close_round(key, ts, st)
            continue

        if et == 'model_request_start':
            request_id = str(p.get('spanId') or '')[:160]
            if not request_id:
                request_id = f'model.request.{len(spans)}'
            rk = _tool_event_round_key(p)
            if rk in rounds:
                parent = f'r{rk}.llm'
                depth = 3
            else:
                parent = 'turn'
                depth = 1
            prior = open_model_requests.pop(request_id, None)
            if prior is not None:
                _close(prior, ts, 'unknown')
                prior['truncated'] = True
            request_span = _span(
                request_id, parent, depth, 'llm',
                p.get('requestTag') or p.get('model') or 'model request', ts,
                attrs={
                    'model': p.get('model') or '',
                    'providerId': p.get('providerId') or '',
                    'requestTag': p.get('requestTag') or '',
                    'roundKey': rk,
                    'attemptLevel': True,
                })
            spans.append(request_span)
            open_model_requests[request_id] = request_span
            continue

        if et == 'model_request_complete':
            request_id = str(p.get('spanId') or '')[:160]
            request_span = open_model_requests.pop(request_id, None)
            rk = str(p.get('roundNum', '')) or (cur_round_key or '')
            if request_span is None:
                duration_ms = _safe_nonnegative_int(p.get('durationMs'))
                parent = f'r{rk}.llm' if rk in rounds else 'turn'
                depth = 3 if parent != 'turn' else 1
                request_span = _span(
                    request_id or f'model.request.{len(spans)}',
                    parent, depth, 'llm',
                    p.get('requestTag') or p.get('model') or 'model request',
                    max(t0, ts - duration_ms),
                    attrs={'roundKey': rk, 'attemptLevel': True})
                spans.append(request_span)
            raw_status = str(p.get('status') or '').lower()
            request_status = (
                'done' if raw_status == 'succeeded'
                else 'aborted' if raw_status == 'aborted'
                else 'error' if raw_status == 'failed'
                else 'unknown')
            request_span['attrs'].update({
                'model': p.get('model') or '',
                'providerId': p.get('providerId') or '',
                'requestTag': p.get('requestTag') or '',
                'finishReason': p.get('finishReason') or '',
                'errorKind': p.get('errorKind') or '',
                'statusCode': p.get('statusCode'),
                'routeId': p.get('routeId') or '',
                'routeMode': p.get('routeMode') or '',
                'routeDecision': p.get('routeDecision') or '',
                'failureStage': p.get('failureStage') or '',
                'durationMs': _safe_nonnegative_int(p.get('durationMs')),
            })
            _close(request_span, ts, request_status)
            continue

        if et == 'phase':
            ph = p.get('phase') or ''
            rule = _PHASE_TRACE_RULE.get(ph)
            coalesce = (open_phase_span is not None
                        and open_phase_span['name'] == ph
                        and rule in ('retry_wait', 'compaction'))
            if open_phase_span is not None and not coalesce:
                _close_phase_span(ts)
            if rule == 'ttft':
                # TTFT candidate: round's llm span is open; first delta (or
                # tool_start / round close) settles it.
                if ttft_open is not None:
                    _close_ttft(ts)
                parent = cur_round_key
                span_id = (f'r{parent}.ttft' if parent is not None
                           else f'ttft.{len(spans)}')
                sp = _span(span_id,
                           f'r{parent}.llm' if parent is not None else 'turn',
                           3 if parent is not None else 1,
                           'llm_ttft', 'ttft', ts,
                           attrs={'model': p.get('model') or ''})
                spans.append(sp)
                ttft_open = {'span': sp, 'round': parent}
            elif rule in ('retry_wait', 'compaction'):
                if coalesce:
                    # Same-phase beat (429 heartbeat ×N): extend, keep the
                    # latest attempt/detail — ONE span per continuous wait.
                    if p.get('attempt') is not None:
                        open_phase_span['attrs']['attempt'] = p['attempt']
                    if p.get('detail'):
                        open_phase_span['attrs']['detail'] = p['detail']
                else:
                    kind = 'retry_wait' if rule == 'retry_wait' else 'compaction'
                    ckey = 'wait' if kind == 'retry_wait' else 'compact'
                    counters[ckey] += 1
                    sp = _span(f'{ckey}.{counters[ckey]}',
                               'turn', 1, kind, ph, ts,
                               attrs={
                                   'detail': p.get('detail') or '',
                                   'detailKey': p.get('detailKey') or '',
                                   'attempt': p.get('attempt'),
                                   'bucket': p.get('bucket') or '',
                                   'statusCode': p.get('statusCode'),
                                   'model': p.get('model') or '',
                               })
                    spans.append(sp)
                    open_phase_span = sp
            # 'covered' / 'ignore' / unknown: no span (unknown phases are
            # forward-compatible wire; their time lands in gaps honestly).
            continue

        if et == 'delta':
            if ttft_open is not None and ts >= ttft_open['span']['tStart']:
                _close_ttft(ts)
            continue

        if et == 'tool_start':
            if ttft_open is not None:
                _close_ttft(ts)
            call_id = p.get('toolCallId') or ''
            t_start = p.get('tStart') or ts
            # Tools execute BETWEEN rounds (the authoring round's round_end
            # already fired), so the payload's roundNum — not the open round
            # — is the parent. A tool child EXTENDS its (closed) round's
            # footprint: round N's true cost includes the tools it authored.
            rk = str(p.get('roundNum', '')) or (cur_round_key or '')
            parent = f'r{rk}' if rk in rounds else 'turn'
            depth = 2 if parent != 'turn' else 1
            if parent == 'turn':
                counters['flat_tool'] += 1
            rnd = rounds.get(rk)
            if rnd is not None and rnd.get('tEnd') is not None \
                    and t_start > rnd['tEnd']:
                rnd['tEnd'] = int(t_start)  # extended again at tool close
            occurrence_key = (rk, str(call_id or '__anonymous__'))
            occurrence = tool_span_occurrences.get(occurrence_key, 0) + 1
            tool_span_occurrences[occurrence_key] = occurrence
            tool_span_token = call_id or f'anonymous-{occurrence}'
            occurrence_suffix = '' if occurrence == 1 else f'.occ{occurrence}'
            sp = _span(f'{parent}.tool.{tool_span_token}{occurrence_suffix}',
                       parent, depth, 'tool',
                       p.get('toolName') or 'tool', t_start,
                       attrs={'toolName': p.get('toolName') or '',
                              'query': (p.get('query') or '')[:200],
                              'roundKey': rk,
                              'toolCallId': call_id})
            spans.append(sp)
            open_tools.append(sp)
            approval_key = (rk, str(call_id))
            pending = pending_approval.get(approval_key) if call_id else None
            if pending:
                ap = _span(f'{sp["spanId"]}.appr', sp['spanId'], depth + 1,
                           'approval_wait', 'approval',
                           pending.pop(0),
                           attrs={'toolName': sp['attrs']['toolName']})
                if not pending:
                    pending_approval.pop(approval_key, None)
                spans.append(ap)
                sp['attrs']['_approval'] = ap
            continue

        if et == 'write_approval_request':
            cid = str(p.get('toolCallId') or '')
            if cid:
                rk = _tool_event_round_key(p)
                tgt = _find_open_tool(
                    rk, cid, require_unapproved=True)
                if tgt is not None and '_approval' not in tgt['attrs']:
                    ap = _span(f'{tgt["spanId"]}.appr', tgt['spanId'],
                               tgt['depth'] + 1, 'approval_wait', 'approval',
                               ts, attrs={'toolName': tgt['attrs']['toolName']})
                    spans.append(ap)
                    tgt['attrs']['_approval'] = ap
                else:
                    pending_approval.setdefault((rk, cid), []).append(ts)
            continue

        if et == 'tool_progress':
            cid = str(p.get('toolCallId') or '')
            rk = _tool_event_round_key(p)
            exec_start = p.get('execStartTs')
            tgt = _find_open_tool(
                rk, cid, require_unmarked=True) if cid else None
            if tgt is None and cid:
                tgt = _find_open_tool(rk, cid)
            if tgt is not None and exec_start:
                # The REAL spawn moment: anything between the tool_start
                # announce and this is queue/approval wait, not execution.
                if not tgt['attrs'].get('_execMarked'):
                    tgt['attrs']['_execMarked'] = True
                    ap = tgt['attrs'].get('_approval')
                    if ap is not None:
                        _close(ap, int(exec_start), 'done')
                        _apply_budget(ap)
                    elif int(exec_start) - int(tgt['tStart'] or ts) > 500:
                        sw = _span(f'{tgt["spanId"]}.spawn', tgt['spanId'],
                                   tgt['depth'] + 1, 'spawn_wait', 'dispatch',
                                   tgt['tStart'] or ts)
                        _close(sw, int(exec_start), 'done')
                        _apply_budget(sw)
                        spans.append(sw)
            continue

        if et in ('tool_result', 'tool_complete'):
            cid = str(p.get('toolCallId') or '')
            rk = _tool_event_round_key(p)
            tgt = _take_open_tool(rk, cid) if cid else None
            if tgt is None and not cid:
                # Fallback: oldest open tool in the current round (legacy
                # rows without call ids) — else drop (orphan result).
                tgt = _take_open_tool(rk, '')
            if tgt is not None:
                verdict = p.get('status') or ''
                if not verdict and et == 'tool_complete':
                    verdict = 'error' if p.get('isError') else ''
                st = ('done' if verdict in ('', 'done')
                      else 'error' if verdict == 'error' else verdict)
                ap = tgt['attrs'].get('_approval')
                if ap is not None and ap.get('tEnd') is None:
                    _close(ap, p.get('tEnd') or ts, 'unknown')
                    ap['truncated'] = True
                    _apply_budget(ap)
                _close(tgt, p.get('tEnd') or ts,
                       st if st in ('done', 'error', 'aborted') else 'error')
                if st not in ('done',):
                    tgt['attrs']['verdict'] = st
                _apply_budget(tgt)
                rnd = rounds.get(tgt['attrs'].get('roundKey'))
                if rnd is not None and rnd.get('tEnd') is not None \
                        and tgt['tEnd'] > rnd['tEnd']:
                    rnd['tEnd'] = tgt['tEnd']
            continue

        if et == 'compaction':
            # The event pair without a phase (some paths emit only events):
            # open the span here; the phase-driven path coalesces by name.
            if open_phase_span is None or open_phase_span['name'] != Phase.COMPACTING:
                _close_phase_span(ts)
                counters['compact'] += 1
                sp = _span(f'compact.{counters["compact"]}', 'turn', 1,
                           'compaction', Phase.COMPACTING, ts,
                           attrs={'detail': p.get('detail') or ''})
                spans.append(sp)
                open_phase_span = sp
            continue

        if et == 'compaction_done':
            if (open_phase_span is not None
                    and open_phase_span['kind'] == 'compaction'):
                _close_phase_span(ts)
            continue

        if et == 'round_usage':
            u = p.get('usage') or {}
            rec = {
                'tag': p.get('tag') or '',
                'model': p.get('model') or '',
                'tokensIn': int(p.get('tokensIn') or 0),
                'tokensOut': int(p.get('tokensOut') or 0),
                'traceId': u.get('trace_id') or '',
                'streamElapsedMs': int(u.get('stream_elapsed_ms') or 0),
                'routeId': ((u.get('_network_route') or {}).get('routeId')
                            if isinstance(u.get('_network_route'), dict) else ''),
                'routeMode': ((u.get('_network_route') or {}).get('routeMode')
                              if isinstance(u.get('_network_route'), dict) else ''),
                'failureStage': u.get('_failure_stage') or '',
                'streamState': u.get('_stream_state') or '',
                'semanticProgressTimeout': bool(
                    u.get('_semantic_progress_timeout')
                    or u.get('_stream_state') ==
                    'semantic_progress_timeout'),
                # Historical projection retained for old trace readers.
                'noActionableTimeout': bool(
                    u.get('_no_actionable_timeout')),
            }
            if (rec['semanticProgressTimeout']
                    or rec['noActionableTimeout']):
                rec.update({
                    'semanticStallWindowMs': _safe_seconds_to_ms(
                        u.get('_no_actionable_timeout_s'))
                    if u.get('_semantic_idle_timeout_ms') is None else
                    _safe_nonnegative_int(u.get('_semantic_idle_timeout_ms')),
                    'lastSemanticProgressAgeMs': _safe_seconds_to_ms(
                        u.get('_no_actionable_stall_elapsed_s'))
                    if u.get('_semantic_progress_idle_ms') is None else
                    _safe_nonnegative_int(u.get('_semantic_progress_idle_ms')),
                    'requestElapsedMs': _safe_seconds_to_ms(
                        u.get('_no_actionable_request_elapsed_s'),
                        rec['streamElapsedMs'] / 1000),
                    'reasoningChars': _safe_nonnegative_int(
                        u.get('_no_actionable_reasoning_chars')),
                    'reasoningChunks': _safe_nonnegative_int(
                        u.get('_no_actionable_reasoning_chunks')),
                    'sseChunks': _safe_nonnegative_int(
                        u.get('_chunks_received')),
                })
            key = str(p.get('roundNum'))
            if key in rounds:
                usage_by_round.setdefault(key, []).append(rec)
            else:
                usage_orphans.append((key, rec))
            continue

        if is_flow_event_type(et):
            flow_seen = True
            continue
        # done/error/retry_reset/model_fallback/ping/…: no span work.

    # ── Settle everything still open (running task or a crashed log) ──
    settle_ts = eff_end
    _close_ttft(settle_ts)
    if open_phase_span is not None:
        _close_phase_span(settle_ts, 'running' if running else 'unknown')
    for tsp in list(open_tools):
        _close(tsp, settle_ts, 'running' if running else 'unknown')
        if not running:
            tsp['truncated'] = True
        _apply_budget(tsp)
    open_tools.clear()
    for request_id, request_span in list(open_model_requests.items()):
        _close(request_span, settle_ts, 'running' if running else 'unknown')
        if not running:
            request_span['truncated'] = True
        open_model_requests.pop(request_id, None)
    for rk in round_order:
        rnd = rounds[rk]
        llm = rnd['attrs'].pop('_llm', None)
        if llm is not None:
            if llm.get('tEnd') is None:
                _close(llm, rnd.get('tEnd') or settle_ts,
                       'running' if running else 'unknown')
            _apply_budget(llm)
        if rnd.get('tEnd') is None:
            _close(rnd, settle_ts, 'running' if running else 'unknown')
            if not running:
                rnd['truncated'] = True

    # Usage join (llm span attrs — model + authoritative attempt elapsed).
    for key, rec in usage_orphans:
        usage_by_round.setdefault(key, []).append(rec)
    for key, attempts in usage_by_round.items():
        rnd = rounds.get(key)
        if rnd is None:
            continue
        llm_span = next((
            s for s in spans if s.get('spanId') == f'r{key}.llm'
        ), None)
        if llm_span is not None:
            llm_span['attrs']['attempts'] = attempts
            if attempts and attempts[0].get('model'):
                llm_span['attrs']['model'] = attempts[0]['model']

    # Drop the internal llm handles from round attrs (wire cleanliness).
    for rk in round_order:
        rounds[rk]['attrs'].pop('_llm', None)
        rounds[rk]['attrs'].pop('_llm_closed', None)
    for s in spans:
        s['attrs'].pop('_approval', None)
        s['attrs'].pop('_execMarked', None)

    # ── Gaps: the complement that makes accounting strict ──
    covered_ivs = []
    for s in spans:
        if s['kind'] in ('turn', 'round'):
            continue
        a, b = s.get('tStart'), s.get('tEnd')
        if a is not None and b is not None and b > a:
            covered_ivs.append((max(a, t0), min(b, eff_end)))
    gaps = []
    iv = sorted(covered_ivs)
    cursor = t0
    for a, b in iv:
        if a > cursor:
            gaps.append({'tStart': cursor, 'tEnd': a})
        cursor = max(cursor, b)
    if eff_end > cursor:
        gaps.append({'tStart': cursor, 'tEnd': eff_end})
    gaps = [g for g in gaps if g['tEnd'] - g['tStart'] >= 1]

    # ── Summary (disjoint priority allocation) ──
    summary = _disjoint_summary(spans, t0, eff_end)
    summary['totalMs'] = eff_end - t0
    ttft_ivs = [(s['tStart'], s['tEnd']) for s in spans
                if s['kind'] == 'llm_ttft']
    summary['ttftMs'] = _union_length(ttft_ivs)  # informational ⊂ llmMs
    over = []
    for s in spans:
        if s.get('overBudget'):
            over.append({
                'spanId': s['spanId'], 'kind': s['kind'], 'name': s['name'],
                'elapsedMs': _elapsed(s), 'budgetMs': s['budgetMs'],
            })
    summary['overBudget'] = over

    coverage = 'full'
    coverage_reason = ''
    if flow_seen:
        coverage, coverage_reason = 'partial', 'flow'
    elif not saw_round_markers:
        # Legacy log predating the round-boundary events: tools/waits still
        # fold flat, but the llm structure is not derivable — say so.
        coverage, coverage_reason = 'partial', 'no-round-markers'

    if status_history is None:
        folded_status_history, folded_status_dropped = _fold_status_history(
            rows, settle_ts=eff_end, running=running)
    else:
        folded_status_history, newly_dropped = _bounded_trace_rows(
            status_history, TRACE_MAX_STATUS_ENTRIES, keep_recent=True)
        folded_status_dropped = max(0, int(status_dropped_count or 0)) \
            + newly_dropped
        if folded_status_history and not running \
                and folded_status_history[-1].get('tEnd') is None:
            folded_status_history[-1]['tEnd'] = eff_end
            folded_status_history[-1]['terminalBoundary'] = True

    # Stable wire order: depth-first by (tStart, spanId insertion).
    order = {id(s): i for i, s in enumerate(spans)}
    spans.sort(key=lambda s: (s['depth'], s['tStart'] or 0, order[id(s)]))

    document = {
        **base,
        'status': status,
        'running': running,
        'coverage': coverage,
        **({'coverageReason': coverage_reason} if coverage_reason else {}),
        'tStart': t0,
        'tEnd': t_end_turn,
        'totalMs': eff_end - t0,
        'summary': summary,
        'spans': spans,
        'gaps': gaps,
        'statusHistory': folded_status_history,
    }
    if folded_status_dropped:
        document['statusDroppedCount'] = folded_status_dropped
    return compact_trace_document(document, source='event-log')


__all__ = [
    'TRACE_CONTRACT_VERSION',
    'TRACE_MAX_CLIENT_OBSERVATIONS',
    'TRACE_MAX_PERSISTED_BYTES',
    'append_client_trace_observation',
    'compact_trace_document',
    'finalize_trace_projection',
    'fold_task_trace',
    'invalidate_trace_cache',
    'merge_client_trace_evidence',
    'observe_task_trace_event',
    'project_running_trace_status',
    'read_persisted_task_trace',
    'task_status_history',
]
