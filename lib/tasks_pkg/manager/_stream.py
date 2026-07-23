"""LLM streaming — ``stream_llm_response`` wires ``dispatch_stream`` deltas into
the task's event system, with periodic crash-recovery checkpoints, TTFT timing,
retry/waiting-model phases, and usage/context-limit auto-learning.

Also ``_display_model_name`` — strips internal gateway/provider prefixes for a
user-facing label.
"""

import time

from lib.agent_core.events import EventType, build_event
from lib.llm_dispatch import dispatch_stream
from lib.log import get_logger

from lib.tasks_pkg.manager._events import append_event
from lib.tasks_pkg.manager._sync import checkpoint_task_partial

logger = get_logger(__name__)


# Gateway/provider routing prefixes that are an internal dispatch detail, not
# something the user picked. Mirrors the canonical list in
# lib/llm_dispatch/discovery.py so the user-facing model name (e.g.
# "claude-opus-4.8") never leaks "aws.claude-opus-4.8" into the UI.
_GATEWAY_PREFIXES = ('aws.', 'vertex.', 'gcp.', 'azure.', 'bedrock.')


def _display_model_name(model: str) -> str:
    """Strip internal gateway/provider prefixes for a user-facing label."""
    name = model or 'the model'
    for prefix in _GATEWAY_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name
# ── Streaming checkpoint interval (seconds) ──
# During LLM token streaming, we periodically persist partial content to
# the DB so data survives server crashes even when there are no tool rounds.
_STREAM_CHECKPOINT_INTERVAL = 5

def stream_llm_response(task, body, tag='', on_tool_call_ready=None):
    """Stream an LLM response, wiring deltas into the task's event system.

    Delegates all key selection, retry, 429/401/403 failover to the
    central ``dispatch_stream`` — no duplicate logic needed here.

    Args:
        on_tool_call_ready: callback(tool_call_dict) — fired as each tool
            call's arguments finish streaming.  The orchestrator uses this
            to start executing read-only tools while the model is still
            generating the next tool call (streaming tool execution).

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

    def _on_retry(attempt, reason='', status_code=0):
        """Emit SSE phase event so user sees retry status instead of 'Waiting…'.

        We attach the MODEL name and current cycle count so a long wait
        reveals exactly which key/model is being throttled instead of a
        generic spinner.  Previously users just saw "Waiting…" for 60-120s
        during 429 cycling with no indication that the server was alive
        and actively retrying.
        """
        if status_code == 429:
            # Rate-limit: surface the model clearly and phrase it as a
            # queue wait rather than an error.
            detail = (f'⏳ 模型 {model} 限流中，正在排队重试 '
                      f'(第 {attempt} 次)…')
        elif reason:
            detail = f'Retrying… {reason} ({model}, attempt {attempt})'
        else:
            detail = f'Retrying {model}… (attempt {attempt})'
        append_event(task, build_event(
            EventType.PHASE,
            phase='retrying',
            detail=detail,
            attempt=attempt,
            statusCode=status_code,
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
        model=model))

    # Resolve dispatch_stream THROUGH the package facade at call time so a test's
    # ``monkeypatch.setattr(lib.tasks_pkg.manager, 'dispatch_stream', …)`` steers
    # this stream exactly as it did on the pre-split single module (which imported
    # dispatch_stream at module top-level, making it patchable on `manager`).
    import lib.tasks_pkg.manager as _mgr_facade
    _dispatch_stream = getattr(_mgr_facade, 'dispatch_stream', dispatch_stream)
    msg, finish_reason, usage = _dispatch_stream(
        body,
        on_thinking=_on_thinking,
        on_content=_on_content,
        on_tool_call_ready=on_tool_call_ready,
        abort_check=lambda: task.get('aborted', False),
        prefer_model=model,
        log_prefix=pfx,
        # ★ User-facing request: the user explicitly chose this model in
        #   the frontend preset selector.  429 retries must stay within
        #   this model's slots (different keys / alias group) — never
        #   silently fall back to a cheaper/different model.
        strict_model=True,
        on_retry=_on_retry,
        avoid_pairs=_avoid_pairs,
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
    try:
        from lib.tasks_pkg import floor_retry as _fr
        _conv_for_fr = task.get('convId', '') or ''
        if (_fr.floor_retry_enabled() and _conv_for_fr
                and _fr.is_floor_collapse(usage)
                and _fr.wire_prefix_stable(_conv_for_fr, usage)):
            _fr_max = _fr.floor_retry_max()
            for _fr_i in range(_fr_max):
                if task.get('aborted', False):
                    break
                logger.warning(
                    '%s conv=%s [FloorRetry] byte-stable floor-collapse '
                    '(read=%s write=%s) — resending identical body (%d/%d)',
                    pfx, _conv_for_fr,
                    (usage or {}).get('cache_read_tokens'),
                    (usage or {}).get('cache_creation_input_tokens'),
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
                        abort_check=lambda: task.get('aborted', False),
                        prefer_model=model, log_prefix=f'{pfx}[floor-retry{_fr_i+1}]',
                        strict_model=True, on_retry=_on_retry,
                        avoid_pairs=_avoid_pairs)
                except Exception as _rerr:
                    # 503/throttle/transient — do NOT keep piling resends on an
                    # already-throttled gateway; that only deepens the throttle.
                    logger.warning('%s [FloorRetry] resend %d errored, stopping: '
                                   '%s: %s', pfx, _fr_i + 1,
                                   type(_rerr).__name__, str(_rerr)[:120])
                    break
                if not _fr.is_floor_collapse(_rusage):
                    # Recovered: the resend hit the now-visible cache write.
                    # Adopt its response + usage (a genuine cache read, cheaper
                    # AND the same conversation content — the body was identical).
                    logger.warning('%s conv=%s [FloorRetry] RECOVERED on resend %d '
                                   '(read=%s write=%s)', pfx, _conv_for_fr, _fr_i + 1,
                                   (_rusage or {}).get('cache_read_tokens'),
                                   (_rusage or {}).get('cache_creation_input_tokens'))
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
            task['content'] = msg.get('content') or ''
            task['thinking'] = msg.get('reasoning_content') or ''
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
            _prompt_tokens = int(
                usage.get('prompt_tokens')
                or usage.get('input_tokens')
                or 0
            )
            # Anthropic excludes cache from input_tokens; add it back so
            # _total_prompt_tokens reflects the FULL prompt the provider
            # accepted (which is what we use for context-limit expansion).
            _cw = int(usage.get('cache_creation_input_tokens') or 0)
            _cr = int(usage.get('cache_read_input_tokens') or 0)
            if (_cw or _cr) and _prompt_tokens <= (_cw + _cr):
                _total_prompt_tokens = _prompt_tokens + _cw + _cr
            else:
                _total_prompt_tokens = _prompt_tokens
        if conv_id and _prompt_tokens > 0:
            from lib.token_counter import record_usage
            # ``body['messages']`` is the exact list we sent. Recording it
            # lets the cache detect edit/regenerate (prefix changed →
            # invalidate) vs append-only (reuse + delta).
            record_usage(
                conv_id,
                prompt_tokens=_prompt_tokens,
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
