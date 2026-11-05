"""lib/compat/openai.py — OpenAI Chat Completions ↔ Tofu translator.

Pure functions, no Flask imports. ``routes/compat_openai.py`` wires
them into HTTP.

Provides:
  * ``translate_openai_request(body)``   — OpenAI body → Tofu cfg + messages
  * ``build_openai_response(task, model, requested_id)``
                                          — Tofu task → OpenAI completion
  * ``stream_openai_chunks(task, ...)``  — generator of SSE wire frames
  * ``models_payload()``                 — /v1/models response
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncGenerator

from lib.compat._common import (
    CompatTerminalFailure,
    apply_common_cfg,
    apply_thinking_cfg,
    apply_tools_and_personal_defaults,
    require_deliverable_terminal,
    short_id,
)
from lib.compat.openai_projection import (
    openai_completion_chunk, openai_finish_reason, project_completion_usage,
)
from lib.error_envelope import BadRequest
from lib.log import get_logger
from lib.turn_verdict import terminal_finish_reason

logger = get_logger(__name__)


# ── Request translation ────────────────────────────────────────────

def translate_openai_request(body: dict) -> tuple[list[dict], dict, dict]:
    """Split an OpenAI body into (messages, cfg, options).

    ``options`` carries fields that don't map onto Tofu cfg but the
    caller (route handler) needs:
        - id              : optional client-supplied completion id
        - stream          : bool
        - timeout_s       : float (for sync mode)
    """
    messages = body.get('messages') or []
    if not isinstance(messages, list):
        raise BadRequest('messages must be an array', field='messages')
    try:
        timeout_s = float(body.get('timeout_s') or 600)
    except (TypeError, ValueError):
        raise BadRequest('timeout_s must be a number', field='timeout_s') from None

    cfg: dict = {}
    apply_common_cfg(cfg, body)
    # OpenAI-specific cfg fields.
    if 'stop' in body:
        cfg['stop'] = body['stop']
    if 'seed' in body:
        cfg['seed'] = body['seed']
    if 'response_format' in body:
        cfg['responseFormat'] = body['response_format']
    if 'user' in body:
        cfg['user'] = body['user']
    if 'parallel_tool_calls' in body:
        cfg['parallelToolCalls'] = body['parallel_tool_calls']

    apply_tools_and_personal_defaults(cfg, body)

    # Reasoning / thinking — the effort rung maps 1:1 onto Tofu's depth
    # ladder (see lib/compat/_common._EFFORT_RUNGS). The old table here
    # shifted every rung UP one (low→medium, medium→high, high→max) and had
    # no `xhigh` at all, so a caller asking for `low` silently paid for
    # `medium`. Shared with the Anthropic surface so both read one vocabulary.
    apply_thinking_cfg(cfg, body)

    options = {
        'id': body.get('id') or '',
        'stream': bool(body.get('stream')),
        'timeout_s': timeout_s,
    }
    return list(messages), cfg, options


# ── Response translation (sync) ────────────────────────────────────

def validate_client_tools(body: dict) -> tuple[list, object]:
    """Validate client declarations without resolving any server tool handler."""
    tools = body.get('tools')
    if not isinstance(tools, list):
        raise BadRequest('tools must be an array', field='tools')
    names = set()
    for tool in tools:
        function = tool.get('function') if isinstance(tool, dict) else None
        if not isinstance(function, dict) or tool.get('type') != 'function':
            raise BadRequest('client tools must be function declarations', field='tools')
        name = function.get('name')
        if not isinstance(name, str) or not name or name in names:
            raise BadRequest('client tool names must be nonempty and unique', field='tools')
        parameters = function.get('parameters')
        if parameters is not None and not isinstance(parameters, dict):
            raise BadRequest('client tool parameters must be a schema object', field='tools')
        names.add(name)
    choice = body.get('tool_choice')
    if choice is not None and choice not in ('auto', 'none', 'required'):
        function = choice.get('function') if isinstance(choice, dict) else None
        if (not isinstance(function, dict) or choice.get('type') != 'function'
                or not isinstance(function.get('name'), str)
                or function.get('name') not in names):
            raise BadRequest('tool_choice must name a declared client tool', field='tool_choice')
    if not tools and choice not in (None, 'auto', 'none'):
        raise BadRequest('tool_choice requires declared client tools', field='tool_choice')
    if body.get('n', 1) != 1:
        raise BadRequest('only n=1 is supported', field='n')
    return tools, choice


def build_direct_openai_response(result, *, model, completion_id, created):
    """Project a verified single provider response after shared settlement."""
    from lib.llm.stream_result import require_verified_provider_stream_result
    require_verified_provider_stream_result(result, context='client-tool completion')
    message = {key: result.message[key] for key in
               ('role', 'content', 'tool_calls', 'reasoning_content') if key in result.message}
    message.setdefault('role', 'assistant')
    message.setdefault('content', None)
    return {
        'id': completion_id, 'object': 'chat.completion', 'created': created,
        'model': model, 'choices': [{'index': 0, 'message': message,
                                   'finish_reason': openai_finish_reason(result.provider_finish_reason)}],
        'usage': project_completion_usage(result.usage or {}),
    }


def _assistant_message(task: dict) -> dict:
    # Deliverable = narration-free answer from the segment model (epic
    # , step 3). Single source of truth across sync +
    # streaming so a headless caller never sees inter-round scaffolding prose.
    from lib.tasks_pkg.segments import deliverable_text
    msg: dict = {'role': 'assistant', 'content': deliverable_text(task)}
    rounds = task.get('toolRounds') or []
    if rounds:
        last = rounds[-1] if isinstance(rounds[-1], dict) else None
        if last and last.get('tool_calls'):
            msg['tool_calls'] = last['tool_calls']
    return msg


def build_openai_response(task: dict, model: str,
                            requested_id: str = '') -> dict:
    """Turn a finished task into an OpenAI ``chat.completion`` body."""
    require_deliverable_terminal(task)
    finish = openai_finish_reason(
        terminal_finish_reason(task), aborted=bool(task.get('aborted')))
    usage = task.get('usage') or {}
    return {
        'id': requested_id or short_id('chatcmpl-'),
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model,
        'choices': [{
            'index': 0,
            'message': _assistant_message(task),
            'finish_reason': finish,
        }],
        'usage': {
            'prompt_tokens': int(usage.get('input_tokens') or
                                  usage.get('prompt_tokens') or 0),
            'completion_tokens': int(usage.get('output_tokens') or
                                      usage.get('completion_tokens') or 0),
            'total_tokens': int(usage.get('total_tokens') or 0),
        },
    }


# ── Streaming ──────────────────────────────────────────────────────

def _terminal_frames(task, model, completion_id, *, emitted_role, terminal_event=None):
    """Project one verified terminal observation, including late connections."""
    event = terminal_event or {}
    try:
        require_deliverable_terminal(task, terminal_event)
    except CompatTerminalFailure as exc:
        yield 'data: ' + json.dumps({
            'error': {
                'message': str(exc),
                'type': 'server_error',
                'code': exc.verdict.cause,
            },
        }, ensure_ascii=False) + '\n\n'
        yield 'data: [DONE]\n\n'
        return
    # Project the final deliverable; intermediate narration stays out of the wire.
    from lib.tasks_pkg.segments import deliverable_text
    answer = deliverable_text(task)
    if answer:
        ans_chunk = openai_completion_chunk(
            completion_id, model,
            created=int(time.time()))
        if not emitted_role:
            ans_chunk['choices'][0]['delta']['role'] = 'assistant'
            emitted_role = True
        ans_chunk['choices'][0]['delta']['content'] = answer
        yield f'data: {json.dumps(ans_chunk, ensure_ascii=False)}\n\n'
    # A tool-use terminal must carry the same call batch as the sync response.
    _rounds = task.get('toolRounds') or []
    _last = _rounds[-1] if (_rounds and isinstance(_rounds[-1], dict)) else None
    _tcs = _last.get('tool_calls') if _last else None
    if _tcs:
        tc_chunk = openai_completion_chunk(
            completion_id, model,
            created=int(time.time()))
        if not emitted_role:
            tc_chunk['choices'][0]['delta']['role'] = 'assistant'
            emitted_role = True
        tc_chunk['choices'][0]['delta']['tool_calls'] = [
            {'index': i, **tc} for i, tc in enumerate(_tcs)]
        yield f'data: {json.dumps(tc_chunk, ensure_ascii=False)}\n\n'
    raw_finish = (str(event.get('finishReason') or '') if 'finishReason' in event
                  else terminal_finish_reason(task))
    final = openai_completion_chunk(
        completion_id, model,
        created=int(time.time()),
        finish_reason=openai_finish_reason(raw_finish, aborted=bool(task.get('aborted'))))
    if event.get('usage') or task.get('usage'):
        final['usage'] = event.get('usage') or task.get('usage')
    yield f'data: {json.dumps(final, ensure_ascii=False)}\n\n'
    yield 'data: [DONE]\n\n'
    return


async def stream_openai_chunks(task, model: str, requested_id: str = '',
                               *, include_tofu_native: bool = False
                               ) -> AsyncGenerator[str, None]:
    """Yield SSE wire frames in OpenAI's ``chat.completion.chunk`` shape.

    ``include_tofu_native=True`` adds a `tofu` envelope to chunks for
    non-delta events (phase/tool_call/etc.). Vanilla OpenAI clients
    ignore unknown fields, so this is safe to leave on.

    Event-driven: blocks on the task's wakeup signal instead of polling,
    so it never pins a thread while the model is generating.
    """
    from lib.agent_core.admission import unregister_waiter, wait_for_event

    completion_id = requested_id or short_id('chatcmpl-')
    emitted_role = False
    cursor = 0
    task_id = task.get('id') or ''
    from lib.task_replay.task_replay import task_memory_replay_page

    try:
        while True:
            page = task_memory_replay_page(task, cursor)
            new_events = page.events
            cursor = page.next_cursor

            for ev in new_events:
                etype = ev.get('type', '')
                if etype == 'delta':
                    # Narrator-leak root fix (, step 3):
                    #   a content delta is UNCLASSIFIABLE mid-stream (narration vs
                    #   answer is only known at round close), and a wire client
                    #   cannot retract bytes already sent. So we do NOT forward raw
                    #   content deltas into the answer channel — the narration-free
                    #   deliverable is emitted from the segment model at `done`.
                    #   Thinking deltas DO stream live (reasoning_content), giving a
                    #   real-time experience without polluting the answer. This
                    #   retires the compat surface's dependence on DELTA_RESET.
                    if not ev.get('thinking'):
                        continue
                    chunk = openai_completion_chunk(
                        completion_id, model,
                        created=int(time.time()))
                    if not emitted_role:
                        chunk['choices'][0]['delta']['role'] = 'assistant'
                        emitted_role = True
                    chunk['choices'][0]['delta']['reasoning_content'] = ev['thinking']
                    yield f'data: {json.dumps(chunk, ensure_ascii=False)}\n\n'
                elif etype == 'done':
                    for frame in _terminal_frames(
                        task, model, completion_id, emitted_role=emitted_role, terminal_event=ev):
                        yield frame
                    return
                elif include_tofu_native:
                    chunk = {
                        'id': completion_id, 'object': 'chat.completion.chunk',
                        'created': int(time.time()), 'model': model,
                        'choices': [{'index': 0, 'delta': {}, 'finish_reason': None}],
                        'tofu': ev,
                    }
                    yield f'data: {json.dumps(chunk, ensure_ascii=False)}\n\n'

            if task.get('status') in ('done', 'error', 'aborted') and not new_events:
                for frame in _terminal_frames(
                    task, model, completion_id, emitted_role=emitted_role):
                    yield frame
                return

            woke = await wait_for_event(task_id, timeout=15.0)
            if not woke:
                yield ': heartbeat\n\n'
    except (GeneratorExit, asyncio.CancelledError):
        logger.info('[compat:openai] stream client disconnected task=%s',
                    task_id[:8])
        raise
    finally:
        unregister_waiter(task_id)


# ── /v1/models ─────────────────────────────────────────────────────

def models_payload(
    *, owner_user_id: int | None = None, tenant_id: str | None = None,
) -> dict:
    """Project the owner's routable v2 identities for ``/v1/models``.

    Official rows keep the standard bare string ID and expose Creator plus
    available Provider preferences through ``tofu``. Pending identities stay
    Provider-scoped; this function never synthesizes ``model@provider``.
    """
    if owner_user_id is None:
        return {'object': 'list', 'data': []}
    from lib.model_routing import ModelRoutingRepository, OwnerBoundary

    document = ModelRoutingRepository().get(OwnerBoundary.create(
        owner_user_id, tenant_id)).document
    enabled_accesses = {
        row['provider_access_id']: row['provider_id']
        for row in document['provider_accesses'] if row['enabled']
    }
    offerings = {
        row['offering_id']: row for row in document['offerings']
        if row['enabled'] and not row.get('stale')
        and row['provider_access_id'] in enabled_accesses
    }
    deployments = [
        row for row in document['deployments']
        if row['offering_id'] in offerings
    ]
    official: dict[tuple[str, str], set[str]] = {}
    pending: list[dict] = []
    for deployment in sorted(
        deployments,
        key=lambda row: (row['offering_id'], row['wire_model_id']),
    ):
        offering_id = deployment['offering_id']
        offering = offerings[offering_id]
        provider_id = enabled_accesses[offering['provider_access_id']]
        if offering['identity_state'] == 'confirmed':
            ref = offering['model']
            official.setdefault(
                (ref['creator_id'], ref['model_id']), set()).add(provider_id)
        else:
            pending.append({
                'id': deployment['wire_model_id'],
                'object': 'model',
                'owned_by': provider_id,
                'tofu': {
                    'preferred_provider_id': provider_id,
                    'offering_id': offering_id,
                    'pending_identity': True,
                },
            })
    data = [{
        'id': model_id,
        'object': 'model',
        'owned_by': creator_id,
        'tofu': {
            'creator_id': creator_id,
            'provider_ids': sorted(provider_ids),
        },
    } for (creator_id, model_id), provider_ids in sorted(official.items())]
    data.extend(pending)
    return {'object': 'list', 'data': data}


__all__ = [
    'translate_openai_request',
    'build_openai_response',
    'stream_openai_chunks',
    'models_payload',
]
