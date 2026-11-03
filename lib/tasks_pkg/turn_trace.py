"""Turn trace — server-authoritative per-task timing fold (the unified
"where did the time go" interface).

Contract: ``docs/contracts/TURN_TRACE_CONTRACT.md``. One pure fold derives the
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
       approvalWaitMs, flowStepMs, orchestrationMs, unattributedMs, ttftMs,
       overBudget, admissionWaitMs, retryWaitMs, spawnWaitMs, startupMs,
       roundPrepMs, roundSettleMs, finalizeMs}`` — the wait and orchestration
       splits come from the same disjoint fold (never extra buckets).

Terminal settlement (``finalize_trace_projection``) also logs ONE
``[TurnTraceSummary]`` line per task from that same fold, including the
per-kind split of ``waitMs`` (admission / retry / spawn waits).

A failed event read is never an empty log: the reader raises
``TurnTraceEventReadError``; settlement then freezes an explicit
``coverage:partial`` / ``coverageReason:event-read-failed`` document without a
fabricated summary, spans or gaps.
"""

from __future__ import annotations

from collections.abc import Mapping
import time

from lib.agent_core.events import Phase
from lib.conversation_sync.settled_rows import settle
from lib.log import get_logger
from lib.orchestration.message_compat import is_flow_event_type
from lib.tasks_pkg.turn_trace_spans import (
    TraceSpanFold,
    _span as _span,
    _close as _close,
    _safe_nonnegative_int as _safe_nonnegative_int,
    _safe_seconds_to_ms as _safe_seconds_to_ms,
)

logger = get_logger(__name__)

# The version/budget constants and the pure evidence merge/append/compaction
# functions live in the dependency-light contract module so the storage
# sidecar (a separate process) can consume them without importing this
# task-execution graph. They are re-exported here unchanged; existing
# ``from lib.tasks_pkg.turn_trace import ...`` call sites keep working.
from lib.turn_trace_contract import (
    CLIENT_OBSERVATION_KINDS,
    TRACE_CONTRACT_VERSION,
    TRACE_MAX_CLIENT_OBSERVATIONS,
    TRACE_MAX_COUNTER,
    TRACE_MAX_GAPS,
    TRACE_MAX_PERSISTED_BYTES,
    TRACE_MAX_SPANS,
    TRACE_MAX_STATUS_ENTRIES,
    append_client_trace_observation,
    compact_trace_document,
    merge_client_trace_evidence,
    _bounded_trace_rows,
    _bounded_trace_text,
    _sanitize_trace_value,
)

TRACE_SOURCES = frozenset({
    'event-log', 'live-projection', 'turn-snapshot', 'attempt-receipts',
})

_TASK_STATUS_HISTORY_KEY = '_timingStatusHistory'
_TASK_STATUS_DROPPED_KEY = '_timingStatusDroppedCount'
_TASK_STATUS_SEQUENCE_KEY = '_timingStatusSequence'
_TRACE_TERMINAL_TYPES = frozenset({'done', 'error', 'aborted', 'interrupted'})
_STATUS_CLEAR_TYPES = frozenset({'delta', *_TRACE_TERMINAL_TYPES})
# ── The declared phase → trace-rule mapping (drift-guarded) ──
# Every chat-domain phase in the registry MUST appear here
# (tests/turn_lifecycle/test_turn_trace.py enforces both directions). Rules:
#   'ttft'       — opens the time-to-first-token candidate span for its round
#   'retry_wait' — opens/extends a wait span (same-phase beats coalesce)
#   'compaction' — opens the compaction span (closed by compaction_done)
#   'covered'    — no own span: its time is inside a structural span
#                  (llm_thinking ⊂ llm span; tool_exec ⊂ the tool spans)
#   'ignore'     — momentary notice, not a duration; any time it actually
#                  covers surfaces honestly as a gap (unattributed)
#   'startup'    — before the first execution boundary it opens the named
#                  startup stage (_STARTUP_STAGE_BY_DETAIL_KEY); afterwards
#                  it is a momentary notice like 'ignore'
_PHASE_TRACE_RULE: dict[str, str] = {
    Phase.WAITING_MODEL: 'ttft',
    Phase.RETRYING: 'retry_wait',
    # The AI-task queue precedes worker entry: a startup stage, not a retry.
    Phase.EXECUTOR_QUEUED: 'startup',
    Phase.COMPACTING: 'compaction',
    Phase.LLM_THINKING: 'covered',
    Phase.STREAM_STALLED: 'covered',
    Phase.TOOL_EXEC: 'covered',
    Phase.WORKING: 'startup',
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
from lib.turn_trace_contract import TOOL_EXECUTION_BUDGETS_MS as _TOOL_BUDGETS_MS
_KIND_BUDGETS_MS: dict[str, int | None] = {
    'llm': None,
    'llm_ttft': None,
    'retry_wait': None,
    'approval_wait': None,
    'compaction': 120_000,
    'spawn_wait': 10_000,
    # Admission waits last as long as another holder's workload (often an
    # unbounded run_command) — declared unbounded like that workload itself.
    'admission_wait': None,
    # A flow step wraps whatever its dispatched child agents do — declared
    # unbounded, same owner ruling as llm/run_command workloads.
    'flow_step': None,
    # Orchestration windows scale with workspace, memory and storage latency;
    # no expectation is declared yet.
    'startup': None,
    'round_prep': None,
    'round_settle': None,
    'finalize': None,
}

# The WORKING startup prompts (detailKey) that open a named startup stage.
# ``executor_queued`` opens ``executor_queue``; the time before the first event
# is ``dispatch``; memory_prefetch events split ``context`` into
# ``memory_load`` / ``memory_select`` / ``context``. An unlisted key keeps the
# current stage.
_STARTUP_STAGE_BY_DETAIL_KEY: dict[str, str] = {
    'stream.phase.workerStarting': 'worker_start',
    'stream.phase.workspacePreparing': 'workspace',
    'translationWait.turn': 'translation',
    'stream.phase.startupConfig': 'config',
    'stream.phase.startupTools': 'tools',
    'stream.phase.startupContext': 'context',
    'stream.phase.vuInjectContext': 'context',
    'stream.phase.vuContextReady': 'handoff',
}

# Payload-bearing event types the fold reads. ``delta`` is deliberately NOT
# here: deltas are the per-token streaming noise (a 50k-row task is mostly
# deltas), so they are read as a ts-only spine for the TTFT boundary.
_TRACE_PAYLOAD_TYPES: frozenset[str] = frozenset({
    'round_start', 'round_end', 'phase',
    'tool_start', 'tool_progress', 'tool_result', 'tool_complete',
    'workspace_admission_wait',
    'compaction', 'compaction_done',
    'write_approval_request',
    'round_usage', 'retry_reset', 'model_fallback',
    'model_request_start', 'model_request_complete',
    'step_start', 'step_complete',
    'memory_prefetch',
    'done', 'error', 'aborted', 'interrupted',
})

# Bucket priority for the DISJOINT summary allocation — a millisecond is
# attributed to the highest-priority span covering it (an approval wait
# inside a tool span is approval time, not tool time; a 429 retry inside
# the LLM window is wait time, not model time).
_BUCKET_PRIORITY: tuple[tuple[str, str], ...] = (
    # Measured at the lease owner with exact start/end, so it outranks every
    # span that merely encloses it: the announce->spawn spawn_wait, an approval
    # span whose end is only bounded by the later spawn/tool-end event (approval
    # precedes admission for the same call), and the tool span itself.
    ('admission_wait', 'waitMs'),
    ('approval_wait', 'approvalWaitMs'),
    ('compaction', 'compactionMs'),
    ('retry_wait', 'waitMs'),
    ('spawn_wait', 'waitMs'),
    ('tool', 'toolMs'),
    # A round's own window minus its logical model requests: preparation
    # before the first request and settlement after the last one. They outrank
    # the round-level llm span that encloses the whole round.
    ('round_prep', 'orchestrationMs'),
    ('round_settle', 'orchestrationMs'),
    ('llm', 'llmMs'),
    ('llm_ttft', 'llmMs'),
    # A flow step WRAPS the child-agent work dispatched inside it: llm/tool
    # time keeps its own attribution, the step only claims the remainder —
    # dispatch, evidence delivery and coordination overhead that previously
    # hid inside a never-closing llm window or the gaps.
    ('flow_step', 'flowStepMs'),
    # Derived windows claim only the remainder: before the first execution
    # boundary (named startup stages) and after the last model round.
    ('startup', 'orchestrationMs'),
    ('finalize', 'orchestrationMs'),
)
# Every kind allocated to ``waitMs`` above, and the summary field carrying its
# share. Persisted with the buckets: the bounded spans list drops evidence on
# long tasks, so the split cannot be recomputed from the stored document.
_WAIT_SPLIT_FIELDS: tuple[tuple[str, str], ...] = (
    ('admission_wait', 'admissionWaitMs'),
    ('retry_wait', 'retryWaitMs'),
    ('spawn_wait', 'spawnWaitMs'),
)
# The same split for ``orchestrationMs``: agent-loop work outside model
# requests, tools and waits.
_ORCHESTRATION_SPLIT_FIELDS: tuple[tuple[str, str], ...] = (
    ('startup', 'startupMs'),
    ('round_prep', 'roundPrepMs'),
    ('round_settle', 'roundSettleMs'),
    ('finalize', 'finalizeMs'),
)

_SPAN_OVERLAP_CLOSE_TYPES = frozenset({
    'phase', 'delta', 'tool_start', 'round_start', 'round_end',
    'model_request_start', 'compaction_done', 'step_start', 'step_complete',
    'done', 'error',
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

class TurnTraceEventReadError(RuntimeError):
    """The event authority failed; absence of trace events was not established."""


TRACE_COVERAGE_EVENT_READ_FAILED = 'event-read-failed'
_READ_FAILURE_DETAIL_MAX_CHARS = 400


def _read_trace_rows(task_id: str) -> list:
    """Return [{event_id, type, payload, ts_ms}] ordered by event_id.

    Payload rows for the structural slice + ts-only spine rows for
    ``delta`` (the TTFT boundary; parsing 50k per-token payloads would be
    pure waste). CACHED with the same short-TTL + write-invalidation
    discipline as the Request Inspector read. Read-only; a failed read raises
    ``TurnTraceEventReadError`` and is never cached as an empty log.
    """
    if not task_id:
        return []
    now = time.monotonic()
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

class _TraceRows(list):
    """List-compatible read seam carrying honest bounded-read evidence."""
    truncated = False

TRACE_MAX_EVENT_ROWS = 200_000

def _read_trace_rows_uncached(task_id: str) -> list:
    """Read the bounded trace projection from the Sidecar authority."""
    try:
        from lib.storage import get_storage_client
        from lib.tasks_pkg.request_inspector_history import is_event_page_size_error

        out = _TraceRows()
        after = -1
        scanned = 0
        page_limit = 1000
        client = get_storage_client()
        while True:
            try:
                rows = client.query(
                    'event.list', {'task_id': task_id,
                                   'after_sequence': after, 'limit': page_limit,
                                   'payload_types': sorted(_TRACE_PAYLOAD_TYPES)}) or []
            except Exception as exc:
                # A deterministic page-size bound: a smaller page at the SAME
                # cursor reads the same rows; a one-row refusal stays a failure.
                if is_event_page_size_error(exc) and page_limit > 1:
                    page_limit = max(1, page_limit // 2)
                    continue
                raise
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
            if len(rows) < page_limit:
                break
            if scanned >= TRACE_MAX_EVENT_ROWS:
                latest = client.query('event.latest', {'task_id': task_id, 'metadata_only': True})
                out.truncated = bool(latest and int(latest['sequence']) > after)
                if out.truncated:
                    # Terminal state is independent of the bounded detail prefix.
                    boundaries = client.query('event.list', {
                        'task_id': task_id, 'after_sequence': after, 'limit': 1000,
                        'types': sorted(_TRACE_TERMINAL_TYPES),
                    }) or []
                    if latest:
                        boundaries.append(latest)
                    known = {row['event_id'] for row in out}
                    for boundary in boundaries:
                        sequence = int(boundary['sequence'])
                        if sequence in known:
                            continue
                        payload = boundary.get('event') or {}
                        out.append({'event_id': sequence,
                                    'type': payload.get('type') or '',
                                    'payload': payload if payload.get('type') in _TRACE_TERMINAL_TYPES else {},
                                    'ts_ms': int(boundary.get('created_at_ms') or 0)})
                        known.add(sequence)
                break
        out.sort(key=lambda row: row['event_id'])
        return out
    except Exception as e:
        from lib.agent_core.storage_errors import describe_storage_failure

        # A bare ``str(StorageError)`` is often just its code; render the
        # classification and message, bounded.
        detail = describe_storage_failure(e)
        if len(detail) > _READ_FAILURE_DETAIL_MAX_CHARS:
            detail = detail[:_READ_FAILURE_DETAIL_MAX_CHARS] + '...'
        logger.warning('[TurnTrace] Sidecar event read failed task=%s %s',
                       task_id, detail)
        raise TurnTraceEventReadError(
            f'Turn Trace event read failed: {detail}') from e

# ── Span helpers ──



def _elapsed(span):
    if span.get('tStart') is None or span.get('tEnd') is None:
        return None
    return max(0, span['tEnd'] - span['tStart'])



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
    # A closed interval never changes again; projection capture shares it.
    history[-1] = settle(current)

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

# The same priority order, one ledger entry per span kind; summary buckets are
# groupings of these entries (llm + llm_ttft -> llmMs, three waits -> waitMs).
_KIND_PRIORITY: tuple[tuple[str, str], ...] = tuple(
    (kind, kind) for kind, _ in _BUCKET_PRIORITY)


def _disjoint_attribution(spans, t0, t1):
    """Priority-allocate every ms of [t0, t1] to exactly ONE span kind.

    Elementary-segment sweep over span boundaries: each segment picks the
    highest-priority span kind covering it (``_BUCKET_PRIORITY`` order), so the
    per-kind totals plus ``unattributedMs`` sum EXACTLY to the interval — no
    double-count when spans nest (approval ⊂ tool, retry ⊂ llm window).
    """
    from .turn_trace_ledger import disjoint_summary

    return disjoint_summary(spans, t0, t1, _KIND_PRIORITY)


def trace_kind_attribution(trace):
    """Per-kind disjoint attribution of an already folded trace document.

    Same ledger and priority order as the document's ``summary``, so the
    kinds group exactly into its buckets — unless the bounded document dropped
    spans (``droppedSpans``), which only the original fold can account for.
    """
    t0, total = trace.get('tStart'), trace.get('totalMs')
    if type(t0) is not int or type(total) is not int:
        return {}
    return _disjoint_attribution(trace.get('spans') or [], t0, t0 + total)


def _disjoint_summary(spans, t0, t1, attribution=None):
    """Group the per-kind attribution into the contract's summary buckets."""
    if attribution is None:
        attribution = _disjoint_attribution(spans, t0, t1)
    summary = {bucket: 0 for _, bucket in _BUCKET_PRIORITY}
    for kind, bucket in _BUCKET_PRIORITY:
        summary[bucket] += attribution[kind]
    summary['unattributedMs'] = attribution['unattributedMs']
    for kind, field in _WAIT_SPLIT_FIELDS + _ORCHESTRATION_SPLIT_FIELDS:
        summary[field] = attribution[kind]
    return summary

def _terminal_trace_status(event_type, payload):
    if event_type == 'error' or payload.get('error'):
        return 'error'
    finish_reason = payload.get('finishReason') or ''
    if event_type in {'aborted', 'interrupted'} \
            or finish_reason in {'aborted', 'interrupted'}:
        return 'aborted'
    return 'done'

def _settled_status_history(status_history, status_dropped_count, *,
                            settle_ts, running):
    """Bound the in-memory prompt ledger; a settled run closes its last prompt."""
    history, newly_dropped = _bounded_trace_rows(
        status_history, TRACE_MAX_STATUS_ENTRIES, keep_recent=True)
    if history and not running and history[-1].get('tEnd') is None:
        history[-1]['tEnd'] = settle_ts
        history[-1]['terminalBoundary'] = True
    return history, max(0, int(status_dropped_count or 0)) + newly_dropped

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
    ledger = {}
    read_failed = False
    try:
        document = fold_task_trace(
            task_id,
            now_ms=now_ms,
            pending_event=raw_event,
            pending_sequence=pending_sequence,
            status_history=history or None,
            status_dropped_count=dropped,
            ledger_out=ledger,
        )
    except TurnTraceEventReadError:
        read_failed = True
        document = _event_read_failed_trace(
            task_id, raw_event, now_ms=now_ms,
            status_history=history, status_dropped_count=dropped)
    if not document.get('eventsAvailable'):
        return dict(projection or {})
    _log_trace_summary(task, task_id, document, ledger)
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
    document['eventLogAvailable'] = not read_failed
    next_projection = dict(projection or {})
    next_projection['timingTrace'] = compact_trace_document(
        document, source='turn-snapshot')
    return next_projection

def _event_read_failed_trace(task_id, pending_event, *, now_ms,
                             status_history, status_dropped_count):
    """Terminal document for a settlement whose event read failed.

    Keeps only evidence that never depended on the failed read: the terminal
    event's status and execution clock, and the in-memory prompt ledger.
    Rounds, spans, gaps and the bucket partition are unknown, so none is
    fabricated. Without a ``summary`` the trace endpoint does not treat this
    marker as the permanent authority and re-folds the retained event log.
    """
    event = pending_event if isinstance(pending_event, Mapping) else {}
    t_end = event.get('executionAtMs')
    if type(t_end) is not int or t_end <= 0:
        t_end = int(now_ms)
    history, dropped = _settled_status_history(
        status_history, status_dropped_count, settle_ts=t_end, running=False)
    document = {
        'version': TRACE_CONTRACT_VERSION,
        'taskId': task_id,
        'eventsAvailable': True,
        'eventLogAvailable': False,
        'source': 'turn-snapshot',
        'status': _terminal_trace_status(str(event.get('type') or ''), event),
        'running': False,
        'coverage': 'partial',
        'coverageReason': TRACE_COVERAGE_EVENT_READ_FAILED,
        'clockSource': 'legacy-wall',
        'tEnd': t_end,
        'statusHistory': history,
    }
    clock_id = event.get('executionClockId')
    origin = event.get('executionOriginAtMs')
    if isinstance(clock_id, str) and clock_id and type(origin) is int \
            and 0 < origin <= t_end:
        document.update(clockSource='server-monotonic', tStart=origin,
                        totalMs=t_end - origin)
    if dropped:
        document['statusDroppedCount'] = dropped
    return document

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

# Terminal projection can be rebuilt for the same task (CAS rebase, retained
# terminal receipt retry); the per-task summary line is emitted only once.
# The value is True once a real fold was logged, or the read-failure marker.
_TRACE_SUMMARY_LOGGED_KEY = '_turnTraceSummaryLogged'


def _log_trace_summary(task, task_id, document, ledger):
    """One authoritative INFO line per task, from the fold being frozen.

    Every number comes from that fold's persisted summary (including its
    per-kind ``waitMs`` split); only ``rounds`` comes from the ledger.
    INFO stays below the WARNING+ duplicate coalescer, and the full task id
    keeps distinct tasks' lines distinct.

    A settlement whose event read failed has no fold: its line carries the
    coverage marker and no number a log aggregator could sum. A rebuild of
    the same terminal projection whose read then succeeds still logs its fold.
    """
    read_failed = (
        document.get('coverageReason') == TRACE_COVERAGE_EVENT_READ_FAILED)
    logged = task.get(_TRACE_SUMMARY_LOGGED_KEY)
    if logged is True or (read_failed and logged):
        return
    task[_TRACE_SUMMARY_LOGGED_KEY] = (
        TRACE_COVERAGE_EVENT_READ_FAILED if read_failed else True)
    try:
        if read_failed:
            logger.info(
                '[TurnTraceSummary] task=%s conv=%s status=%s coverage=%s '
                'coverageReason=%s',
                task_id or '-', task.get('convId') or '-',
                document.get('status') or '-', document.get('coverage') or '-',
                TRACE_COVERAGE_EVENT_READ_FAILED)
            return
        summary = document.get('summary') or {}

        def ms(value):
            return int(value or 0)

        logger.info(
            '[TurnTraceSummary] task=%s conv=%s status=%s totalMs=%d llmMs=%d '
            'toolMs=%d waitMs=%d admissionWaitMs=%d retryWaitMs=%d '
            'compactionMs=%d approvalWaitMs=%d unattributedMs=%d ttftMs=%d '
            'rounds=%d spawnWaitMs=%d flowStepMs=%d orchestrationMs=%d '
            'startupMs=%d roundPrepMs=%d roundSettleMs=%d finalizeMs=%d '
            'coverage=%s',
            task_id or '-', task.get('convId') or '-',
            document.get('status') or '-', ms(summary.get('totalMs')),
            ms(summary.get('llmMs')), ms(summary.get('toolMs')),
            ms(summary.get('waitMs')), ms(summary.get('admissionWaitMs')),
            ms(summary.get('retryWaitMs')), ms(summary.get('compactionMs')),
            ms(summary.get('approvalWaitMs')), ms(summary.get('unattributedMs')),
            ms(summary.get('ttftMs')), ms(ledger.get('rounds')),
            ms(summary.get('spawnWaitMs')), ms(summary.get('flowStepMs')),
            ms(summary.get('orchestrationMs')), ms(summary.get('startupMs')),
            ms(summary.get('roundPrepMs')), ms(summary.get('roundSettleMs')),
            ms(summary.get('finalizeMs')),
            document.get('coverage') or '-')
    except Exception as e:
        # Diagnostic sinks cannot alter fold/settlement outcomes.
        logger.debug('[TurnTraceSummary] emit dropped: %s', e)


def fold_task_trace(
    task_id: str,
    now_ms: int | None = None,
    *,
    pending_event: Mapping | None = None,
    pending_sequence: int | None = None,
    status_history: list | None = None,
    status_dropped_count: int = 0,
    ledger_out: dict | None = None,
) -> dict:
    """Measure read + fold + bounded projection, not the task's execution time.

    ``ledger_out`` (optional) receives this fold's per-kind disjoint attribution
    (``kindMs``) and round count, which the bounded document does not carry.
    """
    started = time.monotonic()
    outcome = 'error'
    try:
        result = _fold_task_trace(
            task_id, now_ms, pending_event=pending_event,
            pending_sequence=pending_sequence, status_history=status_history,
            status_dropped_count=status_dropped_count, ledger_out=ledger_out,
        )
        outcome = 'done'
        return result
    finally:
        elapsed_ms = max(0.0, time.monotonic() - started) * 1000
        try:
            emit = logger.info if elapsed_ms >= 1000 or outcome != 'done' else logger.debug
            emit('[TurnTraceTiming] task=%s status=%s elapsed_ms=%.1f',
                 str(task_id)[:8], outcome, elapsed_ms)
        except Exception as e:
            # Diagnostic sinks cannot alter fold/settlement outcomes.
            logger.debug('[TurnTraceTiming] emit dropped: %s', e)


def _trace_gaps(spans, t0, t1):
    """The complement that makes accounting strict: [t0, t1] minus every span
    except the enclosing turn/round frames."""
    covered = []
    for span in spans:
        if span['kind'] in ('turn', 'round'):
            continue
        a, b = span.get('tStart'), span.get('tEnd')
        if a is not None and b is not None and b > a:
            a, b = max(a, t0), min(b, t1)
            if b > a:
                covered.append((a, b))
    gaps = []
    cursor = t0
    for a, b in sorted(covered):
        if a > cursor:
            gaps.append({'tStart': cursor, 'tEnd': a})
        cursor = max(cursor, b)
    if t1 > cursor:
        gaps.append({'tStart': cursor, 'tEnd': t1})
    return [gap for gap in gaps if gap['tEnd'] - gap['tStart'] >= 1]


def fold_exported_events(task_id: str, events, *, now_ms: int | None = None) -> dict:
    """Replay: fold exported task events (``seq`` + payload) offline.

    Rows are projected as ``_read_trace_rows`` serves them; the producer's
    ``executionAtMs`` stands in for the stored row time, so events without a
    server execution clock cannot be placed and are skipped. ``now_ms``
    defaults to the terminal (else the last) event.
    """
    from lib.event_envelope import EventFormatError, execution_event_time_ms

    rows = []
    for event in events or ():
        if not isinstance(event, Mapping) or type(event.get('seq')) is not int:
            continue
        event_type = str(event.get('type') or '')
        if not (event_type == 'delta' or event_type in _TRACE_PAYLOAD_TYPES
                or is_flow_event_type(event_type)):
            continue
        try:
            timestamp = execution_event_time_ms(event, 0)
        except EventFormatError:
            continue
        if timestamp > 0:
            rows.append({'event_id': event['seq'], 'type': event_type,
                         'payload': {} if event_type == 'delta' else dict(event),
                         'ts_ms': timestamp})
    rows.sort(key=lambda row: row['event_id'])
    if now_ms is None:
        ends = [row['ts_ms'] for row in rows if row['type'] in _TRACE_TERMINAL_TYPES]
        now_ms = ends[-1] if ends else (rows[-1]['ts_ms'] if rows else 0)
    return _fold_task_trace(task_id, now_ms, exported_rows=rows)

# ── The fold ──

def _fold_task_trace(
    task_id: str,
    now_ms: int | None = None,
    *,
    pending_event: Mapping | None = None,
    pending_sequence: int | None = None,
    status_history: list | None = None,
    status_dropped_count: int = 0,
    ledger_out: dict | None = None,
    exported_rows: list | None = None,
) -> dict:
    """Fold a task's persisted event log into the timing span tree.

    Never throws on shape: unknown/expired tasks return
    ``eventsAvailable:false`` (the Request Inspector honesty precedent). A
    failed event read raises ``TurnTraceEventReadError`` instead.
    ``now_ms`` is the server clock used to bound still-running spans;
    injectable for tests. ``exported_rows`` replaces the authority read for
    an offline replay (``fold_exported_events``).
    """
    read_rows = _read_trace_rows(task_id) if exported_rows is None else exported_rows
    event_limit = bool(getattr(read_rows, 'truncated', False))
    rows = list(read_rows)
    missing_live_clock = False
    live_clock = None
    implicit_now = now_ms is None
    if now_ms is None:
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        from lib.agent_core.execution_clock import ExecutionClock, task_execution_time_ms
        live_task = chat_task_runtime.get(task_id)
        live_clock = (live_task or {}).get('_executionClock')
        missing_live_clock = not isinstance(live_clock, ExecutionClock)
        now_ms = task_execution_time_ms(live_task) if live_task else int(time.time() * 1000)
    now_ms = int(now_ms)
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
                'ts_ms': pending_event.get('executionAtMs', now_ms),
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

    # Payload-free delta rows still carry their captured execution timestamp
    # in the metadata spine. Structural rows prove the producing clock domain.
    evidence = [row['payload'] for row in rows if row.get('payload')]
    clocks = set()
    for payload in evidence:
        clock_id, origin = payload.get('executionClockId'), payload.get('executionOriginAtMs')
        if isinstance(clock_id, str) and clock_id and isinstance(origin, int):
            clocks.add((clock_id, origin))
    legacy_evidence = any(not payload.get('executionClockId') for payload in evidence)
    clock_mixed = bool(clocks) and (len(clocks) != 1 or legacy_evidence)
    clock_source = ('mixed' if clock_mixed else 'server-monotonic' if clocks else 'legacy-wall')
    if implicit_now and clock_source == 'server-monotonic' and live_clock is not None:
        missing_live_clock = (
            (getattr(live_clock, 'clock_id', None), getattr(live_clock, 'origin_at_ms', None))
            not in clocks)

    # Sequence is authority; wall clocks can regress. Never sort by timestamp
    # or present clamped durations as complete timing evidence.
    clock_regressed = False
    watermark = rows[0]['ts_ms']
    normalized_rows = []
    for row in rows:
        timestamp = row['ts_ms']
        if timestamp < watermark:
            clock_regressed = True
        watermark = max(watermark, timestamp)
        normalized_rows.append({**row, 'ts_ms': watermark})
    rows = normalized_rows
    if now_ms < watermark and not any(r['type'] in _TRACE_TERMINAL_TYPES for r in rows):
        clock_regressed = True
        now_ms = watermark

    t0 = rows[0]['ts_ms']
    if clock_source == 'server-monotonic':
        origin = next(iter(clocks))[1]
        if isinstance(origin, int) and 0 < origin <= t0:
            t0 = origin
    terminal = None
    flow_seen = False
    observer_gap = False
    for r in rows:
        if r['type'] in _TRACE_TERMINAL_TYPES:
            terminal = r
        elif is_flow_event_type(r['type']):
            flow_seen = True

        if r['type'] == 'model_request_complete':
            isolation = r['payload'].get('observerIsolation')
            if isinstance(isolation, Mapping):
                observer_gap = observer_gap or any(
                    bool(isolation.get(key)) for key in (
                        'droppedEvents', 'deliveryFailures', 'flushIncomplete',
                        'diagnosticFailures',
                    )
                )
    running = terminal is None
    if running and clock_source == 'server-monotonic' and missing_live_clock:
        # A cold/recovered process cannot extend another process's monotonic
        # clock from its wall time. Bound this snapshot to the last proven tick.
        now_ms = watermark
    t_end_turn = terminal['ts_ms'] if terminal else None
    eff_end = t_end_turn if t_end_turn is not None else now_ms
    if eff_end < t0:
        eff_end = t0

    status = ('running' if terminal is None
              else _terminal_trace_status(terminal['type'], terminal['payload']))

    span_fold = TraceSpanFold(
        t0=t0, running=running, status=status, t_end_turn=t_end_turn,
        flow_seen=flow_seen, phase_rules=_PHASE_TRACE_RULE,
        phase_close_types=_SPAN_OVERLAP_CLOSE_TYPES, apply_budget=_apply_budget,
        startup_stages=_STARTUP_STAGE_BY_DETAIL_KEY,
    )
    for row in rows:
        span_fold.consume(row)
    span_fold.finish(eff_end)
    spans = span_fold.spans
    round_order = span_fold.round_order
    saw_round_markers = span_fold.saw_round_markers
    saw_step_markers = span_fold.saw_step_markers
    flow_seen = span_fold.flow_seen

    for span in spans:
        a, b = span.get('tStart'), span.get('tEnd')
        if a is not None and b is not None and b < a:
            clock_regressed = True
            span['tEnd'] = a

    gaps = _trace_gaps(spans, t0, eff_end)

    # ── Summary (disjoint priority allocation) ──
    attribution = _disjoint_attribution(spans, t0, eff_end)
    summary = _disjoint_summary(spans, t0, eff_end, attribution)
    summary['totalMs'] = eff_end - t0
    if ledger_out is not None:
        ledger_out.update(kindMs=dict(attribution), rounds=len(round_order))
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
    if flow_seen and not saw_step_markers:
        # Flow vocabulary without foldable step markers (legacy endpoint_*
        # logs, or a run that died before its first step): the Planner/
        # Critic structure is not derivable — say so.
        coverage, coverage_reason = 'partial', 'flow'
    elif not saw_round_markers and not saw_step_markers:
        # Legacy log predating the round-boundary events: tools/waits still
        # fold flat, but the llm structure is not derivable — say so.
        coverage, coverage_reason = 'partial', 'no-round-markers'

    if observer_gap:
        coverage, coverage_reason = 'partial', 'observer-gap'
    if event_limit:
        coverage, coverage_reason = 'partial', 'event-limit'
    if clock_regressed:
        coverage, coverage_reason = 'partial', 'clock-regression'
    if clock_mixed:
        coverage, coverage_reason = 'partial', 'clock-domain-change'
    if running and clock_source == 'server-monotonic' and missing_live_clock:
        coverage, coverage_reason = 'partial', 'clock-unavailable'

    if status_history is None:
        folded_status_history, folded_status_dropped = _fold_status_history(
            rows, settle_ts=eff_end, running=running)
    else:
        folded_status_history, folded_status_dropped = _settled_status_history(
            status_history, status_dropped_count,
            settle_ts=eff_end, running=running)

    # Stable wire order: depth-first by (tStart, spanId insertion).
    order = {id(s): i for i, s in enumerate(spans)}
    spans.sort(key=lambda s: (s['depth'], s['tStart'] or 0, order[id(s)]))

    document = {
        **base,
        'status': status,
        'running': running,
        'coverage': coverage,
        'clockSource': clock_source,
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
    'CLIENT_OBSERVATION_KINDS',
    'TRACE_CONTRACT_VERSION',
    'TRACE_COVERAGE_EVENT_READ_FAILED',
    'TRACE_MAX_CLIENT_OBSERVATIONS',
    'TRACE_MAX_COUNTER',
    'TRACE_MAX_GAPS',
    'TRACE_MAX_PERSISTED_BYTES',
    'TRACE_MAX_SPANS',
    'TurnTraceEventReadError',
    'append_client_trace_observation',
    'compact_trace_document',
    'finalize_trace_projection',
    'fold_exported_events',
    'fold_task_trace',
    'invalidate_trace_cache',
    'merge_client_trace_evidence',
    'observe_task_trace_event',
    'project_running_trace_status',
    'read_persisted_task_trace',
    'task_status_history',
    'trace_kind_attribution',
]
