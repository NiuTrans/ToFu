"""Post-stream analysis — premature-close detection and loop-exit decisions.

Extracted from the inner loop of ``orchestrator.run_task`` to isolate the
logic that inspects each LLM round's result and decides whether to retry
(premature close), break (normal finish / error / abort), or continue to
tool execution.
"""

from lib.log import get_logger
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)

# Retry caps for abnormal stream termination.
#
# Two qualitatively different failure signatures get different budgets:
#
#   ── Classic premature close ──────────────────────────────────────
#   Model produced substantial thinking (>1000 chars) then stream was
#   cut off.  Each retry costs ~20-60s of tokens/work, so we keep the
#   cap LOW to avoid burning tokens on a model that is genuinely
#   struggling with the prompt.
#
#   ── Zero-byte stream anomaly ─────────────────────────────────────
#   Gateway/proxy opens the SSE connection, returns zero tokens, and
#   closes within a few seconds.  No work is wasted — each retry is
#   essentially free.  These are transient upstream hiccups (e.g. AWS
#   Bedrock behind a gateway occasionally 0-byte'ing requests).
#   With a ~30% per-call failure rate in a bad window, cap=2 gives
#   ~3% "all fail" probability (user-visible error); cap=16 drops it
#   to effectively 0.  Retries go through append_event 'phase:retrying'
#   so the UI shows a spinner with attempt count.
_PREMATURE_RETRY_MAX_CLASSIC = 2
_PREMATURE_RETRY_MAX_ZERO_BYTE = 16


def analyse_stream_result(
    assistant_msg, last_finish_reason, task, tid, model,
    round_num, _premature_retry_count, messages, usage=None,
):
    """Analyse the result of one LLM streaming round and decide next action.

    Inspects the ``assistant_msg`` returned by ``_llm_call_with_fallback``
    and determines whether the main loop should **break**, **continue**
    (retry after premature close), or **proceed** to tool execution.

    Parameters
    ----------
    assistant_msg : dict
        The assistant message returned from the LLM stream.
    last_finish_reason : str | None
        The finish reason reported by the LLM for this round.
    task : dict
        Live task dict (read for ``aborted``, ``error``, ``content``; mutated
        on premature-close to set ``error``).
    tid : str
        Short task ID for logging.
    model : str
        Current model identifier.
    round_num : int
        Zero-based loop iteration index.
    _premature_retry_count : int
        How many premature-close retries have already been attempted.
    messages : list[dict]
        Conversation message list (kept for API compatibility; no longer
        mutated — retries re-use the same messages transparently).
    usage : dict | None
        Raw usage dict from the LLM response.  Contains ``trace_id``,
        ``resp_trace_id``, and ``stream_elapsed_ms`` for gateway
        coordination diagnostics.

    Returns
    -------
    dict
        A decision dict with the following keys:

        - ``action`` : ``'break'`` | ``'continue'`` | ``'proceed'``
        - ``loop_exit_reason`` : str | None — set when action is ``'break'``
        - ``abort_detected_phase`` : str | None — set when abort is the cause
        - ``premature_retry_count`` : int — updated retry counter
        - ``last_finish_reason`` : str | None — possibly updated finish reason
    """
    result = {
        'action': 'proceed',
        'loop_exit_reason': None,
        'abort_detected_phase': None,
        'premature_retry_count': _premature_retry_count,
        'last_finish_reason': last_finish_reason,
    }

    # ── Error finish reason → break ──
    if last_finish_reason == 'error':
        result['action'] = 'break'
        result['loop_exit_reason'] = f'finish_reason_error_round_{round_num}'
        logger.error(
            '[%s] ✕ Loop breaking due to finish_reason=error at round %d. '
            'error=%s content=%dchars',
            tid, round_num, task.get('error', 'none'),
            len(task.get('content') or ''),
        )
        return result

    # ── No tool calls returned ──
    if not assistant_msg.get('tool_calls'):
        # Check if abort happened mid-stream
        if task['aborted']:
            result['action'] = 'break'
            result['abort_detected_phase'] = f'post_stream_round_{round_num}'
            result['loop_exit_reason'] = f'aborted_post_stream_round_{round_num}'
            logger.debug(
                '[%s] Abort detected after LLM stream (round %d, model=%s). '
                'Model returned no tool_calls — likely interrupted mid-generation. '
                'content=%dchars',
                tid, round_num, model, len(task.get('content') or ''),
            )
            return result

        # ── Detect PREMATURE STREAM CLOSE / ABNORMAL STOP ──
        # Two signatures:
        #   A) Classic premature close: no content, no tool_calls, large thinking (>1000)
        #   B) Stream anomaly + empty content: gateway/proxy severed connection so
        #      early that even thinking barely started (the mnbvo192q8u0zo pattern)
        round_thinking = assistant_msg.get('reasoning_content', '') or ''
        round_content = assistant_msg.get('content', '') or ''

        # ★ Extract gateway-coordination fields from usage for log enrichment
        _trace_id = (usage or {}).get('trace_id', 'N/A')
        _resp_trace = (usage or {}).get('resp_trace_id', '')
        _stream_elapsed_ms = (usage or {}).get('stream_elapsed_ms', 0)
        _stream_anomaly = (usage or {}).get('_stream_anomaly', False)
        _empty_stop = (usage or {}).get('_empty_stop', False)

        # Determine if this round looks like an abnormal termination:
        #   - (A) No content + substantial thinking  (classic premature close)
        #   - (B) Stream anomaly flag + no content + at least 1 prior round
        #         (proxy killed connection before model could produce anything)
        _is_classic_premature = (not round_content.strip()
                                 and len(round_thinking) > 1000)
        _is_anomaly_empty = (not round_content.strip()
                             and _stream_anomaly
                             and round_num > 0)
        _is_abnormal = _is_classic_premature or _is_anomaly_empty
        _abnormal_type = ('premature_close' if _is_classic_premature
                          else 'stream_anomaly' if _is_anomaly_empty
                          else None)

        # ── Split retry budget by failure signature ──
        # Zero-byte anomaly: gateway cut the stream before model emitted
        # anything (elapsed < 15s, thinking < 100 chars).  Each retry is
        # ~free, so use the large cap.  Otherwise (classic premature
        # close with substantial thinking), use the low cap.
        _is_zero_byte = (
            _is_anomaly_empty
            and len(round_thinking) < 100
            and _stream_elapsed_ms < 15000
        )
        _retry_cap = (_PREMATURE_RETRY_MAX_ZERO_BYTE if _is_zero_byte
                      else _PREMATURE_RETRY_MAX_CLASSIC)
        _retry_bucket = 'zero_byte' if _is_zero_byte else 'classic'

        if _is_abnormal and _premature_retry_count < _retry_cap:
            _premature_retry_count += 1
            result['premature_retry_count'] = _premature_retry_count
            logger.warning(
                '[%s] ⚠️ ABNORMAL STOP detected at round %d (type=%s bucket=%s): '
                'thinking=%dchars content=%dchars, no tool_calls. '
                'stream_anomaly=%s empty_stop=%s '
                'M-TraceId=%s resp_trace=%s elapsed=%.1fs model=%s '
                'Retrying (%d/%d)… The stream was likely cut off by proxy/gateway.',
                tid, round_num, _abnormal_type, _retry_bucket,
                len(round_thinking), len(round_content),
                _stream_anomaly, _empty_stop,
                _trace_id, _resp_trace or 'none', _stream_elapsed_ms / 1000,
                model, _premature_retry_count, _retry_cap,
            )
            # ★ Transparent retry: re-call LLM with the SAME messages.
            #   No fake assistant+user turns injected — the model starts fresh
            #   from the original context, just like clicking "Continue".
            #   Use a phase event (transient UI status) instead of a delta
            #   (which would permanently pollute the assistant message content).
            #   'attempt' field lets the frontend dedup/update the retry bubble.
            if _is_zero_byte:
                _phase_detail = (
                    f'⚠️ 网关空流异常（0字节，{_stream_elapsed_ms / 1000:.1f}s），'
                    f'正在自动重试 ({_premature_retry_count}/{_retry_cap})…'
                )
            else:
                _phase_detail = (
                    f'⚠️ 网络中断（代理超时），正在自动重试 '
                    f'({_premature_retry_count}/{_retry_cap})…'
                )
            append_event(task, {
                'type': 'phase',
                'phase': 'retrying',
                'attempt': _premature_retry_count,
                'max': _retry_cap,
                'bucket': _retry_bucket,
                'detail': _phase_detail,
            })
            result['action'] = 'continue'
            return result

        # ABNORMAL STOP: retries exhausted — still no content
        if _is_abnormal and _premature_retry_count >= _retry_cap:
            _fr = 'premature_close' if _is_classic_premature else 'abnormal_stop'
            result['action'] = 'break'
            result['last_finish_reason'] = _fr
            result['loop_exit_reason'] = f'{_fr}_retries_exhausted_round_{round_num}'
            task['error'] = (
                f'⚠️ 生成被网关/代理异常中断，重试已用完（{_premature_retry_count}/'
                f'{_retry_cap}）。回复内容可能不完整。'
                f' (type: {_abnormal_type}, bucket: {_retry_bucket}, '
                f'M-TraceId: {_trace_id})'
            )
            logger.error(
                '[%s] ⚠️ ABNORMAL STOP retries exhausted at round %d '
                '(type=%s bucket=%s attempts=%d/%d). '
                'thinking=%dchars, content=%dchars. '
                'stream_anomaly=%s empty_stop=%s '
                'M-TraceId=%s resp_trace=%s elapsed=%.1fs model=%s '
                'Setting finishReason=%s.',
                tid, round_num, _abnormal_type, _retry_bucket,
                _premature_retry_count, _retry_cap,
                len(round_thinking), len(round_content),
                _stream_anomaly, _empty_stop,
                _trace_id, _resp_trace or 'none', _stream_elapsed_ms / 1000,
                model, _fr,
            )
            return result

        # ── Stream anomaly — with or without content ──
        # If the LLM client flagged a stream anomaly (_missing_done,
        # _missing_finish_reason, _empty_stop), the response is likely
        # truncated even if some content was produced.  Expose the
        # anomaly so the user knows the reply may be incomplete.
        if _stream_anomaly:
            result['action'] = 'break'
            result['last_finish_reason'] = 'abnormal_stop'
            _has_content = bool(round_content.strip())
            result['loop_exit_reason'] = (
                f'stream_anomaly_{"partial" if _has_content else "empty"}'
                f'_round_{round_num}'
            )
            task['error'] = (
                f'⚠️ API流异常终止（缺失finish标记），回复内容可能不完整。'
                f' (M-TraceId: {_trace_id})'
            )
            logger.warning(
                '[%s] ⚠️ Stream anomaly at round %d '
                '(has_content=%s, content=%dchars). '
                'stream_anomaly=%s empty_stop=%s '
                'M-TraceId=%s model=%s accumulated_content=%dchars '
                'Setting finishReason=abnormal_stop.',
                tid, round_num, _has_content, len(round_content),
                _stream_anomaly, _empty_stop,
                _trace_id, model, len(task.get('content') or ''),
            )
            return result

        # Normal exit — model returned content without tool calls
        result['action'] = 'break'
        result['loop_exit_reason'] = f'no_tool_calls_round_{round_num}'

        # ★ Fix: API reported finish_reason=tool_calls but all tool calls
        #   were filtered out (phantom/spurious filter in llm_client), or
        #   the gateway reported tool_calls but the stream contained none.
        #   Normalize to 'stop' so the post-loop check in _finalize doesn't
        #   misinterpret this as "loop ended unexpectedly with pending tools".
        if last_finish_reason in ('tool_calls', 'tool_use'):
            logger.warning(
                '[%s] ⚠ finish_reason=%s but assistant_msg has 0 tool_calls '
                '(likely all filtered out by phantom/spurious filter). '
                'Normalizing to stop. model=%s round=%d',
                tid, last_finish_reason, model, round_num,
            )
            result['last_finish_reason'] = 'stop'

        logger.debug(
            '[%s] Loop ending normally: model=%s returned text without '
            'tool_calls at round %d. finish_reason=%s content=%dchars',
            tid, model, round_num, result['last_finish_reason'],
            len(task.get('content') or ''),
        )
        return result

    # assistant_msg has tool_calls → proceed to tool execution (or check budget)
    return result
