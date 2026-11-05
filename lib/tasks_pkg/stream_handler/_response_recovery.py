"""Recovery of unusable text replies after primary terminal/transport policy.

Entry recover_response_artifacts handles the retained empty-stop case, canned
greetings and gateway-limit notices in their established order. It borrows
the task/result counter and existing text/event ports; no model dispatch or
new retry state belongs here. Normal progress clears the shared stall streak.
"""
import random

from lib.agent_core.events import EventType, Phase, build_event, emit_phase
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event, reset_task_text
from lib.tasks_pkg.stream_handler import _budget as retry_budget
from lib.tasks_pkg.stream_handler._budget import (
    _CANNED_GREETING_RETRY_MAX, _EMPTY_STOP_RETRY_MAX, _GATEWAY_LIMIT_RETRY_MAX,
)
from lib.tasks_pkg.stream_handler._canned_greeting import is_canned_greeting_reply

logger = get_logger(__name__)


def recover_response_artifacts(*, task, result, messages, usage, tid, model,
                               round_num, phase_retry_count, round_thinking,
                               round_content, trace_id, elapsed_ms, empty_stop,
                               is_zero_byte):
    """Return a claimed retry/failure, or None to continue classification."""
    # ── Empty-stop retry (model said finish_reason=stop with no
    #    content). Observed on GLM-5.1 (thinking-only response),
    #    MiniMax M2.5/M2.7, and Claude.  Cheap to retry once or
    #    twice; budget is shared with classic premature-close so
    #    a misbehaving turn can never burn more than 2 retries
    #    across both buckets. ──
    _is_empty_stop = (
        empty_stop
        and not round_content.strip()
        and not is_zero_byte
    )
    if (_is_empty_stop
            and phase_retry_count < _EMPTY_STOP_RETRY_MAX):
        phase_retry_count += 1
        result['premature_retry_count'] = phase_retry_count
        if '_premature_retry_count_phase' in task:
            task['_premature_retry_count_phase'] = phase_retry_count
        _backoff_s = 0.5 + random.uniform(0.0, 0.5)
        logger.warning(
            '[%s] ⚠️ EMPTY_STOP detected at round %d: '
            'finish=stop content=0 thinking=%dchars '
            'M-TraceId=%s elapsed=%.1fs model=%s '
            'Retrying (%d/%d) after %.1fs backoff…',
            tid, round_num, len(round_thinking),
            trace_id, elapsed_ms / 1000, model,
            phase_retry_count, _EMPTY_STOP_RETRY_MAX, _backoff_s,
        )
        emit_phase(task, Phase.RETRYING,
                   attempt=phase_retry_count,
                   max=_EMPTY_STOP_RETRY_MAX,
                   bucket='empty_stop',
                   backoff_s=round(_backoff_s, 2),
                   detail=(
                       f'⚠️ 模型空回复（{len(round_thinking)}字符思考但无正文），'
                       f'重试中 ({phase_retry_count}/{_EMPTY_STOP_RETRY_MAX})…'
                   ))
        retry_budget._interruptible_sleep(_backoff_s, task)
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
    if round_content.strip() and not _is_canned_greeting:
        retry_budget._clear_no_actionable_retry_streak(task)
    if (_is_canned_greeting
            and phase_retry_count < _CANNED_GREETING_RETRY_MAX):
        phase_retry_count += 1
        result['premature_retry_count'] = phase_retry_count
        if '_premature_retry_count_phase' in task:
            task['_premature_retry_count_phase'] = phase_retry_count
        _backoff_s = 1.0 + random.uniform(0.0, 1.0)
        logger.warning(
            '[%s] ⚠️ CANNED GREETING detected at round %d: finish=stop '
            'content=%dchars (%r) — a greeting opener incongruent with '
            'the conversation tail. M-TraceId=%s elapsed=%.1fs model=%s '
            'Retrying (%d/%d) after %.1fs backoff…',
            tid, round_num, len(round_content), round_content[:40],
            trace_id, elapsed_ms / 1000, model,
            phase_retry_count, _CANNED_GREETING_RETRY_MAX,
            _backoff_s,
        )
        # Drop the poisoned text BEFORE re-streaming. This is the ONLY
        #   retry bucket whose discarded round HAS content (zero-byte /
        #   classic / empty-stop all require empty content), so it is also
        #   the only one that must reset the accumulators — otherwise each
        #   attempt's greeting concatenates onto the last (2026-08-02
        #   triple-greeting bug). ``discard=True`` tells the client
        #   reducer to clear WITHOUT the tool-round prose-capture guard:
        #   this round issued no tool calls, so there is no batch to
        #   stamp onto and the freeze guard would keep the text forever.
        content_epoch = reset_task_text(task)
        append_event(task, build_event(
            EventType.DELTA_RESET, roundNum=round_num, discard=True,
            contentEpoch=content_epoch))
        emit_phase(task, Phase.RETRYING,
                   attempt=phase_retry_count,
                   max=_CANNED_GREETING_RETRY_MAX,
                   bucket='canned_greeting',
                   backoff_s=round(_backoff_s, 2),
                   detail=(
                       f'⚠️ 上游返回了与任务无关的模板问候（{len(round_content)}字符），'
                       f'重试中 ({phase_retry_count}/{_CANNED_GREETING_RETRY_MAX})…'
                   ))
        retry_budget._interruptible_sleep(_backoff_s, task)
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
            tid, round_num, phase_retry_count,
            _CANNED_GREETING_RETRY_MAX, round_content[:60],
            trace_id, model,
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

    # ── Gateway throttle notice returned as a normal stop completion ──
    # The gateway (CatPaw mcli) answered a rate-limited request with
    # HTTP 200 + finish_reason=stop whose whole body is the hourly-limit
    # notice (conv mtxxaf2l). finalize classified it GATEWAY_LIMIT_RESPONSE
    # (cools the slot) and stamped usage; here we RETRY so the cooled slot
    # rotates onto a healthy key instead of surfacing the notice as the
    # answer. Non-empty content, so like canned_greeting we must reset the
    # accumulators before re-streaming. Shares the per-phase counter.
    _is_gateway_limit = bool((usage or {}).get('_gateway_limit_response'))
    if (_is_gateway_limit
            and phase_retry_count < _GATEWAY_LIMIT_RETRY_MAX):
        phase_retry_count += 1
        result['premature_retry_count'] = phase_retry_count
        if '_premature_retry_count_phase' in task:
            task['_premature_retry_count_phase'] = phase_retry_count
        _backoff_s = 1.0 + random.uniform(0.0, 1.0)
        logger.warning(
            '[%s] ⚠️ GATEWAY LIMIT RESPONSE at round %d: finish=stop '
            'content=%dchars (%r) — a gateway throttle notice, not a '
            'model answer. M-TraceId=%s elapsed=%.1fs model=%s '
            'Retrying (%d/%d) after %.1fs backoff…',
            tid, round_num, len(round_content), round_content[:60],
            trace_id, elapsed_ms / 1000, model,
            phase_retry_count, _GATEWAY_LIMIT_RETRY_MAX, _backoff_s,
        )
        content_epoch = reset_task_text(task)
        append_event(task, build_event(
            EventType.DELTA_RESET, roundNum=round_num, discard=True,
            contentEpoch=content_epoch))
        emit_phase(task, Phase.RETRYING,
                   attempt=phase_retry_count,
                   max=_GATEWAY_LIMIT_RETRY_MAX,
                   bucket='gateway_limit',
                   backoff_s=round(_backoff_s, 2),
                   detail=(
                       f'⚠️ 网关返回了限流提示而非模型回复，正在切换密钥重试 '
                       f'({phase_retry_count}/{_GATEWAY_LIMIT_RETRY_MAX})…'
                   ))
        retry_budget._interruptible_sleep(_backoff_s, task)
        result['action'] = 'continue'
        return result

    if _is_gateway_limit:
        # Budget exhausted — a throttle notice is never a legitimate
        # answer (unlike canned_greeting), so fail honestly instead of
        # delivering it. Mirrors tool_calls_no_payload exhaustion.
        result['action'] = 'break'
        result['last_finish_reason'] = 'premature_close'
        result['loop_exit_reason'] = (
            f'gateway_limit_retries_exhausted_round_{round_num}'
        )
        from lib.error_envelope import make_envelope as _make_env
        task['error'] = _make_env(
            'premature_close',
            detail=('Gateway returned a rate-limit notice as a normal '
                    'stop completion and the retry budget was exhausted '
                    f'({phase_retry_count}/{_GATEWAY_LIMIT_RETRY_MAX}). '
                    f'content={round_content[:80]!r} M-TraceId={trace_id}'),
            model=model,
            context=f'round-{round_num}',
            source='llm-stream',
            raw=(f'bucket=gateway_limit attempts={phase_retry_count}/'
                 f'{_GATEWAY_LIMIT_RETRY_MAX} M-TraceId={trace_id}'),
        )
        logger.error(
            '[%s] ⚠️ GATEWAY LIMIT retries exhausted at round %d (%d/%d). '
            'content=%r M-TraceId=%s model=%s — settling '
            'finishReason=premature_close with error envelope.',
            tid, round_num, phase_retry_count,
            _GATEWAY_LIMIT_RETRY_MAX, round_content[:60], trace_id, model,
        )
        return result

    return None
