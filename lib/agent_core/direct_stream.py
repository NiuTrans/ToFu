"""Native async single-turn LLM relay behind the HTTP adapter.

This application execution owner is the only non-task-backed model stream. It
uses the shared ExecutionSession for deadlines, cancellation, and terminal
resource invariants; HTTP routes only parse/authenticate and bind resources.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from concurrent.futures import Future

from lib.agent_core.execution_session import ExecutionPhase, ExecutionSession
from lib.error_envelope import make_envelope
from lib.llm.stream_result import ProviderStreamResult, ensure_provider_stream_result
from lib.log import get_logger
from lib.turn_verdict import TerminalTaskFailure, derive_provider_stream_verdict


logger = get_logger(__name__)


def _openai_finish_reason(raw_finish: str) -> str:
    mapping = {
        'stop': 'stop', 'done': 'stop', 'completed': 'stop',
        'end_turn': 'stop', 'length': 'length', 'max_tokens': 'length',
        'context_length': 'length', 'budget_exceeded': 'length',
        'incomplete': 'length', 'max_turns': 'length',
        'aborted': 'length', 'interrupted': 'length',
        'tool_calls': 'tool_calls', 'tool_use': 'tool_calls',
        'function_call': 'function_call', 'content_filter': 'content_filter',
    }
    try:
        return mapping[raw_finish]
    except KeyError as exc:
        raise ValueError(
            f'unsupported verified finish reason: {raw_finish!r}') from exc


# Sentinel pushed onto the bridge queue when the dispatch task finishes.
_STREAM_END = object()
_RELAY_FAILURE = object()

_DIRECT_DEFAULT_TIMEOUT_S = 600
_DIRECT_MAX_TIMEOUT_S = 900


class _DirectRelayFailure(RuntimeError):
    """Internal relay failure with a stable, non-sensitive public category."""

    def __init__(self, *, cause: str, envelope_kind: str) -> None:
        self.cause = cause
        self.envelope_kind = envelope_kind
        super().__init__(cause)

# Strong references for direct dispatches whose HTTP consumer disconnected.
# Each started request retains its original execution owner; completed
# tasks remove themselves immediately.  No response chunks are retained after
# detach, so a slow/absent frontend cannot grow this set's per-task memory.
_DETACHED_DIRECT_DISPATCHES: set[asyncio.Task] = set()


async def _join_owned_io(awaitable):
    """Finish one accepted I/O operation before propagating cancellation."""
    pending = asyncio.ensure_future(awaitable)
    cancelled = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
    return pending.result(), cancelled


def _retain_detached_dispatch(task: asyncio.Task, *, completion_id: str) -> None:
    """Keep a disconnected upstream dispatch alive and observe its verdict."""
    if task in _DETACHED_DIRECT_DISPATCHES:
        return
    _DETACHED_DIRECT_DISPATCHES.add(task)

    def _settled(done: asyncio.Task) -> None:
        _DETACHED_DIRECT_DISPATCHES.discard(done)
        if done.cancelled():
            logger.info('[chat_direct] detached dispatch cancelled by runtime '
                        'shutdown completion=%s', completion_id[:80])
            return
        try:
            result = ensure_provider_stream_result(done.result())
            if not result.is_verified_complete:
                verdict = derive_provider_stream_verdict(result)
                logger.warning(
                    '[chat_direct] detached dispatch ended incomplete '
                    'completion=%s cause=%s state=%s',
                    completion_id[:80], verdict.cause, result.state.value,
                )
                return
            logger.info('[chat_direct] detached dispatch completed faithfully '
                        'completion=%s', completion_id[:80])
        except Exception as error:
            logger.warning(
                '[chat_direct] detached dispatch failed completion=%s type=%s',
                completion_id[:80], type(error).__name__)

    task.add_done_callback(_settled)


def _chunk_frame(completion_id: str, model: str, *, role=False, content=None,
                 thinking=None) -> str:
    """One OpenAI ``chat.completion.chunk`` SSE frame."""
    delta: dict = {}
    if role:
        delta['role'] = 'assistant'
    if content is not None:
        delta['content'] = content
    if thinking is not None:
        delta['reasoning_content'] = thinking
    chunk = {
        'id': completion_id,
        'object': 'chat.completion.chunk',
        'created': int(time.time()),
        'model': model,
        'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}],
    }
    return f'data: {json.dumps(chunk, ensure_ascii=False)}\n\n'


async def run_direct_stream(messages, *, model, cfg, completion_id,
                            dispatch_fn=None, queue_maxsize=1000,
                            on_dispatch_started=None,
                            before_dispatch=None, model_budget_admit=None,
                            on_dispatch_settled=None,
                            owner_user_id: int | None = None,
                            pinned_provider_id: str = '',
                            dispatch_timeout_s: int = _DIRECT_DEFAULT_TIMEOUT_S,
                            max_429_attempts: int | None = None,
                            execution_session: ExecutionSession | None = None,
                            dispatch_terminal: dict | None = None,
                            terminal_metadata: dict | None = None,
                            client_tools: list | None = None,
                            client_tool_choice=None):
    """Async generator: drive an on-loop streaming dispatch → SSE frames.

    This is the TESTABLE CORE — ``dispatch_fn`` is injectable so tests can
    stub the LLM. In production it defaults to
    ``lib.llm_dispatch.async_dispatch_stream``.

    Contract of ``dispatch_fn`` (mirrors ``async_dispatch_stream``):
        ``await dispatch_fn(messages, on_content=..., on_thinking=...,
        max_tokens=..., temperature=..., prefer_model=..., capability=...,
        log_prefix=...)`` → ``ProviderStreamResult``; the sync
        ``on_content(str)`` / ``on_thinking(str)`` callbacks fire ON the loop.

    A historical three-tuple is accepted only at the named adapter seam. Its
    explicit finish reason is converted to the same typed result before any
    terminal decision is made.

    Yields OpenAI ``chat.completion.chunk`` SSE frames, then a terminal frame
    (with ``finish_reason`` + ``usage``), then ``data: [DONE]``.

    The queue owns ordered references. Output bursts spill to anonymous JSON
    files instead of terminating accepted dispatch or dropping text. Actual
    spool failures remain typed terminal errors. Provider callbacks may arrive from an adapter worker thread, so they
    cross one loop-owned scheduling seam before touching the queue.

    Transparent provider retry remains possible before the observer sees a
    delta. After any delta is visible, an attempt restart terminates this SSE
    with a typed error because OpenAI-compatible SSE has no retraction frame.
    """
    if dispatch_fn is None:
        from lib.llm_dispatch import async_dispatch_stream as dispatch_fn

    if queue_maxsize < 1:
        raise ValueError('queue_maxsize must be positive')
    timeout_s = max(1, min(_DIRECT_MAX_TIMEOUT_S, int(dispatch_timeout_s)))
    from lib.production.llm_policy import production_llm_dispatch_kwargs
    dispatch_policy = production_llm_dispatch_kwargs(
        max_429_attempts=max_429_attempts)
    if model_budget_admit is not None:
        dispatch_policy['model_budget_admit'] = model_budget_admit
    # Declarations describe a caller-owned execution surface. This service
    # has no tool executor, prefetcher, approvals or continuation loop.
    if client_tools is not None:
        dispatch_policy['tools'] = client_tools
        dispatch_policy['generation_options'] = {
            wire: cfg[key] for key, wire in (
                ('topP', 'top_p'), ('seed', 'seed'), ('stop', 'stop'),
                ('responseFormat', 'response_format'), ('user', 'user'),
                ('parallelToolCalls', 'parallel_tool_calls'),
            ) if key in cfg
        }
    if client_tool_choice is not None:
        dispatch_policy['tool_choice'] = client_tool_choice

    q: asyncio.Queue = asyncio.Queue()
    frame_store = None
    loop = asyncio.get_running_loop()
    loop_thread_id = threading.get_ident()
    consumer_attached = True
    emitted_delta = False
    abort_requested = False
    relay_failure: _DirectRelayFailure | None = None
    deadline = time.monotonic() + timeout_s
    dispatch_terminal = dispatch_terminal if dispatch_terminal is not None else {}
    terminal_metadata = terminal_metadata if terminal_metadata is not None else {}

    def _abort_check() -> bool:
        return (
            abort_requested
            or (execution_session is not None
                and execution_session.cancel_requested)
            or time.monotonic() >= deadline
        )

    def _fail_relay(*, cause: str, envelope_kind: str) -> None:
        nonlocal abort_requested, relay_failure
        if relay_failure is not None:
            return
        abort_requested = True
        relay_failure = _DirectRelayFailure(
            cause=cause, envelope_kind=envelope_kind)
        q.put_nowait((_RELAY_FAILURE, None))

    def _clear_unobserved_attempt() -> None:
        while True:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _push(kind: str, text: str) -> None:
        nonlocal frame_store
        if not consumer_attached or relay_failure is not None:
            return
        try:
            if q.qsize() < queue_maxsize:
                q.put_nowait((kind, text))
            else:
                if frame_store is None:
                    from tofu_workers.json_frames import JsonFrameStore
                    from lib.agent_core.push_policy import resolve_push_budget
                    budget = resolve_push_budget()
                    frame_store = JsonFrameStore(
                        segment_bytes=budget.spool_segment_bytes,
                        reuse_workers=budget.worker_reuse_target,
                        thread_name_prefix='tofu-direct-relay')
                q.put_nowait((kind, frame_store.prepare({'text': text})))
        except Exception:
            _fail_relay(cause='relay_spool_io_failed', envelope_kind='internal')

    def _schedule_on_loop(callback, *args) -> None:
        if threading.get_ident() == loop_thread_id:
            callback(*args)
        else:
            loop.call_soon_threadsafe(callback, *args)

    def _on_content(c):
        if c:
            _schedule_on_loop(_push, 'content', c)

    def _on_thinking(t):
        if t:
            _schedule_on_loop(_push, 'thinking', t)

    def _attempt_restart_on_loop(reason: str) -> None:
        if not consumer_attached:
            return
        if emitted_delta:
            _fail_relay(
                cause='provider_attempt_restarted_after_output',
                envelope_kind='upstream_error',
            )
            return
        _clear_unobserved_attempt()
        logger.debug('[chat_direct] discarded unobserved provider attempt: %s',
                     str(reason)[:120])

    def _on_attempt_restart(*, reason=''):
        _schedule_on_loop(_attempt_restart_on_loop, str(reason))

    async def _drive():
        result = None
        try:
            if before_dispatch is not None:
                _, cancelled = await _join_owned_io(before_dispatch())
                if cancelled:
                    raise asyncio.CancelledError
            if callable(on_dispatch_started):
                try:
                    on_dispatch_started()
                except Exception as e:
                    logger.debug('[chat_direct] dispatch-start observer failed: %s', e)
            if execution_session is not None:
                execution_session.mark_dispatch_started()
            from lib.llm_dispatch.provider_pin import provider_pin
            with provider_pin(pinned_provider_id):
                result = await asyncio.wait_for(
                    dispatch_fn(
                        messages,
                        on_content=_on_content,
                        on_thinking=_on_thinking,
                        on_attempt_restart=_on_attempt_restart,
                        max_tokens=int(cfg.get('maxTokens') or 4096),
                        temperature=float(cfg.get('temperature') or 0),
                        thinking_enabled=cfg.get('thinkingEnabled'),
                        thinking_depth=cfg.get('thinkingDepth') or None,
                        preset=cfg.get('preset') or 'medium',
                        effort=cfg.get('effort'),
                        capability=cfg.get('capability') or 'text',
                        prefer_model=model or None,
                        strict_model=bool(model),
                        owner_user_id=owner_user_id,
                        abort_check=_abort_check,
                        log_prefix='[chat_direct]',
                        **dispatch_policy,
                    ),
                    timeout=timeout_s,
                )
            result = ensure_provider_stream_result(result)
            dispatch_terminal['result'] = result
            return result
        except BaseException as exc:
            dispatch_terminal['exception'] = exc
            raise
        finally:
            # Cross-thread callbacks were scheduled before an adapter future
            # settled. Give the loop one turn to apply them before the sentinel.
            await asyncio.sleep(0)
            # Always unblock the consumer, even on dispatch error.
            if callable(on_dispatch_settled):
                try:
                    settlement = on_dispatch_settled()
                    if asyncio.iscoroutine(settlement):
                        await settlement
                except Exception as e:
                    logger.error(
                        '[chat_direct] dispatch-settle observer failed type=%s',
                        type(e).__name__)
                    if (execution_session is not None
                            and not execution_session.is_terminal):
                        dispatch_terminal['execution_receipt'] = (
                            execution_session.settle(
                                ExecutionPhase.FAILED,
                                cause='dispatch_settlement_failed',
                            )
                        )
            if (execution_session is not None
                    and not execution_session.is_terminal):
                # A production owner must leave one terminal receipt. A
                # missing/no-op settlement callback cannot authorize success.
                dispatch_terminal['execution_receipt'] = (
                    execution_session.settle(
                        ExecutionPhase.FAILED,
                        cause='dispatch_settlement_missing',
                    )
                )
            await q.put((_STREAM_END, None))

    drive_task = asyncio.ensure_future(_drive())

    emitted_role = False
    dispatch_result_observed = False
    try:
        err = None
        failure_cause = 'generation_error'
        envelope_kind = 'internal'
        failure_detail = ''
        while True:
            kind, text = await q.get()
            if isinstance(text, Future):
                try:
                    stored = await asyncio.wrap_future(text)
                    payload = await asyncio.wrap_future(frame_store.read(stored))
                    text = payload['text']
                    stored = payload = None
                except Exception:
                    _fail_relay(cause='relay_spool_io_failed', envelope_kind='internal')
            if relay_failure is not None or kind is _RELAY_FAILURE:
                err = relay_failure or _DirectRelayFailure(
                    cause='relay_failure', envelope_kind='upstream_error')
                failure_cause = err.cause
                envelope_kind = err.envelope_kind
                drive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await drive_task
                dispatch_result_observed = True
                break
            if kind is _STREAM_END:
                break
            if not emitted_role:
                emitted_delta = True
                yield _chunk_frame(completion_id, model, role=True)
                emitted_role = True
            if kind == 'content':
                yield _chunk_frame(completion_id, model, content=text)
            elif kind == 'thinking':
                yield _chunk_frame(completion_id, model, thinking=text)

        # Dispatch finished — surface its result (finish_reason + usage) or error.
        stream_result: ProviderStreamResult | None = None
        if err is None:
            try:
                dispatch_result_observed = True
                stream_result = ensure_provider_stream_result(drive_task.result())
                if not stream_result.is_verified_complete:
                    verdict = derive_provider_stream_verdict(stream_result)
                    raise TerminalTaskFailure(verdict)
                execution_receipt = dispatch_terminal.get('execution_receipt')
                if (execution_receipt is not None
                        and execution_receipt.outcome is not ExecutionPhase.COMPLETED):
                    failure_cause = (
                        execution_receipt.cause
                        or 'terminal_resource_invariant_failed'
                    )
                    envelope_kind = 'internal'
                    raise _DirectRelayFailure(
                        cause=failure_cause, envelope_kind=envelope_kind)
            except Exception as e:  # dispatch raised (exhausted slots, etc.)
                err = e
                from lib.llm.llm_errors import LocalRequestPreparationError
                if isinstance(e, TerminalTaskFailure):
                    failure_cause = e.verdict.cause
                    envelope_kind = 'upstream_error'
                elif isinstance(e, (TimeoutError, asyncio.TimeoutError)):
                    failure_cause = 'timeout'
                    envelope_kind = 'timeout'
                elif isinstance(e, LocalRequestPreparationError):
                    failure_cause = 'request_prepare'
                    # Only the bounded compiler error is public. Arbitrary
                    # adapter exceptions may contain request bodies/secrets.
                    from lib.llm.reasoning import ReasoningContractError
                    cause = e if isinstance(e, ReasoningContractError) else e.__cause__
                    if isinstance(cause, ReasoningContractError):
                        failure_cause = cause.kind
                        failure_detail = str(cause)
                logger.warning(
                    '[chat_direct] dispatch failed type=%s cause=%s',
                    type(e).__name__, failure_cause)

        if err is not None:
            envelope = make_envelope(
                envelope_kind, model=model, detail=failure_detail,
                context='chat_direct', source='routes.api_v1.chat_direct')
            yield f'data: {json.dumps({
                "error": {
                    "message": envelope["message"],
                    "type": "server_error",
                    "code": failure_cause,
                },
                "tofu_error": envelope,
            }, ensure_ascii=False)}\n\n'
            yield 'data: [DONE]\n\n'
            return

        assert stream_result is not None
        if not emitted_role:
            # A verified zero-delta completion still needs a role frame.
            yield _chunk_frame(completion_id, model, role=True)

        provider_finish = stream_result.provider_finish_reason
        assert provider_finish is not None

        calls = stream_result.message.get('tool_calls')
        if calls:
            # Emit only the fully validated provider batch. Partial/failed
            # attempts never hand a client a callable tool result.
            tool_frame = {
                'id': completion_id, 'object': 'chat.completion.chunk',
                'created': int(time.time()), 'model': model,
                'choices': [{'index': 0, 'delta': {'tool_calls': [
                    dict(call, index=index) for index, call in enumerate(calls)
                ]}, 'finish_reason': None}],
            }
            yield f'data: {json.dumps(tool_frame, ensure_ascii=False)}\n\n'

        final = {
            'id': completion_id, 'object': 'chat.completion.chunk',
            'created': int(time.time()), 'model': model,
            'choices': [{'index': 0, 'delta': {},
                         'finish_reason': _openai_finish_reason(
                             provider_finish)}],
        }
        if stream_result.usage:
            final['usage'] = stream_result.usage
            if client_tools is not None:
                from lib.compat.openai import project_completion_usage
                final['usage'] = project_completion_usage(stream_result.usage)
        if terminal_metadata.get('billing'):
            final['billing'] = terminal_metadata['billing']
        if terminal_metadata.get('billing_status'):
            final['billing_status'] = terminal_metadata['billing_status']
        yield f'data: {json.dumps(final, ensure_ascii=False)}\n\n'
        yield 'data: [DONE]\n\n'
    except (GeneratorExit, asyncio.CancelledError):
        # Transport ownership ends here; execution ownership does not. Continue
        # draining/validating the provider response without retaining relay
        # chunks. The admission lease stays held until _drive settles.
        consumer_attached = False
        while True:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
        if not dispatch_result_observed:
            _retain_detached_dispatch(
                drive_task, completion_id=completion_id)
        logger.info('[chat_direct] client disconnected — provider dispatch '
                    'continues completion=%s', completion_id[:80])
        raise
    finally:
        if not dispatch_result_observed and consumer_attached:
            # Any generator-side failure is an observer failure too. Preserve
            # the already-started provider request under the same contract.
            consumer_attached = False
            _retain_detached_dispatch(
                drive_task, completion_id=completion_id)
        if frame_store is not None:
            # Closing joins encoders/readers before anonymous transport ranges
            # retire. Repeated cancellation cannot orphan a running file worker.
            _, cancelled = await _join_owned_io(asyncio.to_thread(frame_store.close))
            while not q.empty():
                q.get_nowait()
            frame_store = None
            if cancelled:
                raise asyncio.CancelledError


__all__ = [
    '_DETACHED_DIRECT_DISPATCHES',
    '_DIRECT_DEFAULT_TIMEOUT_S',
    '_DIRECT_MAX_TIMEOUT_S',
    'run_direct_stream',
]
