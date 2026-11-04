"""Finalize successful and failed agent runs after the execution loop.

**Extraction context** (board epic ```, slice 6):

Two functions consolidated here, both extracted from the tail of
``run_task``:

  1. :func:`finalize_after_loop` — the SUCCESS-PATH post-loop tail:
     - Append the final assistant reply to messages when it wasn't
       already appended (prose-only round path, no tool_calls).
     - Emit RENDER_CONTRACT terminal ROUND_END + final MESSAGES_SNAPSHOT
       event. The final reply is not submitted for incremental
       translation: the durable whole-turn job translates it once after
       settlement (``lib.translate.terminal``).
     - Write updated messages back to ``task['messages']`` so Flow callers
       see the complete conversation.
     - Dispatch to ``_finalize_and_emit_done`` (which emits the DONE
       event + persists + runs autopilot inline).

  2. :func:`handle_task_fatal` — the FATAL-PATH exception handler that
     used to live in the ``except Exception as e:`` block below the
     try body:
     - Extract the user-friendly error message.
     - Turn-level auto-retry (transient first-round errors).
     - Terminal DONE + persist for real errors.

Both functions are pure sinks — they take everything as explicit
parameters, so their signatures are long but the coupling is
observable and testable. Each fault path INSIDE these helpers has
its own defensive try/except so no single failure escapes into the
caller's finally block (that's ``_teardown.finalize_task_lane``, slice 5).

Kept SEPARATE from ``_finalize.py`` (which owns the DONE-event
composition helpers ``_finalize_and_emit_done`` /
``_maybe_auto_retry_turn`` / …) because ``_finalize.py`` is a
LEAF-helper module that ``_post_loop.py`` CALLS INTO; putting these
extraction helpers there would create a circular purpose (compose vs
dispatch).
"""

from __future__ import annotations

from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.assistant_messages import append_assistant_prose_message
from lib.tasks_pkg.failure_attribution import attach_failure_attribution
from lib.tasks_pkg.manager import (
    append_event,
    persist_task_result,
)
from lib.tasks_pkg.orchestrator._finalize import (
    _finalize_and_emit_done,
    _finalize_dangling_tool_rounds,
    _maybe_auto_retry_turn,
)
from lib.tasks_pkg.invocation import is_child_invocation

logger = get_logger(__name__)

# Rendered verbatim: the envelope's ``permission`` kind would otherwise blame
# a provider credential, while this refusal happens before any provider call.
_MODEL_ADMISSION_TITLE = ('⚠️ 模型请求在发送前被授权检查拒绝，任务未完成\n'
                          'Model request was refused by an authority check before it was sent')
_MODEL_ADMISSION_HINT = (
    '解决办法 / How to fix:\n'
    '• 这不是 Key / 配额 / 模型问题：请求没有发往任何服务商，因此不会换 Key 或回退到其他模型。'
    '展开下方详情可查看拒绝原因。\n'
    '• 该请求所依赖的授权已变化（例如授权它的消息被编辑、请求被停止或项目绑定变更）；'
    '重新发送或重新生成即可在当前授权下开始新请求。\n\n'
    '• This is NOT a key / quota / model problem: no provider was contacted, so no other '
    'key or model was tried. Expand the detail for the reason.\n'
    '• An authority this request depends on changed (for example the message that '
    'authorized it was edited, the request was stopped, or the project binding changed); '
    'send or regenerate to start a new request under the current authority.')


def finalize_after_loop(
    task: dict[str, Any],
    *,
    cfg: dict[str, Any],
    tid: str,
    model: str,
    preset: str,
    thinking_depth: Any,
    thinking_enabled: bool,
    temperature: Any,
    max_tokens: Any,
    messages: list,
    original_messages: list,
    tool_list: list | None,
    assistant_msg: dict | None,
    round_num: int,
    accumulated_usage: dict,
    api_rounds: list,
    last_finish_reason: Any,
    last_usage: Any,
    tool_call_happened: bool,
    all_search_results_text: list,
    project_path: str,
    project_enabled: bool,
    loop_exit_reason: str,
    abort_detected_phase: Any,
    last_stream_result: Any = None,
    terminal_assistant_content: str | None = None,
) -> None:
    """Post-loop success tail — see module docstring.

    Every side effect is derived from the task and its authoritative
    turn-native transcript; no process-local conversation mirror is written.
    """
    # ── Append final assistant reply to messages if it wasn't already ──
    # When the LLM returns text content WITHOUT tool_calls, the loop
    # breaks before appending the assistant message (tool_calls path is
    # the only place messages.append(clean_msg) happens). Without this,
    # _run_single_turn returns messages missing the assistant's reply,
    # and a Flow critic never sees the worker's output.
    if assistant_msg and not assistant_msg.get('tool_calls'):
        # RENDER_CONTRACT Phase 3: explicit round-end for the TERMINAL
        #   round — the model finished with prose and no tool calls
        #   (this is the prose-only / final-answer round that never
        #   issued a round_end via the tools path). round_num is the
        #   final round index; a `done` still follows. Emitted before
        #   the message-append bookkeeping so the boundary lands even
        #   when the reply is empty.
        from lib.tasks_pkg.tool_delivery import delivery_round_closed
        if not delivery_round_closed(task, round_num):
            append_event(task, build_event(EventType.ROUND_END,
                                           roundNum=round_num, reason='final'))
        _final_content = assistant_msg.get('content') or ''
        _final_reasoning = assistant_msg.get('reasoning_content') or ''
        _final_assistant = append_assistant_prose_message(
            messages, assistant_msg, task=task)
        if _final_assistant is not None:
            logger.debug('[%s] Appended final assistant reply to messages '
                         '(%d content chars, %d reasoning chars)',
                         tid, len(_final_content), len(_final_reasoning))
            from lib.tasks_pkg.orchestrator._completion_projection import publish_final_messages_snapshot
            publish_final_messages_snapshot(task, messages, model=model, tid=tid,
                tools=tool_list, publish=append_event)

    # ── Write back updated messages to task so callers (e.g.
    #    _run_single_turn → Flow runner) can access the complete
    #    conversation including assistant replies and tool results.
    #    Without this, task['messages'] still holds the PRE-run_task
    #    snapshot, and the Flow critic never sees the worker's
    #    output.
    task['messages'] = messages
    task['_messages_include_current_run'] = True

    # ── Post-loop finalization: fallback, done event, persist ──
    _finalize_and_emit_done(
        task,
        model=model, preset=preset, thinking_depth=thinking_depth, cfg=cfg,
        last_finish_reason=last_finish_reason, last_usage=last_usage,
        last_stream_result=last_stream_result,
        accumulated_usage=accumulated_usage, api_rounds=api_rounds,
        tool_call_happened=tool_call_happened, messages=messages,
        original_messages=original_messages,
        all_search_results_text=all_search_results_text,
        max_tokens=max_tokens, thinking_enabled=thinking_enabled,
        temperature=temperature,
        _loop_exit_reason=loop_exit_reason,
        _abort_detected_phase=abort_detected_phase,
        project_path=project_path, project_enabled=project_enabled,
        round_num=round_num,
        assistant_msg=assistant_msg,
        terminal_assistant_content=terminal_assistant_content,
    )
    # ── Autopilot now runs INSIDE _finalize_and_emit_done (before the
    #    done SSE event is emitted), so its result can ride on the same
    #    event. No standalone hook here.


def retain_failure_messages(task: dict[str, Any], messages: list | None) -> None:
    """Preserve the detached working history before terminal capture, without replay.

    Early preparation may fail before a working history exists. Otherwise use
    the exact history still retained by the loop, including settled tool output;
    this does not recover earlier compaction or tool-policy clipping.
    """
    if messages is not None:
        task['messages'] = messages
        task['_messages_include_current_run'] = True


def handle_task_fatal(task: dict[str, Any], e: Exception, *, allow_retry: bool = True) -> bool:
    """FATAL-PATH exception handler.

    Extracts the user-friendly error, then tries THREE recovery paths
    in order:

      1. Flow-managed short-circuit — return early so the Flow runner handles
         the error (caller checks the return value).
      2. Turn-level auto-retry — transient first-round errors (429 /
         net reset) re-run the whole turn transparently instead of
         surfacing to the user.
      3. Recovery-carrier internal-FATAL preserve — a killed-recovery
         carrier that fatalled from a RECOVERY-INTERNAL cause is
         re-stamped as 'killed' so a calm boot retries the turn,
         bounded by the same per-turn attempt cap (never a loop).

    Falls through to the normal terminal-error persist for real
    model errors (ratelimit / quota / permission / prompt_too_long / …).

    Returns:
        True if the caller (run_task) should ``return`` early without
        re-raising (Flow-managed, retry-in-progress, recovery-carrier
        re-stamp fired). False if the caller should proceed to the
        terminal-DONE + persist path (fall-through).

    NOTE: the caller is responsible for calling this from INSIDE an
    ``except Exception:`` block so ``e`` is bound. This helper does
    NOT re-raise — the caller does not need to either. The
    non-Exception ``BaseException`` handler lives in
    :func:`handle_task_base_exception` below (slice 34).
    """
    _tid8 = (task.get('id', '?') or '?')[:8]
    from lib.llm.llm_errors import (
        AbortedError, AdmissionRefused, ModelAdmissionRefused,
    )
    from lib.tasks_pkg.event_delivery import EventDeliveryCancelled
    from lib.turn_verdict import (
        DISPATCH_AUTHORITY_ABORT_REASONS, TurnStatus, derive_task_verdict,
        dispatch_authority_stop_detail,
    )
    # An admission refusal halts work through the abort lane, but the turn
    # failed for a stated reason; it must not settle as a silent user Stop.
    _admission_refused = isinstance(e, AdmissionRefused)
    _cancelled_delivery = (isinstance(e, (AbortedError, EventDeliveryCancelled))
                           and not _admission_refused)

    if _cancelled_delivery and task.get('_chat_terminal_stamped'):
        # Late cancelled observers cannot replace an acknowledged candidate;
        # existing terminal debt/retry retains ownership of its publication.
        return True
    _interrupted = _cancelled_delivery and derive_task_verdict(
        task, {'type': 'aborted', 'finishReason': 'aborted'}
    ).status is TurnStatus.INTERRUPTED
    _terminal_status = 'aborted' if _interrupted else 'error'
    _terminal_finish = 'aborted' if _interrupted else 'error'
    if _interrupted:
        logger.info('[Orchestrator] event delivery cancelled task=%s', _tid8)
    else:
        logger.error('[Orchestrator] run_task FATAL error task=%s', _tid8, exc_info=True)

    # Prefer the user-friendly message attached by _llm_call_with_fallback;
    # otherwise format the raw exception here so the frontend error-block
    # always tells the user how to recover.
    from lib.tasks_pkg.event_delivery import EventDeliveryError, EventDeliveryRevoked
    _delivery_failure = isinstance(e, (EventDeliveryError, EventDeliveryRevoked))
    _user_err = None if _delivery_failure else (
        task.get('error') if task.get('_invocationStopReason') in {'stalled', 'deadline'}
        else getattr(e, '_user_message', None))
    if _delivery_failure:
        from lib.error_envelope import from_exception
        _user_err = from_exception(
            e, context='task-fatal', source='task-event-delivery')
    # A dispatch authority stop is stamped where the authority failed; it
    # names the turn's cause whichever exception carried the halt here.
    _authority_stop = str(task.get('_abort_reason') or '')
    if _authority_stop not in DISPATCH_AUTHORITY_ABORT_REASONS:
        _authority_stop = ''
    if not _user_err and not _authority_stop and isinstance(e, ModelAdmissionRefused):
        from lib.error_envelope import make_envelope as _make_env
        _user_err = _make_env(
            'permission', message=_MODEL_ADMISSION_TITLE, hint=_MODEL_ADMISSION_HINT,
            detail=f'Refused before any provider was contacted: {e}',
            model=task.get('config', {}).get('model', ''),
            context='model-admission', source='orchestrator', raw=str(e),
            retryable=False)
    if not _user_err and (_admission_refused or _authority_stop):
        from lib.error_envelope import make_envelope as _make_env
        if _admission_refused:
            _refused_tool = f'`{e.tool_name}`' if e.tool_name else 'A tool call'
            _detail = f'{_refused_tool} was refused before execution: {e}'
        else:
            _provenance = str(task.get('_abortReasonDetail') or '')
            _detail = dispatch_authority_stop_detail(_authority_stop, _provenance)
            if str(e) not in _provenance:
                _detail = f'{_detail} Refusal: {e}'
        _user_err = _make_env(
            _authority_stop or 'tool_call_rejected',
            detail=_detail,
            model=task.get('config', {}).get('model', ''),
            context='tool-admission' if _admission_refused else 'dispatch-authority',
            source='orchestrator', raw=str(e),
            retryable=False)
    if not _user_err and isinstance(e, ImportError):
        from lib.error_envelope import make_envelope as _make_env
        _user_err = _make_env(
            'internal', detail=f'Task dependency failed: {e}',
            model=task.get('config', {}).get('model', ''),
            context='task-fatal', source='orchestrator', raw=str(e))
    if not _user_err:
        try:
            from lib.llm.llm_error_format import format_llm_error_for_user
            _user_err = format_llm_error_for_user(
                e, model=task.get('config', {}).get('model', ''),
                context='task-fatal', source='orchestrator')
        except Exception as _fmt_err:
            logger.warning('[Orchestrator] format_llm_error_for_user failed: %s', _fmt_err)
            from lib.error_envelope import make_envelope as _make_env
            _user_err = _make_env(
                'internal',
                detail=f'Task fatal: {e}',
                model=task.get('config', {}).get('model', ''),
                context='task-fatal',
                source='orchestrator',
                raw=str(e),
            )
    # Physical cancellation may be a guard/fence failure; only the shared
    # verdict authorizes clearing a failure diagnostic for interruption.
    if _interrupted:
        _user_err = None
        task['aborted'] = True
    elif _cancelled_delivery and task.get('error'):
        _user_err = task['error']
    task['error'] = (None if _interrupted else attach_failure_attribution(
        _user_err, task, phase='task-fatal'))
    task['status'] = _terminal_status
    task['finishReason'] = _terminal_finish

    try:
        from lib.tasks_pkg.compaction._settlement import settle_compaction_usage
        settle_compaction_usage(task)
    except Exception as exc:
        logger.warning('[Usage] fatal compaction settlement failed: %s', exc)

    # ── Turn-level auto-retry (raise path) ──
    # A transient first-round error (e.g. a 429 with no fallback, or a
    # network reset before any tool ran) RAISES past
    # _finalize_and_emit_done to here rather than error-breaking, so it
    # would otherwise never reach the finalize-seam auto-retry guard.
    # Apply the same self-heal here: if the classified error is
    # transient and the budget remains, re-run the whole turn
    # transparently instead of surfacing it for a manual click.
    if allow_retry and not task.get('aborted') and not _delivery_failure:
        try:
            _rt_cfg = task.get('config') or {}
            if _maybe_auto_retry_turn(task, _rt_cfg):
                return True
        except Exception as _ar_err:
            logger.warning('[Orchestrator] fatal-path auto-retry check '
                           'failed (surfacing original error): %s', _ar_err,
                           exc_info=True)

    if task.get('_flow_managed') and not callable(task.get('_invocationEventSink')):
        return True  # Legacy graph carrier owns terminal projection, not retries.

    # A required presentation port can be unusable without revoking execution
    # resource settlement. This is a port failure, not a creator-based policy.
    if task.get('_invocationDeliveryError') is not None:
        _settle_fatal_tool_rounds(task)
        from lib.tasks_pkg.manager import stamp_chat_task_terminal
        from lib.agent_core.execution_session import settle_task_execution
        stamp_chat_task_terminal(task, status=_terminal_status, finish_reason=_terminal_finish)
        settle_task_execution(task, event={'type': 'done', 'error': task['error'], 'finishReason': _terminal_finish})
        return True

    # Fall-through: terminal-DONE + persist path.
    _settle_fatal_tool_rounds(task)
    from lib.tasks_pkg.manager import stamp_chat_task_terminal
    try:
        stamp_chat_task_terminal(task, status=_terminal_status, finish_reason=_terminal_finish)
        append_event(task, build_event(
            EventType.DONE, finishReason=_terminal_finish,
            **({'error': task['error']} if task['error'] else {})))
    except Exception as terminal_error:
        # The terminal lane may be the very lane that failed. Its later
        # publication error is secondary, never the task's original cause.
        logger.error('[Orchestrator] fatal terminal delivery failed task=%s: %s',
                     _tid8, type(terminal_error).__name__, exc_info=True)
    try:
        if is_child_invocation(task):
            from lib.agent_core.terminal_delivery import deliver_terminal
            try:
                deliver_terminal(task)
            except Exception as error:
                task['_terminalPersistencePending']=True
                task['_terminalPersistenceRetryReady']=True
                task['_invocationDeliveryError']=error
                raise
            return True
        elif callable(task.get('_invocationEventSink')):
            persist_task_result(task, _defer_heavy_release=True)
        else:
            persist_task_result(task)
    except Exception as persistence_error:
        logger.error('[Orchestrator] fatal result persistence failed task=%s: %s',
                     _tid8, type(persistence_error).__name__, exc_info=True)
    return False


def _settle_fatal_tool_rounds(task: dict[str, Any]) -> None:
    """Use the normal terminal sweep before fatal DONE/persistence as well.

    Failed response announcements are display evidence, never adopted calls.
    Recovery paths that continue execution retain ownership of their rounds.
    """
    try:
        _finalize_dangling_tool_rounds(task)
    except Exception as error:
        logger.warning('[%s] fatal tool-round sweep failed: %s',
                       str(task.get('id', '?'))[:8], type(error).__name__,
                       exc_info=True)


def handle_task_base_exception(task: dict[str, Any], be: BaseException) -> None:
    """Non-Exception fatal handler (KeyboardInterrupt / SystemExit /
    asyncio.CancelledError — all derive from BaseException, NOT
    Exception, so they slip past :func:`handle_task_fatal`).

    Extracted 2026-08-01 ( slice 34) from run_task's
    ``except BaseException as be:`` branch. Without this path the task
    would stay NON-TERMINAL forever — stranding its admission slot AND
    (on the headless API) its billing reservation until the slot TTL /
    janitor reclaims them. Emits the terminal DONE(error) so the
    terminal-callback chain (release slot + settle billing via
    on_terminal) still fires, then the CALLER re-raises to preserve the
    cancel/shutdown semantics.

    Contract: ERROR log first; stamp the internal envelope
    ('Task terminated: <Type>'); DONE event + persist ONLY when NOT
    Flow-managed; every finalize step wrapped so a failure there
    logs but never masks the original BaseException. This function
    never returns normally — it always raises ``be`` after the
    best-effort finalize.
    """
    try:
        from lib.tasks_pkg.compaction._settlement import settle_compaction_usage
        settle_compaction_usage(task)
    except Exception as exc:
        logger.warning('[Usage] cancelled compaction settlement failed: %s', exc)
    logger.error('[Orchestrator] run_task FATAL BaseException task=%s: %s',
                 task.get('id', '?')[:8], type(be).__name__, exc_info=True)
    try:
        from lib.error_envelope import make_envelope as _make_env
        task['error'] = attach_failure_attribution(
            _make_env(
                'internal', detail=f'Task terminated: {type(be).__name__}',
                model=task.get('config', {}).get('model', ''),
                context='task-fatal-base', source='orchestrator', raw=str(be)),
            task, phase='task-fatal-base')
        task['status'] = 'error'
        task['finishReason'] = 'error'
        if not task.get('_flow_managed'):
            from lib.tasks_pkg.manager import stamp_chat_task_terminal
            stamp_chat_task_terminal(task, status='error', finish_reason='error')
            append_event(task, build_event(
                EventType.DONE, error=task['error'], finishReason='error'))
            if not is_child_invocation(task):
                persist_task_result(task)
    except Exception as _fin_err:
        logger.error('[Orchestrator] BaseException terminal-finalize failed '
                     'task=%s: %s', task.get('id', '?')[:8], _fin_err,
                     exc_info=True)
    raise be


__all__ = ['finalize_after_loop', 'retain_failure_messages', 'handle_task_fatal', 'handle_task_base_exception']
