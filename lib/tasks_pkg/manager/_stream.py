"""LLM streaming — ``stream_llm_response`` wires ``dispatch_stream`` deltas into
the task's event system, with periodic crash-recovery checkpoints, TTFT timing,
retry/waiting-model phases, and usage/context-limit auto-learning.

Also ``_display_model_name`` — strips internal gateway/provider prefixes for a
user-facing label.
"""

import time

from lib.agent_core.events import EventType, build_event
from lib.cost import normalize_usage
from lib.llm_dispatch import dispatch_stream
from lib.llm_dispatch.retry_i18n import (
    GATEWAY_PREFIXES as _GATEWAY_PREFIXES,  # noqa: F401  (re-exported by the manager facade)
    display_model_name as _display_model_name,
    retry_phase_fields,
)
from lib.log import get_logger

from lib.tasks_pkg.manager._events import append_event
from lib.tasks_pkg.manager._sync import checkpoint_task_partial

logger = get_logger(__name__)


# ``_GATEWAY_PREFIXES`` / ``_display_model_name`` / the retry-reason mapping
# live in lib/llm_dispatch/retry_i18n.py (single source of truth shared with
# the swarm emitter, pt_18ebee9c9ea64cf3) — imported above under their legacy
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

def stream_llm_response(task, body, tag='', on_tool_call_ready=None,
                        *, pool_wide=False, exclude_models=None):
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

    ★ Crash-recovery: periodically checkpoints to DB every ~5s during
    streaming so that even pure-LLM responses (no tool calls) survive
    a server crash with minimal data loss.
    """
    pfx = f'[Task {task["id"][:8]}][{tag}]'
    model = body.get('model', '?')
    # ★ SESSION-STABLE TTL LATCH — single chokepoint guarantee. Every
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
    # ★ Reset the per-round FloorRetry-adoption marker so reconcile_announced_rounds
    #   (called by _run.py right after this returns) attributes THIS round's
    #   orphans correctly — a round that adopted then a later round that did not
    #   must not read a stale True.
    task['_floor_retry_adopted'] = False
    # ★ Per-round BASE for attempt-restart truncation: a transport/dispatch
    #   retry discards an in-flight attempt whose deltas already landed in
    #   task['content']/['thinking'] (and were checkpointed into the conv row).
    #   Capture the round's starting text so _on_attempt_restart can truncate
    #   back to exactly it — the re-streamed attempt then never stacks on the
    #   abandoned one's tail (the "transport-retry 自愈后重复文本落库" latent
    #   class, pt_6e12b1ffd95a453e). The shrink-convergent checkpoint path
    #   then settles the row to the retried attempt's text.
    with task['content_lock']:
        _round_base_content = task['content']
        _round_base_thinking = task['thinking']
    # ★ Init to 0.0 (epoch) so the FIRST content/thinking delta checkpoints
    #   immediately, then settle into the _STREAM_CHECKPOINT_INTERVAL cadence.
    #   Starting at time.time() left a pre-first-checkpoint window where a
    #   server crash after the first tokens but before the 5s tick lost the
    #   whole turn. checkpoint_task_partial() no-ops while content+thinking are
    #   still empty, so an early call before any token is harmless. Mirrors the
    #   orchestrator tool-loop's `_last_checkpoint = 0.0` (orchestrator.py).
    _last_stream_ckpt = 0.0

    # ★ Timing: measure time-to-first-token (TTFT) for the FIRST LLM round
    #   of this task only (the "waiting" window the user sees). Anchored to
    #   '_t_prep_done' (set in run_task once context is assembled) and fired
    #   once, on the first content/thinking delta. Guarded so tool-round
    #   re-calls and tasks without the anchor don't re-log.
    _t_request_start = time.time()

    def _log_ttft_once():
        if task.get('_ttft_done'):
            return
        task['_ttft_done'] = True
        _prep_done = task.get('_t_prep_done')
        _now = time.time()
        if _prep_done:
            logger.info('%s [Timing] TTFT=%.3fs (context-ready→first-token), '
                        'request=%.3fs (build_body→first-token) model=%s',
                        pfx, _now - _prep_done, _now - _t_request_start, model)
        else:
            logger.info('%s [Timing] first-token after %.3fs (request) model=%s',
                        pfx, _now - _t_request_start, model)

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
                    _presence_heartbeat(_pp, _cid, phase='generating')
                except Exception as e:
                    logger.debug('%s presence heartbeat failed (non-fatal): %s', pfx, e)

    def _on_thinking(td):
        _log_ttft_once()
        with task['content_lock']:
            task['thinking'] += td
        append_event(task, build_event(EventType.DELTA, thinking=td))
        _maybe_checkpoint_during_stream()

    def _on_content(cd):
        _log_ttft_once()
        with task['content_lock']:
            task['content'] += cd
        append_event(task, build_event(EventType.DELTA, content=cd))
        _maybe_checkpoint_during_stream()

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
        if status_code == 429:
            # Rate-limit: surface the model clearly and phrase it as a
            # queue wait rather than an error.
            _legacy = (f'⏳ 模型 {model} 限流中，正在排队重试 '
                       f'(第 {attempt} 次)…')
        elif reason:
            _legacy = f'Retrying… {reason} ({model}, attempt {attempt})'
        else:
            _legacy = f'Retrying {model}… (attempt {attempt})'
        _fields = retry_phase_fields(model=model, attempt=attempt,
                                     reason=reason, status_code=status_code,
                                     legacy_detail=_legacy)
        append_event(task, build_event(
            EventType.PHASE,
            phase='retrying',
            detail=_fields['detail'],
            detailKey=_fields['detailKey'],
            detailArgs=_fields['detailArgs'],
            attempt=attempt,
            statusCode=status_code,
            model=model,
        ))

    # ★ Slot cooldown-reason → typed reasonKey for the waiting heartbeat.
    #   Mirrors the dispatcher's honest-label ruling: a cooldown wait is
    #   限流 ONLY when the cooling slot actually says rate_limit.
    _WAIT_CAUSE_KEYS = {
        'rate_limit': 'stream.retryReason.waitingForModel',
        'quota': 'stream.retryReason.keyBalanceExhausted',
        'upstream': 'stream.retryReason.upstreamError',
        'error': 'stream.retryReason.waitingBackoff',
    }

    def _on_waiting(elapsed, slot=None):
        """Heartbeat while an attempt is SILENT (no bytes yet, or a
        mid-stream stall).

        Two jobs:

        1. **HUD.** Emits a transient ``retrying`` PHASE event so a slow
           upstream shows a LIVE "still waiting + what the pool knows"
           label instead of a static spinner. ``phase='retrying'`` is
           deliberate and load-bearing: the frontend retrying branch keys
           its DOM refresh on ``attempt``, so each beat (attempt=beat
           number) actually repaints — a constant-phase heartbeat would
           freeze on the first beat's text.

        2. **Reaper liveness.** Refreshes ``_dispatch_heartbeat``. The
           stuck-task reaper (manager/_maintenance.reap_stuck_running_tasks)
           force-fails a task once BOTH ``_t_last_event`` AND
           ``_dispatch_heartbeat`` are stale past 30 min. There is no read
           timeout any more, so a genuinely long silence is no longer
           interrupted-and-retried — without this bump the reaper would
           become the new 30-minute timeout and kill exactly the long waits
           we made legal, writing a "terminated as wedged" error bubble
           into the conversation. ``append_event`` below covers
           ``_t_last_event``; this line covers the other clock, so EITHER
           being fresh (the reaper's own AND-gate) is guaranteed while we
           are legitimately waiting. A truly dead worker emits no beats at
           all, so the reaper keeps its real job.
        """
        task['_dispatch_heartbeat'] = time.time()
        _secs = int(elapsed)
        try:
            from lib.llm._transport import IDLE_HEARTBEAT_S as _hb
            _beat = max(1, int(elapsed // max(1, _hb)))
        except Exception as _e:
            logger.debug('on waiting: failed (%s)', _e)
            _beat = max(1, int(elapsed // 20))
        _label = _display_model_name(model)
        _reason = ''
        _reason_key = ''
        if slot is not None:
            _cr = getattr(slot, 'cooldown_reason', '') or ''
            _cooled = (getattr(slot, 'cooldown_until', 0) or 0) > time.time()
            if _cooled and _cr in _WAIT_CAUSE_KEYS:
                _reason_key = _WAIT_CAUSE_KEYS[_cr]
                _reason = _cr  # raw fallback if the key is unknown client-side
            else:
                _lem = getattr(slot, 'last_error_msg', '') or ''
                if _lem:
                    _reason = str(_lem)[:80]
                else:
                    _ce = getattr(slot, 'consecutive_errors', 0) or 0
                    if _ce >= 2:
                        _reason = f'{_ce} consecutive errors on this line'
        # ★ Honest label: the beat now fires for TWO shapes, and calling a
        #   mid-stream stall "no first byte yet" would be a lie the user can
        #   see (text is already on screen). Distinguish by whether this
        #   round has produced anything yet.
        with task['content_lock']:
            _started = bool(task['content'] != _round_base_content
                            or task['thinking'] != _round_base_thinking)
        if _started:
            _detail_key = ('stream.phase.stalledMidStreamReason' if _reason
                           else 'stream.phase.stalledMidStream')
            _detail = (f'Paused {_secs}s — {_label} stopped mid-reply'
                       + (f' ({_reason})' if _reason else '…'))
        elif _reason:
            _detail_key = 'stream.phase.waitingFirstByteReason'
            _detail = (f'Waiting {_secs}s — no first byte from {_label} yet '
                       f'({_reason})')
        else:
            _detail_key = 'stream.phase.waitingFirstByte'
            _detail = f'Waiting {_secs}s — no first byte from {_label} yet…'
        _args = {'model': _label, 'elapsed': _secs}
        if _reason:
            _args['reason'] = _reason
        if _reason_key:
            _args['reasonKey'] = _reason_key
        append_event(task, build_event(
            EventType.PHASE,
            phase='retrying',
            detail=_detail,
            detailKey=_detail_key,
            detailArgs=_args,
            attempt=_beat,
            model=model,
        ))

    # ── Consume zero-byte force-rotate signal ──
    # If the previous round zero-byte'd, ``analyse_stream_result`` set
    # ``task['_force_rotate_pair']`` to ``(key_name, model)``.  We pass
    # it as ``avoid_pairs`` to dispatch so the picker steers away from
    # the poisoned slot for THIS attempt only — clear immediately after
    # so a third zero-byte on a different slot doesn't keep the avoid
    # list stuck on the original.
    _avoid_pairs = None
    _rotate_signal = task.pop('_force_rotate_pair', None)
    if _rotate_signal:
        _avoid_pairs = {_rotate_signal}
        logger.info('%s zero-byte force-rotate: avoiding %s:%s for this dispatch',
                    pfx, _rotate_signal[0], _rotate_signal[1])

    # ★ Surface the in-flight request as a live phase BEFORE the first token.
    #   Between a finished tool and the model's next token there is a silent
    #   gap (prompt prefill / TTFT) during which no content/thinking delta
    #   fires — and if the next turn is a tool call with no preamble, nothing
    #   renders until tool_start.  Without this the spinner stays frozen on
    #   the previous "Analyzing results…" label and the task looks hung.
    #   Cleared automatically by the first content/thinking delta, or by
    #   tool_start (hasActiveSearch) on the frontend.
    _model_label = _display_model_name(model)
    append_event(task, build_event(
        EventType.PHASE, phase='waiting_model',
        detail=f'Sent to {_model_label}, waiting for it to start replying…',
        detailKey='stream.phase.waitingForModel',
        detailArgs={'model': _model_label},
        model=model))

    # Resolve dispatch_stream THROUGH the package facade at call time so a test's
    # ``monkeypatch.setattr(lib.tasks_pkg.manager, 'dispatch_stream', …)`` steers
    # this stream exactly as it did on the pre-split single module (which imported
    # dispatch_stream at module top-level, making it patchable on `manager`).
    import lib.tasks_pkg.manager as _mgr_facade
    _dispatch_stream = getattr(_mgr_facade, 'dispatch_stream', dispatch_stream)
    # ★ pt_a21cd6eb ③-3: the abort_check now ALSO consumes the tombstone
    #   channel (in-memory set + throttled DB mark), so an abort that arrived
    #   while this task was missing from the registry still reaches the loop.
    #   Facade-resolved like everything else; falls back to the bare flag when
    #   the facade predates the factory (partial bundle / legacy tests).
    _mk_abort_check = getattr(_mgr_facade, 'make_task_abort_check', None)
    _abort_check = (_mk_abort_check(task) if callable(_mk_abort_check)
                    else (lambda: task.get('aborted', False)))
    msg, finish_reason, usage = _dispatch_stream(
        body,
        on_thinking=_on_thinking,
        on_content=_on_content,
        on_tool_call_ready=on_tool_call_ready,
        abort_check=_abort_check,
        prefer_model=None if pool_wide else model,
        log_prefix=pfx,
        # ★ User-facing request: the user explicitly chose this model in
        #   the frontend preset selector.  429 retries must stay within
        #   this model's slots (different keys / alias group) — never
        #   silently fall back to a cheaper/different model.  The pool-wide
        #   rescue is the ONE sanctioned exception: the requested model's
        #   keys are already proven unavailable, so holding the pin would
        #   mean dying while healthy slots sit idle.
        strict_model=not pool_wide,
        exclude_models=exclude_models,
        on_retry=_on_retry,
        avoid_pairs=_avoid_pairs,
        on_attempt_restart=_on_attempt_restart,
        on_waiting=_on_waiting,
    )

    # ★ Timing fallback: if the first round was tool-call-only (no content/
    #   thinking deltas fired the TTFT hook), log it now using stream return.
    _log_ttft_once()

    # ★ Floor-collapse identical-resend mitigation (env-gated, default OFF).
    #   A byte-STABLE round whose cache_read pinned at the system+tools floor
    #   is the SERVER-SIDE stochastic cache-write-visibility miss (proven by
    #   4-run identical-byte replay: different rounds collapse each run). A
    #   resend of the IDENTICAL body re-rolls the gateway's dice and usually
    #   hits the now-visible cache write — driving effective floor% toward zero
    #   (harness: mrsfs9d6 20%->0%). Discipline: only on a proven byte-stable
    #   collapse, capped, and STOP on a throttle error (don't pile retries on
    #   an already-throttled gateway). See lib/tasks_pkg/floor_retry.py +
    #   docs/CACHE_GATEWAY_STOCHASTIC_REPORT.md.
    # ★ Tracks whether ANY floor-retry resend's response was adopted into the
    #   returned (msg, finish_reason, usage). Both adoption sites below
    #   (RECOVERED and still-floored-loop-exhausted) stream with
    #   on_content=None / on_thinking=None, so the adopted resend's text NEVER
    #   reached task['content']/task['thinking'] — those still hold ONLY the
    #   FIRST attempt's (floor-collapsed, often partial) deltas. Since _sync
    #   persists from task['content'] (not the returned msg), an adopted resend
    #   would silently persist the first-attempt residue (the live 3411→215
    #   loss). We converge ONCE after the loop, covering both doors.
    _fr_adopted = False
    # ★ HONEST ACCOUNTING: every attempt the gateway processed (whether
    #   ADOPTED or DISCARDED) was BILLED. Collect their usage dicts here
    #   so the outer LLM-fallback loop can append them to api_rounds and
    #   accumulate them — the "reported cost < actual gateway bill" bug
    #   is impossible when every billed request appears once in api_rounds.
    _fr_discarded_billing = []  # list of {'model', 'usage', 'tag'}
    try:
        from lib.tasks_pkg import floor_retry as _fr
        _conv_for_fr = task.get('convId', '') or ''
        # pool_wide rescue: floor-retry resends the identical body to the
        # SAME model — undefined when the rescue is free to roam models —
        # so the mitigation stays off for rescue dispatches.
        if (_fr.floor_retry_enabled() and _conv_for_fr and not pool_wide
                and _fr.is_floor_collapse(usage)
                and _fr.wire_prefix_stable(_conv_for_fr, usage)):
            _fr_max = _fr.floor_retry_max()
            # The primary attempt (whose msg/usage `usage` currently holds) is
            # the FIRST billed request; it is about to be superseded by a resend
            # if one recovers. Preserve its usage now so it survives the
            # `usage = _rusage` reassignments below.
            _fr_primary_billed_usage = dict(usage) if isinstance(usage, dict) else None
            for _fr_i in range(_fr_max):
                if task.get('aborted', False):
                    break
                _fr_u = normalize_usage(usage)
                logger.warning(
                    '%s conv=%s [FloorRetry] byte-stable floor-collapse '
                    '(read=%s write=%s) — resending identical body (%d/%d)',
                    pfx, _conv_for_fr,
                    _fr_u['cache_read'], _fr_u['cache_write'],
                    _fr_i + 1, _fr_max)
                try:
                    # ★ Layer-1 orphan fix: a FloorRetry resend re-streams the
                    #   IDENTICAL body purely to re-roll the gateway's cache-write
                    #   dice for a cheaper usage — its token/tool deltas are
                    #   THROWAWAY unless it RECOVERS (adopted below). Reusing
                    #   on_tool_call_ready here made every discarded resend
                    #   announce a fresh 'searching' tool round (new tc_id) that
                    #   never survived into the final assistant_msg → an orphan
                    #   swept to status='aborted' with an empty result, which the
                    #   reader then had to defend against (layer 2). Pass None —
                    #   exactly as on_thinking/on_content already are — so the
                    #   resend announces NOTHING. If it RECOVERS, parse_tool_calls
                    #   re-emits the adopted response's tool_start (a few-hundred-ms
                    #   later chip; functionally lossless — owner-approved).
                    _rmsg, _rfin, _rusage = _dispatch_stream(
                        body,
                        on_thinking=None, on_content=None,
                        on_tool_call_ready=None,
                        abort_check=_abort_check,
                        prefer_model=model, log_prefix=f'{pfx}[floor-retry{_fr_i+1}]',
                        strict_model=True, on_retry=_on_retry,
                        avoid_pairs=_avoid_pairs,
                        on_waiting=_on_waiting)
                except Exception as _rerr:
                    # 503/throttle/transient — do NOT keep piling resends on an
                    # already-throttled gateway; that only deepens the throttle.
                    logger.warning('%s [FloorRetry] resend %d errored, stopping: '
                                   '%s: %s', pfx, _fr_i + 1,
                                   type(_rerr).__name__, str(_rerr)[:120])
                    break
                # ★ HONEST ACCOUNTING: the CURRENT `usage` is about to be
                #   superseded. Whatever it points to now (the primary attempt
                #   on iter 0, or the previously-floored resend on iter >0) was
                #   BILLED by the gateway — preserve it before overwriting.
                if isinstance(usage, dict):
                    _disc_tag_suffix = ('primary' if _fr_i == 0
                                        else f'resend{_fr_i}')
                    _fr_discarded_billing.append({
                        'model': model,
                        'usage': {k: v for k, v in usage.items()
                                  if k != '_extra_billing_rounds'},
                        'tag': f'{tag}-FLOOR-DISCARDED-{_disc_tag_suffix}'
                        if tag else f'FLOOR-DISCARDED-{_disc_tag_suffix}',
                    })
                if not _fr.is_floor_collapse(_rusage):
                    # Recovered: the resend hit the now-visible cache write.
                    # Adopt its response + usage (a genuine cache read, cheaper
                    # AND the same conversation content — the body was identical).
                    _ru = normalize_usage(_rusage)
                    logger.warning('%s conv=%s [FloorRetry] RECOVERED on resend %d '
                                   '(read=%s write=%s)', pfx, _conv_for_fr, _fr_i + 1,
                                   _ru['cache_read'], _ru['cache_write'])
                    msg, finish_reason, usage = _rmsg, _rfin, _rusage
                    _fr_adopted = True
                    break
                # Still floored — keep the freshest usage and try again.
                msg, finish_reason, usage = _rmsg, _rfin, _rusage
                _fr_adopted = True
    except Exception as _fre:
        logger.debug('%s [FloorRetry] mitigation skipped (non-fatal): %s', pfx, _fre)

    # ★ FloorRetry content-track convergence (fixes the 3411→215 silent loss).
    #   When a resend was adopted, its full text lives ONLY in the returned
    #   `msg` — the adopted resend streamed with on_content=None/on_thinking=None,
    #   so task['content']/task['thinking'] still hold the FIRST attempt's
    #   (floor-collapsed, partial) deltas. _sync persists from task['content'],
    #   so without this the partial first-attempt text is what lands in the DB.
    #   A resend is a byte-identical-body FRESH generation, so REPLACE (not
    #   append) — the adopted msg is the whole, authoritative answer. We do NOT
    #   emit DELTA_RESET / replay here: the live tab is reconciled by the done
    #   event's committedMessage (existing mechanism), so no new visual behavior.
    if _fr_adopted:
        with task['content_lock']:
            _discarded_content = task['content']
            _discarded_thinking = task['thinking']
            # ★ Base-preserve (owner audit on pt_6e12b1f): task['content']/
            #   ['thinking'] ACCUMULATE across ALL rounds of this turn — the
            #   main orchestrator loop has no per-round content reset (only
            #   the one-time contentPrefix seed at _run.py:501). The adopted
            #   msg holds THIS round's text only, so a wholesale replace
            #   would silently drop every prior round's prose from the
            #   persisted answer (the R1+R2 preamble the user already read).
            #   Keep the round base captured at stream entry and replace
            #   only this round's tail. The residue recording below stays
            #   the FULL pre-convergence snapshot — the checkpointed conv
            #   row mirrors that full text and the terminal-guard exemption
            #   byte-matches on it.
            task['content'] = _round_base_content + (msg.get('content') or '')
            task['thinking'] = _round_base_thinking + (msg.get('reasoning_content') or '')
        # ★ Record the DISCARDED first-attempt text verbatim (bounded). The
        #   ~5s streaming checkpoint mirrors task['content']/['thinking'] into
        #   conversations.messages DURING the attempt — so after this
        #   convergence the conv row can still hold the discarded draft while
        #   the task holds the adopted one. Downstream guards (the terminal
        #   content guard / CAS re-read guard in _sync.py) treat "existing >
        #   new" as "frontend genuinely won"; an EXACT byte-match against this
        #   recorded residue is how they tell our own discarded attempt apart
        #   from a real frontend win and overwrite it with the authoritative
        #   final answer (the live mrxij7q34xm070 "abrupt stop" bug: the
        #   4344-char discarded draft survived with a stop finish-tag).
        if _discarded_content or _discarded_thinking:
            _residue = task.setdefault('_floor_retry_residue', [])
            if len(_residue) < 8:
                _residue.append({'content': _discarded_content,
                                 'thinking': _discarded_thinking})
        # ★ Record the TRUE cause of any orphan tool round this turn produces.
        #   When a FloorRetry resend is adopted, the FIRST attempt's tool calls
        #   (announced live via on_tool_call_ready → 'searching' rounds) are NOT
        #   in the adopted msg (the resend re-minted fresh tc_ids), so
        #   reconcile_announced_rounds settles them as 'superseded' orphans.
        #   This marker lets reconcile log the accurate cause (FloorRetry
        #   adoption) instead of the hardcoded — and, per the app.log evidence,
        #   FALSE — "discarded stream-retry attempt" story: stream transient
        #   retries were 0 while FloorRetry drove 100% of observed orphans.
        task['_floor_retry_adopted'] = True
        logger.info('%s [FloorRetry] converged task content/thinking from adopted '
                    'resend (content=%dchars thinking=%dchars) — prevents first-'
                    'attempt residue from being persisted',
                    pfx, len(task['content']), len(task['thinking']))

    # ★ HONEST ACCOUNTING: expose every discarded-but-billed FloorRetry
    #   attempt on the returned usage dict so the LLM-fallback loop can
    #   append them to api_rounds and accumulated_usage. The gateway billed
    #   each of these; the cost popover / wallet / daily-report MUST see them.
    #   Silent covering-up of billed rounds is what motivated flipping the
    #   floor-retry default OFF — but even opt-in usage must be honest.
    if _fr_discarded_billing and isinstance(usage, dict):
        # dict.setdefault: never clobber a caller-provided list (defensive).
        _bill_list = usage.setdefault('_extra_billing_rounds', [])
        if isinstance(_bill_list, list):
            _bill_list.extend(_fr_discarded_billing)
        else:
            usage['_extra_billing_rounds'] = list(_fr_discarded_billing)
        logger.warning('%s [FloorRetry] preserved %d discarded-but-billed '
                       'attempt(s) for honest cost accounting: tags=%s',
                       pfx, len(_fr_discarded_billing),
                       [b['tag'] for b in _fr_discarded_billing])

    # ★ Propagate provider_id from dispatch metadata into task
    _dispatch = (usage or {}).get('_dispatch', {})
    if _dispatch.get('provider_id'):
        task['provider_id'] = _dispatch['provider_id']

    # ★ Notify user if a model token limit was auto-learned during this request
    _limit_info = (usage or {}).get('_model_limit_learned')
    if _limit_info:
        # Notify via phase event (transient UI status, does NOT pollute
        # assistantMsg.content).  The limit is persisted automatically.
        append_event(task, build_event(
            EventType.PHASE,
            phase='retrying',
            detail=(f'⚙️ Auto-detected model limit: {_limit_info["model"]} '
                    f'max_tokens={_limit_info["new_limit"]:,} '
                    f'(was {_limit_info["old_limit"]:,})'),
        ))
        logger.info('%s ⚙️ Model limit auto-learned and user notified: %s max_tokens=%d',
                    pfx, _limit_info['model'], _limit_info['new_limit'])

    _content_len = len(msg.get('content', '') or '')
    _thinking_len = len(msg.get('reasoning_content', '') or '')
    _tool_calls = len(msg.get('tool_calls', []))
    _provider = task.get('provider_id', '?')
    logger.info('%s conv=%s stream_llm_response complete: finish_reason=%s model=%s '
                'provider=%s content=%dchars thinking=%dchars tool_calls=%d',
                pfx, task.get('convId', ''), finish_reason, model,
                _provider, _content_len, _thinking_len, _tool_calls)

    # ★ Feed authoritative prompt_tokens into the usage cache so the NEXT
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
            # ★ Record the FULL normalized prompt, NOT the raw input figure:
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

    # ★ Auto-learn an EXPANDED context limit when this provider just
    #   accepted a prompt larger than our presumed ceiling. Mirrors the
    #   shrink-on-overflow path in llm_fallback.py.
    if _total_prompt_tokens > 0:
        try:
            from lib.context_limits import learn_expand_from_success
            from lib.tasks_pkg.compaction import _get_context_limit
            _prior_limit = _get_context_limit(task)
            _expand_info = learn_expand_from_success(
                task.get('provider_id') or '',
                model,
                _total_prompt_tokens,
                preset_limit=_prior_limit,
            )
            if _expand_info:
                append_event(task, build_event(
                    EventType.PHASE,
                    phase='retrying',
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

    return msg, finish_reason, usage
