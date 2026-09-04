"""routes/api_v1/agent_run.py — Single-call agent runtime façade.

``POST /api/v1/agent/run`` is the headline "Tofu is an agent runtime;
you bring the model" endpoint. One request body that bundles:

* the prompt (``messages``)
* WHICH model is requested — an official ``{creator_id, model_id}`` or
  provider-scoped ``{provider_id, offering_id}`` identity. Optional routing
  policy is orthogonal to that identity.
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

1. **Resolves the model** through the owner-scoped model-routing v2
   repository, including capability, context, price, and provider preference.
2. **Mints/disposes a bounded request-scoped route group**. Disposal happens
   after the task reaches terminal state, even in stream or async mode.
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
    await_terminal, controller, register_waiter,
    unregister_waiter, wait_for_event,
)
from lib.agent_core.execution_session import (
    ExecutionPhase,
    bind_admission_lease,
    bind_billing_reservation,
    bind_model_route,
    execution_session_for_task,
)
from lib.agent_core.run_contract import (
    build_agent_config, project_agent_result,
)
from lib.api_response import (
    api_bad_request, api_error, api_internal_error, api_ok,
    api_payload, sse_response,
)
from lib.billing.request_flow import (
    estimate_prompt_tokens, release_reservation, reserve_for_task, settle_task,
)
from lib.model_routing import ModelRoutingError
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
from routes.model_routing_adapter import (
    dispose_routed_slot_group,
    mint_native_request_route,
    routing_error_fields,
)

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
                            *, billing_user_id: str = '',
                            provider_id: str = ''):
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
                if provider_id:
                    chunk['provider_id'] = provider_id
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
                    byo_provider: dict | None,
                    provider_id: str = '') -> dict:
    """Compatibility name for the transport-neutral result projector."""
    return project_agent_result(
        task,
        model=model,
        requested_id=requested_id,
        trajectory_fmt=trajectory_fmt,
        byo_provider=byo_provider,
        provider_id=provider_id,
    )


# ── Route ───────────────────────────────────────────────────────────


@api_v1_agent_run_bp.route('/api/v1/agent/run', methods=['POST'])
@require_scope('agents:run')
@idempotent_post()
@api_meta(
    summary='Single-call agent runtime',
    description=(
        'Run an agent turn end-to-end. `model` is a structured v2 identity: '
        'either `{creator_id, model_id}` for an official model or '
        '`{provider_id, offering_id}` for a provider-scoped identity. '
        'Provider preference and resource policy live under `routing`; '
        'model-string suffixes and inline secret-bearing provider blocks are '
        'rejected.\n\n'
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
            'required': ['messages', 'model'],
            'properties': {
                'messages': {'type': 'array',
                              'items': {'$ref': '#/components/schemas/ChatMessage'}},
                'model': {
                    '$ref': '#/components/schemas/NativeModelSelection',
                },
                'routing': {
                    'type': 'object',
                    'description': (
                        'Optional resource and ProviderAccess preference; '
                        'never changes the requested model identity.'),
                    'additionalProperties': False,
                    'properties': {
                        'preferred_provider_id': {'type': 'string'},
                        'required_context': {
                            'type': 'integer', 'minimum': 1},
                        'price_budget': {'type': 'object'},
                        'cache_affinity_connection_id': {'type': 'string'},
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

    # ── 1. Resolve the structured v2 ModelRef ─────────────────────
    route_group = None
    try:
        model_id, model_selection, route_group = mint_native_request_route(
            body,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            owner_tag=f'agent-run:{owner_user_id}',
        )
    except ModelRoutingError as exc:
        return api_bad_request(str(exc), **routing_error_fields(exc))
    relay_denial = guard_model_relay_or_dispose(route_group)
    if relay_denial is not None:
        return relay_denial
    selected_provider_id = route_group.candidates[0].provider_id

    # ── 2. Build cfg from unified config + legacy capabilities ─────
    raw_config = optional_dict(body, 'config') or {}
    capabilities_legacy = optional_dict(body, 'capabilities') or {}
    if (raw_config and not isinstance(raw_config, dict)) or (
            capabilities_legacy and not isinstance(capabilities_legacy, dict)):
        dispose_routed_slot_group(route_group)
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
            dispose_routed_slot_group(route_group)
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
        dispose_routed_slot_group(route_group)
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
            dispose_routed_slot_group(route_group)
            return api_bad_request(str(e), field='tools')
        except RuntimeError as e:
            dispose_routed_slot_group(route_group)
            return api_internal_error(e, context='api_v1.agent_run.tools')
        cfg['_customToolSchemas'] = tool_env.schemas

    audit_log('agent_run_start', key_id=credential_key_id,
              owner_user_id=owner_user_id,
              model=model_id, provider_id=selected_provider_id,
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
    task['_pinned_provider_id'] = route_group.pin_id
    task['_requested_model_ref'] = (
        model_selection.model.public_dict()
        if model_selection.model is not None
        else model_selection.provider_offering.public_dict()
    )
    execution_session = execution_session_for_task(task)
    bind_model_route(
        execution_session,
        lambda: dispose_routed_slot_group(route_group),
    )
    if tool_env is not None:
        execution_session.hold_resource(
            'tool_environment',
            lambda _context: dispose_tool_env(tool_env),
            release_order=250,
        )

    # ── Billing: pre-flight reserve (multi-user installs only) ──
    # Mirrors routes/api_v1/chat.py. Personal / open installs have an
    # empty user_id and short-circuit to a no-op. The headline agent-runtime
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
            execution_session.settle(
                ExecutionPhase.FAILED, cause='billing_reservation_refused')
            return api_error(
                f'Insufficient credits. '
                f'Estimated cost {e.needed_micro / 1_000_000:.4f} credits, '
                f'balance {e.balance_micro / 1_000_000:.4f}.',
                status=402, error_kind='insufficient_funds',
                balance_micro=e.balance_micro, needed_micro=e.needed_micro)

    if billing_user_id:
        bind_billing_reservation(
            execution_session,
            reservation_micro=reservation_micro,
            settle=lambda: settle_task(
                task, user_id=billing_user_id, model=model_id,
                raise_on_error=True,
            ),
            release=lambda: release_reservation(
                task, user_id=billing_user_id,
                reservation_micro=reservation_micro, raise_on_error=True,
            ),
        )

    # ── Admission control: bound concurrent in-flight tasks ───────
    # When the server is saturated, refuse with 503 + Retry-After
    # instead of spawning unbounded work that starves the thread pool.
    admission_lease = controller.acquire()
    if admission_lease is None:
        execution_session.settle(
            ExecutionPhase.FAILED, cause='task_admission_refused')
        logger.warning('[agent.run] admission refused (in_flight=%d/%d) '
                       'key=%s model=%s',
                       controller.in_flight, controller.capacity,
                       credential_key_id or 'open-mode', model_id)
        return api_error(
            'Server at capacity; retry shortly.', status=503,
            error_kind='overloaded', retry_after=5)
    bind_admission_lease(
        execution_session,
        lambda: controller.release(admission_lease),
    )
    register_waiter(task['id'])

    try:
        spawn_task(task)
    except Exception as e:
        execution_session.settle(
            ExecutionPhase.FAILED, cause='task_spawn_failed')
        unregister_waiter(task['id'])
        logger.exception('[agent.run] spawn_task failed task=%s', task['id'][:8])
        return api_internal_error(e, context='api_v1.agent_run')

    logger.info('[agent.run] spawned task=%s conv=%s key=%s model=%s '
                'stream=%s in_flight=%d/%d', task['id'][:8],
                conversation_id, credential_key_id or 'open-mode',
                model_id,
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
            'provider_id': selected_provider_id,
        }, status=202)
        response.headers['Location'] = f'/api/v1/tasks/{task["id"]}'
        response.headers['X-Tofu-Task-Id'] = task['id']
        return response, status

    if stream:
        completion_id = requested_id or short_id('run-')
        return sse_response(
            _stream_generator(task, model_id, completion_id,
                              billing_user_id=billing_user_id,
                              provider_id=selected_provider_id),
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
        trajectory_fmt=trajectory_fmt, byo_provider=None,
        provider_id=selected_provider_id)
    billing = settle_task(task, user_id=billing_user_id, model=model_id)
    if billing:
        out['billing'] = billing
    return api_ok(out)


__all__ = ['api_v1_agent_run_bp']
