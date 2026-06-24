"""LLM call with automatic fallback to Opus on failure.

Extracted from ``orchestrator.py`` to keep the main orchestration loop
focused on control flow.  The single public entry-point is
:func:`_llm_call_with_fallback`, which streams one LLM round and
transparently retries with Claude Opus 4 (medium preset) when the
primary model errors out.
"""

from lib.llm import build_body
from lib.llm_error_format import format_llm_error_for_user
from lib.log import audit_log, get_logger
from lib.tasks_pkg.manager import append_event, stream_llm_response

logger = get_logger(__name__)


def _get_fallback_model(task: dict | None = None) -> str:
    """Return the configured fallback model, or empty string if disabled.

    Reads from ``lib.FALLBACK_MODEL`` which is backed by
    ``data/config/server_config.json`` → ``model_defaults.fallback_model``.
    Users can set this via Settings UI > 显示 > 模型默认.

    A per-task opt-out (``task['config']['disableModelFallback'] == True``)
    forces an empty string regardless of global config. This is essential
    for controlled experiments: without it, a transient primary-model
    error silently switches the round to the global fallback model
    (e.g. Opus), cross-contaminating a benchmark that is supposed to
    measure ONLY the requested model. With it, the round simply errors
    and the caller/harness retries on the SAME model.
    """
    if task is not None:
        try:
            if (task.get('config') or {}).get('disableModelFallback'):
                return ''
        except Exception as e:
            logger.debug('disableModelFallback check failed: %s', e)
    import lib as _lib
    return getattr(_lib, 'FALLBACK_MODEL', '') or ''

# Track reactive compact attempts per task to avoid infinite loops
_reactive_compact_attempts: dict[str, int] = {}
_REACTIVE_COMPACT_MAX_RETRIES = 2


def cleanup_reactive_compact_state(task_id: str):
    """Remove reactive compact tracking for a finished task.

    Called from orchestrator._finalize_and_emit_done to prevent memory leak.
    """
    _reactive_compact_attempts.pop(task_id, None)


def _emit_round_usage(task, round_num, model, usage, *, tag=''):
    """Emit a per-round usage SSE event so the frontend context-health
    gauge can reflect the size of the prompt JUST sent to the model,
    without waiting for the final ``done`` event to land ``apiRounds``.

    The orchestrator's ``accumulated_usage`` is the per-message running
    sum across all rounds (see :func:`_llm_call_with_fallback`), so the
    only intra-task data that maps to "next-prompt size" is the raw
    ``usage`` dict from THIS round.  We forward exactly that, plus a
    pre-computed ``tokensIn`` (total input tokens including cache) so
    the frontend doesn't need to know the Anthropic-vs-OpenAI cache
    convention.

    Parameters
    ----------
    task : dict
        Live task dict — used by ``append_event`` for SSE delivery.
    round_num : int
        1-based round number (matches the value pushed into
        ``api_rounds``).
    model : str
        Model id actually used for this round (may differ from the
        primary on fallback / reactive paths).
    usage : dict
        Raw usage dict returned by the LLM (post-streaming).
    tag : str
        Diagnostic label such as ``R1`` / ``R3-FALLBACK`` /
        ``R5-REACTIVE``.  Logged only.
    """
    if not usage:
        return
    try:
        inp = usage.get('prompt_tokens') or usage.get('input_tokens') or 0
        cw = (usage.get('cache_write_tokens')
              or usage.get('cache_creation_input_tokens') or 0)
        cr = (usage.get('cache_read_tokens')
              or usage.get('cache_read_input_tokens') or 0)
        # Anthropic convention: prompt_tokens excludes cache. OpenAI
        # convention: prompt_tokens already includes cache. Mirrors the
        # frontend test in ui.js:1853 / context-bar.js:_promptTokensFromUsage.
        if (cw > 0 or cr > 0) and inp <= cw + cr:
            tokens_in = inp + cw + cr
        else:
            tokens_in = inp
        out = usage.get('completion_tokens') or usage.get('output_tokens') or 0
        append_event(task, {
            'type': 'round_usage',
            'round': round_num,
            'model': model,
            'tag': tag,
            'tokensIn': int(tokens_in or 0),
            'tokensOut': int(out or 0),
            'usage': dict(usage),
        })
    except Exception as e:
        logger.debug('[round_usage] emit failed (round=%s tag=%s): %s',
                     round_num, tag, e)


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

        # Detect safety-filter block: empty content on first round only.
        # Later rounds may legitimately have empty content after tool calls.
        # IMPORTANT: skip when the stream layer flagged a transport anomaly
        # (zero-byte close, missing [DONE]) — that's a gateway hiccup, not
        # a safety block.  ``analyse_stream_result`` will route it to the
        # zero-byte retry path (``_PREMATURE_RETRY_MAX_ZERO_BYTE`` = 16).
        _stream_anom = bool((usage or {}).get('_stream_anomaly'))
        if (finish_reason == 'stop'
                and not assistant_msg.get('content', '').strip()
                and not task['content'].strip()
                and not task['thinking'].strip()
                and round_num == 0
                and not _stream_anom):
            last_finish_reason = 'content_filter'
            logger.warning('[%s] 🚫 CONTENT_FILTER detected at round %d: model returned stop '
                           'with empty content on first round — likely safety-blocked. model=%s',
                           tid, round_num, model)
        elif (finish_reason == 'stop'
                and not assistant_msg.get('content', '').strip()
                and round_num == 0
                and _stream_anom):
            logger.info('[%s] 🔄 Round 0 empty stop with stream anomaly — deferring to '
                        'zero-byte retry path (NOT content_filter). model=%s', tid, model)

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
            else:
                _hint_cn = '会话中某张图片超过了 API 大小限制。请使用更小的图片或删除过大的图片。'
                _hint_en = ('One or more images in this conversation exceed the API size '
                            'limit. Please use a smaller image or remove the oversized image.')
            envelope = _make_env(
                'invalid_image',
                hint=f'解决办法 / How to fix:\n• {_hint_cn}\n\n• {_hint_en}',
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
            err_str = str(e)[:200]
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
        err_str = str(e)[:200]
        logger.error('[%s] conv=%s LLM call failed at round %d (model=%s): %s '
                     '(check M-TraceId in preceding debug logs for gateway coordination)',
                     tid, task.get('convId', ''), round_num + 1, model, err_str, exc_info=True)

        # If already on the fallback model, or no fallback configured — give up
        if not _FALLBACK_MODEL or model == _FALLBACK_MODEL:
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

            _fb_stream_anom = bool((usage or {}).get('_stream_anomaly'))
            if (finish_reason == 'stop'
                    and not assistant_msg.get('content', '').strip()
                    and not task['content'].strip()
                    and not task['thinking'].strip()
                    and round_num == 0
                    and not _fb_stream_anom):
                last_finish_reason = 'content_filter'
                logger.warning('[%s] 🚫 CONTENT_FILTER detected at round %d (fallback model=%s): '
                               'stop with empty content on first round — likely safety-blocked',
                               tid, round_num, _FALLBACK_MODEL)

            if finish_reason in ('length', 'max_tokens'):
                _fb_trace = (usage or {}).get('trace_id', 'N/A')
                _fb_elapsed = (usage or {}).get('stream_elapsed_ms', 0)
                logger.warning('[%s] ⚠️ TRUNCATED at round %d (fallback model=%s): '
                               'finish_reason=%s M-TraceId=%s elapsed=%.1fs — '
                               'output token limit reached',
                               tid, round_num, _FALLBACK_MODEL, finish_reason,
                               _fb_trace, _fb_elapsed / 1000)

            task['_fallback_model'] = _FALLBACK_MODEL
            task['_fallback_from'] = original_model
            task['_fallback_reason'] = _fb_reason[:300]
            task['_fallback_kind'] = _fb_kind
            if usage:
                for k, v in usage.items():
                    if isinstance(v, (int, float)):
                        accumulated_usage[k] = accumulated_usage.get(k, 0) + v
                api_rounds.append({'round': round_num + 1, 'model': _FALLBACK_MODEL,
                                   'usage': dict(usage), 'tag': f'R{round_num+1}-FALLBACK'})
                _emit_round_usage(task, round_num + 1, _FALLBACK_MODEL, usage,
                                   tag=f'R{round_num+1}-FALLBACK')

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
