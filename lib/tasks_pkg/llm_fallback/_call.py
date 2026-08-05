"""Core LLM-call-with-fallback entry point.

Streams one LLM round and transparently retries with the configured
fallback model (Claude Opus 4, medium preset) when the primary model
errors out; also drives reactive compaction on ``PromptTooLongError``.

Collaborators that tests may monkeypatch (``stream_llm_response``,
``_get_fallback_model``, ``_flag_empty_stop_for_retry``, ``_emit_round_usage``)
are resolved through the package facade at CALL TIME, so a
``patch('lib.tasks_pkg.llm_fallback.<name>')`` is honoured here.

The reactive-compaction retry state (``_reactive_compact_attempts`` /
``_REACTIVE_COMPACT_MAX_RETRIES``) is imported BY REFERENCE from
``._state`` — there is exactly one such dict in the process.
"""

from lib.llm import build_body
from lib.llm_error_format import format_llm_error_for_user
from lib.llm_errors import _ERR_BODY_LIMIT
from lib.log import audit_log, get_logger

# Shared reactive-compaction state — imported by reference (never reassigned)
# so cleanup_reactive_compact_state mutates the SAME dict this module reads.
from lib.tasks_pkg.llm_fallback._state import (
    _reactive_compact_attempts,
    _REACTIVE_COMPACT_MAX_RETRIES,
)

logger = get_logger(__name__)


def _facade():
    """Return the package facade so collaborators resolve at call time.

    Lets ``patch('lib.tasks_pkg.llm_fallback.<name>')`` in tests take effect
    even though the core loop calls the helper by a bare name below.
    """
    import lib.tasks_pkg.llm_fallback as _pkg
    return _pkg


def stream_llm_response(*args, **kwargs):
    """Facade-resolved shim for the streaming primitive (patchable at package level)."""
    return _facade().stream_llm_response(*args, **kwargs)


def append_event(*args, **kwargs):
    """Facade-resolved shim for SSE delivery (patchable at package level)."""
    return _facade().append_event(*args, **kwargs)


def _get_fallback_model(*args, **kwargs):
    """Facade-resolved shim (patchable at package level)."""
    return _facade()._get_fallback_model(*args, **kwargs)


def _flag_empty_stop_for_retry(*args, **kwargs):
    """Facade-resolved shim (patchable at package level)."""
    return _facade()._flag_empty_stop_for_retry(*args, **kwargs)


def _emit_round_usage(*args, **kwargs):
    """Facade-resolved shim (patchable at package level)."""
    return _facade()._emit_round_usage(*args, **kwargs)


def _attempt_pool_rescue(task, body, round_num, max_tokens, tool_list,
                         max_tool_rounds, accumulated_usage, api_rounds,
                         *, failed_models, original_model, cause_exc,
                         preset, thinking_enabled, on_tool_call_ready=None):
    """Last-resort pool-wide dispatch before a turn is allowed to die.

    Owner directive 2026-08-03: an error envelope may surface ONLY when every
    key in the pool is unavailable. The fallback chain (primary → configured
    fallback model) covers two models; when both fail — e.g. the fallback
    model's only key got a durable 401 while the rest of the pool is healthy
    (task 7ea8c25f, kimi-k3 pinned to one key_access cell whose AppId the
    vendor rejected) — the turn used to die with a "check your API keys"
    envelope even though other models could have completed it. This helper
    makes ONE more dispatch attempt across every remaining (key, model)
    slot: ``pool_wide=True`` (non-strict, no preferred model) and
    ``exclude_models`` skips the models already proven dead in this chain,
    so the rescue cannot re-enter their failure / 429 wall.

    Returns the standard OK result dict on rescue success, else ``None``
    (the caller then surfaces the original error envelope unchanged).
    """
    tid = task['id'][:8]
    failed_models = {m for m in (failed_models or set()) if m}

    # ── Gate: is there anything healthy BEYOND the failed models? ──
    # A probe failure must not suppress the rescue attempt (the attempt is
    # bounded by dispatch_stream's own max_retries either way).
    try:
        from lib.llm_dispatch.factory import get_dispatcher
        _disp = get_dispatcher()
        _has = _disp.has_capable_slots('text', exclude_models=failed_models)
    except Exception as _ge:
        logger.debug('[%s] pool-rescue gate probe failed: %s', tid, _ge)
        _has = True
    if not _has:
        logger.warning('[%s] pool-rescue skipped — no capable slot beyond '
                       'failed models %s', tid, sorted(failed_models))
        return None

    # Classify the cause so the badge names WHY the rescue fired (the same
    # typed kind the normal fallback badge uses — 'permission', …).
    try:
        from lib.error_envelope import from_exception as _from_exc
        _rk_env = _from_exc(cause_exc, model=original_model,
                            context='pool-rescue', source='llm-stream')
        _rk_kind = _rk_env.get('kind', 'generic')
        _rk_detail = (_rk_env.get('detail') or str(cause_exc)).strip()
    except Exception as _ce:
        logger.debug('[%s] pool-rescue cause classify failed: %s', tid, _ce)
        _rk_kind, _rk_detail = 'generic', str(cause_exc)[:200]

    append_event(task, {
        'type': 'phase',
        'phase': 'retrying',
        'detail': (f'⚠️ {original_model} 及其回退模型均不可用（{_rk_kind}）— '
                   f'正在尝试池中其它可用模型…'),
    })

    _tools_this = tool_list if (tool_list and round_num < max_tool_rounds) else None
    _rescue_body = dict(body)
    if _tools_this is not None:
        _rescue_body['tools'] = _tools_this
    # Same session-stable TTL latch rule as every rebuild path.
    _rescue_body['_task_id'] = task.get('id', '')

    try:
        assistant_msg, finish_reason, usage = stream_llm_response(
            task, _rescue_body, tag=f'R{round_num+1}-RESCUE',
            on_tool_call_ready=on_tool_call_ready,
            pool_wide=True, exclude_models=failed_models)
    except Exception as e3:
        logger.warning('[%s] pool-rescue dispatch also failed: %s', tid, e3)
        return None

    _rescue_model = ((usage or {}).get('_dispatch') or {}).get('model') \
        or _rescue_body.get('model') or original_model

    if usage is not None and _flag_empty_stop_for_retry(
            assistant_msg, finish_reason, task, round_num, usage):
        logger.warning('[%s] ⚠️ Round-0 EMPTY STOP (pool-rescue model=%s) — '
                       'flagging for empty_stop retry', tid, _rescue_model)

    # Badge: keep the ORIGINAL primary as _fallback_from when an earlier hop
    # already recorded it (the rescue is the tail of the same chain).
    if not task.get('_fallback_from'):
        task['_fallback_from'] = original_model
    task['_fallback_model'] = _rescue_model
    task['_fallback_reason'] = (
        f'{_rk_kind}: {_rk_detail}' if _rk_detail else _rk_kind)[:300]
    task['_fallback_kind'] = _rk_kind

    # Honest accounting — identical to the primary/fallback success paths.
    if usage:
        for k, v in usage.items():
            if isinstance(v, (int, float)):
                accumulated_usage[k] = accumulated_usage.get(k, 0) + v
        api_rounds.append({'round': round_num + 1, 'model': _rescue_model,
                           'usage': dict(usage), 'tag': f'R{round_num+1}-RESCUE'})
        _emit_round_usage(task, round_num + 1, _rescue_model, usage,
                          tag=f'R{round_num+1}-RESCUE')
        for _bill in (usage.get('_extra_billing_rounds') or []):
            _bu = _bill.get('usage') or {}
            for k, v in _bu.items():
                if isinstance(v, (int, float)):
                    accumulated_usage[k] = accumulated_usage.get(k, 0) + v
            api_rounds.append({
                'round': round_num + 1,
                'model': _bill.get('model') or _rescue_model,
                'usage': dict(_bu),
                'tag': _bill.get('tag') or f'R{round_num+1}-RESCUE-DISCARDED',
            })
            _emit_round_usage(task, round_num + 1,
                              _bill.get('model') or _rescue_model, _bu,
                              tag=_bill.get('tag') or f'R{round_num+1}-RESCUE-DISCARDED')

    audit_log('model_fallback', old=original_model, new=_rescue_model,
              reason=f'pool-rescue: {_rk_kind}: {_rk_detail[:160]}',
              kind=_rk_kind, tid=tid, conv=task.get('convId', ''))
    logger.warning('[%s] ✓ POOL-RESCUE round %d OK: finish_reason=%s '
                   'model=%s (rescued from %s; failed chain: %s)',
                   tid, round_num + 1, finish_reason, _rescue_model,
                   original_model, sorted(failed_models))

    return {
        'assistant_msg': assistant_msg,
        'finish_reason': finish_reason,
        'usage': usage,
        'model': _rescue_model,
        'preset': preset,
        'thinking_enabled': thinking_enabled,
        '_loop_action': None,
        '_loop_exit_reason': None,
    }


def _llm_call_with_fallback(task, body, model, round_num, max_tokens,
                             tool_call_happened, tool_list, max_tool_rounds,
                             messages, preset, thinking_enabled,
                             accumulated_usage, api_rounds,
                             on_tool_call_ready=None):
    """Make an LLM call with automatic fallback to Opus on failure.

    Streams the LLM response for the current round.  If the primary model
    fails, transparently falls back to Claude Opus 4 (medium preset) and
    retries once.  Detects content-filter blocks (empty first-round
    responses) and output-token truncation, logging at appropriate levels.

    Parameters
    ----------
    task : dict
        Live task dict — mutated in-place (content, _fallback_model, etc.).
    body : dict
        Pre-built request body for the primary LLM call.
    model : str
        Current model identifier.
    round_num : int
        Zero-based loop iteration index.
    max_tokens : int
        Max output tokens (for truncation logging).
    tool_call_happened : bool
        Whether any tool call executed in prior rounds.
    tool_list : list | None
        Tool definitions list (needed if fallback must rebuild body).
    max_tool_rounds : int
        Max tool round ceiling.
    messages : list
        Conversation messages (needed if fallback rebuilds body).
    preset : str
        Current preset name.
    thinking_enabled : bool
        Whether extended thinking is active.
    accumulated_usage : dict
        Mutable usage accumulator — updated in-place.
    api_rounds : list
        Mutable per-round usage list — appended in-place.

    Returns
    -------
    dict with keys:
        assistant_msg    – The parsed assistant message dict.
        finish_reason    – Finish reason string from the API.
        usage            – Raw usage dict from the response (or None).
        model            – Model actually used (may differ if fallback fired).
        preset           – Preset actually used.
        thinking_enabled – Thinking flag actually used.
        _loop_action     – 'break' if caller must break the loop, else None.
        _loop_exit_reason – Set when _loop_action == 'break'.

    Raises
    ------
    Exception
        Re-raised when both primary and fallback models fail and no prior
        tool calls exist (unrecoverable first-round error).
    lib.llm.AbortedError
        Never caught — propagates directly to signal user abort.
    """
    tid = task['id'][:8]
    _FALLBACK_MODEL = _get_fallback_model(task)
    # Distinguish "admin never configured a fallback" from "this request
    # explicitly opted out" so the surfaced error envelope names the
    # actual cause (context='fallback-disabled') instead of an opaque
    # 'no-fallback'.  Headless callers who set disableModelFallback need
    # this to understand why a transient primary error wasn't masked.
    _fb_disabled_by_request = False
    try:
        _fb_disabled_by_request = bool((task.get('config') or {}).get('disableModelFallback'))
    except Exception as _e:
        logger.debug('[%s] disableModelFallback flag read failed: %s', tid, _e)
    _no_fb_context = ('fallback-disabled' if _fb_disabled_by_request
                      else 'no-fallback')

    # ── Primary model call ──
    try:
        assistant_msg, finish_reason, usage = stream_llm_response(
            task, body, tag=f'R{round_num+1}',
            on_tool_call_ready=on_tool_call_ready)
        last_finish_reason = finish_reason

        # Round-0 empty stop → flag for the empty_stop/zero_byte RETRY bucket,
        # NOT a terminal content_filter. A genuine policy block is HTTP 450
        # (ContentFilterError, handled below and terminal); a plain empty stop
        # is a transient gateway artifact (proven by debug/repro_conv_empty_stop.py).
        # When the stream layer already flagged an anomaly, analyse_stream_result
        # retries it unchanged; when it slipped through unflagged (whitespace-only
        # body, or a zero-chunk clean [DONE]), the helper sets the flags so the
        # retry bucket still fires. Only after retries are exhausted does it
        # surface as abnormal_stop.
        if usage is not None and _flag_empty_stop_for_retry(
                assistant_msg, finish_reason, task, round_num, usage):
            logger.warning('[%s] ⚠️ Round-0 EMPTY STOP (model=%s) — flagging for '
                           'empty_stop retry (NOT content_filter; a real policy '
                           'block would be HTTP 450). Will retry then surface as '
                           'abnormal_stop if it persists.', tid, model)

        # Log output-token truncation so operators can tune max_tokens
        if finish_reason in ('length', 'max_tokens'):
            _trunc_content_len = len(assistant_msg.get('content', ''))
            _trunc_tool_calls = len(assistant_msg.get('tool_calls', []))
            _u_trace = (usage or {}).get('trace_id', 'N/A')
            _u_elapsed = (usage or {}).get('stream_elapsed_ms', 0)
            logger.warning('[%s] ⚠️ TRUNCATED at round %d: finish_reason=%s '
                           'content=%dchars tool_calls=%d model=%s max_tokens=%s '
                           'M-TraceId=%s elapsed=%.1fs — '
                           'output token limit reached',
                           tid, round_num, finish_reason, _trunc_content_len,
                           _trunc_tool_calls, model, max_tokens,
                           _u_trace, _u_elapsed / 1000)

        if usage:
            for k, v in usage.items():
                if isinstance(v, (int, float)):
                    accumulated_usage[k] = accumulated_usage.get(k, 0) + v
            api_rounds.append({'round': round_num + 1, 'model': model,
                               'usage': dict(usage), 'tag': f'R{round_num+1}'})
            _emit_round_usage(task, round_num + 1, model, usage, tag=f'R{round_num+1}')
            # ★ HONEST ACCOUNTING: bill every DISCARDED FloorRetry attempt the
            #   gateway processed. Each was a real request the provider charged
            #   for — hiding them made cost popover / wallet debit / daily
            #   report under-report by ~9%~50% per triggered round. See
            #   lib.tasks_pkg.floor_retry docstring for the full story.
            _extra = (usage or {}).get('_extra_billing_rounds') or []
            for _bill in _extra:
                _bill_usage = _bill.get('usage') or {}
                for k, v in _bill_usage.items():
                    if isinstance(v, (int, float)):
                        accumulated_usage[k] = accumulated_usage.get(k, 0) + v
                api_rounds.append({
                    'round': round_num + 1,
                    'model': _bill.get('model') or model,
                    'usage': dict(_bill_usage),
                    'tag': _bill.get('tag') or f'R{round_num+1}-DISCARDED',
                })
                _emit_round_usage(task, round_num + 1,
                                  _bill.get('model') or model,
                                  _bill_usage,
                                  tag=_bill.get('tag') or f'R{round_num+1}-DISCARDED')
            if _extra:
                logger.warning('[%s] conv=%s billed %d discarded FloorRetry '
                               'attempt(s) into api_rounds/accumulated_usage — '
                               'cost popover now matches gateway bill',
                               tid, task.get('convId', ''), len(_extra))

        _content_len = len(assistant_msg.get('content', '') or '')
        _tool_calls = len(assistant_msg.get('tool_calls', []))
        _u_trace = (usage or {}).get('trace_id', 'N/A')
        _u_elapsed = (usage or {}).get('stream_elapsed_ms', 0)
        logger.info('[%s] conv=%s ✓ LLM round %d OK: finish_reason=%s model=%s '
                    'content=%dchars tool_calls=%d M-TraceId=%s elapsed=%.1fs',
                    tid, task.get('convId', ''), round_num + 1, last_finish_reason, model,
                    _content_len, _tool_calls, _u_trace, _u_elapsed / 1000)

        return {
            'assistant_msg': assistant_msg,
            'finish_reason': last_finish_reason,
            'usage': usage,
            'model': model,
            'preset': preset,
            'thinking_enabled': thinking_enabled,
            '_loop_action': None,
            '_loop_exit_reason': None,
        }

    except Exception as e:
        # AbortedError must escape — never fallback/retry on user abort
        from lib.llm import AbortedError, ContentFilterError, PromptTooLongError
        if isinstance(e, AbortedError):
            logger.debug('[%s] ✋ AbortedError at round %d — stopping immediately', tid, round_num)
            raise

        # ── PromptTooLongError → reactive compaction + retry ──
        # Inspired by Claude Code's reactive compact: when the API rejects
        # with "prompt too long", compress the conversation and retry.
        if isinstance(e, PromptTooLongError):
            _task_id = task.get('id', '')
            _attempts = _reactive_compact_attempts.get(_task_id, 0)

            # ── Auto-learn a SHRUNK context limit for this (provider, model)
            # ── before we compact. The next call will use the corrected
            # ── ceiling, so future overflows on this provider become rarer.
            try:
                from lib.context_limits import learn_shrink_from_error
                from lib.tasks_pkg.compaction import (
                    _get_context_limit,
                    _parse_context_overflow,
                )
                _reported, _stated_max = _parse_context_overflow(str(e))
                _prior_limit = _get_context_limit(task)
                _learned_info = learn_shrink_from_error(
                    task.get('provider_id') or '',
                    model,
                    _reported,
                    preset_limit=_prior_limit,
                    stated_max=_stated_max,
                )
                if _learned_info:
                    append_event(task, {
                        'type': 'phase',
                        'phase': 'retrying',
                        'detail': (
                            f'⚙️ Auto-detected smaller context window for '
                            f'{model}: '
                            f'{_learned_info["new_limit"]:,} tokens '
                            f'(was {_learned_info["old_limit"]:,})'
                        ),
                    })
            except Exception as _learn_e:
                logger.debug('[%s] context_limits shrink-learn failed: %s',
                             tid, _learn_e)

            if _attempts < _REACTIVE_COMPACT_MAX_RETRIES:
                _reactive_compact_attempts[_task_id] = _attempts + 1
                logger.warning(
                    '[%s] ⚡ REACTIVE COMPACT triggered at round %d (attempt %d/%d): '
                    'prompt too long for model=%s — compressing and retrying',
                    tid, round_num, _attempts + 1, _REACTIVE_COMPACT_MAX_RETRIES, model)

                from lib.tasks_pkg.compaction import reactive_compact
                # Pass the raw error text so reactive_compact can extract
                # the upstream-reported token count ("N tokens > M maximum")
                # and use it as the authoritative seed for head-truncate
                # sizing. Without this we fall back to our under-counting
                # heuristic and shed ~5 % when we actually need to shed ~30 %.
                reactive_compact(messages, task=task, error_text=str(e))

                # Rebuild body with compressed messages
                _tools_this_round = tool_list if (tool_list and round_num < max_tool_rounds) else None
                body = build_body(
                    model, messages,
                    max_tokens=task.get('config', {}).get('maxTokens', 128000),
                    temperature=body.get('temperature', 1.0),
                    thinking_enabled=thinking_enabled,
                    preset=preset,
                    tools=_tools_this_round,
                    response_format=body.get('response_format'),
                    stream=True,
                )
                # ★ Preserve the session-stable TTL latch key. Without
                #   _task_id, add_cache_breakpoints / the extended-cache-ttl
                #   beta header fall back to the LIVE global CACHE_EXTENDED_TTL
                #   instead of the per-task latch — flipping the cache key
                #   mid-task and forcing a full prefix re-write (cache_read=0).
                body['_task_id'] = task.get('id', '')

                # Notify frontend (phase event = transient UI status,
                # does NOT pollute assistantMsg.content)
                append_event(task, {
                    'type': 'phase',
                    'phase': 'retrying',
                    'detail': f'⚡ 上下文超长，已自动压缩 (reactive compact {_attempts + 1}/{_REACTIVE_COMPACT_MAX_RETRIES})…',
                    'detailKey': 'stream.phase.reactiveCompact',
                    'detailArgs': {
                        'attempt': _attempts + 1,
                        'max': _REACTIVE_COMPACT_MAX_RETRIES,
                    },
                })

                # Retry the LLM call with compacted messages
                try:
                    assistant_msg, finish_reason, usage = stream_llm_response(
                        task, body, tag=f'R{round_num+1}-REACTIVE')
                    if usage:
                        for k, v in usage.items():
                            if isinstance(v, (int, float)):
                                accumulated_usage[k] = accumulated_usage.get(k, 0) + v
                        api_rounds.append({'round': round_num + 1, 'model': model,
                                           'usage': dict(usage), 'tag': f'R{round_num+1}-REACTIVE'})
                        _emit_round_usage(task, round_num + 1, model, usage,
                                           tag=f'R{round_num+1}-REACTIVE')
                        # Honest accounting (same as primary path)
                        for _bill in (usage.get('_extra_billing_rounds') or []):
                            _bu = _bill.get('usage') or {}
                            for k, v in _bu.items():
                                if isinstance(v, (int, float)):
                                    accumulated_usage[k] = accumulated_usage.get(k, 0) + v
                            api_rounds.append({
                                'round': round_num + 1,
                                'model': _bill.get('model') or model,
                                'usage': dict(_bu),
                                'tag': _bill.get('tag') or f'R{round_num+1}-REACTIVE-DISCARDED',
                            })
                            _emit_round_usage(task, round_num + 1,
                                              _bill.get('model') or model,
                                              _bu,
                                              tag=_bill.get('tag') or
                                              f'R{round_num+1}-REACTIVE-DISCARDED')
                    return {
                        'assistant_msg': assistant_msg,
                        'finish_reason': finish_reason,
                        'usage': usage,
                        'model': model,
                        'preset': preset,
                        'thinking_enabled': thinking_enabled,
                        '_loop_action': None,
                        '_loop_exit_reason': None,
                    }
                except Exception as e2:
                    logger.error('[%s] Reactive compact retry also failed: %s', tid, e2, exc_info=True)
                    # Fall through to normal fallback handling
            else:
                logger.error('[%s] Reactive compact retries exhausted (%d/%d) — '
                             'falling through to model fallback',
                             tid, _attempts, _REACTIVE_COMPACT_MAX_RETRIES)

        # InvalidImageError — image content rejected (too large, corrupt, etc.)
        # Fallback to another model won't help (same image = same rejection).
        from lib.llm import InvalidImageError
        if isinstance(e, InvalidImageError):
            err_str = str(e)[:300]
            logger.warning('[%s] 🖼️ INVALID_IMAGE at round %d model=%s: %s',
                           tid, round_num, model, err_str)
            from lib.error_envelope import make_envelope as _make_env
            if 'many-image' in err_str.lower():
                _hint_cn = '过多大图。同时发送 5 张以上图片时，每张需小于 2000×2000像素。请压缩或删除部分图片。'
                _hint_en = ('Too many large images. When sending 5+ images, each must be '
                            'under 2000×2000 pixels. Please resize or remove some images.')
                _hint_key = 'err.k.invalid_image.hintMany'
            else:
                _hint_cn = '会话中某张图片超过了 API 大小限制。请使用更小的图片或删除过大的图片。'
                _hint_en = ('One or more images in this conversation exceed the API size '
                            'limit. Please use a smaller image or remove the oversized image.')
                _hint_key = 'err.k.invalid_image.hintSize'
            envelope = _make_env(
                'invalid_image',
                # Legacy bilingual hint stays byte-identical for headless
                # clients; the keyed variant lets the frontend localize.
                hint=f'解决办法 / How to fix:\n• {_hint_cn}\n\n• {_hint_en}',
                hint_key=_hint_key,
                detail=err_str,
                model=model,
                context=f'round-{round_num}',
                source='llm-stream',
                raw=str(e),
            )
            task['error'] = envelope
            return {
                'assistant_msg': {'role': 'assistant', 'content': ''},
                'finish_reason': 'error',
                'usage': None,
                'model': model,
                'preset': preset,
                'thinking_enabled': thinking_enabled,
                '_loop_action': 'break',
                '_loop_exit_reason': f'invalid_image_round_{round_num}',
            }

        # ContentFilterError (HTTP 450) — content policy violation.
        # Fallback to another model won't help (same content = same filter).
        # Return content_filter finish_reason so orchestrator shows the right message.
        if isinstance(e, ContentFilterError):
            err_str = str(e)[:_ERR_BODY_LIMIT]
            logger.warning('[%s] 🚫 CONTENT_FILTER (HTTP 450) at round %d model=%s: %s',
                           tid, round_num, model, err_str, exc_info=True)
            return {
                'assistant_msg': {'role': 'assistant', 'content': ''},
                'finish_reason': 'content_filter',
                'usage': None,
                'model': model,
                'preset': preset,
                'thinking_enabled': thinking_enabled,
                '_loop_action': 'break',
                '_loop_exit_reason': f'content_filter_http450_round_{round_num}',
            }

        original_model = model
        err_str = str(e)[:_ERR_BODY_LIMIT]
        logger.error('[%s] conv=%s LLM call failed at round %d (model=%s): %s '
                     '(check M-TraceId in preceding debug logs for gateway coordination)',
                     tid, task.get('convId', ''), round_num + 1, model, err_str, exc_info=True)

        # If already on the fallback model, or no fallback configured —
        # try the pool-wide last resort before giving up (owner directive
        # 2026-08-03: an error surfaces ONLY when every key is unavailable).
        # An explicit per-request opt-out (disableModelFallback) skips it.
        if not _FALLBACK_MODEL or model == _FALLBACK_MODEL:
            if not _fb_disabled_by_request:
                _rescue = _attempt_pool_rescue(
                    task, body, round_num, max_tokens, tool_list,
                    max_tool_rounds, accumulated_usage, api_rounds,
                    failed_models={model}, original_model=original_model,
                    cause_exc=e, preset=preset,
                    thinking_enabled=thinking_enabled,
                    on_tool_call_ready=on_tool_call_ready)
                if _rescue is not None:
                    return _rescue
            if tool_call_happened:
                _user_err = format_llm_error_for_user(
                    e, model=model,
                    context=(_no_fb_context if not _FALLBACK_MODEL else 'on-fallback-model'),
                    source='llm-stream')
                task['error'] = _user_err
                logger.warning('[%s] 🛑 Fallback model error with prior tool calls — giving up: %s',
                               tid, err_str, exc_info=True)
                # ``content`` must be a string — the typed envelope is
                # carried separately on task['error'] / done.error.  The
                # assistant bubble shows the empty string; the frontend
                # renders the error envelope as a typed error block.
                return {
                    'assistant_msg': {'role': 'assistant', 'content': ''},
                    'finish_reason': 'error', 'usage': None,
                    'model': model, 'preset': preset, 'thinking_enabled': thinking_enabled,
                    '_loop_action': 'break',
                    '_loop_exit_reason': f'opus_error_with_tool_calls_round_{round_num}',
                }
            # No fallback / already on fallback and no prior tool calls —
            # stash the typed envelope on the exception so the top-level
            # FATAL handler in orchestrator can surface actionable text
            # without losing the exception type (subclasses may have
            # non-trivial __init__ signatures).
            try:
                e._user_message = format_llm_error_for_user(  # type: ignore[attr-defined]
                    e, model=model,
                    context=(_no_fb_context if not _FALLBACK_MODEL else 'on-fallback-model'),
                    source='llm-stream')
            except Exception as _attr_err:
                logger.debug('[%s] Could not attach _user_message: %s', tid, _attr_err)
            raise

        # ── Fallback: switch to configured fallback model ──
        # Build a short, typed reason string from the original exception so
        # the UI can show *why* the fallback fired (kind + detail) instead
        # of an opaque "primary failed" message.
        from lib.error_envelope import from_exception as _from_exc
        _fb_envelope = _from_exc(
            e, model=original_model,
            context='fallback-trigger', source='llm-stream')
        _fb_kind = _fb_envelope.get('kind', 'generic')
        _fb_detail = (_fb_envelope.get('detail') or err_str).strip()
        _fb_reason = f'{_fb_kind}: {_fb_detail}' if _fb_detail else _fb_kind

        # Notify via phase event (transient UI status, does NOT pollute
        # assistantMsg.content).  The done event already carries
        # fallbackModel / fallbackFrom / fallbackReason for the persistent badge.
        append_event(task, {
            'type': 'phase',
            'phase': 'retrying',
            'detail': (f'⚠️ 模型 {original_model} 请求失败（{_fb_kind}）：'
                       f'{_fb_detail[:120]} — 已自动回退到 {_FALLBACK_MODEL} 继续生成…'),
        })
        # ★ EARLY notification, at the DECISION MOMENT — before the fallback
        #   stream starts. A fallback generation can run for minutes; the
        #   transient phase line is cleared the moment fallback content
        #   starts streaming, so without a STRUCTURED event + early task
        #   stamps the user has no indication the model changed for the
        #   whole generation, and a cold reload mid-fallback cannot repaint
        #   the banner (build_fresh_state_snapshot reads these fields).
        #   Cleared again below if the fallback itself fails.
        append_event(task, {
            'type': 'model_fallback',
            'fallbackModel': _FALLBACK_MODEL,
            'fallbackFrom': original_model,
            'fallbackKind': _fb_kind,
            'fallbackReason': _fb_reason[:300],
        })
        task['_fallback_model'] = _FALLBACK_MODEL
        task['_fallback_from'] = original_model
        task['_fallback_reason'] = _fb_reason[:300]
        task['_fallback_kind'] = _fb_kind
        # A model fallback is a significant state change — record it in the
        # audit trail so the optimizer/operator can see WHICH model failed,
        # how often, and why (the analyzer already mines 'model_fallback').
        # The fallback itself is self-recovering, so the log line is WARNING
        # WITHOUT a traceback (the originating error was already logged with
        # exc_info just above); a traceback here would imply an unhandled bug.
        audit_log('model_fallback', old=original_model, new=_FALLBACK_MODEL,
                  reason=_fb_reason[:200], kind=_fb_kind, tid=tid,
                  conv=task.get('convId', ''))
        logger.warning('[%s] Model fallback: %s → %s (reason: %s)',
                       tid, original_model, _FALLBACK_MODEL,
                       _fb_reason[:200])

        fallback_body = build_body(
            _FALLBACK_MODEL, messages,
            max_tokens=max_tokens,
            temperature=1.0,
            thinking_enabled=True,
            preset='opus',
            thinking_depth='medium',
            tools=tool_list if (tool_list and round_num < max_tool_rounds) else None,
            response_format=body.get('response_format'),
            stream=True,
        )
        # ★ Preserve the session-stable TTL latch key on the fallback body
        #   too (see reactive-compact rebuild above). The fallback model is a
        #   different cache namespace anyway, but a stable TTL decision keeps
        #   the fallback model's OWN prefix reusable across its rounds.
        fallback_body['_task_id'] = task.get('id', '')

        try:
            assistant_msg, finish_reason, usage = stream_llm_response(
                task, fallback_body, tag=f'R{round_num+1}-FALLBACK')
            last_finish_reason = finish_reason

            if usage is not None and _flag_empty_stop_for_retry(
                    assistant_msg, finish_reason, task, round_num, usage):
                logger.warning('[%s] ⚠️ Round-0 EMPTY STOP (fallback model=%s) — '
                               'flagging for empty_stop retry (NOT content_filter). '
                               'Will surface as abnormal_stop if it persists.',
                               tid, _FALLBACK_MODEL)

            if finish_reason in ('length', 'max_tokens'):
                _fb_trace = (usage or {}).get('trace_id', 'N/A')
                _fb_elapsed = (usage or {}).get('stream_elapsed_ms', 0)
                logger.warning('[%s] ⚠️ TRUNCATED at round %d (fallback model=%s): '
                               'finish_reason=%s M-TraceId=%s elapsed=%.1fs — '
                               'output token limit reached',
                               tid, round_num, _FALLBACK_MODEL, finish_reason,
                               _fb_trace, _fb_elapsed / 1000)

            # (fallback fields were stamped at the DECISION moment, before
            # the stream started — see above; nothing to re-stamp here.)
            if usage:
                for k, v in usage.items():
                    if isinstance(v, (int, float)):
                        accumulated_usage[k] = accumulated_usage.get(k, 0) + v
                api_rounds.append({'round': round_num + 1, 'model': _FALLBACK_MODEL,
                                   'usage': dict(usage), 'tag': f'R{round_num+1}-FALLBACK'})
                _emit_round_usage(task, round_num + 1, _FALLBACK_MODEL, usage,
                                   tag=f'R{round_num+1}-FALLBACK')
                # Honest accounting (same as primary path)
                for _bill in (usage.get('_extra_billing_rounds') or []):
                    _bu = _bill.get('usage') or {}
                    for k, v in _bu.items():
                        if isinstance(v, (int, float)):
                            accumulated_usage[k] = accumulated_usage.get(k, 0) + v
                    api_rounds.append({
                        'round': round_num + 1,
                        'model': _bill.get('model') or _FALLBACK_MODEL,
                        'usage': dict(_bu),
                        'tag': _bill.get('tag') or f'R{round_num+1}-FALLBACK-DISCARDED',
                    })
                    _emit_round_usage(task, round_num + 1,
                                      _bill.get('model') or _FALLBACK_MODEL,
                                      _bu,
                                      tag=_bill.get('tag') or
                                      f'R{round_num+1}-FALLBACK-DISCARDED')

            _fb_content_len = len(assistant_msg.get('content', '') or '')
            _fb_tool_calls = len(assistant_msg.get('tool_calls', []))
            _fb_trace = (usage or {}).get('trace_id', 'N/A')
            _fb_elapsed = (usage or {}).get('stream_elapsed_ms', 0)
            logger.info('[%s] ✓ FALLBACK round %d OK: finish_reason=%s model=%s '
                        '(fallback from %s) content=%dchars tool_calls=%d '
                        'M-TraceId=%s elapsed=%.1fs',
                        tid, round_num + 1, last_finish_reason, _FALLBACK_MODEL,
                        original_model, _fb_content_len, _fb_tool_calls,
                        _fb_trace, _fb_elapsed / 1000)

            return {
                'assistant_msg': assistant_msg,
                'finish_reason': last_finish_reason,
                'usage': usage,
                'model': _FALLBACK_MODEL,
                'preset': 'medium',
                'thinking_enabled': True,
                '_loop_action': None,
                '_loop_exit_reason': None,
            }

        except Exception as e2:
            logger.error('[%s] Opus fallback also failed: %s', tid, e2, exc_info=True)
            # ★ Pool-wide last resort (owner directive 2026-08-03) — the
            #   configured fallback dying must not kill the turn while the
            #   pool still has healthy (key, model) slots.
            if not _fb_disabled_by_request:
                _rescue = _attempt_pool_rescue(
                    task, fallback_body, round_num, max_tokens, tool_list,
                    max_tool_rounds, accumulated_usage, api_rounds,
                    failed_models={original_model, _FALLBACK_MODEL},
                    original_model=original_model,
                    cause_exc=e2, preset='medium',
                    thinking_enabled=True,
                    on_tool_call_ready=on_tool_call_ready)
                if _rescue is not None:
                    return _rescue
            # The configured fallback produced NOTHING — clear the
            # decision-time stamp. done/persist read these fields to claim a
            # fallback happened; claiming one that failed is a lie. (A
            # successful pool-rescue above re-stamps with its own values.)
            for _fk in ('_fallback_model', '_fallback_from',
                        '_fallback_reason', '_fallback_kind'):
                task.pop(_fk, None)
            if tool_call_happened:
                _user_err = format_llm_error_for_user(
                    e2, model=_FALLBACK_MODEL,
                    context=f'both-failed ({original_model}→{_FALLBACK_MODEL})',
                    source='llm-fallback')
                task['error'] = _user_err
                logger.warning('[%s] 🛑 Both %s and fallback failed — giving up',
                               tid, original_model, exc_info=True)
                return {
                    'assistant_msg': {'role': 'assistant', 'content': ''},
                    'finish_reason': 'error', 'usage': None,
                    'model': _FALLBACK_MODEL, 'preset': 'medium',
                    'thinking_enabled': True,
                    '_loop_action': 'break',
                    '_loop_exit_reason': f'both_models_failed_round_{round_num}',
                }
            # Attach typed envelope for top-level FATAL handler.
            try:
                e2._user_message = format_llm_error_for_user(  # type: ignore[attr-defined]
                    e2, model=_FALLBACK_MODEL,
                    context=f'both-failed ({original_model}→{_FALLBACK_MODEL})',
                    source='llm-fallback')
            except Exception as _attr_err:
                logger.debug('[%s] Could not attach _user_message: %s', tid, _attr_err)
            raise
