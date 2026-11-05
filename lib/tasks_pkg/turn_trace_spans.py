"""Pure event-to-span projection for one trace read.

TraceSpanFold consumes already ordered, clock-normalized rows and closes only
its private projection state. It never reads tasks/storage, emits events or
owns execution. The caller supplies the phase/budget policy; turn_trace owns
clock evidence, strict accounting, status history and durable publication.
"""
from __future__ import annotations

from collections.abc import Mapping

from lib.agent_core.events import Phase
from lib.orchestration.message_compat import is_flow_event_type
from lib.turn_trace_contract import _bounded_trace_text

# The first execution evidence (or a terminal) ends the startup window.
_STARTUP_END_TYPES = frozenset({
    'round_start', 'step_start', 'model_request_start', 'tool_start',
    'done', 'error', 'aborted', 'interrupted',
})
_TERMINAL_TYPES = frozenset({'done', 'error', 'aborted', 'interrupted'})


def _span(span_id, parent, depth, kind, name, t_start, status='running',
          attrs=None):
    return {
        'spanId': span_id, 'parent': parent, 'depth': depth, 'kind': kind,
        'name': name, 'tStart': int(t_start) if t_start is not None else None,
        'tEnd': None, 'status': status, 'attrs': attrs or {},
    }


def _close(span, t_end, status=None):
    if span.get('tEnd') is None:
        span['tEnd'] = int(t_end) if t_end is not None else None
    if status:
        span['status'] = status


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


def _admission_waiter_identity(p, owner):
    """The waiting call occurrence, so the span names its call without its
    parent tool span (a bounded document may drop that)."""
    round_key = p.get('roundNum')
    return {
        'waiterCallId': _bounded_trace_text(p.get('toolCallId'), 96),
        'waiterRoundKey': str(round_key) if round_key is not None
        else (owner['attrs'].get('roundKey', '') if owner else ''),
        'waiterQuery': (owner['attrs'].get('query') or '') if owner else '',
    }


class TraceSpanFold:
    """Ephemeral occurrence state; no task references or retained cache."""

    def __init__(self, *, t0, running, status, t_end_turn, flow_seen,
                 phase_rules, phase_close_types, apply_budget,
                 startup_stages=None):
        self.start_ms = t0
        self.end_ms = t_end_turn
        self.running = running
        self.status = status
        self.flow_seen = flow_seen
        self.phase_rules = phase_rules
        self.phase_close_types = phase_close_types
        self.apply_budget = apply_budget
        self.used_span_ids: set[str] = set()
        self.span_id_suffixes: dict[str, int] = {}
        self.spans: list[dict] = []
        turn_span = self.make_span('turn', None, 0, 'turn', 'turn', self.start_ms,
                          status=('running' if self.running else self.status))
        if not self.running:
            _close(turn_span, self.end_ms, self.status)
        self.spans.append(turn_span)

        self.rounds: dict[str, dict] = {}       # roundNum(str) -> round span
        self.round_order: list[str] = []
        self.current_round_key: str | None = None
        self.last_round_key: str | None = None
        # Logical model requests started while each round was open.
        self.round_requests: dict[str, list[dict]] = {}
        # Call ids are provider correlation tokens, not task-global identities.
        # Keep occurrence-ordered spans so positional ``call_0`` reuse and even a
        # malformed duplicate inside one round cannot overwrite an older span.
        self.open_tools: dict[str, dict] = {}
        self.open_tools_by_call: dict[tuple[str, str], dict[str, dict]] = {}
        self.tool_span_occurrences: dict[tuple[str, str], int] = {}
        self.open_model_requests: dict[str, dict] = {}  # raw correlation id -> active occurrence
        self.pending_approval: dict[tuple[str, str], list[float]] = {}
        self.open_phase_span: dict | None = None  # retry_wait / compaction span
        self.ttft_open: dict | None = None      # {'round': key, 'tStart': ts}
        self.usage_by_round: dict[str, list] = {}
        self.usage_orphans: list[tuple[str, dict]] = []
        self.saw_round_markers = False
        # Flow node executions (Planner/Critic/Executor …) fold as their own
        # span kind instead of only flagging the task partial. The engine walk
        # is sequential, so at most one step is open; pairing stays keyed by
        # node id so a lost completion closes honestly as truncated.
        self.open_steps: dict[str, dict] = {}
        self.saw_step_markers = False
        self.counters = {'wait': 0, 'compact': 0, 'flat_tool': 0, 'step': 0, 'admission': 0}
        # Startup: [t0, first execution boundary) as named stages; an open
        # stage is [name, tStart] and becomes a span when the next begins.
        self.startup_stages = startup_stages or {}
        self.startup_span: dict | None = None
        self.startup_stage: list | None = None
        self.startup_done = False
        # End of the last model round / flow step: settlement starts there.
        self.settle_from: int | None = None

    def make_span(self, span_id, *args, **kwargs):
        # Correlation tokens are not public occurrence identities. Allocate for
        # every kind so request tokens cannot collide with structural spans.
        candidate = span_id
        suffix = self.span_id_suffixes.get(span_id, 1)
        while candidate in self.used_span_ids:
            suffix += 1
            candidate = f'{span_id}.occ{suffix}'
        self.span_id_suffixes[span_id] = suffix
        self.used_span_ids.add(candidate)
        return _span(candidate, *args, **kwargs)

    def close_tool(self, span, ts, status, *, truncated=False,
                   approval_status=None, approval_truncated=None):
        _close(span, ts, status)
        if truncated:
            span['truncated'] = True
        approval = span['attrs'].get('_approval')
        if approval is not None and approval.get('tEnd') is None:
            _close(approval, span['tEnd'], approval_status or status)
            if (truncated if approval_truncated is None else approval_truncated):
                approval['truncated'] = True
            self.apply_budget(approval)
        self.apply_budget(span)

    def _tool_event_round_key(self, payload: dict) -> str:
        return str(payload.get('roundNum', '')) or (self.current_round_key or '')

    def _find_open_tool(self, round_key: str, call_id: str, *,
                        require_unapproved: bool = False,
                        require_unmarked: bool = False) -> dict | None:
        if round_key and call_id:
            candidates = self.open_tools_by_call.get((round_key, call_id), {}).values()
        else:
            candidates = self.open_tools.values()
        selected = None
        for span in candidates:
            if ((round_key and span['attrs'].get('roundKey') != round_key)
                    or (call_id and span['attrs'].get('toolCallId') != call_id)
                    or (require_unapproved and '_approval' in span['attrs'])
                    or (require_unmarked and span['attrs'].get('_execMarked'))):
                continue
            if round_key or call_id:
                return span
            # A legacy event without either selector is safe only when exactly
            # one occurrence qualifies; do not guess between concurrent calls.
            if selected is not None:
                return None
            selected = span
        return selected

    def _remove_open_tool(self, span):
        span_id = span['spanId']
        del self.open_tools[span_id]
        call_id = span['attrs'].get('toolCallId')
        if isinstance(call_id, str):
            key = (span['attrs']['roundKey'], call_id)
            occurrences = self.open_tools_by_call[key]
            del occurrences[span_id]
            if not occurrences:
                del self.open_tools_by_call[key]

    def _take_open_tool(self, round_key: str, call_id: str) -> dict | None:
        target = self._find_open_tool(round_key, call_id)
        if target is not None:
            self._remove_open_tool(target)
        return target

    def _latest_tool_span(self, round_key: str, call_id: str) -> dict | None:
        # Open occurrence first; else the newest settled one (e.g. a command
        # handed off to the background while it still waited for admission).
        target = self._find_open_tool(round_key, call_id)
        if target is not None:
            return target
        return next((span for span in reversed(self.spans)
                     if span['kind'] == 'tool'
                     and span['attrs'].get('toolCallId') == call_id
                     and (not round_key
                          or span['attrs'].get('roundKey') == round_key)),
                    None)

    def _close_phase_span(self, ts, status='done'):
        if self.open_phase_span is not None:
            _close(self.open_phase_span, ts, status)
            self.apply_budget(self.open_phase_span)
            self.open_phase_span = None

    def _close_ttft(self, ts):
        if self.ttft_open is not None:
            sp = self.ttft_open['span']
            _close(sp, ts, 'done')
            self.apply_budget(sp)
            self.ttft_open = None

    def _close_llm(self, round_key, ts, status_):
        rk = str(round_key)
        rnd = self.rounds.get(rk)
        if rnd is None:
            return
        llm = rnd['attrs'].get('_llm')
        if llm is not None and llm.get('tEnd') is None:
            _close(llm, ts, status_)
            self.apply_budget(llm)

    def _close_round(self, round_key, ts, status_):
        rk = str(round_key)
        rnd = self.rounds.get(rk)
        if rnd is None or rnd.get('tEnd') is not None:
            return
        if self.ttft_open is not None and self.ttft_open.get('round') == rk:
            self._close_ttft(ts)
        self._close_llm(rk, ts, status_)
        for tsp in list(self.open_tools.values()):
            if tsp['attrs'].get('roundKey') == rk:
                self.close_tool(tsp, ts, 'unknown', truncated=True)
                self._remove_open_tool(tsp)
        for request_id, request_span in list(self.open_model_requests.items()):
            if request_span['attrs'].get('roundKey') == rk:
                _close(request_span, ts, 'unknown')
                request_span['truncated'] = True
                self.open_model_requests.pop(request_id, None)
        _close(rnd, ts, status_)
        if self.current_round_key == rk:
            self.current_round_key = None

    def _startup_stage_name(self, event_type, p):
        if event_type == 'phase':
            if p.get('phase') == Phase.EXECUTOR_QUEUED:
                return 'executor_queue'
            if p.get('phase') == Phase.WORKING:
                return self.startup_stages.get(p.get('detailKey') or '')
            return None
        if event_type == 'memory_prefetch':
            # 'started' and the no-memories/load-error outcomes follow the
            # memory store load, so the context stage they interrupt was it.
            started = p.get('phase') == 'started'
            loaded = started or str(p.get('reason') or '').startswith(
                ('no_memories', 'load_error'))
            if (loaded and self.startup_stage is not None
                    and self.startup_stage[0] == 'context'):
                self.startup_stage[0] = 'memory_load'
            return 'memory_select' if started else 'context'
        return None

    def _close_startup_stage(self, ts, status):
        if self.startup_stage is None:
            return
        name, t_start = self.startup_stage
        self.startup_stage = None
        if ts > t_start:
            stage = self.make_span(f'startup.{name}', self.startup_span['spanId'], 2,
                                   'startup', name, t_start)
            _close(stage, ts, status)
            self.spans.append(stage)

    def _close_startup(self, ts, status):
        self._close_startup_stage(ts, status)
        if self.startup_span is not None:
            _close(self.startup_span, ts, status)
        self.startup_done = True

    def _advance_startup(self, event_type, p, ts):
        if self.startup_span is None:
            if event_type in _STARTUP_END_TYPES and ts <= self.start_ms:
                self.startup_done = True
                return
            self.startup_span = self.make_span(
                'startup', 'turn', 1, 'startup', 'startup', self.start_ms)
            self.spans.append(self.startup_span)
            if ts > self.start_ms:
                # Before the first event: request validation, command commit,
                # registration and submission (HTTP) or executor preparation.
                self.startup_stage = ['dispatch', self.start_ms]
        if event_type in _STARTUP_END_TYPES:
            self._close_startup(
                ts, self.status if event_type in _TERMINAL_TYPES else 'done')
            return
        name = self._startup_stage_name(event_type, p)
        if name is not None and (self.startup_stage is None
                                 or self.startup_stage[0] != name):
            self._close_startup_stage(ts, 'done')
            self.startup_stage = [name, ts]

    def consume(self, row):
        event_type, payload, timestamp = row['type'], row['payload'], row['ts_ms']
        if not self.startup_done:
            self._advance_startup(event_type, payload, timestamp)
        # Observational beats cannot close a real retry/compaction interval.
        if (self.open_phase_span is not None
                and event_type in self.phase_close_types
                and event_type != 'phase'):
            self._close_phase_span(timestamp)
        handler = _EVENT_HANDLERS.get(event_type)
        if handler is not None:
            handler(self, payload, timestamp, event_type)
        elif is_flow_event_type(event_type):
            self.flow_seen = True

    def on_round_start(self, p, ts, event_type):
        self.saw_round_markers = True
        self.settle_from = None
        rn = p.get('roundNum')
        key = str(rn)
        # The next model round proves all tools authored by prior rounds
        # have stopped blocking the loop. If their terminal event was
        # lost, close those diagnostic spans honestly instead of allowing
        # a recycled call id to overwrite them later.
        for tsp in list(self.open_tools.values()):
            if tsp['attrs'].get('roundKey') != key:
                self.close_tool(tsp, ts, 'unknown', truncated=True)
                self._remove_open_tool(tsp)
        if self.current_round_key is not None and self.current_round_key != key:
            # A new round implicitly closes the previous one; a missing
            # round_end (crash window) shows as truncated, never a
            # silent overlap.
            prev = self.rounds.get(self.current_round_key)
            if prev is not None and prev.get('tEnd') is None:
                prev['truncated'] = True
            self._close_round(self.current_round_key, ts, 'unknown')
        rnd = self.make_span(f'r{key}', 'turn', 1, 'round', f'round {key}', ts)
        rnd['attrs']['roundKey'] = key
        llm = self.make_span(f'r{key}.llm', rnd['spanId'], 2, 'llm',
                    'llm', ts)
        rnd['attrs']['_llm'] = llm
        self.spans.append(rnd)
        self.spans.append(llm)
        self.rounds[key] = rnd
        self.round_order.append(key)
        self.current_round_key = key
        self.last_round_key = key
        return

    def on_round_end(self, p, ts, event_type):
        self.saw_round_markers = True
        key = str(p.get('roundNum'))
        reason = p.get('reason') or ''
        st = 'done'
        if reason == 'aborted':
            st = 'aborted'
        elif reason == 'error':
            st = 'error'
        self._close_round(key, ts, st)
        self.settle_from = ts
        return

    def on_model_request_start(self, p, ts, event_type):
        request_id = str(p.get('spanId') or '')[:160]
        if not request_id:
            request_id = f'model.request.{len(self.spans)}'
        # A request belongs to the round open when it starts. Its payload
        # roundNum (kept as ``roundKey``) is the 1-based request tag, while
        # ROUND_START numbers loop rounds from 0.
        open_round = self.current_round_key
        rk = self._tool_event_round_key(p)
        if open_round is not None and open_round in self.rounds:
            parent = self.rounds[open_round]['attrs']['_llm']['spanId']
            depth = 3
        else:
            parent = 'turn'
            depth = 1
        supplied_parent = str(p.get('parentSpanId') or '')[:160]
        if p.get('requestLevel') == 'provider_attempt' and supplied_parent:
            parent = supplied_parent
            parent_span = self.open_model_requests.get(parent)
            if parent_span is not None:
                parent = parent_span['spanId']
            depth = parent_span['depth'] + 1 if parent_span else depth + 1
        prior = self.open_model_requests.pop(request_id, None)
        if prior is not None:
            _close(prior, ts, 'unknown')
            prior['truncated'] = True
        request_span = self.make_span(
            request_id, parent, depth, 'llm',
            p.get('requestTag') or p.get('model') or 'model request', ts,
            attrs={
                'model': p.get('model') or '',
                'providerId': p.get('providerId') or '',
                'requestTag': p.get('requestTag') or '',
                'roundKey': rk,
                'requestLevel': p.get('requestLevel') or 'logical',
                **({'attemptLevel': True, 'attempt': p.get('attempt'),
                    'attemptScope': str(p.get('attemptScope') or '')[:80]}
                   if p.get('requestLevel') == 'provider_attempt' else {}),
            })
        self.spans.append(request_span)
        self.open_model_requests[request_id] = request_span
        if open_round is not None and p.get('requestLevel') != 'provider_attempt':
            self.round_requests.setdefault(open_round, []).append(request_span)
        return

    def on_model_request_complete(self, p, ts, event_type):
        request_id = str(p.get('spanId') or '')[:160]
        request_span = self.open_model_requests.pop(request_id, None)
        rk = (self.current_round_key if self.current_round_key is not None
              else str(p.get('roundNum', '')))
        if request_span is None:
            duration_ms = _safe_nonnegative_int(p.get('durationMs'))
            parent = self.rounds[rk]['attrs']['_llm']['spanId'] if rk in self.rounds else 'turn'
            depth = 3 if parent != 'turn' else 1
            supplied_parent = str(p.get('parentSpanId') or '')[:160]
            if p.get('requestLevel') == 'provider_attempt' and supplied_parent:
                parent = supplied_parent
                parent_span = self.open_model_requests.get(parent)
                if parent_span is not None:
                    parent = parent_span['spanId']
                depth = parent_span['depth'] + 1 if parent_span else depth + 1
            request_span = self.make_span(
                request_id or f'model.request.{len(self.spans)}',
                parent, depth, 'llm',
                p.get('requestTag') or p.get('model') or 'model request',
                max(self.start_ms, ts - duration_ms),
                attrs={'roundKey': rk,
                       'requestLevel': p.get('requestLevel') or 'logical',
                       **({'attemptLevel': True, 'attempt': p.get('attempt'),
                           'attemptScope': str(p.get('attemptScope') or '')[:80]}
                          if p.get('requestLevel') == 'provider_attempt' else {})})
            self.spans.append(request_span)
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

        isolation = p.get('observerIsolation')
        if isinstance(isolation, Mapping):
            request_span['attrs']['observerIsolation'] = {
                key: bool(isolation.get(key)) if key == 'flushIncomplete'
                else _safe_nonnegative_int(isolation.get(key))
                for key in ('providerDispatches', 'deferredEvents',
                            'deferredCheckpoints', 'droppedEvents',
                            'deliveryFailures', 'flushIncomplete',
                            'diagnosticFailures') if key in isolation
            }
        return

    def on_phase(self, p, ts, event_type):
        ph = p.get('phase') or ''
        rule = self.phase_rules.get(ph)
        # Observational beats cannot end a real backoff or restart TTFT.
        if ph in (Phase.WAITING_MODEL, Phase.STREAM_STALLED):
            if self.open_phase_span is not None and self.open_phase_span['kind'] == 'retry_wait':
                return
            if ph == Phase.WAITING_MODEL and self.ttft_open is not None:
                return
        coalesce = (self.open_phase_span is not None
                    and self.open_phase_span['name'] == ph
                    and rule in ('retry_wait', 'compaction'))
        if self.open_phase_span is not None and not coalesce:
            self._close_phase_span(ts)
        if rule == 'ttft':
            # TTFT candidate: round's llm span is open; first delta (or
            # tool_start / round close) settles it.
            if self.ttft_open is not None:
                self._close_ttft(ts)
            parent = self.current_round_key
            span_id = (f'r{parent}.ttft.{len(self.spans)}' if parent is not None
                       else f'ttft.{len(self.spans)}')
            sp = self.make_span(span_id,
                       self.rounds[parent]['attrs']['_llm']['spanId'] if parent is not None else 'turn',
                       3 if parent is not None else 1,
                       'llm_ttft', 'ttft', ts,
                       attrs={'model': p.get('model') or ''})
            self.spans.append(sp)
            self.ttft_open = {'span': sp, 'round': parent}
        elif rule in ('retry_wait', 'compaction'):
            if coalesce:
                # Same-phase beat (429 heartbeat ×N): extend, keep the
                # latest attempt/detail — ONE span per continuous wait.
                if p.get('attempt') is not None:
                    self.open_phase_span['attrs']['attempt'] = p['attempt']
                if p.get('detail'):
                    self.open_phase_span['attrs']['detail'] = p['detail']
            else:
                kind = 'retry_wait' if rule == 'retry_wait' else 'compaction'
                ckey = 'wait' if kind == 'retry_wait' else 'compact'
                self.counters[ckey] += 1
                sp = self.make_span(f'{ckey}.{self.counters[ckey]}',
                           'turn', 1, kind, ph, ts,
                           attrs={
                               'detail': p.get('detail') or '',
                               'detailKey': p.get('detailKey') or '',
                               'attempt': p.get('attempt'),
                               'bucket': p.get('bucket') or '',
                               'statusCode': p.get('statusCode'),
                               'model': p.get('model') or '',
                           })
                self.spans.append(sp)
                self.open_phase_span = sp
        # 'covered' / 'ignore' / unknown: no span (unknown phases are
        # forward-compatible wire; their time lands in gaps honestly).
        return

    def on_delta(self, p, ts, event_type):
        if self.ttft_open is not None and ts >= self.ttft_open['span']['tStart']:
            self._close_ttft(ts)
        return

    def on_tool_start(self, p, ts, event_type):
        if self.ttft_open is not None:
            self._close_ttft(ts)
        call_id = p.get('toolCallId') or ''
        t_start = p.get('tStart') or ts
        # Tools execute BETWEEN rounds (the authoring round's round_end
        # already fired), so the payload's roundNum — not the open round
        # — is the parent. A tool child EXTENDS its (closed) round's
        # footprint: round N's true cost includes the tools it authored.
        rk = str(p.get('roundNum', '')) or (self.current_round_key or '')
        # ``roundNum`` here is the tool-call allocator (pairing identity), not
        # the loop round: the authoring round is the open one, or the round
        # that just ended for a tool announced after its round_end.
        owner_round = (self.current_round_key if self.current_round_key is not None
                       else self.last_round_key)
        parent = self.rounds[owner_round]['spanId'] if owner_round in self.rounds else 'turn'
        depth = 2 if parent != 'turn' else 1
        if parent == 'turn':
            self.counters['flat_tool'] += 1
        rnd = self.rounds.get(owner_round)
        if rnd is not None and rnd.get('tEnd') is not None \
                and t_start > rnd['tEnd']:
            rnd['tEnd'] = int(t_start)  # extended again at tool close
        occurrence_key = (rk, str(call_id or '__anonymous__'))
        occurrence = self.tool_span_occurrences.get(occurrence_key, 0) + 1
        self.tool_span_occurrences[occurrence_key] = occurrence
        tool_span_token = call_id or f'anonymous-{occurrence}'
        occurrence_suffix = '' if occurrence == 1 else f'.occ{occurrence}'
        sp = self.make_span(f'{parent}.tool.{tool_span_token}{occurrence_suffix}',
                   parent, depth, 'tool',
                   p.get('toolName') or 'tool', t_start,
                   attrs={'toolName': p.get('toolName') or '',
                          'query': (p.get('query') or '')[:200],
                          'roundKey': rk,
                          'toolCallId': call_id,
                          '_ownerRound': owner_round})
        self.spans.append(sp)
        self.open_tools[sp['spanId']] = sp
        # Validated IDs use the qualified occurrence index. Preserve the old
        # comparison behavior for malformed legacy IDs without hashing them.
        if isinstance(call_id, str):
            self.open_tools_by_call.setdefault((rk, call_id), {})[sp['spanId']] = sp
        approval_key = (rk, str(call_id))
        pending = self.pending_approval.get(approval_key) if call_id else None
        if pending:
            ap = self.make_span(f'{sp["spanId"]}.appr', sp['spanId'], depth + 1,
                       'approval_wait', 'approval',
                       pending.pop(0),
                       attrs={'toolName': sp['attrs']['toolName']})
            if not pending:
                self.pending_approval.pop(approval_key, None)
            self.spans.append(ap)
            sp['attrs']['_approval'] = ap
        return

    def on_write_approval_request(self, p, ts, event_type):
        cid = str(p.get('toolCallId') or '')
        if cid:
            rk = self._tool_event_round_key(p)
            tgt = self._find_open_tool(
                rk, cid, require_unapproved=True)
            if tgt is not None and '_approval' not in tgt['attrs']:
                ap = self.make_span(f'{tgt["spanId"]}.appr', tgt['spanId'],
                           tgt['depth'] + 1, 'approval_wait', 'approval',
                           ts, attrs={'toolName': tgt['attrs']['toolName']})
                self.spans.append(ap)
                tgt['attrs']['_approval'] = ap
            else:
                self.pending_approval.setdefault((rk, cid), []).append(ts)
        return

    def on_tool_progress(self, p, ts, event_type):
        cid = str(p.get('toolCallId') or '')
        rk = self._tool_event_round_key(p)
        exec_start = p.get('execStartTs')
        tgt = self._find_open_tool(
            rk, cid, require_unmarked=True) if cid else None
        if tgt is None and cid:
            tgt = self._find_open_tool(rk, cid)
        if tgt is not None and exec_start:
            # The REAL spawn moment: anything between the tool_start
            # announce and this is queue/approval wait, not execution.
            if not tgt['attrs'].get('_execMarked'):
                tgt['attrs']['_execMarked'] = True
                ap = tgt['attrs'].get('_approval')
                if ap is not None:
                    _close(ap, int(exec_start), 'done')
                    self.apply_budget(ap)
                elif int(exec_start) - int(tgt['tStart'] or ts) > 500:
                    # Self-describing: a bounded document may keep this
                    # span without its parent tool span.
                    sw = self.make_span(f'{tgt["spanId"]}.spawn', tgt['spanId'],
                               tgt['depth'] + 1, 'spawn_wait', 'dispatch',
                               tgt['tStart'] or ts,
                               attrs={key: tgt['attrs'].get(key) or '' for key in (
                                   'toolName', 'toolCallId', 'roundKey', 'query')})
                    _close(sw, int(exec_start), 'done')
                    self.apply_budget(sw)
                    self.spans.append(sw)
        return

    def on_workspace_admission_wait(self, p, ts, event_type):
        t_start, t_end = p.get('waitStartedAtMs'), p.get('waitEndedAtMs')
        if type(t_start) is not int or type(t_end) is not int:
            return
        t_end = max(t_start, t_end)
        cid = str(p.get('toolCallId') or '')
        owner = self._latest_tool_span(self._tool_event_round_key(p), cid) if cid else None
        if owner is not None:
            # A child never outlives its tool: the part of a backgrounded
            # wait after the call returned did not block this turn.
            if owner.get('tStart') is not None:
                t_start = max(t_start, owner['tStart'])
            if owner.get('tEnd') is not None:
                t_end = min(t_end, owner['tEnd'])
            t_end = max(t_start, t_end)
            span_id, parent, depth = (f'{owner["spanId"]}.admission',
                                      owner['spanId'], owner['depth'] + 1)
        else:
            self.counters['admission'] += 1
            span_id, parent, depth = f'admission.{self.counters["admission"]}', 'turn', 1
        held = p.get('holderHeldMs')
        attrs = {
            'waiterTool': _bounded_trace_text(
                p.get('toolName')
                or (owner['attrs'].get('toolName') if owner else ''), 80),
            'holderTool': _bounded_trace_text(p.get('holderTool'), 80),
            'holderConversation': _bounded_trace_text(
                p.get('holderConversation'), 96),
            'heldMs': held if type(held) is int else None,
            'mode': _bounded_trace_text(p.get('mode'), 32),
            'lock': _bounded_trace_text(
                f"{p.get('lockDomain') or ''}:{p.get('lockRoot') or ''}", 128),
        }
        # Both sides of the wait, so one span diagnoses the stall.
        for name, limit in (('waiterClaims', 400), ('holderTask', 96), ('holderMode', 48),
                            ('holderClaims', 400), ('holderCommand', 200)):
            if p.get(name):
                attrs[name] = _bounded_trace_text(p.get(name), limit)
        attrs.update(_admission_waiter_identity(p, owner))
        sp = self.make_span(
            span_id, parent, depth, 'admission_wait', 'workspace admission', t_start,
            attrs=attrs)
        outcome = p.get('outcome')
        _close(sp, t_end, 'done' if outcome == 'admitted'
               else 'aborted' if outcome == 'cancelled' else 'error')
        self.apply_budget(sp)
        self.spans.append(sp)
        return

    def on_tool_result(self, p, ts, event_type):
        cid = str(p.get('toolCallId') or '')
        rk = self._tool_event_round_key(p)
        tgt = self._take_open_tool(rk, cid) if cid else None
        if tgt is None and not cid:
            # Fallback: oldest open tool in the current round (legacy
            # rows without call ids) — else drop (orphan result).
            tgt = self._take_open_tool(rk, '')
        if tgt is not None:
            verdict = p.get('status') or ''
            if not verdict and event_type == 'tool_complete':
                verdict = 'error' if p.get('isError') else ''
            st = ('done' if verdict in ('', 'done')
                  else 'error' if verdict == 'error' else verdict)
            self.close_tool(
                tgt, p.get('tEnd') or ts,
                st if st in ('done', 'error', 'aborted') else 'error',
                approval_status='unknown', approval_truncated=True)
            if st not in ('done',):
                tgt['attrs']['verdict'] = st
            self.apply_budget(tgt)
            rnd = self.rounds.get(tgt['attrs'].get('_ownerRound'))
            if rnd is not None and rnd.get('tEnd') is not None \
                    and tgt['tEnd'] > rnd['tEnd']:
                rnd['tEnd'] = tgt['tEnd']
        return

    def on_compaction(self, p, ts, event_type):
        if self.open_phase_span is None or self.open_phase_span['name'] != Phase.COMPACTING:
            self._close_phase_span(ts)
            self.counters['compact'] += 1
            sp = self.make_span(f'compact.{self.counters["compact"]}', 'turn', 1,
                       'compaction', Phase.COMPACTING, ts,
                       attrs={'detail': p.get('detail') or ''})
            self.spans.append(sp)
            self.open_phase_span = sp
        return

    def on_compaction_done(self, p, ts, event_type):
        if (self.open_phase_span is not None
                and self.open_phase_span['kind'] == 'compaction'):
            self._close_phase_span(ts)
        return

    def on_round_usage(self, p, ts, event_type):
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
        # Usage of the open round's request (its roundNum is the 1-based tag).
        key = (self.current_round_key if self.current_round_key is not None
               else str(p.get('roundNum')))
        if key in self.rounds:
            self.usage_by_round.setdefault(key, []).append(rec)
        else:
            self.usage_orphans.append((key, rec))
        return

    def on_step_start(self, p, ts, event_type):
        self.flow_seen = True
        self.saw_step_markers = True
        self.settle_from = None
        node_id = str(p.get('node_id') or '')[:120]
        # Sequential walk: a new step proves the previous one's
        # completion event was lost if it is still open — close it
        # honestly instead of letting spans overlap.
        for open_node, open_step in list(self.open_steps.items()):
            _close(open_step, ts, 'unknown')
            open_step['truncated'] = True
            self.apply_budget(open_step)
            self.open_steps.pop(open_node, None)
        self.counters['step'] += 1
        step_span = self.make_span(
            f'flowstep.{self.counters["step"]}', 'turn', 1, 'flow_step',
            str(p.get('name') or p.get('role') or 'flow step')[:160], ts,
            attrs={'nodeId': node_id, 'role': str(p.get('role') or '')[:80]})
        self.spans.append(step_span)
        if node_id:
            self.open_steps[node_id] = step_span
        return

    def on_step_complete(self, p, ts, event_type):
        self.flow_seen = True
        self.saw_step_markers = True
        node_id = str(p.get('node_id') or '')[:120]
        step_span = self.open_steps.pop(node_id, None)
        if step_span is not None:
            raw = str(p.get('status') or '')
            step_status = (
                'done' if raw in {'completed', 'done', 'ok', 'success'}
                else 'error' if raw in {'failed', 'error'}
                else 'aborted' if raw == 'aborted' else 'unknown')
            _close(step_span, ts, step_status)
            self.apply_budget(step_span)
        # A completion without its start proves nothing about duration;
        # the missing window stays honestly unattributed.
        self.settle_from = ts
        return

    def _finish_startup_and_settlement(self, settle_ts):
        if not self.startup_done and self.startup_span is not None:
            self._close_startup(settle_ts, 'running' if self.running else 'unknown')
        if (not self.running and self.end_ms is not None
                and self.settle_from is not None and self.end_ms > self.settle_from):
            # Last model round (or flow step) end -> terminal event: final
            # settlement; tools/compaction inside it keep their own kinds.
            settle = self.make_span('finalize', 'turn', 1, 'finalize', 'finalize',
                                    self.settle_from)
            _close(settle, self.end_ms, self.status)
            self.spans.append(settle)

    def _derive_round_windows(self):
        """Carve each round around its logical model requests.

        The round-level llm span encloses the whole round; logical requests
        prove the model's share. Before the first request is preparation;
        after the last (to the next round_start, or the final round_end) is
        settlement — tool dispatch, result persistence and loop bookkeeping,
        while tools/waits inside keep their own kinds. Without request
        evidence only the inter-round window is settlement.
        """
        uses_requests = any(self.round_requests.values())
        last = len(self.round_order) - 1
        for index, key in enumerate(self.round_order):
            rnd = self.rounds[key]
            llm = rnd['attrs'].get('_llm')
            if llm is None or llm.get('tStart') is None or llm.get('tEnd') is None:
                continue
            status = 'running' if self.running and index == last else 'done'
            requests = [s for s in self.round_requests.get(key, ())
                        if s.get('tStart') is not None and s.get('tEnd') is not None]
            prep_to, settle_from = None, llm['tEnd']
            if requests:
                prep_to = min(s['tStart'] for s in requests)
                settle_from = max(s['tEnd'] for s in requests)
            elif uses_requests:
                prep_to = llm['tEnd']  # no request started: still preparing
            settle_to = (self.rounds[self.round_order[index + 1]]['tStart']
                         if index < last else llm['tEnd'])
            for kind, name, t_start, t_end in (
                    ('round_prep', 'prep', llm['tStart'], prep_to),
                    ('round_settle', 'settle', settle_from, settle_to)):
                if t_start is None or t_end is None or t_end <= t_start:
                    continue
                window = self.make_span(f"{rnd['spanId']}.{name}", rnd['spanId'], 2,
                                        kind, name, t_start)
                _close(window, t_end, status)
                self.spans.append(window)

    def finish(self, boundary_ms):
        settle_ts = boundary_ms
        self._finish_startup_and_settlement(settle_ts)
        self._close_ttft(settle_ts)
        if self.open_phase_span is not None:
            self._close_phase_span(settle_ts, 'running' if self.running else 'unknown')
        for tsp in list(self.open_tools.values()):
            self.close_tool(tsp, settle_ts, 'running' if self.running else 'unknown',
                       truncated=not self.running)
        self.open_tools.clear()
        self.open_tools_by_call.clear()
        for request_id, request_span in list(self.open_model_requests.items()):
            _close(request_span, settle_ts, 'running' if self.running else 'unknown')
            if not self.running:
                request_span['truncated'] = True
            self.open_model_requests.pop(request_id, None)
        for rk in self.round_order:
            rnd = self.rounds[rk]
            llm = rnd['attrs'].get('_llm')
            if llm is not None:
                if llm.get('tEnd') is None:
                    _close(llm, rnd.get('tEnd') or settle_ts,
                           'running' if self.running else 'unknown')
                self.apply_budget(llm)
            if rnd.get('tEnd') is None:
                _close(rnd, settle_ts, 'running' if self.running else 'unknown')
                if not self.running:
                    rnd['truncated'] = True
        for open_node, open_step in list(self.open_steps.items()):
            _close(open_step, settle_ts, 'running' if self.running else 'unknown')
            if not self.running:
                open_step['truncated'] = True
            self.apply_budget(open_step)
            self.open_steps.pop(open_node, None)

        # Usage join (llm span attrs — model + authoritative attempt elapsed).
        for key, rec in self.usage_orphans:
            self.usage_by_round.setdefault(key, []).append(rec)
        for key, attempts in self.usage_by_round.items():
            rnd = self.rounds.get(key)
            if rnd is None:
                continue
            llm_span = rnd['attrs'].get('_llm')
            if llm_span is not None:
                llm_span['attrs']['attempts'] = attempts
                if attempts and attempts[0].get('model'):
                    llm_span['attrs']['model'] = attempts[0]['model']

        self._derive_round_windows()

        # Drop the internal llm handles from round attrs (wire cleanliness).
        for rk in self.round_order:
            self.rounds[rk]['attrs'].pop('_llm', None)
            self.rounds[rk]['attrs'].pop('_llm_closed', None)
        for s in self.spans:
            s['attrs'].pop('_approval', None)
            s['attrs'].pop('_execMarked', None)
            s['attrs'].pop('_ownerRound', None)


_EVENT_HANDLERS = {
    'round_start': TraceSpanFold.on_round_start,
    'round_end': TraceSpanFold.on_round_end,
    'model_request_start': TraceSpanFold.on_model_request_start,
    'model_request_complete': TraceSpanFold.on_model_request_complete,
    'phase': TraceSpanFold.on_phase,
    'delta': TraceSpanFold.on_delta,
    'tool_start': TraceSpanFold.on_tool_start,
    'write_approval_request': TraceSpanFold.on_write_approval_request,
    'tool_progress': TraceSpanFold.on_tool_progress,
    'workspace_admission_wait': TraceSpanFold.on_workspace_admission_wait,
    'tool_result': TraceSpanFold.on_tool_result,
    'tool_complete': TraceSpanFold.on_tool_result,
    'compaction': TraceSpanFold.on_compaction,
    'compaction_done': TraceSpanFold.on_compaction_done,
    'round_usage': TraceSpanFold.on_round_usage,
    'step_start': TraceSpanFold.on_step_start,
    'step_complete': TraceSpanFold.on_step_complete,
}
