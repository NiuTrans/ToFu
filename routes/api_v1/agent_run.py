"""routes/api_v1/agent_run.py — Single-call agent runtime façade.

``POST /api/v1/agent/run`` is the headline "Tofu is an agent runtime;
you bring the model" endpoint. One request body that bundles:

* the prompt (``messages``)
* WHERE the LLM lives — ``model: string`` (alias or ``name@prov_xxx``)
  and an optional flat ``provider`` block ``{base_url, api_key,
  extra_headers}`` for inline BYO (no registration round-trip).
* WHICH agent capabilities to enable (``config`` — aliases like
  ``thinking``, ``tools``, ``memory`` mix freely with raw orchestrator
  keys like ``thinkingDepth``, ``searchMode``, ``memoryEnabled``).
* WHICH trajectory format to return (``trajectory`` — sharegpt /
  openai-finetune / anthropic / tofu-native, or omit for none). When
  set, the response carries top-level ``trajectory_format`` +
  ``trajectory`` fields (no nested envelope).
* HOW to deliver the result (``stream: true`` for SSE, otherwise
  blocks until terminal).

Everything else (orchestrator, fallback, retries, tool execution) is
shared with :mod:`routes.api_v1.chat` — this module is a thin façade
that does three things on top:

1. **Resolves the model** — ``model="name@prov_xxx"`` looks up the
   caller's registered BYO provider; an inline ``provider`` block
   (with a plain ``model`` name) mints a one-shot endpoint; otherwise
   we fall back to the global slot pool.
2. **Mints/disposes an ephemeral slot** when a BYO endpoint is
   resolved. Disposal happens after the task reaches terminal state,
   even on stream mode.
3. **Optionally flattens** the finished task into a known trajectory
   format via :func:`lib.trajectory.flatten`.

Capability vocabulary
=====================
``config`` accepts BOTH curated aliases AND raw orchestrator keys.
The dict is translated through a small alias table; any key that
isn't a known alias passes through to the orchestrator unchanged.

  +-----------------+--------------------+--------------------+
  | Alias (snake)   | Orchestrator key   | Notes              |
  +-----------------+--------------------+--------------------+
  | thinking        | thinkingDepth (+   | string -> 'low'…   |
  |                 | thinkingEnabled)   | 'max'; bool also OK|
  | tools           | (per-tool toggles) | list[str] or '*'   |
  | search          | searchMode         | 'multi'/'single'/  |
  |                 |                    | 'off'              |
  | memory          | memoryEnabled      | bool               |
  | swarm           | swarmEnabled       | bool               |
  | mcp             | mcpEnabled         | bool               |
  | browser         | browserEnabled     | bool               |
  | desktop         | desktopEnabled     | bool               |
  | code_exec       | codeExecEnabled    | bool               |
  | image_gen       | imageGenEnabled    | bool               |
  | human_guidance  | humanGuidanceEnabled                    |
  | scheduler       | schedulerEnabled                        |
  | project         | projectPath        | absolute path      |
  | max_tokens      | maxTokens          | int                |
  | temperature     | temperature        | float              |
  +-----------------+--------------------+--------------------+

For backwards compatibility the legacy ``capabilities`` field is still
accepted and merged into ``config`` (config wins on conflict).
"""

from __future__ import annotations

import json
import threading
import time
import uuid

from flask import Blueprint, Response

from lib.api_response import (
    api_bad_request, api_internal_error, api_not_found, api_ok,
)
from lib.byo_providers import resolve_model_string, touch_provider
from lib.idempotency import idempotent_post
from lib.llm_dispatch.ephemeral import (
    EphemeralSlotHandle, dispose_ephemeral_slot, mint_ephemeral_slot,
)
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import (
    optional_bool, optional_dict, optional_str, parse_body, require_list,
)
from lib.trajectory import AVAILABLE_FORMATS, flatten

from .auth import current_auth, require_scope
from .providers import sanitise_extra_headers

logger = get_logger(__name__)

api_v1_agent_run_bp = Blueprint('api_v1_agent_run', __name__)


# ── Capability translation ──────────────────────────────────────────


_THINKING_DEPTHS = {'low', 'medium', 'high', 'xhigh', 'max'}

# Friendly tool tags → orchestrator cfg toggles.
_TOOL_TAG_MAP = {
    'search':       ('searchMode', 'multi'),
    'search:single': ('searchMode', 'single'),
    'search:multi': ('searchMode', 'multi'),
    'fetch':        ('fetchEnabled', True),
    'memory':       ('memoryEnabled', True),
    'swarm':        ('swarmEnabled', True),
    'mcp':          ('mcpEnabled', True),
    'browser':      ('browserEnabled', True),
    'desktop':      ('desktopEnabled', True),
    'code_exec':    ('codeExecEnabled', True),
    'image_gen':    ('imageGenEnabled', True),
    'human_guidance': ('humanGuidanceEnabled', True),
    'scheduler':    ('schedulerEnabled', True),
}

# When tools='*' or tools=['all'] is requested, enable every safe
# capability. Excludes anything that requires external setup (browser
# extension, desktop agent, project path) — those are explicit opt-ins.
_TOOLS_ALL = {
    'searchMode': 'multi', 'fetchEnabled': True, 'memoryEnabled': True,
    'swarmEnabled': False, 'mcpEnabled': True, 'codeExecEnabled': False,
    'imageGenEnabled': False, 'humanGuidanceEnabled': False,
    'schedulerEnabled': False,
}

# Top-level alias keys → orchestrator cfg keys. Applied AFTER the
# tools[] expansion so a direct alias wins over an entry in tools[].
# The right-hand side is a callable that mutates the cfg dict; this
# lets multi-key aliases (like ``thinking`` setting both
# thinkingDepth + thinkingEnabled) live in one place.
def _set_thinking(cfg: dict, value):
    if isinstance(value, str) and value.lower() in _THINKING_DEPTHS:
        cfg['thinkingEnabled'] = True
        cfg['thinkingDepth'] = value.lower()
    elif isinstance(value, bool):
        cfg['thinkingEnabled'] = bool(value)


def _set_search(cfg: dict, value):
    if isinstance(value, str) and value.lower() in ('off', 'single', 'multi'):
        cfg['searchMode'] = value.lower()
    elif value is False:
        cfg['searchMode'] = 'off'
    elif value is True:
        cfg['searchMode'] = 'multi'


_ALIAS_SETTERS = {
    'thinking':       _set_thinking,
    'search':         _set_search,
    'memory':         lambda c, v: c.__setitem__('memoryEnabled', bool(v)),
    'swarm':          lambda c, v: c.__setitem__('swarmEnabled', bool(v)),
    'mcp':            lambda c, v: c.__setitem__('mcpEnabled', bool(v)),
    'browser':        lambda c, v: c.__setitem__('browserEnabled', bool(v)),
    'desktop':        lambda c, v: c.__setitem__('desktopEnabled', bool(v)),
    'code_exec':      lambda c, v: c.__setitem__('codeExecEnabled', bool(v)),
    'image_gen':      lambda c, v: c.__setitem__('imageGenEnabled', bool(v)),
    'human_guidance': lambda c, v: c.__setitem__('humanGuidanceEnabled', bool(v)),
    'scheduler':      lambda c, v: c.__setitem__('schedulerEnabled', bool(v)),
    'project':        lambda c, v: c.__setitem__('projectPath', str(v)) if v else None,
    'max_tokens':     lambda c, v: c.__setitem__('maxTokens', v),
    'temperature':    lambda c, v: c.__setitem__('temperature', v),
}


def _build_cfg(model_id: str, raw_config: dict | None,
                capabilities_legacy: dict | None) -> dict:
    """Translate the unified ``config`` dict into an orchestrator cfg.

    ``raw_config`` accepts both curated aliases (``thinking``,
    ``tools``, ``memory``, …) AND raw orchestrator keys
    (``thinkingDepth``, ``searchMode``, …). Aliases are translated
    first; raw keys flow through unchanged.

    ``capabilities_legacy`` is the deprecated ``capabilities`` field
    name — accepted for back-compat and merged into ``config`` (with
    ``config`` winning).
    """
    cfg: dict = {'model': model_id}

    # Merge legacy `capabilities` into the unified shape (config wins).
    merged: dict = {}
    if capabilities_legacy:
        merged.update(capabilities_legacy)
    if raw_config:
        merged.update(raw_config)

    # Tools list FIRST (so direct aliases below can override).
    tools = merged.pop('tools', None)
    if tools == '*' or tools == 'all':
        cfg.update(_TOOLS_ALL)
    elif isinstance(tools, list):
        # Magic single-element list values for "all".
        if tools == ['*'] or tools == ['all']:
            cfg.update(_TOOLS_ALL)
        else:
            for t in tools:
                mapping = _TOOL_TAG_MAP.get(str(t))
                if mapping:
                    key, val = mapping
                    cfg[key] = val
                else:
                    logger.debug('[agent.run] unknown tool tag: %r', t)
    elif tools is not None:
        logger.debug('[agent.run] ignoring unknown tools shape: %r', tools)

    # Aliases.
    for alias_key, setter in _ALIAS_SETTERS.items():
        if alias_key in merged:
            setter(cfg, merged.pop(alias_key))

    # Anything left passes through as a raw orchestrator key. Don't
    # filter — the orchestrator's own _resolve_model_config is the
    # final authority on what's valid; we treat unknown keys as
    # forward-compat extensions.
    cfg.update(merged)
    return cfg


# ── Model resolution ────────────────────────────────────────────────


def _resolve_model_and_provider(model_str: str, provider_block: dict | None,
                                  owner_key_id: str
                                  ) -> tuple[str, EphemeralSlotHandle | None,
                                              dict | None, str | None,
                                              int | None]:
    """Map ``(model, provider)`` → ``(model_id, handle, byo_row, error, status)``.

    Status is the HTTP code we should map an error to (400 vs 404).
    None on success.

    Resolution order (first match wins):

    1. ``provider`` block present — inline BYO. ``model`` MUST be a
       plain alias with no ``@prov_xxx`` suffix.
    2. ``model="name@prov_xxx"`` — registered BYO provider lookup.
    3. plain ``model`` — falls through to global slot pool, no
       ephemeral handle.
    """
    has_block = isinstance(provider_block, dict) and provider_block
    has_suffix = isinstance(model_str, str) and '@' in model_str

    # Both at once is ambiguous.
    if has_block and has_suffix:
        return '', None, None, (
            'cannot combine `model="name@prov_xxx"` with an inline '
            '`provider` block; pick one'), 400

    # 1. Inline provider block
    if has_block:
        if not isinstance(model_str, str) or not model_str.strip():
            return '', None, None, (
                '`model` is required when `provider` is supplied'), 400
        url = (provider_block.get('base_url') or '').strip()
        if not url:
            return '', None, None, (
                '`provider.base_url` is required'), 400
        headers, hdr_err = sanitise_extra_headers(
            provider_block.get('extra_headers') or {})
        if hdr_err:
            return '', None, None, hdr_err, 400
        try:
            handle = mint_ephemeral_slot(
                base_url=url,
                api_key=provider_block.get('api_key') or '',
                model_id=model_str.strip(),
                owner=owner_key_id or 'agent_run',
                extra_headers=headers,
                thinking_format=(provider_block.get('thinking_format')
                                 or ''),
            )
        except (ValueError, RuntimeError) as e:
            logger.warning('[agent.run] inline-provider mint failed url=%s: %s',
                           url, e)
            return '', None, None, str(e), 400
        return model_str.strip(), handle, None, None, None

    # 2/3. String form
    if isinstance(model_str, str) and model_str.strip():
        rm = resolve_model_string(model_str, owner_key_id)
        if rm is None:
            return '', None, None, (
                f'model string {model_str!r} references an unknown '
                f'or disabled BYO provider; check the @prov_xxx '
                f'suffix'), 404
        if rm.provider is None:
            return rm.model_id, None, None, None, None
        prov = rm.provider
        try:
            handle = mint_ephemeral_slot(
                base_url=prov['base_url'],
                api_key=prov.get('api_key') or '',
                model_id=rm.model_id,
                owner=f'{owner_key_id}:{prov["id"]}',
                extra_headers=prov.get('extra_headers') or {},
                # Carry the persisted dialect so the dispatcher uses
                # the right body shape for this engine. Without this,
                # a registered self-hosted sglang Qwen3 would silently
                # downgrade to top-level enable_thinking and the engine
                # would ignore it.
                thinking_format=prov.get('thinking_format') or '',
            )
        except (ValueError, RuntimeError) as e:
            logger.warning('[agent.run] BYO provider mint failed prov=%s: %s',
                           prov.get('id'), e)
            return '', None, None, str(e), 400
        touch_provider(prov['id'])
        return rm.model_id, handle, prov, None, None

    return '', None, None, '`model` is required', 400


# ── Streaming + blocking response shapes ────────────────────────────


def _wait_for_terminal(task, *, timeout_s: float):
    deadline = time.time() + timeout_s
    poll = 0.1
    while task.get('status') not in ('done', 'error', 'aborted'):
        if time.time() >= deadline:
            raise RuntimeError('agent run timed out')
        time.sleep(poll)
        poll = min(poll * 1.2, 1.5)


def _dispose_after_terminal(task: dict, handle: EphemeralSlotHandle) -> None:
    """Dispose the ephemeral slot once the task reaches a terminal state.

    Runs in a daemon thread so SSE responses can return immediately
    while the slot lives long enough for the orchestrator to make its
    last LLM call. Bounded by a 1-hour ceiling so a stuck task can't
    leak the slot forever.
    """
    deadline = time.time() + 3600
    while task.get('status') not in ('done', 'error', 'aborted'):
        if time.time() >= deadline:
            logger.warning('[agent.run] ephemeral handle %s: task %s '
                            'still running after 1h, force-disposing',
                            handle.handle_id, task.get('id', '?')[:8])
            break
        time.sleep(0.5)
    dispose_ephemeral_slot(handle)


def _stream_generator(task, model: str, completion_id: str):
    """SSE generator. Mirrors routes/api_v1/chat::_stream_generator
    but emits an ``agent.run.chunk`` object so consumers can tell the
    surface apart from compat-OpenAI streams.
    """
    cursor = 0
    last_heartbeat = time.time()
    emitted_role = False
    while True:
        with task['events_lock']:
            new_events = list(task['events'][cursor:])
            cursor = len(task['events'])
        for ev in new_events:
            etype = ev.get('type', '')
            chunk = {
                'id': completion_id,
                'object': 'agent.run.chunk',
                'created': int(time.time()),
                'model': model,
                'task_id': task.get('id'),
                'event': etype,
                'data': {k: v for k, v in ev.items() if k != 'type'},
            }
            if not emitted_role and etype == 'delta':
                chunk['delta'] = {'role': 'assistant',
                                   'content': ev.get('content', '')}
                emitted_role = True
            elif etype == 'delta':
                chunk['delta'] = {'content': ev.get('content', '')}
                if ev.get('thinking'):
                    chunk['delta']['reasoning_content'] = ev['thinking']
            yield f'data: {json.dumps(chunk, ensure_ascii=False)}\n\n'
            if etype in ('done', 'error', 'aborted'):
                yield 'data: [DONE]\n\n'
                return
        if task.get('status') in ('done', 'error', 'aborted') and not new_events:
            yield (f'data: '
                    f'{json.dumps({"object":"agent.run.chunk","event":task.get("status"),"task_id":task.get("id")})}'
                    '\n\n')
            yield 'data: [DONE]\n\n'
            return
        now = time.time()
        if now - last_heartbeat > 15:
            yield ': heartbeat\n\n'
            last_heartbeat = now
        time.sleep(0.05)


def _final_response(task: dict, *, model: str, requested_id: str,
                    trajectory_fmt: str | None,
                    byo_provider: dict | None) -> dict:
    """Build the JSON response for a finished task.

    Top-level fields:
      id, object, created, model, task_id, status, finish_reason,
      content, thinking, usage, n_tool_rounds, [tool_calls],
      [provider_id], [trajectory_format + trajectory].
    """
    rounds = task.get('toolRounds') or []
    last_round = rounds[-1] if rounds else None
    out: dict = {
        'id': requested_id or f'run-{uuid.uuid4().hex[:24]}',
        'object': 'agent.run',
        'created': int(time.time()),
        'model': model,
        'task_id': task.get('id'),
        'status': task.get('status'),
        'finish_reason': task.get('finishReason') or 'stop',
        'content': task.get('content') or '',
        'thinking': task.get('thinking') or '',
        'usage': task.get('usage') or {},
        'n_tool_rounds': len(rounds),
    }
    if last_round and isinstance(last_round, dict) and last_round.get('tool_calls'):
        out['tool_calls'] = last_round['tool_calls']
    if task.get('error'):
        out['error'] = task['error']
    if byo_provider:
        out['provider_id'] = byo_provider['id']
    if trajectory_fmt:
        try:
            shaped = flatten(task, trajectory_fmt)
            # FLAT envelope: top-level format + trajectory body, never
            # wrapped in another `trajectory: {...}` dict.
            out['trajectory_format'] = shaped['format']
            out['trajectory'] = shaped['trajectory']
        except ValueError as e:
            logger.warning('[agent.run] trajectory flatten failed fmt=%s task=%s: %s',
                           trajectory_fmt, task.get('id', '?')[:8], e)
            out['trajectory_error'] = str(e)
    return out


# ── Route ───────────────────────────────────────────────────────────


@api_v1_agent_run_bp.route('/api/v1/agent/run', methods=['POST'])
@require_scope('agents:run')
@idempotent_post()
@api_meta(
    summary='Single-call agent runtime',
    description=(
        'Run an agent turn end-to-end. Bring your own model — either '
        'register it once via /api/v1/providers and pin runs with '
        '`model="name@prov_xxx"`, or pass an inline `provider: '
        '{base_url, api_key}` block. Plain alias names route to the '
        'operator-curated slot pool.\n\n'
        '`config` accepts both curated aliases (`thinking`, `tools`, '
        '`memory`, `swarm`, `mcp`, `project`, `max_tokens`, `temperature`, '
        '…) AND raw orchestrator keys (`thinkingDepth`, `searchMode`, '
        '`memoryEnabled`, …). Aliases translate to the corresponding raw '
        'keys; unknown keys pass through unchanged.\n\n'
        'When `trajectory` is set the response carries top-level '
        '`trajectory_format` + `trajectory` fields (no nested envelope) '
        'in sharegpt / openai-finetune / anthropic / tofu-native shape.\n\n'
        'Set `stream=true` for SSE; otherwise blocks until terminal. '
        'The response always carries `task_id` so callers can switch '
        'to `/api/v1/tasks/{id}/...` for replay or abort.'),
    tags=['agents'], scope='agents:run',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['messages', 'model'],
            'properties': {
                'messages': {'type': 'array',
                              'items': {'$ref': '#/components/schemas/ChatMessage'}},
                'model': {'type': 'string',
                            'description': (
                                'Model name. May be a plain alias '
                                '(`deepseek-v4-pro`), a BYO suffix '
                                '(`deepseek-v4-pro@prov_a3f2c1`), or '
                                'any name when paired with an inline '
                                '`provider` block.')},
                'provider': {
                    'type': 'object',
                    'description': (
                        'Inline BYO endpoint. Mints a one-shot slot '
                        'scoped to this single task; never persisted.'),
                    'required': ['base_url'],
                    'properties': {
                        'base_url': {'type': 'string'},
                        'api_key': {'type': 'string'},
                        'extra_headers': {'type': 'object'},
                        'thinking_format': {
                            'type': 'string',
                            'enum': ['', 'enable_thinking', 'thinking_type',
                                      'chat_template_kwargs', 'none'],
                            'description': (
                                'Body-shape dialect for the thinking '
                                'flag on this engine. Leave empty to '
                                'auto-detect from model name; set '
                                'explicitly when serving a model whose '
                                'name matches a cloud family but the '
                                'engine speaks a different dialect '
                                '(most commonly self-hosted '
                                'sglang/vLLM → `chat_template_kwargs`).')},
                    }},
                'config': {'type': 'object',
                            'description': (
                                'Mixed alias + raw-key cfg block. See '
                                'route docstring for the alias table.')},
                'capabilities': {
                    'type': 'object',
                    'deprecated': True,
                    'description': 'Legacy alias for `config`. '
                                    'Merged into `config` (config wins).'},
                'trajectory': {'type': 'string',
                                'enum': list(AVAILABLE_FORMATS)},
                'stream': {'type': 'boolean'},
                'timeout_s': {'type': 'number'},
                'conversation_id': {'type': 'string'},
            }}}}})
def agent_run():
    body = parse_body()
    try:
        messages_in = require_list(body, 'messages')
    except ValueError as e:
        return api_bad_request(str(e), field='messages')
    if not messages_in:
        return api_bad_request('messages is empty', field='messages')

    auth = current_auth()
    owner_key_id = (auth.key_id if auth else '') or 'anonymous'

    # ── 1. Resolve the model ──────────────────────────────────────
    model_str = optional_str(body, 'model', default='', max_len=200)
    provider_block = optional_dict(body, 'provider')
    if not model_str:
        return api_bad_request('`model` is required', field='model')
    model_id, handle, byo_prov, err, err_status = _resolve_model_and_provider(
        model_str, provider_block, owner_key_id)
    if err:
        if err_status == 404:
            return api_not_found(err)
        return api_bad_request(err, field='model')

    # ── 2. Build cfg from unified config + legacy capabilities ─────
    raw_config = optional_dict(body, 'config') or {}
    capabilities_legacy = optional_dict(body, 'capabilities') or {}
    if (raw_config and not isinstance(raw_config, dict)) or (
            capabilities_legacy and not isinstance(capabilities_legacy, dict)):
        if handle:
            dispose_ephemeral_slot(handle)
        return api_bad_request('`config` / `capabilities` must be objects',
                                field='config')
    cfg = _build_cfg(model_id, raw_config, capabilities_legacy)

    # ── 3. Other request knobs ────────────────────────────────────
    stream = optional_bool(body, 'stream', default=False)
    timeout_s = float(body.get('timeout_s') or 600)
    requested_id = optional_str(body, 'id', default='', max_len=200)
    conversation_id = optional_str(body, 'conversation_id',
                                    default='', max_len=200)
    if not conversation_id:
        conversation_id = f'agent-{uuid.uuid4().hex[:12]}'
    trajectory_fmt = optional_str(body, 'trajectory',
                                    default='', max_len=40) or None
    if trajectory_fmt and trajectory_fmt not in AVAILABLE_FORMATS:
        if handle:
            dispose_ephemeral_slot(handle)
        return api_bad_request(
            f'unknown trajectory format {trajectory_fmt!r}; must be one of '
            f'{list(AVAILABLE_FORMATS)}', field='trajectory')

    audit_log('agent_run_start', key_id=owner_key_id,
              model=model_id, byo=bool(handle), provider_id=(byo_prov or {}).get('id'),
              n_messages=len(messages_in), stream=stream,
              trajectory=trajectory_fmt)

    # ── 4. Dispatch ───────────────────────────────────────────────
    from lib.tasks_pkg import create_task, spawn_task
    task = create_task(conversation_id, messages_in, cfg)
    task['_inline_messages'] = True
    task['_api_v1'] = True
    task['_via_agent_run'] = True
    if owner_key_id:
        task['_api_key_id'] = owner_key_id

    try:
        spawn_task(task)
    except Exception as e:
        if handle:
            dispose_ephemeral_slot(handle)
        logger.exception('[agent.run] spawn_task failed')
        return api_internal_error(e, context='api_v1.agent_run')

    # Schedule disposal whenever the task terminates (handles both
    # stream and blocking response modes uniformly).
    if handle:
        threading.Thread(
            target=_dispose_after_terminal, args=(task, handle),
            name=f'ephemeral-dispose-{handle.handle_id}',
            daemon=True,
        ).start()

    # ── 5. Stream or block ────────────────────────────────────────
    if stream:
        completion_id = requested_id or f'run-{uuid.uuid4().hex[:24]}'
        return Response(
            _stream_generator(task, model_id, completion_id),
            mimetype='text/event-stream',
            headers={
                'Content-Type': 'text/event-stream; charset=utf-8',
                'Cache-Control': 'no-cache, no-transform',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
                'X-Tofu-Task-Id': task['id'],
            })

    try:
        _wait_for_terminal(task, timeout_s=timeout_s)
    except RuntimeError as e:
        return api_internal_error(str(e), context='api_v1.agent_run')

    return api_ok(_final_response(
        task, model=model_id, requested_id=requested_id,
        trajectory_fmt=trajectory_fmt, byo_provider=byo_prov))


__all__ = ['api_v1_agent_run_bp']
