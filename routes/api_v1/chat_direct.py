"""routes/api_v1/chat_direct.py — POST /api/v1/chat/stream-direct.

A NATIVE-ASYNC, ON-LOOP streaming chat endpoint. Unlike
``/api/v1/chat/completions`` (which ``create_task`` + ``spawn_task`` onto an
OFF-loop worker thread and then tails the task's event buffer), this handler
drives ``lib.llm_dispatch.async_dispatch_stream`` **directly on the event
loop** — the httpx streaming call never occupies a thread-pool worker. This is
the production home that finally makes the native-async streaming path live
(see docs/FOLLOWUPS_ASYNC_MIGRATION.md §9).

How the on-loop bridge works
----------------------------
``async_dispatch_stream`` invokes its sync ``on_content`` / ``on_thinking``
callbacks synchronously inside the async SSE-parse loop (``aiter_lines`` →
``SSEAccumulator``), i.e. **on the event-loop thread**. So the callbacks can
push frames straight into an ``asyncio.Queue`` via ``put_nowait`` (no
``call_soon_threadsafe`` needed). We run the dispatch as a background
``asyncio.Task`` and an async generator drains the queue into SSE frames; when
the dispatch task completes we flush the queue, emit the terminal frame, and
close with ``[DONE]``.

Deliberate scope (NOT a replacement for /chat/completions)
----------------------------------------------------------
This is a single-turn, loop-resident streaming relay: NO tool loop, NO MCP, NO
multi-round orchestration, NO task-replay/abort handle. Those require the full
orchestrator, which is correctly thread-based. Callers needing tools/replay use
``/chat/completions``. This endpoint is for low-latency, pure-text (± thinking)
streaming that benefits from staying on the loop. It shares the admission
controller (backpressure) with the rest of the headless surface and touches
NONE of the create_task / spawn_task / thread-worker machinery.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from flask import Blueprint, Response

from lib.agent_core.admission import controller
from lib.api_response import api_bad_request, api_error
from lib.idempotency import idempotent_post
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import (
    async_parse_body, optional_dict, optional_str, require_list,
)

from .auth import current_auth, require_scope
from .chat import _validate_messages  # shared message sanity-check

logger = get_logger(__name__)

api_v1_chat_direct_bp = Blueprint('api_v1_chat_direct', __name__)


# Sentinel pushed onto the bridge queue when the dispatch task finishes.
_STREAM_END = object()


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
                            dispatch_fn=None, queue_maxsize=1000):
    """Async generator: drive an on-loop streaming dispatch → SSE frames.

    This is the TESTABLE CORE — ``dispatch_fn`` is injectable so tests can
    stub the LLM. In production it defaults to
    ``lib.llm_dispatch.async_dispatch_stream``.

    Contract of ``dispatch_fn`` (mirrors ``async_dispatch_stream``):
        ``await dispatch_fn(messages, on_content=..., on_thinking=...,
        max_tokens=..., temperature=..., prefer_model=..., capability=...,
        log_prefix=...)`` → ``(msg, finish_reason, usage)``; the sync
        ``on_content(str)`` / ``on_thinking(str)`` callbacks fire ON the loop.

    Yields OpenAI ``chat.completion.chunk`` SSE frames, then a terminal frame
    (with ``finish_reason`` + ``usage``), then ``data: [DONE]``.

    The queue is bounded; ``put_nowait`` is safe because both producer
    (callbacks) and consumer (this generator) run on the same loop and the
    consumer drains between dispatch suspensions. On overflow we drop to a
    blocking put scheduled on the loop (still ordered) — see _push.
    """
    if dispatch_fn is None:
        from lib.llm_dispatch import async_dispatch_stream as dispatch_fn

    q: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)

    def _push(kind: str, text: str) -> None:
        # Runs on the loop thread (callback fires inside the async parse loop).
        try:
            q.put_nowait((kind, text))
        except asyncio.QueueFull:
            # Extremely unlikely (consumer drains every suspension); drop the
            # oldest to keep the stream live rather than stall the producer.
            try:
                q.get_nowait()
                q.put_nowait((kind, text))
            except Exception as e:
                logger.warning('[chat_direct] queue overflow drop failed: %s', e)

    def _on_content(c):
        if c:
            _push('content', c)

    def _on_thinking(t):
        if t:
            _push('thinking', t)

    async def _drive():
        try:
            result = await dispatch_fn(
                messages,
                on_content=_on_content,
                on_thinking=_on_thinking,
                max_tokens=int(cfg.get('maxTokens') or 4096),
                temperature=float(cfg.get('temperature') or 0),
                thinking_enabled=bool(cfg.get('thinkingEnabled')),
                preset=cfg.get('preset') or 'low',
                capability=cfg.get('capability') or 'text',
                prefer_model=model or None,
                strict_model=bool(model),
                log_prefix='[chat_direct]',
            )
            return result
        finally:
            # Always unblock the consumer, even on dispatch error.
            q.put_nowait((_STREAM_END, None))

    drive_task = asyncio.ensure_future(_drive())

    emitted_role = False
    try:
        while True:
            kind, text = await q.get()
            if kind is _STREAM_END:
                break
            if not emitted_role:
                yield _chunk_frame(completion_id, model, role=True)
                emitted_role = True
            if kind == 'content':
                yield _chunk_frame(completion_id, model, content=text)
            elif kind == 'thinking':
                yield _chunk_frame(completion_id, model, thinking=text)

        # Dispatch finished — surface its result (finish_reason + usage) or error.
        finish_reason = 'stop'
        usage = None
        err = None
        try:
            _msg, finish_reason, usage = drive_task.result()
        except Exception as e:  # dispatch raised (exhausted slots, etc.)
            err = e
            logger.warning('[chat_direct] dispatch failed: %s', e)

        if not emitted_role:
            # Zero deltas (e.g. immediate error) — still emit the role frame
            # so a generic client gets a well-formed (empty) assistant turn.
            yield _chunk_frame(completion_id, model, role=True)

        final = {
            'id': completion_id, 'object': 'chat.completion.chunk',
            'created': int(time.time()), 'model': model,
            'choices': [{'index': 0, 'delta': {},
                         'finish_reason': finish_reason or 'stop'}],
        }
        if usage:
            final['usage'] = usage
        if err is not None:
            from lib.error_envelope import make_envelope
            final['finish_reason'] = 'stop'
            final['tofu_error'] = make_envelope(
                'internal', detail=str(err)[:300], model=model,
                context='chat_direct', source='routes.api_v1.chat_direct')
        yield f'data: {json.dumps(final, ensure_ascii=False)}\n\n'
        yield 'data: [DONE]\n\n'
    except (GeneratorExit, asyncio.CancelledError):
        # Client disconnected — cancel the in-flight dispatch so we don't keep
        # streaming from the upstream into the void.
        logger.info('[chat_direct] client disconnected — cancelling dispatch')
        drive_task.cancel()
        raise
    finally:
        if not drive_task.done():
            drive_task.cancel()


@api_v1_chat_direct_bp.route('/api/v1/chat/stream-direct', methods=['POST'])
@require_scope('chat')
@idempotent_post()
@api_meta(
    summary='Native-async streaming chat (on-loop, single-turn)',
    description=(
        'Stream a single-turn chat completion driven directly on the event '
        'loop via the native-async dispatcher — the httpx stream never '
        'occupies a worker thread. SSE only (always streams). NO tool loop / '
        'MCP / multi-round orchestration — use `/api/v1/chat/completions` for '
        'those. Frames are OpenAI `chat.completion.chunk` shape, terminated by '
        '`data: [DONE]`.'),
    tags=['chat'],
    scope='chat',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'$ref': '#/components/schemas/ChatCompletionRequest'},
    }}},
    responses={'200': {'description': 'SSE stream of chat.completion.chunk frames',
                       'content': {'text/event-stream': {
                           'schema': {'type': 'string'}}}}},
)
async def chat_stream_direct():
    body = await async_parse_body()
    try:
        messages = _validate_messages(require_list(body, 'messages'))
    except ValueError as e:
        return api_bad_request(str(e), field='messages')
    if not messages:
        return api_bad_request('messages is empty', field='messages')

    model = optional_str(body, 'model', default='', max_len=200)
    cfg_in = optional_dict(body, 'config') or {}
    from lib.tasks_pkg.entry import build_chat_config
    cfg = build_chat_config(
        model, cfg_in,
        max_tokens=body.get('max_tokens') if 'max_tokens' in body else None,
        temperature=body.get('temperature') if 'temperature' in body else None,
        thinking_depth=(body.get('thinking_depth')
                        or body.get('thinkingDepth') or ''),
    )
    requested_id = optional_str(body, 'id', default='', max_len=200)
    completion_id = requested_id or f'chatcmpl-{uuid.uuid4().hex[:24]}'

    auth = current_auth()
    audit_log('api_chat_stream_direct',
              key_id=(auth.key_id if auth else ''),
              model=cfg.get('model', '?'), n_messages=len(messages))

    # Admission control: shared backpressure with the rest of the headless
    # surface. Unlike the task path there is no spawn_task; the dispatch runs
    # inline on the loop, so we release the instant the generator finishes.
    if not controller.try_acquire():
        logger.warning('[chat_direct] admission refused (in_flight=%d/%d)',
                       controller.in_flight, controller.capacity)
        return api_error('Server at capacity; retry shortly.', status=503,
                         error_kind='overloaded', retry_after=5)

    inner = run_direct_stream(messages, model=cfg.get('model', model or '?'),
                              cfg=cfg, completion_id=completion_id)

    async def _gen():
        try:
            async for frame in inner:
                yield frame
        finally:
            controller.release()

    return Response(_gen(), mimetype='text/event-stream', headers={
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache, no-transform',
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive',
    })


__all__ = ['api_v1_chat_direct_bp', 'run_direct_stream']
