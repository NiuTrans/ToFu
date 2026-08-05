"""Post-stream RESULT ANALYSIS — the ``analyse_stream_result`` classifier.

Extracted from the inner loop of ``orchestrator.run_task`` to isolate the
logic that inspects each LLM round's result and decides whether to retry
(premature close), break (normal finish / error / abort), or continue to
tool execution.
"""

import random

from lib.agent_core.events import EventType, Phase, build_event, emit_phase
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event

from lib.tasks_pkg.stream_handler._audit import _maybe_audit_phase_scope
from lib.tasks_pkg.stream_handler._budget import (
    _CANNED_GREETING_RETRY_MAX,
    _EMPTY_STOP_RETRY_MAX,
    _PREMATURE_RETRY_MAX_CLASSIC,
    _PREMATURE_RETRY_MAX_ZERO_BYTE,
    _zero_byte_backoff_seconds,
)
from lib.tasks_pkg.stream_handler._canned_greeting import (
    is_canned_greeting_reply,
)

logger = get_logger(__name__)


# ── Facade-routed helpers (monkeypatch-friendly) ──
# ``_interruptible_sleep`` and ``_todo_continuation_max`` are invoked through
# the package facade module (``lib.tasks_pkg.stream_handler``) rather than by a
# direct name binding.  Tests monkeypatch these symbols on the facade — e.g.
# ``monkeypatch.setattr(stream_handler, '_interruptible_sleep', ...)`` and
# ``monkeypatch.setattr(sh, '_todo_continuation_max', lambda: 0)`` — so routing
# each call through the live module attribute makes those patches take effect
# inside ``analyse_stream_result``.  The package module is fetched from
# ``sys.modules`` at CALL time (not import time) to avoid an import cycle:
# ``_analyse`` is imported *by* the package ``__init__``.
import sys as _sys


def _interruptible_sleep(seconds, task):
    return _sys.modules['lib.tasks_pkg.stream_handler']._interruptible_sleep(
        seconds, task)


def _todo_continuation_max():
    return _sys.modules['lib.tasks_pkg.stream_handler']._todo_continuation_max()


def analyse_stream_result(
    assistant_msg, last_finish_reason, task, tid, model,
    round_num, _premature_retry_count, messages, usage=None,
):
    """Analyse the result of one LLM streaming round and decide next action.

    Inspects the ``assistant_msg`` returned by ``_llm_call_with_fallback``
    and determines whether the main loop should **break**, **continue**
    (retry after premature close), or **proceed** to tool execution.

    Per-phase counter scope
    -----------------------
    The premature-retry counter survives across rounds within the same
    Worker / Planner phase: each ``analyse_stream_result`` call reads
    ``task['_premature_retry_count_phase']`` as the source of truth (when
    present) and writes the updated value back.  The legacy
    ``_premature_retry_count`` argument is kept for back-compat (paper
    reports / swarm agents pass a local counter); when the task dict
    has the phase counter set, that overrides the argument.

    Per-retry slot rotation
    -----------------------
    On each zero-byte retry decision, the analyser also signals to the
    next dispatch call to AVOID the pair ``(slot.key_name, slot.model)``
    that just zero-byte'd.  The signal is written to
    ``task['_force_rotate_pair']`` and consumed (cleared) by
    ``stream_llm_response`` on the next call.  Mirrors the
    ``gateway-5xx-treated-as-429`` pattern.

    Parameters
    ----------
    assistant_msg : dict
        The assistant message returned from the LLM stream.
    last_finish_reason : str | None
        The finish reason reported by the LLM for this round.
    task : dict
        Live task dict (read for ``aborted``, ``error``, ``content``; mutated
        on premature-close to set ``error`` AND set the per-phase counter
        and force-rotate signal).
    tid : str
        Short task ID for logging.
    model : str
        Current model identifier.
    round_num : int
        Zero-based loop iteration index.
    _premature_retry_count : int
        How many premature-close retries have already been attempted.
        Treated as a fallback when ``task['_premature_retry_count_phase']``
        is not yet initialised — caller code in the orchestrator that
        sets the phase counter on the task dict overrides this argument.
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
    # ── Per-phase counter override ──
    # If the orchestrator has set ``task['_premature_retry_count_phase']``,
    # use it as the source of truth so the cap survives across rounds
    # within one phase.  Otherwise fall back to the legacy local counter
    # passed in by the caller.
    if '_premature_retry_count_phase' in task:
        _premature_retry_count = int(task.get('_premature_retry_count_phase') or 0)
        _maybe_audit_phase_scope()

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
        # Real SSE chunk count from the LLM client. Zero == gateway opened
        # the stream but never delivered a single token (true zero-byte).
        # Falls back to None when older clients haven't propagated it.
        _chunks_received = (usage or {}).get('_chunks_received')

        # Determine if this round looks like an abnormal termination:
        #   - (A) No content + substantial thinking  (classic premature close)
        #   - (B) Stream anomaly flag + no content + at least 1 prior round
        #         (proxy killed connection before model could produce anything)
        # ── Zero-byte gateway anomaly (computed first, allowed on round 0) ──
        # The gateway opened the SSE connection and closed it before any
        # meaningful token came through.  No work was done, no tokens
        # were spent — retrying is essentially free, so we admit this
        # case on EVERY round including round 0.  This is the recurring
        # ``aws.claude-opus-4.7`` via sankuai gateway pattern documented
        # in the ``stream-retry-cap-split-by-signature`` memory.
        #
        # Detection: prefer the real ``_chunks_received`` from the LLM
        # client (0 = no SSE chunks at all → gateway hang, retry is
        # free regardless of how long we waited).  Fall back to the
        # legacy thinking-length + elapsed-time heuristic when the
        # client field isn't present.  The legacy bound originally
        # used ``< 15s`` but production logs show ~36 % of true
        # zero-byte gateway hangs took 15–40 s before the upstream
        # closed the socket, so we widen the bound to 60 s — still
        # less than the 5-minute read timeout, and still cheap to redo
        # because no tokens were actually generated.
        if _chunks_received is not None:
            _is_zero_byte = (
                not round_content.strip()
                and not round_thinking.strip()
                and _stream_anomaly
                and (
                    _chunks_received == 0
                    # Stub response: gateway returned protocol framing
                    # (role + stop chunks) but model generated nothing.
                    # prompt_tokens/completion_tokens are nonsensical.
                    # Same cost to retry as true zero-byte.
                    or (_empty_stop
                        and _chunks_received <= 5
                        and _stream_elapsed_ms < 60000)
                )
            )
        else:
            _is_zero_byte = (
                not round_content.strip()
                and _stream_anomaly
                and len(round_thinking) < 100
                and _stream_elapsed_ms < 60000
            )

        # ── Classic premature close: substantial thinking, then cut off ──
        _is_classic_premature = (not round_content.strip()
                                 and len(round_thinking) > 1000)

        # ── Other stream-anomaly empty (later rounds only) ──
        # Without the zero-byte signature we can't be sure the round is
        # cheap to redo, so we keep the historical ``round_num > 0``
        # guard to avoid retrying a legitimate empty first-round stop.
        _is_anomaly_empty = (not round_content.strip()
                             and _stream_anomaly
                             and round_num > 0
                             and not _is_zero_byte)

        _is_abnormal = (_is_classic_premature or _is_anomaly_empty
                        or _is_zero_byte)
        _abnormal_type = ('premature_close' if _is_classic_premature
                          else 'zero_byte' if _is_zero_byte
                          else 'stream_anomaly' if _is_anomaly_empty
                          else None)

        # ── Retry budget split by failure signature ──
        # Zero-byte: gateway never delivered output, retry is ~free,
        # use the large cap.  Anything else (classic close, late-round
        # anomaly): tokens were already spent, use the low cap.
        _retry_cap = (_PREMATURE_RETRY_MAX_ZERO_BYTE if _is_zero_byte
                      else _PREMATURE_RETRY_MAX_CLASSIC)
        _retry_bucket = 'zero_byte' if _is_zero_byte else 'classic'

        if _is_abnormal and _premature_retry_count < _retry_cap:
            _premature_retry_count += 1
            result['premature_retry_count'] = _premature_retry_count
            # Persist the per-phase counter back so the next round of
            # this phase sees the bumped value.
            if '_premature_retry_count_phase' in task:
                task['_premature_retry_count_phase'] = _premature_retry_count
            # Pace abnormal-stop retries with exponential backoff + jitter so
            # we don't hammer a poisoned upstream pool. Both zero-byte and
            # classic premature-close use the same backoff schedule; the
            # late-round stream-anomaly bucket keeps the historical no-backoff
            # behaviour.
            _backoff_s = (_zero_byte_backoff_seconds(_premature_retry_count)
                          if (_is_zero_byte or _is_classic_premature) else 0.0)

            # ── Force slot rotation on zero-byte retries ──
            # Zero-byte gateway hangs cluster per-pool — production logs
            # show 34/34 anomalies hit one slot in a 2-minute window.
            # Signal the next dispatch to avoid re-using the slot that
            # just zero-byte'd.  ``stream_llm_response`` reads this and
            # passes ``avoid_pairs`` to ``dispatch_stream``, then clears
            # the signal.  Best-effort — when ``_dispatch`` metadata is
            # absent (older gateway path), we fall through without the
            # rotation hint and the existing 429-style cooldown still
            # naturally rotates slots. Classic premature-close keeps the
            # SAME slot (strict_model is on; the slot already produced
            # output, so it's likely transient and worth retrying as-is).
            if _is_zero_byte:
                _disp = (usage or {}).get('_dispatch') or {}
                _key = _disp.get('key')
                _mod = _disp.get('model') or model
                if _key:
                    task['_force_rotate_pair'] = (_key, _mod)
                    logger.info(
                        '[%s] zero-byte retry: force-rotating away from '
                        'slot %s:%s for next dispatch attempt',
                        tid, _key, _mod,
                    )
            logger.warning(
                '[%s] ⚠️ ABNORMAL STOP detected at round %d (type=%s bucket=%s): '
                'thinking=%dchars content=%dchars, no tool_calls. '
                'stream_anomaly=%s empty_stop=%s '
                'M-TraceId=%s resp_trace=%s elapsed=%.1fs model=%s '
                'Retrying (%d/%d) after %.1fs backoff… '
                'The stream was likely cut off by proxy/gateway.',
                tid, round_num, _abnormal_type, _retry_bucket,
                len(round_thinking), len(round_content),
                _stream_anomaly, _empty_stop,
                _trace_id, _resp_trace or 'none', _stream_elapsed_ms / 1000,
                model, _premature_retry_count, _retry_cap, _backoff_s,
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
                    f'退避 {_backoff_s:.1f}s 后重试 '
                    f'({_premature_retry_count}/{_retry_cap})…'
                )
            else:
                _phase_detail = (
                    f'⚠️ 网络中断（代理超时），正在自动重试 '
                    f'({_premature_retry_count}/{_retry_cap})…'
                )
            emit_phase(task, Phase.RETRYING,
                       attempt=_premature_retry_count,
                       max=_retry_cap,
                       bucket=_retry_bucket,
                       backoff_s=round(_backoff_s, 2),
                       detail=_phase_detail)
            if _backoff_s > 0:
                _interruptible_sleep(_backoff_s, task)
            result['action'] = 'continue'
            return result

        # ABNORMAL STOP: retries exhausted — still no content
        if _is_abnormal and _premature_retry_count >= _retry_cap:
            _fr = 'premature_close' if _is_classic_premature else 'abnormal_stop'
            result['action'] = 'break'
            result['last_finish_reason'] = _fr
            result['loop_exit_reason'] = f'{_fr}_retries_exhausted_round_{round_num}'
            from lib.error_envelope import make_envelope as _make_env
            task['error'] = _make_env(
                _fr,
                detail=(f'Retries exhausted ({_premature_retry_count}/{_retry_cap}). '
                        f'type={_abnormal_type} bucket={_retry_bucket} '
                        f'M-TraceId={_trace_id}'),
                model=model,
                context=f'round-{round_num}',
                source='llm-stream',
                raw=(f'abnormal_type={_abnormal_type} bucket={_retry_bucket} '
                     f'attempts={_premature_retry_count}/{_retry_cap} '
                     f'thinking={len(round_thinking)}chars content={len(round_content)}chars'),
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

        # ── Empty-stop retry (model said finish_reason=stop with no
        #    content). Observed on GLM-5.1 (thinking-only response),
        #    MiniMax M2.5/M2.7, and Claude.  Cheap to retry once or
        #    twice; budget is shared with classic premature-close so
        #    a misbehaving turn can never burn more than 2 retries
        #    across both buckets. ──
        _is_empty_stop = (
            _empty_stop
            and not round_content.strip()
            and not _is_zero_byte
        )
        if (_is_empty_stop
                and _premature_retry_count < _EMPTY_STOP_RETRY_MAX):
            _premature_retry_count += 1
            result['premature_retry_count'] = _premature_retry_count
            if '_premature_retry_count_phase' in task:
                task['_premature_retry_count_phase'] = _premature_retry_count
            _backoff_s = 0.5 + random.uniform(0.0, 0.5)
            logger.warning(
                '[%s] ⚠️ EMPTY_STOP detected at round %d: '
                'finish=stop content=0 thinking=%dchars '
                'M-TraceId=%s elapsed=%.1fs model=%s '
                'Retrying (%d/%d) after %.1fs backoff…',
                tid, round_num, len(round_thinking),
                _trace_id, _stream_elapsed_ms / 1000, model,
                _premature_retry_count, _EMPTY_STOP_RETRY_MAX, _backoff_s,
            )
            emit_phase(task, Phase.RETRYING,
                       attempt=_premature_retry_count,
                       max=_EMPTY_STOP_RETRY_MAX,
                       bucket='empty_stop',
                       backoff_s=round(_backoff_s, 2),
                       detail=(
                           f'⚠️ 模型空回复（{len(round_thinking)}字符思考但无正文），'
                           f'重试中 ({_premature_retry_count}/{_EMPTY_STOP_RETRY_MAX})…'
                       ))
            _interruptible_sleep(_backoff_s, task)
            result['action'] = 'continue'
            return result

        # ── Canned-greeting upstream artifact (2026-07-28 Opus 5 incident) ──
        # The gateway's only Opus 5 request-id (a daily eval build) began
        # answering ANY request — including mid-tool-work continuations —
        # with an identical canned greeting and a CLEAN finish_reason=stop
        # (real M-TraceId, real usage). Every transport guard keys off
        # MISSING output, so this "successful" degenerate response ended
        # turns and was persisted over accumulated tool work (68+ events in
        # ~5h, see _canned_greeting.py). Detect by CONTENT + INCONGRUENCE
        # and retry like the other transient buckets — the failure was
        # intermittent (~50%/round), so a bounded retry recovers most
        # turns. Shares the per-phase counter (runaway-guard discipline).
        _is_canned_greeting = is_canned_greeting_reply(round_content, messages)
        if (_is_canned_greeting
                and _premature_retry_count < _CANNED_GREETING_RETRY_MAX):
            _premature_retry_count += 1
            result['premature_retry_count'] = _premature_retry_count
            if '_premature_retry_count_phase' in task:
                task['_premature_retry_count_phase'] = _premature_retry_count
            _backoff_s = 1.0 + random.uniform(0.0, 1.0)
            logger.warning(
                '[%s] ⚠️ CANNED GREETING detected at round %d: finish=stop '
                'content=%dchars (%r) — a greeting opener incongruent with '
                'the conversation tail. M-TraceId=%s elapsed=%.1fs model=%s '
                'Retrying (%d/%d) after %.1fs backoff…',
                tid, round_num, len(round_content), round_content[:40],
                _trace_id, _stream_elapsed_ms / 1000, model,
                _premature_retry_count, _CANNED_GREETING_RETRY_MAX,
                _backoff_s,
            )
            # ★ Drop the poisoned text BEFORE re-streaming. This is the ONLY
            #   retry bucket whose discarded round HAS content (zero-byte /
            #   classic / empty-stop all require empty content), so it is also
            #   the only one that must reset the accumulators — otherwise each
            #   attempt's greeting concatenates onto the last (2026-08-02
            #   triple-greeting bug). ``discard=True`` tells the client
            #   reducer to clear WITHOUT the tool-round prose-capture guard:
            #   this round issued no tool calls, so there is no batch to
            #   stamp onto and the freeze guard would keep the text forever.
            with task['content_lock']:
                task['content'] = ''
                task['thinking'] = ''
            append_event(task, build_event(
                EventType.DELTA_RESET, roundNum=round_num, discard=True))
            emit_phase(task, Phase.RETRYING,
                       attempt=_premature_retry_count,
                       max=_CANNED_GREETING_RETRY_MAX,
                       bucket='canned_greeting',
                       backoff_s=round(_backoff_s, 2),
                       detail=(
                           f'⚠️ 上游返回了与任务无关的模板问候（{len(round_content)}字符），'
                           f'重试中 ({_premature_retry_count}/{_CANNED_GREETING_RETRY_MAX})…'
                       ))
            _interruptible_sleep(_backoff_s, task)
            result['action'] = 'continue'
            return result

        if _is_canned_greeting:
            # Budget exhausted — ACCEPT, never fabricate an error: a greeting
            # can be legitimate, and the persist-layer interception
            # (_maybe_preserve_accumulated_on_suspicion) rebuilds accumulated
            # narration when this overwrote real tool work. Loud + audited so
            # the upstream incident stays observable.
            logger.warning(
                '[%s] ⚠️ CANNED GREETING retries exhausted at round %d '
                '(%d/%d) — accepting the response. content=%r '
                'M-TraceId=%s model=%s',
                tid, round_num, _premature_retry_count,
                _CANNED_GREETING_RETRY_MAX, round_content[:60],
                _trace_id, model,
            )
            try:
                from lib.log import audit_log
                audit_log('canned_greeting_retries_exhausted',
                          task_id=task.get('id', ''),
                          conv=task.get('convId', ''),
                          round=round_num, model=model,
                          content=round_content[:60])
            except Exception as _ae:
                logger.debug('[%s] canned-greeting audit failed: %s', tid, _ae)

        # ── Stream anomaly — with or without content ──
        # If the LLM client flagged a stream anomaly (_missing_done,
        # _missing_finish_reason, _empty_stop), the response is likely
        # truncated even if some content was produced.
        if _stream_anomaly:
            _has_content = bool(round_content.strip())
            if _has_content:
                # ★ Soft landing (owner directive 2026-08-05): when the stream
                #   died but a partial answer exists, do NOT fail the turn —
                #   an error card only offers Retry/Continue anyway, and the
                #   whole-turn auto-retry would wipe text the user may already
                #   be reading. Settle as premature_close instead: the
                #   settlement vocabulary maps that to interrupted/gateway
                #   (Continue stays available, no error state, no auto-retry),
                #   and the finish tag renders the persistent visible notice
                #   ("网关中断 · 内容可能不完整"). The failure stays visible;
                #   the turn is not interrupted.
                result['action'] = 'break'
                result['last_finish_reason'] = 'premature_close'
                result['loop_exit_reason'] = (
                    f'stream_anomaly_partial_soft_round_{round_num}'
                )
                logger.warning(
                    '[%s] ⚠️ Stream anomaly at round %d with partial content '
                    '(%dchars) — soft-landing as premature_close (no error '
                    'envelope; partial reply kept). stream_anomaly=%s '
                    'empty_stop=%s M-TraceId=%s model=%s',
                    tid, round_num, len(round_content),
                    _stream_anomaly, _empty_stop, _trace_id, model,
                )
                return result
            result['action'] = 'break'
            result['last_finish_reason'] = 'abnormal_stop'
            result['loop_exit_reason'] = (
                f'stream_anomaly_empty_round_{round_num}'
            )
            from lib.error_envelope import make_envelope as _make_env
            task['error'] = _make_env(
                'abnormal_stop',
                detail=f'Stream ended without finish marker (M-TraceId: {_trace_id})',
                model=model,
                context=f'round-{round_num}',
                source='llm-stream',
                raw=(f'has_content=False '
                     f'stream_anomaly={_stream_anomaly} empty_stop={_empty_stop} '
                     f'M-TraceId={_trace_id}'),
            )
            logger.warning(
                '[%s] ⚠️ Stream anomaly at round %d (no content). '
                'stream_anomaly=%s empty_stop=%s '
                'M-TraceId=%s model=%s accumulated_content=%dchars '
                'Setting finishReason=abnormal_stop.',
                tid, round_num,
                _stream_anomaly, _empty_stop,
                _trace_id, model, len(task.get('content') or ''),
            )
            return result

        # ── Todo-continuation enforcer (Rec 2) ──
        # The model is about to end its turn with a genuine final answer. If it
        # declared a structured checklist (task['_todos']) that still has
        # incomplete items, re-drive the loop with a reminder instead of
        # letting it stop — the productive-but-premature-stop case that the
        # zero-deliverable guard (INACTION) and suspicious-completion
        # (content-shape) both structurally miss. Only for a genuine content
        # stop; abort / error / anomaly paths above have already returned.
        _todo_max = _todo_continuation_max()
        if _todo_max and round_content.strip():
            from lib.tools.todo import incomplete_todos, render_todo_list
            _todos = task.get('_todos') or []
            _incomplete = incomplete_todos(_todos)
            _nudges = int(task.get('_todo_continuation_count') or 0)
            if _incomplete and _nudges < _todo_max:
                task['_todo_continuation_count'] = _nudges + 1
                messages.append({
                    'role': 'user',
                    'content': (
                        '[SYSTEM: TODO CONTINUATION REQUIRED]\n'
                        f'You have {len(_incomplete)} incomplete checklist '
                        f'item(s):\n{render_todo_list(_todos)}\n\n'
                        'Do NOT end your turn yet. Continue working and complete '
                        'ALL items, updating the checklist with todo_write as you '
                        'go. If an item is genuinely impossible or no longer '
                        'applies, either remove it or mark it completed with a '
                        'one-line explanation — then finish.'
                    ),
                })
                logger.info(
                    '[%s] 📋 Todo-continuation enforcer: %d incomplete item(s) '
                    'at stop — re-driving loop (nudge %d/%d) round=%d',
                    tid, len(_incomplete), _nudges + 1, _todo_max, round_num)
                emit_phase(task, Phase.TODO_CONTINUATION,
                           attempt=_nudges + 1,
                           max=_todo_max,
                           incomplete=len(_incomplete),
                           detail=(f'📋 检测到 {len(_incomplete)} 项待办未完成，'
                                   f'继续执行 ({_nudges + 1}/{_todo_max})…'))
                result['action'] = 'continue'
                return result
            if _incomplete and _nudges >= _todo_max:
                logger.warning(
                    '[%s] 📋 Todo-continuation cap reached (%d/%d) with %d '
                    'incomplete item(s) — allowing stop to avoid runaway loop',
                    tid, _nudges, _todo_max, len(_incomplete))

        # ── Intent-stall nudge (epic pt_33ba079f5cea4841) ──
        # The model's previous tool call was rejected/errored, and this round
        # is prose-only — it said what it would do and then stopped. Ground
        # truth: conv ms34yw0k74o2lq R18 ("Let me use explicit paths only."
        # after a blocked run_command). The task settled normally and the user
        # saw the conversation stop mid-thought.
        #
        # Four structural criteria, never wording — the ticket's A∧B pair
        # alone measured 60% false positives over 7 days (5 hand-backs, 4 VU
        # endings, 3 non-retryable), so C and D are load-bearing, not polish.
        # See docs/INTENT_STALL_MEASUREMENT.md and _intent_stall.py.
        #
        # ONE nudge per task: the counter is checked and bumped here, so a
        # model that stalls again after being nudged is allowed to stop (the
        # runaway guard — same discipline as the retry caps above).
        _stall_nudges = int(task.get('_intent_stall_nudge_count') or 0)
        if _stall_nudges < 1:
            from lib.tasks_pkg.stream_handler._intent_stall import (
                NUDGE_TEXT as _stall_text,
                should_nudge_intent_stall as _should_stall_nudge,
            )
            _do_nudge, _stall_reason = _should_stall_nudge(
                task, assistant_msg, round_content)
            if _do_nudge:
                task['_intent_stall_nudge_count'] = _stall_nudges + 1
                messages.append({'role': 'user', 'content': _stall_text})
                # DISPLAY-ONLY sidecar accumulation — the in-timeline chip.
                # Unlike the peer / steer lanes this is emitted AT INJECTION
                # rather than deferred until the next LLM call confirms
                # consumption: those lanes defer because an abort must re-route
                # an undelivered HUMAN message to the durable queue (never
                # zero, never double). A nudge has no human author and nothing
                # to salvage — if the turn dies here the nudge is simply moot,
                # and the fact worth showing ('the system re-drove the model')
                # is true the moment we append it.
                from lib.tasks_pkg.stream_handler._intent_stall import (
                    build_stall_nudge_record as _build_stall_record,
                )
                try:
                    task.setdefault('_stallNudges', []).append(
                        _build_stall_record(task, round_num))
                except Exception as _sn_e:  # a chip must never break the loop
                    logger.warning('[%s] stall-nudge chip record failed: %s',
                                   tid, _sn_e)
                logger.info(
                    '[%s] ↻ Intent-stall nudge at round %d: previous tool '
                    'round failed and this round was prose-only with no tool '
                    'calls — re-driving once. model=%s content=%dchars',
                    tid, round_num, model, len(round_content))
                emit_phase(task, Phase.INTENT_STALL_NUDGE,
                           attempt=_stall_nudges + 1,
                           max=1,
                           detail='↻ Previous tool call did not run — nudging the '
                                  'model to continue…',
                           detailKey='stream.phase.intentStallNudge')
                result['action'] = 'continue'
                return result
            if _stall_reason not in ('prev_tool_ok', 'no_tool_rounds',
                                     'no_content', 'has_tool_calls'):
                # Log only the INTERESTING skips (a stop that looked like a
                # stall but was deliberately left alone), so the criteria that
                # do the real work are observable in production.
                logger.debug(
                    '[%s] intent-stall nudge skipped at round %d: %s',
                    tid, round_num, _stall_reason)

        # Normal exit — model returned content without tool calls
        result['action'] = 'break'
        result['loop_exit_reason'] = f'no_tool_calls_round_{round_num}'

        # ★ Fix: API reported finish_reason=tool_calls but all tool calls
        #   were filtered out (phantom/spurious filter in lib/llm/stream.py), or
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

    # assistant_msg has tool_calls → but a premature close may have cut the
    # stream MID-ARGUMENTS, leaving tool calls whose accumulated JSON cannot
    # parse. Executing those would run tools on corrupt arguments (or on the
    # sanitizer's '{}' substitution). Validate BEFORE proceeding: unparseable
    # → retry the round transparently (classic-bucket budget), never execute.
    # A cut that left every arguments string parseable lost only the terminal
    # frames (JSON is self-delimiting) — proceeding is then provably safe.
    if (usage or {}).get('_missing_done'):
        from lib.agent_loop import unparseable_tool_calls
        _bad_tcs = unparseable_tool_calls(assistant_msg)
        if _bad_tcs:
            _trace_id = (usage or {}).get('trace_id', 'N/A')
            _bad_names = [(tc.get('function') or {}).get('name', '?')
                          for tc in _bad_tcs]
            if _premature_retry_count < _PREMATURE_RETRY_MAX_CLASSIC:
                _premature_retry_count += 1
                result['premature_retry_count'] = _premature_retry_count
                if '_premature_retry_count_phase' in task:
                    task['_premature_retry_count_phase'] = _premature_retry_count
                _backoff_s = _zero_byte_backoff_seconds(_premature_retry_count)
                logger.warning(
                    '[%s] ⚠️ TRUNCATED TOOL CALL at round %d: stream lost '
                    '[DONE] and %d tool call(s) have unparseable arguments '
                    '(%s) — the cut landed mid-arguments. Retrying (%d/%d) '
                    'after %.1fs backoff instead of executing corrupt calls. '
                    'M-TraceId=%s model=%s',
                    tid, round_num, len(_bad_tcs), _bad_names,
                    _premature_retry_count, _PREMATURE_RETRY_MAX_CLASSIC,
                    _backoff_s, _trace_id, model,
                )
                # Reset this round's partial text to the round base stamped by
                # stream_llm_response so the re-streamed attempt never stacks
                # on the poisoned one's tail. Record the discarded snapshot in
                # the FloorRetry residue list so the shrink-convergent
                # checkpoint/settle guards recognise it as our own discard
                # (exact byte-match) and allow the overwrite.
                with task['content_lock']:
                    _discarded_c = task['content']
                    _discarded_t = task['thinking']
                    _bc = task.get('_round_base_content')
                    _bt = task.get('_round_base_thinking')
                    if _bc is not None:
                        task['content'] = _bc
                    if _bt is not None:
                        task['thinking'] = _bt
                    _shrunk = (task['content'] != _discarded_c
                               or task['thinking'] != _discarded_t)
                if _shrunk:
                    _residue = task.setdefault('_floor_retry_residue', [])
                    if len(_residue) < 8:
                        _residue.append({'content': _discarded_c,
                                         'thinking': _discarded_t})
                append_event(task, build_event(
                    EventType.DELTA_RESET, roundNum=round_num, discard=True))
                emit_phase(task, Phase.RETRYING,
                           attempt=_premature_retry_count,
                           max=_PREMATURE_RETRY_MAX_CLASSIC,
                           bucket='truncated_tool_args',
                           backoff_s=round(_backoff_s, 2),
                           detail=(
                               f'⚠️ 网关断流截断了工具参数（{len(_bad_tcs)} 个调用），'
                               f'退避 {_backoff_s:.1f}s 后重试 '
                               f'({_premature_retry_count}/{_PREMATURE_RETRY_MAX_CLASSIC})…'
                           ))
                _interruptible_sleep(_backoff_s, task)
                result['action'] = 'continue'
                return result
            # Budget exhausted — honest terminal error (same shape as the
            # classic premature-close exhaustion: the turn-level auto-retry
            # may still re-run the whole turn from pristine input).
            result['action'] = 'break'
            result['last_finish_reason'] = 'premature_close'
            result['loop_exit_reason'] = (
                f'truncated_tool_args_retries_exhausted_round_{round_num}'
            )
            from lib.error_envelope import make_envelope as _make_env
            task['error'] = _make_env(
                'premature_close',
                detail=(f'Stream repeatedly cut mid-tool-arguments '
                        f'({len(_bad_tcs)} corrupt call(s): {_bad_names}); '
                        f'retries exhausted '
                        f'({_premature_retry_count}/{_PREMATURE_RETRY_MAX_CLASSIC}). '
                        f'M-TraceId={_trace_id}'),
                model=model,
                context=f'round-{round_num}',
                source='llm-stream',
                raw=(f'bucket=truncated_tool_args bad_calls={_bad_names} '
                     f'attempts={_premature_retry_count}/'
                     f'{_PREMATURE_RETRY_MAX_CLASSIC} M-TraceId={_trace_id}'),
            )
            logger.error(
                '[%s] ⚠️ TRUNCATED TOOL CALL retries exhausted at round %d '
                '(%d/%d). bad_calls=%s M-TraceId=%s model=%s — settling '
                'finishReason=premature_close with error envelope.',
                tid, round_num, _premature_retry_count,
                _PREMATURE_RETRY_MAX_CLASSIC, _bad_names, _trace_id, model,
            )
            return result

    # assistant_msg has tool_calls → proceed to tool execution (or check budget)
    return result
