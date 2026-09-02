"""Turn trace — server-authoritative per-task timing fold (the unified
"where did the time go" interface).

Contract: ``docs/TURN_TRACE_CONTRACT.md`` (the single source of truth for
the wire shape). ONE pure fold derives the hierarchical span tree of a task
— turn → round → llm / tool / wait / compaction — from the persisted
``task_events`` log (durable-before-visible, 6h TTL). No new instrumentation
at emit sites: the event log already carries every boundary (round_start /
round_end), every clock (tool tStart/tEnd), and a server ``ts_ms`` on every
row, so the timing structure is DERIVED, never a second fact source that
could drift from what actually happened.

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

import time

from lib.agent_core.events import Phase
from lib.log import get_logger
from lib.orchestration_message_compat import is_flow_event_type

logger = get_logger(__name__)

TRACE_CONTRACT_VERSION = 1

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
    'done', 'error',
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


# ── The fold ──

def fold_task_trace(task_id: str, now_ms: int | None = None) -> dict:
    """Fold a task's persisted event log into the timing span tree.

    Never throws on shape: unknown/expired tasks return
    ``eventsAvailable:false`` (the Request Inspector honesty precedent).
    ``now_ms`` is the server clock used to bound still-running spans;
    injectable for tests.
    """
    rows = _read_trace_rows(task_id)
    base = {
        'version': TRACE_CONTRACT_VERSION,
        'taskId': task_id,
        'eventsAvailable': bool(rows),
    }
    if not rows:
        return base
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)

    t0 = rows[0]['ts_ms']
    terminal = None
    flow_seen = False
    for r in rows:
        if r['type'] in ('done', 'error'):
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
            status = 'aborted' if fr == 'aborted' else 'done'

    spans: list[dict] = []
    turn_span = _span('turn', None, 0, 'turn', 'turn', t0,
                      status=('running' if running else status))
    if not running:
        _close(turn_span, t_end_turn, status)
    spans.append(turn_span)

    rounds: dict[str, dict] = {}       # roundNum(str) -> round span
    round_order: list[str] = []
    cur_round_key: str | None = None
    open_tools: dict[str, dict] = {}   # toolCallId -> tool span
    open_model_requests: dict[str, dict] = {}  # diagnostic spanId -> span
    pending_approval: dict[str, float] = {}  # toolCallId -> request ts
    open_phase_span: dict | None = None  # retry_wait / compaction span
    ttft_open: dict | None = None      # {'round': key, 'tStart': ts}
    usage_by_round: dict[str, list] = {}
    usage_orphans: list[dict] = []
    saw_round_markers = False
    counters = {'wait': 0, 'compact': 0, 'flat_tool': 0}

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
        for cid, tsp in list(open_tools.items()):
            if tsp['attrs'].get('roundKey') == rk:
                _close(tsp, ts, 'unknown')
                tsp['truncated'] = True
                _apply_budget(tsp)
                open_tools.pop(cid, None)
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
            rk = str(p.get('roundNum', '')) or (cur_round_key or '')
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
            sp = _span(f'{parent}.tool.{call_id or counters["flat_tool"]}',
                       parent, depth, 'tool',
                       p.get('toolName') or 'tool', t_start,
                       attrs={'toolName': p.get('toolName') or '',
                              'query': (p.get('query') or '')[:200],
                              'roundKey': rk,
                              'toolCallId': call_id})
            spans.append(sp)
            if call_id:
                open_tools[call_id] = sp
            else:
                open_tools[f'__anon_{id(sp)}'] = sp
            if call_id and call_id in pending_approval:
                ap = _span(f'{sp["spanId"]}.appr', sp['spanId'], depth + 1,
                           'approval_wait', 'approval',
                           pending_approval.pop(call_id),
                           attrs={'toolName': sp['attrs']['toolName']})
                spans.append(ap)
                sp['attrs']['_approval'] = ap
            continue

        if et == 'write_approval_request':
            cid = p.get('toolCallId') or ''
            if cid:
                pending_approval[cid] = ts
                tgt = open_tools.get(cid)
                if tgt is not None and '_approval' not in tgt['attrs']:
                    ap = _span(f'{tgt["spanId"]}.appr', tgt['spanId'],
                               tgt['depth'] + 1, 'approval_wait', 'approval',
                               ts, attrs={'toolName': tgt['attrs']['toolName']})
                    spans.append(ap)
                    tgt['attrs']['_approval'] = ap
            continue

        if et == 'tool_progress':
            cid = p.get('toolCallId') or ''
            exec_start = p.get('execStartTs')
            tgt = open_tools.get(cid) if cid else None
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
            cid = p.get('toolCallId') or ''
            tgt = open_tools.pop(cid, None) if cid else None
            if tgt is None:
                # Fallback: oldest open tool in the current round (legacy
                # rows without call ids) — else drop (orphan result).
                for k, v in list(open_tools.items()):
                    if v['attrs'].get('roundKey') == cur_round_key:
                        tgt = open_tools.pop(k)
                        break
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
    for cid, tsp in list(open_tools.items()):
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

    # Stable wire order: depth-first by (tStart, spanId insertion).
    order = {id(s): i for i, s in enumerate(spans)}
    spans.sort(key=lambda s: (s['depth'], s['tStart'] or 0, order[id(s)]))

    return {
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
    }


__all__ = [
    'TRACE_CONTRACT_VERSION',
    'fold_task_trace',
    'invalidate_trace_cache',
]
