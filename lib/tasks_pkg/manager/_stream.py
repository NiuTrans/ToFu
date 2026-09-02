"""LLM streaming — ``stream_llm_response`` wires ``dispatch_stream`` deltas into
the task's event system, with periodic crash-recovery checkpoints, TTFT timing,
retry/waiting-model phases, and usage/context-limit auto-learning.

Also ``_display_model_name`` — strips internal gateway/provider prefixes for a
user-facing label.
"""

import time

from lib.agent_core.events import EventType, Phase, build_event, build_phase
from lib.cost import normalize_usage
from lib.llm.stream_result import (
    ProviderStreamResult,
    ensure_provider_stream_result,
)
from lib.llm_dispatch.retry_i18n import (
    GATEWAY_PREFIXES as _GATEWAY_PREFIXES,  # noqa: F401  (re-exported by the manager facade)
    GATEWAY_RETRY_TOKEN as _GATEWAY_RETRY_TOKEN,
    RetryPhaseEventBudget,
    display_model_name as _display_model_name,
    retry_phase_fields,
)
from lib.log import get_logger
from lib.log_redaction import redact_text

from lib.tasks_pkg.manager._delta_coalescer import TaskTextDeltaCoalescer
from lib.tasks_pkg.manager._events import append_event
from lib.tasks_pkg.manager._floor_retry_stream import apply_floor_retry
from lib.tasks_pkg.manager._registry import make_task_abort_check
from lib.tasks_pkg.manager._sync import checkpoint_task_partial

logger = get_logger(__name__)


def dispatch_stream(*args, **kwargs):
    """Load provider dispatch only when a task starts an LLM stream.

    The module-level seam remains patchable by the task/floor-retry tests.
    """
    from lib.llm_dispatch.api import dispatch_stream as _dispatch_stream

    return _dispatch_stream(*args, **kwargs)


# ``_GATEWAY_PREFIXES`` / ``_display_model_name`` / the retry-reason mapping
# live in lib/llm_dispatch/retry_i18n.py (single source of truth shared with
# the swarm emitter, ) — imported above under their legacy
# private names so this module's existing references AND the manager facade's
# re-export (manager/__init__.py) keep working byte-identically.
#
# The dispatcher (lib/llm_dispatch/api.py) passes short English log tokens as
# ``reason``; leaking them verbatim into the phase HUD showed raw English
# jargon mid-generation ("Retrying… Endpoint unreachable (kimi-k3, attempt
# 1)"). retry_phase_fields maps the known tokens to stable typed reasonKeys
# so the frontend localizes the cause; unknown tokens fall back to the raw
# reason (same ruling as an unknown detailKey).
# ── Streaming checkpoint interval (seconds) ──
# During LLM token streaming, we periodically persist partial content to
# the DB so data survives server crashes even when there are no tool rounds.
_STREAM_CHECKPOINT_INTERVAL = 5

_RETRY_REASON_CLASSES = frozenset({
    _GATEWAY_RETRY_TOKEN,
    'Endpoint unreachable',
    'Request timed out',
    'Waiting for model (rate-limited)',
    'Waiting for model (retry backoff)',
    'Waiting for model (shared project limit)',
    'Key balance exhausted',
    'Subscription quota reached',
    'Rate limited (429)',
})

_WAIT_CAUSE_KEYS = {
    'rate_limit': 'stream.retryReason.waitingForModel',
    'quota': 'stream.retryReason.keyBalanceExhausted',
    'upstream': 'stream.retryReason.upstreamError',
    'error': 'stream.retryReason.waitingBackoff',
}


def _append_dispatch_retry_phase(task, event_budget, *, model, attempt,
                                 reason='', status_code=0):
    """Refresh exact liveness and persist only sampled retry phase frames."""
    # Dispatch invokes this callback on every direct 429 retry (~3/s). Keep the
    # independent liveness clock exact even when a durable/UI frame is sampled
    # out; otherwise a legitimate indefinite wait could look wedged.
    task['_dispatch_heartbeat'] = time.time()
    reason_class = (reason if reason in _RETRY_REASON_CLASSES
                    else ('other' if reason else ''))
    if not event_budget.should_emit(
            ('dispatch_retry', reason_class, int(status_code or 0))):
        return

    if status_code == 429:
        legacy = (f'⏳ 模型 {model} 限流中，正在排队重试 '
                  f'(第 {attempt} 次)…')
    elif reason == _GATEWAY_RETRY_TOKEN:
        legacy = (f'⚠️ 后端网关暂时不可用，正在等待恢复'
                  f'（模型保持 {model}，不会自动切换）… 第 {attempt} 次')
    elif reason:
        legacy = f'Retrying… {reason} ({model}, attempt {attempt})'
    else:
        legacy = f'Retrying {model}… (attempt {attempt})'
    fields = retry_phase_fields(
        model=model,
        attempt=attempt,
        reason=reason,
        status_code=status_code,
        legacy_detail=legacy,
    )
    append_event(task, build_phase(
        Phase.RETRYING,
        detail=fields['detail'],
        detailKey=fields['detailKey'],
        detailArgs=fields['detailArgs'],
        attempt=attempt,
        statusCode=status_code,
        model=model,
    ))


def _model_request_complete_fields(
    task,
    *,
    status,
    finish_reason,
    error,
    usage_value,
    span_id,
    model,
    started_ms,
    tag,
    round_num,
):
    """Project provider diagnostics into the bounded request-complete event."""
    dispatch_meta = (
        usage_value.get('_dispatch')
        if isinstance(usage_value, dict)
        and isinstance(usage_value.get('_dispatch'), dict)
        else {}
    )
    network_route = (
        usage_value.get('_network_route')
        if isinstance(usage_value, dict)
        and isinstance(usage_value.get('_network_route'), dict)
        else getattr(error, 'network_route', None)
    )
    if not isinstance(network_route, dict):
        network_route = {}
    failure_stage = str(
        (usage_value.get('_failure_stage')
         if isinstance(usage_value, dict) else '')
        or getattr(error, 'failure_stage', '') or ''
    )[:80]
    fields = {
        'spanId': span_id,
        'model': str(dispatch_meta.get('model') or model or '?')[:160],
        'providerId': str(
            dispatch_meta.get('provider_id')
            or task.get('provider_id') or ''
        )[:160],
        'status': status,
        'finishReason': str(finish_reason or '')[:80],
        'durationMs': max(0, int(time.time() * 1000) - started_ms),
        'requestTag': str(tag or '')[:80],
    }
    stream_state = str(
        usage_value.get('_stream_state')
        if isinstance(usage_value, dict) else ''
    )[:80]
    if stream_state:
        fields['streamState'] = stream_state
    if round_num is not None:
        fields['roundNum'] = round_num
    for event_key, route_key, limit in (
            ('routeId', 'routeId', 160),
            ('routeMode', 'routeMode', 24),
            ('routeDecision', 'decisionReason', 80)):
        route_value = str(network_route.get(route_key) or '')[:limit]
        if route_value:
            fields[event_key] = route_value
    if failure_stage:
        fields['failureStage'] = failure_stage
    if error is not None:
        fields['errorKind'] = type(error).__name__[:160]
        fields['errorDetail'] = ' '.join(
            redact_text(error, max_chars=400).split())[:400]
        try:
            status_code = int(getattr(error, 'status_code', 0) or 0)
        except (TypeError, ValueError, OverflowError):
            status_code = 0
        if status_code > 0:
            fields['statusCode'] = status_code
    elif status == 'failed':
        semantic_progress_timeout = bool(
            isinstance(usage_value, dict)
            and (usage_value.get('_semantic_progress_timeout')
                 or usage_value.get('_no_actionable_timeout')))
        malformed_stream = bool(
            isinstance(usage_value, dict)
            and usage_value.get('_malformed_stream'))
        try:
            semantic_idle_ms = (
                usage_value.get('_semantic_progress_idle_ms')
                if isinstance(usage_value, dict) else None)
            semantic_stall_s = max(0.0, (
                float(semantic_idle_ms) / 1000
                if semantic_idle_ms is not None else float(
                    usage_value.get('_no_actionable_stall_elapsed_s')
                    or usage_value.get('_no_actionable_timeout_s')
                    or (float(usage_value.get('stream_elapsed_ms') or 0) / 1000)
                    or 0))) if isinstance(usage_value, dict) else 0.0
        except (TypeError, ValueError, OverflowError):
            semantic_stall_s = 0.0
        fields['errorKind'] = (
            'SemanticProgressTimeout' if semantic_progress_timeout else
            'MalformedProviderStream' if malformed_stream else
            'PrematureStreamClose')
        fields['errorDetail'] = (
            ('No new reasoning progress, assistant text, or tool action for '
             f'{semantic_stall_s:.1f}s; the rolling semantic-stall window '
             'expired.') if semantic_progress_timeout else
            'At least one provider stream frame was malformed and discarded.'
            if malformed_stream else
            'The upstream stream ended without a complete terminal frame.'
        )
    return fields


def _log_stream_completion(task, *, prefix, model, finish_reason, message):
    """Log the compact terminal shape of one provider stream."""
    logger.info(
        '%s conv=%s stream_llm_response complete: finish_reason=%s model=%s '
        'provider=%s content=%dchars thinking=%dchars tool_calls=%d',
        prefix,
        task.get('convId', ''),
        finish_reason,
        model,
        task.get('provider_id', '?'),
        len(message.get('content', '') or ''),
        len(message.get('reasoning_content', '') or ''),
        len(message.get('tool_calls', [])),
    )


def stream_llm_response(task, body, tag='', on_tool_call_ready=None,
                        *, pool_wide=False,
                        exclude_models=None) -> ProviderStreamResult:
    """Stream an LLM response, wiring deltas into the task's event system.

    Delegates all key selection, retry, 429/401/403 failover to the
    central ``dispatch_stream`` — no duplicate logic needed here.

    Args:
        on_tool_call_ready: callback(tool_call_dict) — fired as each tool
            call's arguments finish streaming.  The orchestrator uses this
            to start executing read-only tools while the model is still
            generating the next tool call (streaming tool execution).
        pool_wide: last-resort mode (llm_fallback pool rescue, owner
            directive 2026-08-03): dispatch NON-strict with no preferred
            model, so the picker may land on ANY healthy (key, model) in
            the pool instead of dying when the requested model's keys are
            all unavailable. ``body['model']`` is still the fallback wire
            value — ``_adapt_stream_body_for_slot`` rewrites it per slot.
        exclude_models: models the rescue must NOT re-try (they already
            failed hard earlier in this fallback chain). Forwarded to
            ``dispatch_stream`` (caller-provided exclusions are permanent
            for the dispatch call).

    Crash-recovery: periodically checkpoints to DB every ~5s during
    streaming so that even pure-LLM responses (no tool calls) survive
    a server crash with minimal data loss.
    """
    pfx = f'[Task {task["id"][:8]}][{tag}]'
    model = body.get('model', '?')
    try:
        _model_request_ordinal = int(task.get('_modelRequestOrdinal') or 0) + 1
    except (TypeError, ValueError, OverflowError):
        _model_request_ordinal = 1
    task['_modelRequestOrdinal'] = _model_request_ordinal
    _attempt_token = str(
        task.get('_attemptId') or task.get('attemptId') or task.get('id') or 'task'
    )[:80]
    _model_request_span = (
        f'model:{_attempt_token}:{_model_request_ordinal}'
    )
    _tag_digits = ''.join(character for character in str(tag) if character.isdigit())
    _activity_round_num = int(_tag_digits) if _tag_digits else None
    _model_request_started_ms = int(time.time() * 1000)
    _model_request_settled = False

    def _clear_request_activity_state():
        if task.get('_activeModelRequestSpan') == _model_request_span:
            task.pop('_activeModelRequestSpan', None)
        if body.get('_request_activity_sink') is _request_activity_sink:
            body.pop('_request_activity_sink', None)

    def _emit_model_request_complete(
        status, *, finish_reason='', error=None, usage_value=None,
    ):
        """Close this request span exactly once without changing LLM control flow."""
        nonlocal _model_request_settled
        if _model_request_settled:
            return
        _model_request_settled = True
        fields = _model_request_complete_fields(
            task,
            status=status,
            finish_reason=finish_reason,
            error=error,
            usage_value=usage_value,
            span_id=_model_request_span,
            model=model,
            started_ms=_model_request_started_ms,
            tag=tag,
            round_num=_activity_round_num,
        )
        try:
            append_event(
                task,
                build_event(EventType.MODEL_REQUEST_COMPLETE, **fields),
            )
        finally:
            _clear_request_activity_state()

    def _on_request_diagnostic(diagnostic):
        """Persist bounded provider projection/isolation diagnostics."""
        if not isinstance(diagnostic, dict):
            return
        if diagnostic.get('kind') == 'wire_projection':
            fields = {
                'model': str(diagnostic.get('model') or model or '?')[:160],
                'backend': str(diagnostic.get('backend') or '')[:80],
                'toolNames': [
                    str(name)[:160]
                    for name in (diagnostic.get('toolNames') or [])[:128]
                ],
                'toolCount': max(0, int(diagnostic.get('toolCount') or 0)),
                'schemaTokens': max(
                    0, int(diagnostic.get('schemaTokens') or 0)),
                'schemaFingerprint': str(
                    diagnostic.get('schemaFingerprint') or '')[:64],
                'schemaBudgetTokens': max(
                    0, int(diagnostic.get('schemaBudgetTokens') or 0)),
                'budgetDroppedNames': [
                    str(name)[:160]
                    for name in (
                        diagnostic.get('budgetDroppedNames') or [])[:128]
                ],
                'compactedNames': [
                    str(name)[:160]
                    for name in (diagnostic.get('compactedNames') or [])[:128]
                ],
                'executableToolCount': max(
                    0, int(diagnostic.get('executableToolCount') or 0)),
                'parentSpanId': _model_request_span,
                'turn': str(task.get('_flow_phase') or '')[:80],
            }
            if _activity_round_num is not None:
                fields['roundNum'] = _activity_round_num
            append_event(
                task,
                build_event(EventType.TOOL_WIRE_PROJECTION, **fields),
            )
            return
        fields = {
            'toolName': str(diagnostic.get('toolName') or 'unknown tool')[:160],
            'stage': str(diagnostic.get('stage') or 'wire_preflight')[:80],
            'reasonCode': str(
                diagnostic.get('reasonCode') or 'invalid_schema'
            )[:160],
            'detail': ' '.join(str(diagnostic.get('detail') or '').split())[:400],
            'action': 'omitted',
            'model': str(model or '?')[:160],
            'parentSpanId': _model_request_span,
        }
        if _activity_round_num is not None:
            fields['roundNum'] = _activity_round_num
        append_event(
            task,
            build_event(EventType.TOOL_SCHEMA_REJECTED, **fields),
        )

    # SESSION-STABLE TTL LATCH — single chokepoint guarantee. Every
    #   task-based LLM send flows through here, so stamp the task id on the body
    #   unconditionally (only when absent — never clobber a call site that set
    #   its own latch key, e.g. the swarm agent's agent_id). add_cache_breakpoints
    #   keys the CACHE_EXTENDED_TTL decision on _task_id via latch_extended_ttl();
    #   a body that reaches the wire WITHOUT it silently falls back to the LIVE
    #   GLOBAL CACHE_EXTENDED_TTL — which can differ from the value this task
    #   latched, flipping the stable system/tools cache_control ttl (1h↔5m) and
    #   re-keying the ENTIRE prefix (the live "<ttl-flip> sole culprit" re-key,
    #   144 rounds in one log window). The main loop / reactive-compact /
    #   fallback set it too, but a synthesize-answer / endpoint / future path can
    #   forget; stamping HERE makes the latch impossible to bypass regardless of
    #   which call site built the body.
    _tid = task.get('id')
    if _tid and not body.get('_task_id'):
        body['_task_id'] = _tid
    # Reset the per-round FloorRetry-adoption marker so reconcile_announced_rounds
    #   (called by _run.py right after this returns) attributes THIS round's
    #   orphans correctly — a round that adopted then a later round that did not
    #   must not read a stale True.
    task['_floor_retry_adopted'] = False
    # Per-round BASE for attempt-restart truncation: a transport/dispatch
    #   retry discards an in-flight attempt whose deltas already landed in
    #   task['content']/['thinking'] (and were checkpointed into the conv row).
    #   Capture the round's starting text so _on_attempt_restart can truncate
    #   back to exactly it — the re-streamed attempt then never stacks on the
    #   abandoned one's tail (the "transport-retry 自愈后重复文本落库" latent
    #   class, ). The shrink-convergent checkpoint path
    #   then settles the row to the retried attempt's text.
    with task['content_lock']:
        _round_base_content = task['content']
        _round_base_thinking = task['thinking']
    # Stamp the round base on the task so analyse_stream_result's
    #   truncated-tool-call guard can reset THIS round's poisoned partial text
    #   (and only this round's) before the transparent retry re-streams.
    task['_round_base_content'] = _round_base_content
    task['_round_base_thinking'] = _round_base_thinking
    # Init to 0.0 (epoch) so the FIRST content/thinking delta checkpoints
    #   immediately, then settle into the _STREAM_CHECKPOINT_INTERVAL cadence.
    #   Starting at time.time() left a pre-first-checkpoint window where a
    #   server crash after the first tokens but before the 5s tick lost the
    #   whole turn. checkpoint_task_partial() no-ops while content+thinking are
    #   still empty, so an early call before any token is harmless. Mirrors the
    #   orchestrator tool-loop's `_last_checkpoint = 0.0` (orchestrator.py).
    _last_stream_ckpt = 0.0

    # Timing: measure time-to-first-token (TTFT) for the FIRST LLM round
    #   of this task only (the "waiting" window the user sees). Anchored to
    #   '_t_prep_done' (set in run_task once context is assembled) and fired
    #   once, on the first content/thinking delta. Guarded so tool-round
    #   re-calls and tasks without the anchor don't re-log.
    _t_request_start = time.time()
    # A retry is allowed to wait forever, but its transient phase history is
    # not.  The callbacks still refresh liveness on every cycle; this sampler
    # bounds only duplicate durable/UI frames for the current LLM round.
    _retry_phase_budget = RetryPhaseEventBudget()

    def _log_ttft_once():
        if task.get('_ttft_done'):
            return
        task['_ttft_done'] = True
        _prep_done = task.get('_t_prep_done')
        _now = time.time()
        if _prep_done:
            _ttft_seconds = max(0.0, _now - _prep_done)
            logger.info('%s [Timing] TTFT=%.3fs (context-ready→first-token), '
                        'request=%.3fs (build_body→first-token) model=%s',
                        pfx, _now - _prep_done, _now - _t_request_start, model)
        else:
            _ttft_seconds = max(0.0, _now - _t_request_start)
            logger.info('%s [Timing] first-token after %.3fs (request) model=%s',
                        pfx, _now - _t_request_start, model)
        try:
            from lib.observability import record_llm_first_token
            record_llm_first_token(
                model, _ttft_seconds, task.get('provider_id') or '')
        except Exception as _metrics_err:
            logger.debug('%s TTFT metric skipped: %s', pfx, _metrics_err)

    def _maybe_checkpoint_during_stream():
        """Called on every content/thinking delta — checkpoint if interval elapsed."""
        nonlocal _last_stream_ckpt
        now = time.time()
        if now - _last_stream_ckpt >= _STREAM_CHECKPOINT_INTERVAL:
            _last_stream_ckpt = now
            try:
                checkpoint_task_partial(task)
            except Exception as e:
                logger.debug('%s streaming checkpoint failed (non-fatal): %s', pfx, e)
            # ── Presence heartbeat (throttled, rides the checkpoint cadence).
            #    Token flow IS work — a long single-LLM turn with no tool rounds
            #    must keep the peer ACTIVE, not flap to idle. One bump per
            #    checkpoint interval (~5s), inside the ACTIVE_TTL window, so no
            #    per-token writes. Best-effort.
            _cfg = task.get('config') or {}
            _pp = _cfg.get('projectPath') or ''
            _cid = task.get('convId') or ''
            if _pp and _cid:
                try:
                    from lib.presence import heartbeat as _presence_heartbeat
                    from lib.tasks_pkg.manager._registry import task_user_id
                    _presence_heartbeat(
                        _pp,
                        _cid,
                        user_id=int(task_user_id(task)),
                        phase='generating',
                    )
                except Exception as e:
                    logger.debug('%s presence heartbeat failed (non-fatal): %s', pfx, e)

    _text_deltas = TaskTextDeltaCoalescer(
        task, append_event, on_first_delta=_log_ttft_once,
        on_after_delta=_maybe_checkpoint_during_stream, log_prefix=pfx)
    _request_activity_sink = _text_deltas.wrap_boundary(
        _on_request_diagnostic)

    def _on_attempt_restart(reason=''):
        """A transport/dispatch-level retry discarded an in-flight attempt:
        truncate the task's text accumulators back to this round's base so the
        re-streamed attempt doesn't stack on the abandoned one's partial tail.
        No-op when nothing was streamed this attempt (pure cooldown waits).
        Deliberately NOT passed to the FloorRetry resend call — during resends
        the first attempt's text is still the fallback content and must
        survive unless a resend is adopted."""
        with task['content_lock']:
            _c, _t = task['content'], task['thinking']
            if _c == _round_base_content and _t == _round_base_thinking:
                return
            task['content'] = _round_base_content
            task['thinking'] = _round_base_thinking
        logger.info('%s conv=%s attempt-restart (%s): truncated discarded '
                    'partial attempt text content %d→%d, thinking %d→%d chars',
                    pfx, task.get('convId', ''), reason,
                    len(_c), len(_round_base_content),
                    len(_t), len(_round_base_thinking))

    def _on_retry(attempt, reason='', status_code=0):
        """Emit SSE phase event so user sees retry status instead of 'Waiting…'.

        We attach the MODEL name and current cycle count so a long wait
        reveals exactly which key/model is being throttled instead of a
        generic spinner.  Previously users just saw "Waiting…" for 60-120s
        during 429 cycling with no indication that the server was alive
        and actively retrying.

        i18n: ships ``detailKey``/``detailArgs`` (plus a typed ``reasonKey``
        for known dispatcher reason tokens) so the frontend HUD localizes;
        the legacy ``detail`` string is kept byte-identical for headless /
        non-i18n clients. ``detailArgs['model']`` uses the display label
        (gateway prefixes stripped) — it is new wire surface, not a legacy
        string change. The structured fields come from the SHARED helper
        (lib/llm_dispatch/retry_i18n.retry_phase_fields) so the swarm
        emitter can never drift from this mapping.
        """
        _append_dispatch_retry_phase(
            task,
            _retry_phase_budget,
            model=model,
            attempt=attempt,
            reason=reason,
            status_code=status_code,
        )

    def _on_waiting(*, status=None, elapsed=None, slot=None):
        """Heartbeat from the current attempt's typed progress snapshot.

        Two jobs:

        1. **HUD.** Emits ``waiting_model`` before semantic output and
           ``stream_stalled`` after reasoning/text/tool progress pauses. Each
           event receives its ordinary event sequence; ``attempt`` remains a
           real retry count and is never reused as a heartbeat counter.

        2. **Reaper liveness.** Refreshes ``_dispatch_heartbeat``. The
           stuck-task reaper (manager/_maintenance.reap_stuck_running_tasks)
           force-fails a task once BOTH ``_t_last_event`` AND
           ``_dispatch_heartbeat`` are stale past 30 min. There is no socket
           read timeout, so this beat keeps the task live during its configured
           semantic window (including deployments that disable or extend it)
           instead of letting the unrelated reaper become the effective
           timeout. ``append_event`` below covers
           ``_t_last_event``; this line covers the other clock, so EITHER
           being fresh (the reaper's own AND-gate) is guaranteed while we
           are legitimately waiting. A truly dead worker emits no beats at
        all, so the reaper keeps its real job.
        """
        task['_dispatch_heartbeat'] = time.time()
        _request_elapsed = getattr(status, 'request_elapsed_s', elapsed or 0)
        _semantic_idle = getattr(status, 'semantic_idle_s', elapsed or 0)
        _status_kind = str(getattr(status, 'kind', '') or '')
        _secs = max(0, int(_request_elapsed or 0))
        _idle_secs = max(0, int(_semantic_idle or 0))
        _label = _display_model_name(model)
        if not _status_kind:
            with task['content_lock']:
                _started = bool(task['content'] != _round_base_content
                                or task['thinking'] != _round_base_thinking)
            _status_kind = 'stream_stalled' if _started else 'waiting_event'
        if _status_kind == 'stream_stalled':
            _phase = Phase.STREAM_STALLED
            _detail_key = 'stream.phase.streamStalled'
            _detail = (f'No new model progress for {_idle_secs}s '
                       f'({_secs}s total) — {_label} is still connected…')
            _args = {
                'model': _label,
                'elapsed': _secs,
                'idle': _idle_secs,
            }
        else:
            _phase = Phase.WAITING_MODEL
            _detail_key = 'stream.phase.waitingForResponse'
            _detail = f'Waiting {_secs}s for {_label} to respond…'
            _args = {'model': _label, 'elapsed': _secs}
        if not _retry_phase_budget.should_emit(
                ('transport_wait', _status_kind)):
            return
        append_event(task, build_phase(
            _phase,
            detail=_detail,
            detailKey=_detail_key,
            detailArgs=_args,
            model=model,
        ))

    # ── Consume unusable-stream force-rotate signal ──
    # If the previous round returned an empty/truncated stream,
    # ``analyse_stream_result`` set
    # ``task['_force_rotate_pair']`` to ``(key_name, model)``.  We pass
    # it as ``avoid_pairs`` to dispatch so the picker steers away from
    # the poisoned slot for THIS attempt only — clear immediately after
    # so a third zero-byte on a different slot doesn't keep the avoid
    # list stuck on the original.
    _avoid_pairs = None
    _rotate_signal = task.pop('_force_rotate_pair', None)
    if _rotate_signal:
        _avoid_pairs = {_rotate_signal}
        logger.info('%s stream-recovery rotate: avoiding %s:%s for this dispatch',
                    pfx, _rotate_signal[0], _rotate_signal[1])

    # Surface the in-flight request as a live phase BEFORE the first token.
    #   Between a finished tool and the model's next token there is a silent
    #   gap (prompt prefill / TTFT) during which no content/thinking delta
    #   fires — and if the next turn is a tool call with no preamble, nothing
    #   renders until tool_start.  Without this the spinner stays frozen on
    #   the previous "Analyzing results…" label and the task looks hung.
    #   Cleared automatically by the first content/thinking delta, or by
    #   tool_start (hasActiveSearch) on the frontend.
    _model_label = _display_model_name(model)
    # The callable is task-scoped request metadata and is stripped at every
    # provider serialization boundary. Install it only when the model span is
    # ready to open, so an earlier setup exception cannot retain the closure on
    # the caller's canonical body.
    body['_request_activity_sink'] = _request_activity_sink
    task['_activeModelRequestSpan'] = _model_request_span
    _model_start_fields = {
        'spanId': _model_request_span,
        'model': str(model or '?')[:160],
        'providerId': str(task.get('provider_id') or '')[:160],
        'requestTag': str(tag or '')[:80],
    }
    if _activity_round_num is not None:
        _model_start_fields['roundNum'] = _activity_round_num
    try:
        append_event(
            task,
            build_event(EventType.MODEL_REQUEST_START, **_model_start_fields),
        )
        append_event(task, build_phase(
            Phase.WAITING_MODEL,
            detail=f'Sent to {_model_label}, waiting for it to start replying…',
            detailKey='stream.phase.waitingForModel',
            detailArgs={'model': _model_label},
            model=model))
    except Exception:
        _clear_request_activity_state()
        raise

    #  ③-3: the abort_check now ALSO consumes the tombstone
    #   channel (in-memory set + throttled DB mark), so an abort that arrived
    #   while this task was missing from the registry still reaches the loop.
    _abort_check = make_task_abort_check(task)
    try:
        stream_result = ensure_provider_stream_result(dispatch_stream(
            body,
            on_thinking=_text_deltas.on_thinking,
            on_content=_text_deltas.on_content,
            on_tool_call_ready=on_tool_call_ready,
            on_before_tool_call_ready=_text_deltas.flush,
            abort_check=_abort_check,
            prefer_model=None if pool_wide else model,
            log_prefix=pfx,
            # User-facing request: the user explicitly chose this model in
            #   the frontend preset selector.  429 retries must stay within
            #   this model's slots (different keys / alias group) — never
            #   silently fall back to a cheaper/different model.  The pool-wide
            #   rescue is the ONE sanctioned exception: the requested model's
            #   keys are already proven unavailable, so holding the pin would
            #   mean dying while healthy slots sit idle.
            strict_model=not pool_wide,
            exclude_models=exclude_models,
            on_retry=_text_deltas.wrap_boundary(_on_retry),
            avoid_pairs=_avoid_pairs,
            on_attempt_restart=_text_deltas.wrap_boundary(_on_attempt_restart),
            on_waiting=_text_deltas.wrap_boundary(_on_waiting),
        ))
        msg, finish_reason, usage = stream_result
        # Final flush shares the request failure/span-cleanup path.
        _text_deltas.close()
    except Exception as error:
        _text_deltas.close_after_error(error)
        request_status = (
            'aborted' if type(error).__name__ == 'AbortedError' else 'failed'
        )
        _emit_model_request_complete(request_status, error=error)
        raise

    # Timing fallback: if the first round was tool-call-only (no content/
    #   thinking deltas fired the TTFT hook), log it now using stream return.
    _log_ttft_once()
    stream_result = apply_floor_retry(
        task, body, msg, finish_reason, usage,
        model=model, pool_wide=pool_wide, pfx=pfx, tag=tag,
        dispatch_stream_fn=dispatch_stream, abort_check=_abort_check,
        on_retry=_on_retry, avoid_pairs=_avoid_pairs,
        on_waiting=_on_waiting,
        round_base_content=_round_base_content,
        round_base_thinking=_round_base_thinking,
        stream_result=stream_result,
    )
    msg, finish_reason, usage = stream_result

    # Propagate provider_id from dispatch metadata into task
    _dispatch = (usage or {}).get('_dispatch', {})
    if _dispatch.get('provider_id'):
        task['provider_id'] = _dispatch['provider_id']

    # Notify user if a model token limit was auto-learned during this request
    _limit_info = (usage or {}).get('_model_limit_learned')
    if _limit_info:
        # Notify via phase event (transient UI status, does NOT pollute
        # assistantMsg.content).  The limit is persisted automatically.
        append_event(task, build_phase(
            Phase.WORKING,
            detail=(f'⚙️ Auto-detected model limit: {_limit_info["model"]} '
                    f'max_tokens={_limit_info["new_limit"]:,} '
                    f'(was {_limit_info["old_limit"]:,})'),
        ))
        logger.info('%s ⚙️ Model limit auto-learned and user notified: %s max_tokens=%d',
                    pfx, _limit_info['model'], _limit_info['new_limit'])

    _log_stream_completion(
        task, prefix=pfx, model=model,
        finish_reason=finish_reason, message=msg,
    )

    # Feed authoritative prompt_tokens into the usage cache so the NEXT
    #   round's compaction check returns a bit-exact number instead of
    #   falling back to the CJK-aware heuristic. Inspired by OpenCode's
    #   MessageV2.Assistant.tokens — the provider already told us the
    #   truth, so trust it instead of re-estimating.
    _total_prompt_tokens = 0
    try:
        conv_id = task.get('convId', '') or ''
        # prompt_tokens is OpenAI-shape; Anthropic returns input_tokens.
        _prompt_tokens = 0
        if isinstance(usage, dict):
            _nu = normalize_usage(usage)
            _prompt_tokens = _nu['input']
            # Anthropic excludes cache from input_tokens; add it back so
            # _total_prompt_tokens reflects the FULL prompt the provider
            # accepted (which is what we use for context-limit expansion).
            _cw = _nu['cache_write']
            _cr = _nu['cache_read']
            if (_cw or _cr) and _prompt_tokens <= (_cw + _cr):
                _total_prompt_tokens = _prompt_tokens + _cw + _cr
            else:
                _total_prompt_tokens = _prompt_tokens
        if conv_id and _total_prompt_tokens > 0:
            from lib.token_counter import record_usage
            # ``body['messages']`` is the exact list we sent. Recording it
            # lets the cache detect edit/regenerate (prefix changed →
            # invalidate) vs append-only (reuse + delta).
            # Record the FULL normalized prompt, NOT the raw input figure:
            #   on Anthropic-convention wires input_tokens EXCLUDES the cache
            #   (a 99%-hit warm round reports only the ~2K residual), so
            #   recording ``_prompt_tokens`` left the usage_cache tier — and
            #   with it the proactive compaction gate — reading ~2K forever
            #   on exactly the warm conversations that need it (the
            #   "context ball at 100% yet compaction never fires" class).
            #   ``_total_prompt_tokens`` is the same normalization the cost
            #   engine and the context-ball already agree on.
            record_usage(
                conv_id,
                prompt_tokens=_total_prompt_tokens,
                model=model,
                message_count=len(body.get('messages') or []),
                messages=body.get('messages'),
            )
    except Exception as e:
        # Usage-cache is a best-effort optimisation — never let a bug
        # here break the LLM return path.
        logger.debug('%s record_usage failed (non-fatal): %s', pfx, e)

    # Auto-learn an EXPANDED context limit when this provider just
    #   accepted a prompt larger than our presumed ceiling. Mirrors the
    #   shrink-on-overflow path in llm_fallback.py.
    if _total_prompt_tokens > 0:
        try:
            from lib.context_limits import learn_expand_from_success
            from lib.tasks_pkg.compaction._tokens import _get_context_limit
            _prior_limit = _get_context_limit(task)
            _expand_info = learn_expand_from_success(
                task.get('provider_id') or '',
                model,
                _total_prompt_tokens,
                preset_limit=_prior_limit,
            )
            if _expand_info:
                append_event(task, build_phase(
                    Phase.WORKING,
                    detail=(
                        f'⚙️ Auto-detected larger context window for '
                        f'{model}: '
                        f'{_expand_info["new_limit"]:,} tokens '
                        f'(was {_expand_info["old_limit"]:,})'
                    ),
                ))
                logger.info('%s ⚙️ Context limit expanded: %s %d → %d '
                            '(observed prompt=%d)',
                            pfx, model, _expand_info['old_limit'],
                            _expand_info['new_limit'], _total_prompt_tokens)
        except Exception as e:
            logger.debug('%s context_limits expand-learn failed: %s', pfx, e)

    _emit_model_request_complete(
        'succeeded' if stream_result.is_verified_complete else 'failed',
        finish_reason=finish_reason,
        usage_value=usage,
    )
    return stream_result
