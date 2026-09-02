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
  | search          | searchMode         | 'multi' / 'off'    |
  | memory          | memoryEnabled      | bool               |
  | preferences     | preferencesEnabled | bool               |
  | mcp             | mcpEnabled         | bool               |
  | browser         | browserEnabled     | bool               |
  | desktop         | desktopEnabled     | bool               |
  | code_exec       | codeExecEnabled    | bool               |
  | image_gen       | imageGenEnabled    | bool               |
  | human_guidance  | humanGuidanceEnabled                    |
  | scheduler       | schedulerEnabled                        |
  | project         | projectPath        | absolute path      |
  | plugins         | plugins            | list/'*' /comma-str|
  | max_tokens      | maxTokens          | int                |
  | temperature     | temperature        | float              |
  +-----------------+--------------------+--------------------+

Tool-plugin isolation (multi-tenant)
====================================
Third-party tools contributed via the ``tofu.tools`` entry-point group are
**process-global** once installed, so on a shared server they would otherwise
be visible to every caller. They are therefore gated per request: a plugin is
only exposed to the model when its entry-point name is allow-listed via
``config.plugins`` (this request) or the ``TOFU_DEFAULT_TOOL_PLUGINS`` env var
(deployment default). With neither set the default is **fail-closed** — no
third-party plugins. Pass ``config.plugins='*'`` (or set the env var to ``*``)
to expose all installed plugins (single-tenant convenience). Built-in tools
(search, project, memory, swarm, …) are never affected. See docs/TOOL_PLUGINS.md.

For backwards compatibility the legacy ``capabilities`` field is still
accepted and merged into ``config`` (config wins on conflict).
"""

from __future__ import annotations

import asyncio
import json
import time

from quart import Blueprint, request

from lib.agent_core.admission import (
    await_terminal, controller, on_terminal, register_waiter,
    unregister_waiter, wait_for_event,
)
from lib.agent_core.run_contract import (
    build_agent_config, project_agent_result,
)
from lib.api_response import (
    api_bad_request, api_error, api_internal_error, api_not_found, api_ok,
    api_payload, sse_response,
)
from lib.billing.request_flow import (
    estimate_prompt_tokens, release_reservation, reserve_for_task, settle_task,
)
from lib.byo_resolve import dispose_ephemeral_slot, resolve_model_and_provider
from lib.idempotency import idempotent_post
from lib.ids import short_id
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import (
    async_parse_body, optional_bool, optional_dict, optional_str, require_list,
)
from lib.tools.tool_env import (
    CustomToolError, dispose_tool_env, mint_tool_env,
)
from lib.trajectory import AVAILABLE_FORMATS

from .auth import current_auth, guard_model_relay_or_dispose, require_scope

logger = get_logger(__name__)

api_v1_agent_run_bp = Blueprint('api_v1_agent_run', __name__)


# ── Capability translation ──────────────────────────────────────────


def _apply_remote_alias(cfg: dict, value, *, user_id: str = ''):
    """``config.remote = '<agent_id>:<root>'`` → ``cfg['project_remote']``.

    RWA P4 入口:validates the target against the live bridge registry
    (agent online, root declared, bridge-user match) before binding —
    every refusal is an honest 400, never a silent fall-through.
    Returns ``(cfg, error)``.
    """
    text = str(value or '').strip()
    if ':' not in text:
        return cfg, ("config.remote must be '<agent_id>:<root>' "
                     "(see /api/v1/desktop/status for online agents)")
    agent_id, root = (p.strip() for p in text.split(':', 1))
    if not agent_id or not root:
        return cfg, ("config.remote must be '<agent_id>:<root>' "
                     '(both parts non-empty)')
    from lib.desktop.remote import validate_remote_binding
    binding, error = validate_remote_binding(agent_id, root, user_id=user_id)
    if error:
        return cfg, error
    cfg['project_remote'] = binding
    return cfg, None


def _build_cfg(model_id: str, raw_config: dict | None,
                capabilities_legacy: dict | None) -> dict:
    """Compatibility name for the transport-neutral contract helper."""
    return build_agent_config(model_id, raw_config, capabilities_legacy)


# ── Streaming + blocking response shapes ────────────────────────────


async def _wait_for_terminal(task, *, timeout_s: float):
    """Await terminal state without busy-waiting (event-driven).

    Returns normally on terminal; raises RuntimeError on timeout (logged
    by the caller with task/model context).
    """
    ok = await await_terminal(task, timeout_s=timeout_s)
    if not ok:
        raise RuntimeError('agent run timed out')


async def _stream_generator(task, model: str, completion_id: str,
                            *, billing_user_id: str = ''):
    """Async SSE generator. Mirrors routes/api_v1/chat::_stream_generator
    but emits an ``agent.run.chunk`` object so consumers can tell the
    surface apart from compat-OpenAI streams.

    Event-driven: waits on the task's terminal/nudge signal instead of
    polling, so it never pins a thread while the LLM is generating.

    When ``billing_user_id`` is set (multi-user installs), the actual
    token usage is settled exactly once before the terminal ``[DONE]``
    line — mirroring the blocking path so stream mode is never free.
    """
    cursor = 0
    emitted_role = False
    _billed = False

    def _settle_once():
        nonlocal _billed
        if billing_user_id and not _billed:
            settle_task(task, user_id=billing_user_id, model=model)
            _billed = True

    task_id = task.get('id') or ''
    from lib.task_replay import task_memory_replay_page
    try:
        while True:
            page = task_memory_replay_page(task, cursor)
            new_events = page.events
            cursor = page.next_cursor
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
                    _settle_once()
                    yield 'data: [DONE]\n\n'
                    return
            if task.get('status') in ('done', 'error', 'aborted') and not new_events:
                yield (f'data: '
                        f'{json.dumps({"object":"agent.run.chunk","event":task.get("status"),"task_id":task.get("id")})}'
                        '\n\n')
                _settle_once()
                yield 'data: [DONE]\n\n'
                return
            # Block until the next event (or 15s heartbeat) — no busy-wait.
            woke = await wait_for_event(task_id, timeout=15.0)
            if not woke:
                yield ': heartbeat\n\n'
    except (GeneratorExit, asyncio.CancelledError):
        # Client disconnected. The task keeps running (its terminal
        # callback still releases the admission slot); just stop streaming.
        logger.info('[agent.run] stream client disconnected task=%s', task_id[:8])
        raise
    finally:
        unregister_waiter(task_id)


def _final_response(task: dict, *, model: str, requested_id: str,
                    trajectory_fmt: str | None,
                    byo_provider: dict | None) -> dict:
    """Compatibility name for the transport-neutral result projector."""
    return project_agent_result(
        task,
        model=model,
        requested_id=requested_id,
        trajectory_fmt=trajectory_fmt,
        byo_provider=byo_provider,
    )


# ── Route ───────────────────────────────────────────────────────────


@api_v1_agent_run_bp.route('/api/v1/agent/run', methods=['POST'])
@require_scope('agents:run')
@idempotent_post()
@api_meta(
    summary='Single-call agent runtime',
    description=(
        'Run an agent turn end-to-end. Bring your own model — either '
        'use the deployment default, register one via /api/v1/providers and '
        'pin runs with '
        '`model="name@prov_xxx"`, or pass an inline `provider: '
        '{base_url, api_key, model}` block. When `model` is omitted the '
        'provider block or deployment default supplies it. Plain aliases route to the '
        'operator-curated slot pool.\n\n'
        '`config` accepts both curated aliases (`thinking`, `tools`, '
        '`memory`, `swarm`, `mcp`, `project`, `max_tokens`, `temperature`, '
        '…) AND raw orchestrator keys (`thinkingDepth`, `searchMode`, '
        '`memoryEnabled`, …). Aliases translate to the corresponding raw '
        'keys; unknown keys pass through unchanged.\n\n'
        'When `trajectory` is set the response carries top-level '
        '`trajectory_format` + `trajectory` fields (no nested envelope) '
        'in sharegpt / openai-finetune / anthropic / tofu-native shape.\n\n'
        'Set `stream=true` for SSE, or `async=true` / '
        '`Prefer: respond-async` for an HTTP 202 task handle; otherwise '
        'the request blocks until terminal. '
        'The response always carries `task_id` so callers can switch '
        'to `/api/v1/tasks/{id}/...` for replay or abort.'),
    tags=['agents'], scope='agents:run',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['messages'],
            'properties': {
                'messages': {'type': 'array',
                              'items': {'$ref': '#/components/schemas/ChatMessage'}},
                'model': {'type': 'string',
                            'description': (
                                'Optional model name; the deployment default '
                                'is used when absent. May be a plain alias '
                                '(`deepseek-v4-pro`), a BYO suffix '
                                '(`deepseek-v4-pro@prov_a3f2c1`), or '
                                'any name when paired with an inline '
                                '`provider` block.')},
                'provider': {
                    'type': 'object',
                    'description': (
                        'Inline BYO endpoint. Mints a one-shot slot '
                        'scoped to this single task; never persisted.'),
                    'properties': {
                        'base_url': {'type': 'string'},
                        'endpoint': {
                            'type': 'string',
                            'description': 'Friendly alias for base_url.'},
                        'api_key': {'type': 'string'},
                        'model': {
                            'type': 'string',
                            'description': 'Used when top-level model is absent.'},
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
                'async': {
                    'type': 'boolean',
                    'description': (
                        'Return HTTP 202 with Location and task_id instead '
                        'of waiting for terminal settlement.')},
                'timeout_s': {'type': 'number'},
                'conversation_id': {'type': 'string'},
            }}}}})
async def agent_run():
    body = await async_parse_body()
    from routes.api_v1.auth import request_user_id

    owner_user_id = int(request_user_id())
    try:
        messages_in = require_list(body, 'messages')
    except ValueError as e:
        return api_bad_request(str(e), field='messages')
    if not messages_in:
        return api_bad_request('messages is empty', field='messages')

    auth = current_auth()
    credential_key_id = (auth.key_id if auth else '') or ''
    tenant_id = auth.tenant_id if auth else None

    # ── 1. Resolve the model ──────────────────────────────────────
    model_str = optional_str(body, 'model', default='', max_len=200)
    provider_block = optional_dict(body, 'provider')
    if not model_str:
        model_str = str((provider_block or {}).get('model') or '').strip()
    if not model_str:
        # Managed-default mode: callers only need the Tofu endpoint/token.
        # Composition owns the provider/model selection; no lower agent layer
        # reaches for an ambient user or provider.
        from lib import LLM_MODEL
        model_str = str(LLM_MODEL or '').strip()
    if not model_str:
        return api_bad_request(
            'no model is configured; pass `model`, set `provider.model`, or '
            'configure the deployment default', field='model')
    model_id, handle, byo_prov, err, err_status = resolve_model_and_provider(
        model_str, provider_block, owner_user_id, tenant_id=tenant_id)
    if err:
        if err_status == 404:
            return api_not_found(err)
        return api_bad_request(err, field='model')

    # ── 1b. BYO-only relay backstop ───────────────────────────────
    # A plain-alias model (no BYO handle) routes to the OPERATOR's slot
    # pool, which a model_relay_enabled=false deployment forbids — even
    # though such deployments DO grant tenants `agents:run` (so they can
    # run agents against their OWN endpoint). BYO requests and admin keys
    # pass; everything else is refused. Mirrors chat.py + compat routes.
    _relay_denied = guard_model_relay_or_dispose(handle)
    if _relay_denied is not None:
        return _relay_denied

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

    # ── 2b. RWA remote-worktree binding (config.remote='<agent>:<root>') ──
    _remote_val = cfg.pop('remote', None)
    if _remote_val is not None:
        cfg, _remote_err = _apply_remote_alias(
            cfg, _remote_val,
            user_id=(str(auth.owner_user_id or '') if auth else ''))
        if _remote_err:
            if handle:
                dispose_ephemeral_slot(handle)
            return api_bad_request(_remote_err, field='config.remote')
        audit_log('agent_run_remote_bind', key_id=credential_key_id,
                  owner_user_id=owner_user_id,
                  agent_id=cfg['project_remote']['agent_id'],
                  root=cfg['project_remote']['root'])

    # ── 3. Other request knobs ────────────────────────────────────
    stream = optional_bool(body, 'stream', default=False)
    respond_async = optional_bool(body, 'async', default=False)
    timeout_s = float(body.get('timeout_s') or 600)
    requested_id = optional_str(body, 'id', default='', max_len=200)
    conversation_id = optional_str(body, 'conversation_id',
                                    default='', max_len=200)
    if not conversation_id:
        conversation_id = short_id('agent-', 12)
    trajectory_fmt = optional_str(body, 'trajectory',
                                    default='', max_len=40) or None
    if trajectory_fmt and trajectory_fmt not in AVAILABLE_FORMATS:
        if handle:
            dispose_ephemeral_slot(handle)
        return api_bad_request(
            f'unknown trajectory format {trajectory_fmt!r}; must be one of '
            f'{list(AVAILABLE_FORMATS)}', field='trajectory')

    # ── 3b. Per-request custom tools (optional) ───────────────────
    # Validated + minted into a request-scoped ToolEnvironment. Its clean
    # schemas ride the normal tool list via cfg['_customToolSchemas']; its
    # handlers resolve task-locally in _execute_tool_one. Nothing persists
    # into the global tool_registry. See docs/CUSTOM_TOOLS.md.
    tool_env = None
    custom_tools = body.get('tools')
    if custom_tools:
        try:
            tool_env = mint_tool_env(
                tools=custom_tools, owner=f'owner:{owner_user_id}')
        except CustomToolError as e:
            if handle:
                dispose_ephemeral_slot(handle)
            return api_bad_request(str(e), field='tools')
        except RuntimeError as e:
            if handle:
                dispose_ephemeral_slot(handle)
            return api_internal_error(e, context='api_v1.agent_run.tools')
        cfg['_customToolSchemas'] = tool_env.schemas

    audit_log('agent_run_start', key_id=credential_key_id,
              owner_user_id=owner_user_id,
              model=model_id, byo=bool(handle), provider_id=(byo_prov or {}).get('id'),
              n_messages=len(messages_in), stream=stream,
              trajectory=trajectory_fmt, n_custom_tools=len(tool_env.tools) if tool_env else 0)

    # ── 4. Dispatch ───────────────────────────────────────────────
    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.spawn import spawn_task
    task = create_task(
        conversation_id, messages_in, cfg, user_id=owner_user_id
    )
    task['_tenant_id'] = tenant_id
    task['_inline_messages'] = True
    task['_api_v1'] = True
    task['_via_agent_run'] = True
    if credential_key_id:
        task['_api_key_id'] = credential_key_id
    if tool_env is not None:
        task['_tool_env'] = tool_env
    # ── Hard provider isolation ──
    # When a BYO endpoint was resolved (inline `provider` block or a
    # registered @prov_xxx), bind the whole task to that provider's slot so
    # NO dispatch on it can leak onto the operator's configured keys. See
    # lib/llm_dispatch/provider_pin.py.
    if handle is not None:
        task['_pinned_provider_id'] = handle.slot.provider_id

    # ── Billing: pre-flight reserve (multi-user installs only) ──
    # Mirrors routes/api_v1/chat.py. Personal / open installs have an
    # empty user_id and short-circuit to a no-op. The headline BYOM
    # endpoint must bill identically to /chat/completions — stream and
    # block alike (see _settle_once in _stream_generator + settle below).
    billing_user_id = auth.account_user_id if auth else ''
    reservation_micro = 0
    if billing_user_id:
        from lib.billing import InsufficientFunds
        try:
            est_completion = int(cfg.get('maxTokens')
                                 or body.get('max_tokens') or 1024)
            reservation_micro = reserve_for_task(
                task, user_id=billing_user_id, model=model_id,
                prompt_tokens=estimate_prompt_tokens(messages_in),
                max_completion_tokens=est_completion)
        except InsufficientFunds as e:
            if handle:
                dispose_ephemeral_slot(handle)
            if tool_env is not None:
                dispose_tool_env(tool_env)
            return api_error(
                f'Insufficient credits. '
                f'Estimated cost {e.needed_micro / 1_000_000:.4f} credits, '
                f'balance {e.balance_micro / 1_000_000:.4f}.',
                status=402, error_kind='insufficient_funds',
                balance_micro=e.balance_micro, needed_micro=e.needed_micro)

    # ── Admission control: bound concurrent in-flight tasks ───────
    # When the server is saturated, refuse with 503 + Retry-After
    # instead of spawning unbounded work that starves the thread pool.
    if not controller.try_acquire():
        if handle:
            dispose_ephemeral_slot(handle)
        if tool_env is not None:
            dispose_tool_env(tool_env)
        release_reservation(task, user_id=billing_user_id,
                            reservation_micro=reservation_micro)
        logger.warning('[agent.run] admission refused (in_flight=%d/%d) '
                       'key=%s model=%s',
                       controller.in_flight, controller.capacity,
                       credential_key_id or 'open-mode', model_id)
        return api_error(
            'Server at capacity; retry shortly.', status=503,
            error_kind='overloaded', retry_after=5)

    # Release the admission slot + dispose BYO/tool resources + SETTLE
    # BILLING exactly once, the moment the task reaches a terminal state.
    # Event-driven (fired from manager.append_event) — no per-request
    # polling thread. Binding settlement HERE (not to the HTTP request
    # lifecycle) is the root-cause fix for the reservation-leak paths: a
    # blocking-timeout that outran the client, a mid-stream client
    # disconnect, and an in-process reaper finalize all reach terminal via
    # this callback, so the reservation is settled against ACTUAL usage
    # rather than stranded until the 30-min janitor. settle_task is
    # idempotent on ref_id=task_id, so the happy-path settle below is a
    # harmless no-op second call.
    _slot_released = {'done': False}

    def _on_done(_tid, _handle=handle, _tool_env=tool_env):
        if _slot_released['done']:
            return
        _slot_released['done'] = True
        controller.release()
        try:
            settle_task(task, user_id=billing_user_id, model=model_id)
        except Exception as ex:
            logger.error('[agent.run] terminal settle failed task=%s: %s',
                         _tid[:8], ex, exc_info=True)
        if _handle is not None:
            try:
                dispose_ephemeral_slot(_handle)
            except Exception as ex:
                logger.error('[agent.run] ephemeral dispose failed handle=%s '
                             'task=%s: %s', _handle.handle_id,
                             _tid[:8], ex, exc_info=True)
        if _tool_env is not None:
            try:
                dispose_tool_env(_tool_env)
            except Exception as ex:
                logger.error('[agent.run] tool-env dispose failed task=%s: %s',
                             _tid[:8], ex, exc_info=True)

    on_terminal(task['id'], _on_done)
    register_waiter(task['id'])

    try:
        spawn_task(task)
    except Exception as e:
        # spawn failed → fire the cleanup callback synchronously (it
        # releases the slot + disposes resources) and drop the waiter.
        _on_done(task['id'])
        unregister_waiter(task['id'])
        release_reservation(task, user_id=billing_user_id,
                            reservation_micro=reservation_micro)
        logger.exception('[agent.run] spawn_task failed task=%s', task['id'][:8])
        return api_internal_error(e, context='api_v1.agent_run')

    logger.info('[agent.run] spawned task=%s conv=%s key=%s model=%s byo=%s '
                'stream=%s in_flight=%d/%d', task['id'][:8],
                conversation_id, credential_key_id or 'open-mode',
                model_id, bool(handle),
                stream, controller.in_flight, controller.capacity)

    # ── 5. Return a handle, stream, or block ──────────────────────
    if respond_async or 'respond-async' in str(
            request.headers.get('Prefer') or '').lower():
        # The terminal callback remains registered and owns admission,
        # provider/tool cleanup, and billing. Only the HTTP waiter can be
        # released now; the caller resumes through the task endpoints.
        unregister_waiter(task['id'])
        response, status = api_payload({
            'ok': True,
            'id': requested_id or short_id('run-'),
            'object': 'agent.run',
            'task_id': task['id'],
            'status': task.get('status') or 'running',
            'model': model_id,
        }, status=202)
        response.headers['Location'] = f'/api/v1/tasks/{task["id"]}'
        response.headers['X-Tofu-Task-Id'] = task['id']
        return response, status

    if stream:
        completion_id = requested_id or short_id('run-')
        return sse_response(
            _stream_generator(task, model_id, completion_id,
                              billing_user_id=billing_user_id),
            extra_headers={'X-Tofu-Task-Id': task['id']})

    try:
        await _wait_for_terminal(task, timeout_s=timeout_s)
    except RuntimeError as e:
        logger.warning('[agent.run] task=%s timed out model=%s elapsed=%.0fs',
                       task['id'][:8], model_id, timeout_s)
        return api_internal_error(str(e), context='api_v1.agent_run')
    finally:
        unregister_waiter(task['id'])

    out = _final_response(
        task, model=model_id, requested_id=requested_id,
        trajectory_fmt=trajectory_fmt, byo_provider=byo_prov)
    billing = settle_task(task, user_id=billing_user_id, model=model_id)
    if billing:
        out['billing'] = billing
    return api_ok(out)


__all__ = ['api_v1_agent_run_bp']
