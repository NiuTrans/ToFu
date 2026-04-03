"""LLM call with automatic fallback to Opus on failure.

Extracted from ``orchestrator.py`` to keep the main orchestration loop
focused on control flow.  The single public entry-point is
:func:`_llm_call_with_fallback`, which streams one LLM round and
transparently retries with Claude Opus 4 (medium preset) when the
primary model errors out.
"""

from lib.llm_client import build_body
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event, stream_llm_response

logger = get_logger(__name__)


def _get_fallback_model() -> str:
    """Return the configured fallback model, or empty string if disabled.

    Reads from ``lib.FALLBACK_MODEL`` which is backed by
    ``data/config/server_config.json`` → ``model_defaults.fallback_model``.
    Users can set this via Settings UI > 显示 > 模型默认.
    """
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
    lib.llm_client.AbortedError
        Never caught — propagates directly to signal user abort.
    """
    tid = task['id'][:8]
    _FALLBACK_MODEL = _get_fallback_model()

    # ── Primary model call ──
    try:
        assistant_msg, finish_reason, usage = stream_llm_response(
            task, body, tag=f'R{round_num+1}',
            on_tool_call_ready=on_tool_call_ready)
        last_finish_reason = finish_reason

        # Detect safety-filter block: empty content on first round only.
        # Later rounds may legitimately have empty content after tool calls.
        if (finish_reason == 'stop'
                and not assistant_msg.get('content', '').strip()
                and not task['content'].strip()
                and not task['thinking'].strip()
                and round_num == 0):
            last_finish_reason = 'content_filter'
            logger.warning('[%s] 🚫 CONTENT_FILTER detected at round %d: model returned stop '
                           'with empty content on first round — likely safety-blocked. model=%s',
                           tid, round_num, model)

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
        from lib.llm_client import AbortedError, ContentFilterError, PromptTooLongError
        if isinstance(e, AbortedError):
            logger.debug('[%s] ✋ AbortedError at round %d — stopping immediately', tid, round_num)
            raise

        # ── PromptTooLongError → reactive compaction + retry ──
        # Inspired by Claude Code's reactive compact: when the API rejects
        # with "prompt too long", compress the conversation and retry.
        if isinstance(e, PromptTooLongError):
            _task_id = task.get('id', '')
            _attempts = _reactive_compact_attempts.get(_task_id, 0)
            if _attempts < _REACTIVE_COMPACT_MAX_RETRIES:
                _reactive_compact_attempts[_task_id] = _attempts + 1
                logger.warning(
                    '[%s] ⚡ REACTIVE COMPACT triggered at round %d (attempt %d/%d): '
                    'prompt too long for model=%s — compressing and retrying',
                    tid, round_num, _attempts + 1, _REACTIVE_COMPACT_MAX_RETRIES, model)

                from lib.tasks_pkg.compaction import reactive_compact
                reactive_compact(messages, task=task)

                # Rebuild body with compressed messages
                _tools_this_round = tool_list if (tool_list and round_num < max_tool_rounds) else None
                body = build_body(
                    model, messages,
                    max_tokens=task.get('config', {}).get('maxTokens', 128000),
                    temperature=body.get('temperature', 1.0),
                    thinking_enabled=thinking_enabled,
                    preset=preset,
                    tools=_tools_this_round,
                    stream=True,
                )

                # Notify frontend
                append_event(task, {
                    'type': 'delta',
                    'content': f'\n\n> ⚡ 上下文超长，已自动压缩 (reactive compact {_attempts + 1}/{_REACTIVE_COMPACT_MAX_RETRIES})…\n\n',
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
                task['error'] = f'API error ({_FALLBACK_MODEL}): {err_str}'
                logger.warning('[%s] 🛑 Fallback model error with prior tool calls — giving up: %s',
                               tid, err_str, exc_info=True)
                return {
                    'assistant_msg': {'role': 'assistant', 'content': f'[Error: {err_str}]'},
                    'finish_reason': 'error', 'usage': None,
                    'model': model, 'preset': preset, 'thinking_enabled': thinking_enabled,
                    '_loop_action': 'break',
                    '_loop_exit_reason': f'opus_error_with_tool_calls_round_{round_num}',
                }
            raise

        # ── Fallback: switch to configured fallback model ──
        # Show a concise error reason to the user (truncate for display)
        _display_err = err_str[:120].rstrip()
        fallback_notice = (f'\n\n> ⚠️ 模型 `{original_model}` 请求失败 (`{_display_err}`)，'
                           f'已自动回退到 **{_FALLBACK_MODEL}** 继续生成…\n\n')
        with task['content_lock']:
            task['content'] += fallback_notice
        append_event(task, {'type': 'delta', 'content': fallback_notice})
        logger.warning('[%s] Model fallback: %s → %s',
                       tid, original_model, _FALLBACK_MODEL, exc_info=True)

        fallback_body = build_body(
            _FALLBACK_MODEL, messages,
            max_tokens=max_tokens,
            temperature=1.0,
            thinking_enabled=True,
            preset='opus',
            thinking_depth='medium',
            tools=tool_list if (tool_list and round_num < max_tool_rounds) else None,
            stream=True,
        )

        try:
            assistant_msg, finish_reason, usage = stream_llm_response(
                task, fallback_body, tag=f'R{round_num+1}-FALLBACK')
            last_finish_reason = finish_reason

            if (finish_reason == 'stop'
                    and not assistant_msg.get('content', '').strip()
                    and not task['content'].strip()
                    and not task['thinking'].strip()
                    and round_num == 0):
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
            if usage:
                for k, v in usage.items():
                    if isinstance(v, (int, float)):
                        accumulated_usage[k] = accumulated_usage.get(k, 0) + v
                api_rounds.append({'round': round_num + 1, 'model': _FALLBACK_MODEL,
                                   'usage': dict(usage), 'tag': f'R{round_num+1}-FALLBACK'})

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
                task['error'] = (f'{original_model} and Opus fallback both failed: '
                                 f'{str(e2)[:200]}')
                logger.warning('[%s] 🛑 Both %s and fallback failed — giving up',
                               tid, original_model, exc_info=True)
                return {
                    'assistant_msg': {'role': 'assistant', 'content': f'[Error: {str(e2)[:200]}]'},
                    'finish_reason': 'error', 'usage': None,
                    'model': _FALLBACK_MODEL, 'preset': 'medium',
                    'thinking_enabled': True,
                    '_loop_action': 'break',
                    '_loop_exit_reason': f'both_models_failed_round_{round_num}',
                }
            raise
