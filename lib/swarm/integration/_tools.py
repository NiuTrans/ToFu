"""lib/swarm/integration/_tools.py — swarm tool dispatch + handlers.

Routes the swarm-control tools the master LLM may call:

  * ``spawn_agents``      — fire-and-forget; returns a handle dict
  * ``await_agents``      — internal/legacy wait adapter behind ``poll``
  * ``get_agent_result``  — pull outcomes, canonical answers and partial evidence
  * artifact tools        — proxied to the live session's ArtifactStore

There is **no** synchronous "run swarm and return synthesised answer" path
anymore. The async swarm handle is a JSON object the LLM sees as the tool
result; sub-agent completions arrive on subsequent turns as auto-injected
``<swarm-update>`` user messages.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable

from typing import Any
from lib import agent_inbox
from lib.log import get_logger
from lib.swarm.integration._config import (
    AWAIT_AGENTS_HARD_CAP_SEC,
    _persist_config,
    swarm_key_for,
)
from lib.swarm.integration._logs import _resolve_output_dir
from lib.swarm.integration._recovery import recover_agent_result
from lib.swarm.integration._state import (
    _cleanup_stale_sessions,
    _get_session,
    _remove_session,
    _sessions_lock,
    _set_session,
    SwarmSessionConflict,
    add_session_alias,
)
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubTaskSpec
from lib.swarm.resource_policy import (
    swarm_max_parallel,
    swarm_max_retries,
)
from lib.tasks_pkg.manager import task_user_id
from lib.tools.result_envelope import ToolResultErrorV2

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════
#  Typed error results
# ═══════════════════════════════════════════════════════════

def _swarm_tool_error(code: str, *, retryable: bool, message: str = '',
                      next_action: str = '', **context: Any) -> str:
    """Serialize one swarm failure in the typed-error shape the server parses.

    The payload stays a FLAT legacy dict rather than a
    ``ToolResultEnvelopeV2`` wrapper: a top-level ``contractVersion:
    tofu.tool-result/v2`` + ``status`` marker is detected by
    ``split_tool_result_delivery`` / ``budget_tool_result_v2`` as a model
    envelope and projected, which would strip the context fields
    (``limit``/``requested``/``remaining_tokens``) into the evidence sidecar
    and hide them from the model. So instead the error triple
    (``code``/``retryable``/``next_action``) is emitted at the TOP level —
    the exact shape ``tool_result_error``'s sparse branch matches (``code``
    plus ``next_action``/``nextAction`` present, ``retryable`` a real bool)
    — and mirrored into a nested ``error`` dict for structural readers. The
    legacy string-``error`` failed open (``tool_result_error`` returned
    None); this flat object is what lets the tool-loop breaker and terminal-
    failure policy finally SEE swarm failures, while ``context`` fields ride
    to the model byte-for-byte.
    """
    err = ToolResultErrorV2(
        code=str(code or 'swarm_error'),
        retryable=bool(retryable),
        next_action=str(next_action or 'Re-issue the call with corrected arguments.'),
        message=str(message or ''),
    )
    payload = {
        'status': 'error',
        # Top-level triple: what tool_result_error parses.
        'code': err.code,
        'retryable': err.retryable,
        'next_action': err.next_action,
        # Nested mirror for readers that expect an ``error`` object.
        'error': {
            'code': err.code,
            'retryable': err.retryable,
            'next_action': err.next_action,
            'message': err.message,
        },
    }
    if err.message:
        payload['message'] = err.message
    for key, value in context.items():
        payload[key] = value
    return json.dumps(payload, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
#  Aggregate child budgets
# ═══════════════════════════════════════════════════════════

def plan_child_execution_limits(parent_task: dict, wave_size: int):
    """Slice the spawning task's remaining execution budget across one wave.

    Returns ``(per_child_limits, error)``: both None for an unbounded parent
    (no ``executionLimits`` — ordinary chats keep unbounded children), an
    error marker when the budget is already exhausted, else one per-child
    ``{max_tokens, max_tool_calls, deadline}`` dict enforced at the child's
    own admission boundaries (lib/agent_core/execution_limits.py).
    """
    limits = ((parent_task.get('config') or {}).get('executionLimits'))
    if limits is None or wave_size < 1:
        return None, None
    try:
        from lib.agent_core.execution_limits import validate_execution_limits
        validate_execution_limits(limits)
        max_tokens = limits['max_tokens']
        max_tool_calls = limits['max_tool_calls']
        deadline = limits['deadline']
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, (0, 0)
    from lib.cost import normalize_usage
    used = normalize_usage(parent_task.get('usage'))
    child_used = normalize_usage(parent_task.get('_swarmChildUsage'))
    remaining_tokens = (max(
        0, max_tokens
        - (used['input'] + used['output'])
        - (child_used['input'] + child_used['output'])) if max_tokens is not None else None)
    remaining_calls = (max(
        0, max_tool_calls - int(parent_task.get('_executionToolsAdmitted') or 0))
        if max_tool_calls is not None else None)
    if remaining_tokens == 0 or remaining_calls == 0:
        return None, (remaining_tokens, remaining_calls)
    return ({'max_tokens': max(1, remaining_tokens // wave_size) if remaining_tokens is not None else None,
             'max_tool_calls': remaining_calls // wave_size if remaining_calls is not None else None,
             'deadline': deadline},
            None)


def _child_usage_rollup(task: dict):
    """Fold one completed child's bill into the spawning task's ledger.

    Bounded tasks (``executionLimits``) get aggregate child budgets: merged
    tokens count at the parent's admission boundaries and in evolution
    episode actuals (``derive_actuals``). Unbounded parents keep the
    parent-only ledger (billing unchanged). An accepted Continue supplies its
    current parent task; startup-only recovery has no parent ledger to fold into.
    """
    if not isinstance((task.get('config') or {}).get('executionLimits'), dict):
        return None
    import threading

    def rollup(usage: dict, tool_calls: int) -> None:
        from lib.cost import merge_usage_totals
        lock = task.setdefault('_executionBudgetLock', threading.Lock())
        with lock:
            task['_swarmChildUsage'] = merge_usage_totals(
                task.get('_swarmChildUsage'), usage)
            admitted = int(task.get('_executionToolsAdmitted') or 0) + tool_calls
            task['_executionToolsAdmitted'] = admitted
            task['_swarmChildToolCalls'] = int(
                task.get('_swarmChildToolCalls') or 0) + tool_calls

    return rollup


# ═══════════════════════════════════════════════════════════
#  Tool dispatch
# ═══════════════════════════════════════════════════════════

def execute_swarm_tool(fn_name: str, fn_args: dict, task: dict | None = None,
                       *,
                       cfg: dict | None = None,
                       all_tools: list | None = None,
                       project_path: str = '',
                       project_enabled: bool = False,
                       model: str = '',
                       thinking_enabled: bool | None = None,
                       search_mode: str = 'multi',
                       abort_check: Callable | None = None,
                       pending_input_check: Callable[[], bool] | None = None,
                       on_event: Callable | None = None,
                       retained_binding: dict | None = None,
                       ) -> str:
    """Dispatch one swarm tool call.

    Returns a string — either a JSON-encoded handle/result dict (for
    ``spawn_agents`` / ``await_agents`` / ``get_agent_result``) or a plain
    text body (for the artifact tools).
    """
    with _sessions_lock:
        _cleanup_stale_sessions()

    task = task or {}
    all_tools = all_tools or []
    task_id = task.get('id', 'unknown')
    cfg = dict(cfg or {})
    if model:
        cfg['model'] = model
    from lib.tasks_pkg.child_invocation import inherit_child_config
    cfg.update(inherit_child_config(task.get('config') or {}, cfg))
    if thinking_enabled is not None:
        cfg['thinkingEnabled'] = thinking_enabled
    if search_mode:
        cfg['search_mode'] = search_mode
    model = cfg.get('model', '')
    thinking_enabled = cfg.get('thinkingEnabled')

    logger.info('[Swarm:%s] tool=%s args_keys=%s', task_id, fn_name, list(fn_args.keys()))

    try:
        if fn_name == 'spawn_agents':
            return _handle_spawn_agents(
                fn_args, task_id=task_id, task=task, cfg=cfg,
                all_tools=all_tools, model=model,
                thinking_enabled=thinking_enabled,
                project_path=project_path,
                abort_check=abort_check, on_event=on_event,
                retained_binding=retained_binding,
            )

        if fn_name in ('send_agent_message', 'get_agent_messages'):
            from lib.swarm import message_service
            from lib.swarm.run_messages import RunMessageError
            from lib.storage.errors import StorageError
            try:
                if fn_name == 'send_agent_message':
                    session = _get_session(swarm_key_for(task))
                    result = message_service.send(task, fn_args, session)
                else:
                    result = message_service.read(task, fn_args)
                return json.dumps(result, ensure_ascii=False)
            except RunMessageError as error:
                return _swarm_tool_error(error.code, retryable=False, message=str(error),
                    next_action='Query the exact run receipt; correct scope/identity or start a separately authorized assignment.')
            except StorageError as error:
                return _swarm_tool_error(error.code, retryable=error.retryable, message=str(error),
                    next_action='Query get_agent_messages; retry only with the same idempotency_key and content.')

        if fn_name == 'await_agents':
            return _handle_await_agents(
                fn_args, task_id=task_id, task=task, abort_check=abort_check,
                pending_input_check=pending_input_check)

        if fn_name == 'get_agent_result':
            return _handle_get_agent_result(fn_args, task_id=task_id, task=task)

        if fn_name in ('store_artifact', 'read_artifact', 'list_artifacts'):
            return _handle_artifact_tool(fn_name, fn_args, task_id)

        return f'Unknown swarm tool: {fn_name}'

    except Exception as e:
        logger.error('[Swarm:%s] Tool %s error: %s', task_id, fn_name, e, exc_info=True)
        return f'Swarm tool error: {type(e).__name__}: {e}'


# ═══════════════════════════════════════════════════════════
#  spawn_agents — async; returns handle immediately
# ═══════════════════════════════════════════════════════════

def _handle_spawn_agents(fn_args: dict, *,
                         task_id: str,
                         task: dict,
                         cfg: dict,
                         all_tools: list,
                         model: str,
                         thinking_enabled: bool | None,
                         project_path: str,
                         abort_check: Callable | None,
                         on_event: Callable | None,
                         retained_binding: dict | None = None) -> str:
    # Work on request-local copies: projecting read-only authority must not mutate
    # the parent task's sticky config or authority catalog for later rounds.
    cfg = dict(cfg or {})
    all_tools = list(all_tools or [])
    agents_data = fn_args.get('agents') or []
    if not agents_data:
        return _swarm_tool_error(
            'invalid_agents', retryable=True, message='no agents specified',
            next_action='Call spawn_agents with a non-empty agents array.')
    if not isinstance(agents_data, list):
        return _swarm_tool_error(
            'invalid_agents', retryable=True,
            message='agents must be an array.',
            next_action='Pass agents as an array of agent objects.')
    parallel_ceiling = swarm_max_parallel()
    retry_ceiling = swarm_max_retries()
    try:
        configured_parallel = int(
            cfg.get('max_parallel') or parallel_ceiling)
    except (TypeError, ValueError, OverflowError):
        configured_parallel = parallel_ceiling
    try:
        configured_retries = int(
            cfg.get('max_retries') if cfg.get('max_retries') is not None
            else retry_ceiling)
    except (TypeError, ValueError, OverflowError):
        configured_retries = retry_ceiling
    cfg['max_parallel'] = max(
        1, min(configured_parallel, parallel_ceiling))
    cfg['max_retries'] = max(
        0, min(configured_retries, retry_ceiling))

    orchestration = task.get('_toolOrchestration')
    orchestration = orchestration if isinstance(orchestration, dict) else {}
    read_only_orchestration = (
        str(orchestration.get('multiAgent') or '').lower() == 'read_only')
    if read_only_orchestration:
        from lib.swarm.routing import read_only_swarm_tools
        all_tools = read_only_swarm_tools(task, all_tools)

    specs: list[SubTaskSpec] = []
    for agent_def in agents_data:
        if not isinstance(agent_def, dict):
            return _swarm_tool_error(
                'invalid_agent', retryable=True,
                message='Every agents entry must be an object.',
                next_action='Make every agents entry an object.')
        if type(agent_def.get('required_for_completion', True)) is not bool:
            return _swarm_tool_error('invalid_agent', retryable=True,
                message='required_for_completion must be boolean.',
                next_action='Use true for required delivery or false for optional exploration.')
        try:
            requested_retries = int(
                agent_def.get('max_retries')
                if agent_def.get('max_retries') is not None
                else cfg['max_retries'])
        except (TypeError, ValueError, OverflowError):
            requested_retries = cfg['max_retries']
        explicit_tools = [
            str(t).strip()
            for t in (agent_def.get('tools') or [])
            if str(t or '').strip()
        ][:64] if isinstance(agent_def.get('tools'), list) else []
        spec = SubTaskSpec(
            required_for_completion=agent_def.get('required_for_completion', True),
            role=agent_def.get('role', 'general'),
            objective=agent_def.get('objective', ''),
            context=agent_def.get('context', ''),
            depends_on=agent_def.get('depends_on', []),
            # ``or``, NOT ``get('id', default)``. These ids come from the MODEL,
            # and an explicit ``"id": ""`` SETS the key — so a default-arg
            # fallback never fires and two such agents collapse onto ONE key in
            # ``master._results_by_id``. Measured: the first agent's entry then
            # holds the SECOND agent's result, and its own id can never be
            # resolved, so ``await_agents`` reports it as an agent that never
            # produced a result — the "no result" card, from a second root
            # cause independent of the parallel-tool one.
            id=agent_def.get('id') or str(uuid.uuid4())[:8],
            max_retries=max(0, min(requested_retries, retry_ceiling)),
            model_override=(agent_def.get('model_override')
                            or agent_def.get('model') or ''),
            tools=explicit_tools,
        )
        specs.append(spec)

    if retained_binding is not None:
        if len(specs) != 1 or specs[0].id != retained_binding.get('run_id'):
            return _swarm_tool_error(
                'retained_agent_binding', retryable=False,
                message='Retained assignment requires one matching child run.',
                next_action='Inspect the retained agent before retrying.')
        specs[0].retained_agent_id = str(retained_binding['agent_id'])
        specs[0].retained_scope = str(retained_binding['scope'])
        specs[0].retained_run_id = str(retained_binding['run_id'])

    # A bounded parent delegates only its remaining budget: every child runs
    # under its own slice, so one wave can never outspend the parent's envelope.
    per_child_limits, budget_exhausted = plan_child_execution_limits(task, len(specs))
    if budget_exhausted is not None:
        remaining_tokens, remaining_calls = budget_exhausted
        return _swarm_tool_error(
            'execution_budget_exhausted', retryable=False,
            message=(
                'The spawning task has exhausted its execution budget; no '
                'child agents can be admitted. Deliver with the evidence '
                'already gathered instead of delegating.'),
            next_action=(
                'Deliver with the evidence already gathered instead of '
                'delegating to child agents.'),
            remaining_tokens=remaining_tokens,
            remaining_tool_calls=remaining_calls)
    if per_child_limits:
        for spec in specs:
            spec.execution_limits = dict(per_child_limits)

    # Conversation-scoped session key (Option A): a swarm spawned on one
    # turn is reachable from later turns in the SAME conversation. Falls
    # back to task_id when there's no conv (tests, standalone).
    swarm_key = swarm_key_for(task)
    owner_id = task_user_id(task)

    # If a session already exists for this conversation, ADD to it instead
    # of creating a fresh one. This is how the main agent re-uses the same
    # swarm to launch a follow-up wave (replaces legacy
    # ``spawn_more_agents``).  If the existing session has already
    # terminated, drop it so we fall through to the "new session" branch
    # — otherwise the user can never spawn again after the first wave
    # completes.
    session = _get_session(swarm_key)
    if session is not None and session.user_id != owner_id:
        logger.warning('[Swarm:%s] owner mismatch for existing session',
                       swarm_key)
        return _swarm_tool_error(
            'swarm_not_found', retryable=False,
            message='No swarm session exists for this conversation.',
            next_action='Call spawn_agents to start a new swarm session.')
    if session is not None and session.is_terminated:
        logger.info('[Swarm:%s] previous session terminated — recycling key', swarm_key)
        _remove_session(swarm_key)
        session = None
    if session is not None:
        existing_contract = ((session._parent_task_proxy.get('config') or {})
                             .get('_toolOrchestration'))
        existing_contract = (existing_contract
                             if isinstance(existing_contract, dict) else {})
        existing_read_only = (
            str(existing_contract.get('multiAgent') or '').lower()
            == 'read_only')
        if read_only_orchestration and not existing_read_only:
            # Never add an allegedly read-only wave to a session whose worker
            # catalog was created with standard/mutating authority.
            return _swarm_tool_error(
                'orchestration_session_mode_conflict', retryable=True,
                message=(
                    'An existing standard-authority swarm is still active for '
                    'this conversation. Wait for it to settle before starting '
                    'a read-only orchestration wave.'),
                next_action=(
                    'Wait for the active standard-authority swarm to settle '
                    'before starting a read-only orchestration wave.'))
        if existing_read_only:
            # Follow-up waves inherit the session's original authority even
            # when the current root round no longer carries the first-round
            # auto-routing decision.
            read_only_orchestration = True
            orchestration = existing_contract
    # A fresh wave must be able to enqueue <swarm-update>s even if a prior
    # wave on this key was explicitly aborted (which tombstoned the inbox).
    agent_inbox.untombstone(swarm_key)

    swarm_id_existing = bool(session)
    # Per-agent logs stay task-scoped. Cross-turn reads require the durable
    # checkpoint's authorized spawning task, never a global log-name search.
    output_dir = _resolve_output_dir(task_id)

    # Conversation id for the durable push channel. Unlike ``on_event`` (the
    # SSE stream of the SPAWNING turn, which dies when that turn ends), the
    # /api/push WebSocket is conversation-global and survives turn end — so a
    # detached swarm's later events (esp. the terminal ``swarm_phase:complete``
    # that clears the "N running async" badge) still reach the browser when the
    # agents finish AFTER the turn that spawned them. Without this the panel
    # stays stuck on "running" forever (the bug this fixes).
    push_conv_id = (task.get('convId') or cfg.get('convId') or '')

    def _emit(ev: dict):
        # The SSE sink belongs to the SPAWNING turn and can raise once that
        # turn detaches (its stream is closed). Isolate it so a dead SSE sink
        # can NEVER prevent the push mirror below from running — otherwise the
        # frame at the live→detached boundary (often the terminal
        # swarm_phase:complete) is lost and the panel sticks on "running".
        if on_event:
            try:
                on_event(ev)
            except Exception as e:
                logger.debug('[Swarm:%s] SSE emit failed (turn likely detached): %s', task_id, e)
        if push_conv_id:
            try:
                from lib.agent_core.push import push_event
                push_event(
                    'swarm', push_conv_id,
                    {**ev, 'swarmId': session.task_id if session else task_id},
                    user_id=owner_id,
                )
            except Exception as e:
                # The push mirror is the ONLY channel that survives the
                # spawning turn's end. Its failure silently strands every
                # later event (incl. the terminal swarm_phase:complete) for a
                # detached panel — that is an error path, not chatter.
                logger.warning('[Swarm:%s] push mirror failed — detached panel '
                               'may miss event %s: %s',
                               task_id, ev.get('type'), e)

    # Resolve the settle hook through the facade package so a test that
    # patches ``_maybe_autocontinue`` / ``_start_autocontinue_turn`` on the
    # ``lib.swarm.integration`` module still drives the settle path.
    # A parent task can launch multiple sequential sessions. Persist a fresh
    # identity per session, not the reused task alias or model-chosen agent ids.
    continuation_source = f'{task_id}:{uuid.uuid4().hex}'

    def _on_settled(k=swarm_key, owner=owner_id, source=continuation_source):
        import lib.swarm.integration as _pkg
        return _pkg._maybe_autocontinue(k, owner, source_id=source)

    deduped_dropped: list[SubTaskSpec] = []
    if session is None:
        conv_id = task.get('convId', cfg.get('convId', '')) or ''
        from lib.tasks_pkg.child_invocation import (
            inherit_child_config, snapshot_conversation_child_policy,
        )
        parent_cfg = inherit_child_config(task.get('config') or {}, cfg)
        child_policy = snapshot_conversation_child_policy(
            task, model=model, tools=all_tools, project_path=project_path,
            thinking_enabled=thinking_enabled)
        parent_cfg.update(child_policy['conversation_tool_policy'])
        parent_cfg['_swarmContinuationSource'] = continuation_source

        # Bind recovery to the spawning Turn/Attempt, not model-chosen ids or
        # the reusable conversation key. Continue must validate this lineage.
        parent_cfg['_swarmParentTurnId'] = (task.get('config') or {}).get('_turnId', '')
        parent_cfg['_swarmParentAttemptId'] = (task.get('config') or {}).get('_attemptId', '')

        parent_cfg['_swarmParentTaskId'] = task_id

        from lib.swarm.routing import tool_schema_name
        parent_cfg['_swarmToolNames'] = [tool_schema_name(tool) for tool in all_tools]
        parent_cfg['_principalContext'] = task['_principalContext']
        parent_cfg['_transientRuntime'] = bool(
            task.get('_transientRuntime') or task.get('_invocationStorageFree'))
        for _k in ('browserClientId',):
            if _k in (task.get('config') or {}):
                parent_cfg[_k] = task['config'][_k]
            elif _k in cfg:
                parent_cfg[_k] = cfg[_k]
        # A detached session outlives the spawning turn's ephemeral slots.
        # Persist the authorized selection, never the parent's disposable pin.
        if task.get('_requestedModelRef'):
            parent_cfg['modelRef'] = copy.deepcopy(task['_requestedModelRef'])
            parent_cfg['model'] = task['model']
            for key in ('routing', 'preferredProviderId'):
                if key in (task.get('config') or {}):
                    parent_cfg[key] = copy.deepcopy(task['config'][key])
            parent_cfg.pop('_pinned_provider_id', None)
        else:
            _pin = task.get('_pinned_provider_id')
            if _pin:
                parent_cfg['_pinned_provider_id'] = _pin
        if task.get('_tenant_id') is not None:
            parent_cfg['_tenant_id'] = task.get('_tenant_id')
        if read_only_orchestration:
            # Serializable worker contract.  Master copies this into its
            # isolated parent proxy and persistence snapshot; workers then
            # enforce read-only authority and locally compose the PTC lane.
            parent_cfg['_toolOrchestration'] = dict(orchestration)

        session = MasterOrchestrator(
            task_id=task_id,
            conv_id=conv_id,
            user_id=owner_id,
            specs=specs,
            project_path=project_path,
            model=model,
            thinking_enabled=thinking_enabled,
            search_mode=cfg.get('search_mode', 'multi'),
            on_progress=_emit,
            # A session outlives its spawning turn. Never keep a generic
            # "parent no longer running" predicate armed after normal finish.
            abort_check=lambda: bool(task.get('aborted')) or (
                task.get('status') not in ('done', 'completed', 'failed', 'error', 'cancelled')
                and bool(abort_check and abort_check())),
            all_tools=all_tools,
            max_parallel=cfg['max_parallel'],
            max_retries=cfg['max_retries'],
            output_dir=output_dir,
            parent_config=parent_cfg,
            parent_task=task,
            inbox_key=swarm_key,
            on_settled=_on_settled,
            on_agent_usage=_child_usage_rollup(task),
        )
        try:
            _set_session(swarm_key, session, task_id=task_id)
        except SwarmSessionConflict as exc:
            session.abort()
            return _swarm_tool_error(
                'swarm_session_conflict', retryable=True, message=str(exc),
                next_action=(
                    'Retry this conversation after its ongoing recovery '
                    'activation finishes.'))

        # Persist the session so a server restart can rehydrate + resume it.
        # ``config`` carries everything ``_rehydrate_one`` needs to rebuild the
        # sub-agent tool list and model. Best-effort — never blocks the spawn.
        try:
            from lib.swarm import persistence
            persistence.save_session(
                swarm_key,
                conv_id=conv_id, task_id=task_id,
                specs=[s.to_dict() for s in specs],
                config=_persist_config(cfg, model, thinking_enabled,
                                       project_path, parent_cfg,
                                       user_id=owner_id,
                                       parent_task=session._parent_task_proxy,
                                       tool_catalog=session.all_tools),
                status='running')
        except Exception as e:
            logger.debug('[Swarm:%s] session persist failed (non-fatal): %s',
                         swarm_key, e)

        try:
            session.run_in_background()
        except ValueError as e:
            # Cycle detection raised by add_specs
            logger.warning('[Swarm:%s] spawn_agents rejected: %s',
                           swarm_key, e)
            _remove_session(swarm_key)
            return _swarm_tool_error(
                'agent_dependency_cycle', retryable=False,
                message=(
                    'Cycle detected in agent dependencies. Re-issue '
                    'spawn_agents without circular depends_on entries. '
                    f'Detail: {e}'),
                next_action=(
                    'Re-issue spawn_agents without circular depends_on '
                    'entries.'))
        # On a fresh session, run_in_background's add_specs accepted everything
        # (no prior state to dedup against). ``specs`` is already correct.
    else:
        # Existing live session — inject new specs into the running scheduler.
        # A live session reached from a later turn: alias this task_id so
        # await_agents / get_agent_result on THIS turn resolve to it.
        add_session_alias(task_id, swarm_key)
        try:
            accepted_specs = session._scheduler.add_specs(specs)  # type: ignore[union-attr]
        except ValueError as e:
            logger.warning('[Swarm:%s] follow-up spawn rejected: %s',
                           swarm_key, e)
            return _swarm_tool_error(
                'agent_dependency_cycle', retryable=False,
                message=(
                    'Cycle detected when adding specs; existing swarm '
                    f'unchanged. Detail: {e}'),
                next_action=(
                    'Re-issue spawn_agents without circular depends_on '
                    'entries.'))
        if accepted_specs:
            # Track followup specs in MasterOrchestrator so ``get_status``
            # (and the /api/v1/swarm/status route) sees the full agent list.
            session.register_followup_specs(
                accepted_specs, parent_turn_id=str(task.get('_turnId') or ''))
            # Update the persisted spec set so a restart rehydrates the full
            # roster (first wave + this follow-up wave).
            try:
                from lib.swarm import persistence
                persistence.save_session(
                    swarm_key,
                    conv_id=session.conv_id, task_id=session.task_id,
                    specs=[s.to_dict() for s in session.specs],
                    config=_persist_config(cfg, model, thinking_enabled,
                                           project_path,
                                           session._parent_task_proxy.get('config') or {},
                                           user_id=session.user_id),
                    status='running')
            except Exception as e:
                logger.debug('[Swarm:%s] followup session persist failed: %s',
                             swarm_key, e)
        if accepted_specs:
            # objective is for the UI agent card — full text, CSS wraps it.
            # Use _emit (not on_event) so a follow-up wave spawned AFTER the
            # turn detached is mirrored to the conv-scoped push channel too —
            # otherwise its new agent cards never reach the detached panel.
            _emit({
                'type': 'swarm_phase', 'phase': 'spawn_more',
                'content': f'🚀 Spawning {len(accepted_specs)} more agent(s) (live)…',
                'swarmKey': swarm_key,
                'agents': [
                    {'agentId': s.id, 'role': s.role,
                     'objective': s.objective,
                     'required_for_completion': s.required_for_completion,
                     'depends_on': list(s.depends_on or [])}
                    for s in accepted_specs
                ],
            })
        accepted_ids = {s.id for s in accepted_specs}
        deduped_dropped = [s for s in specs if s.id not in accepted_ids]
        specs = accepted_specs

    handle = {
        'status':    'async_launched',
        'swarm_id':  task_id,
        # Stable across follow-up turns; swarm_id remains the launch-task alias.
        'swarm_run_id': session.task_id,
        # The conversation-scoped session key — the frontend stamps it onto
        # the swarm panel so its reconciler probes /api/v1/swarm/status with
        # the key that ALWAYS resolves (alias misses were a false-settle
        # vector). Persisted inside toolContent, so it survives reload.
        'swarm_key': swarm_key,
        'is_followup': swarm_id_existing,
        'orchestration': ({
            'backend': 'local_swarm',
            'mode': 'read_only',
            'compositionMode': orchestration.get('compositionMode') or '',
            'programmaticWorkers': bool(
                orchestration.get('programmaticCalling') in ('auto', 'on')),
        } if read_only_orchestration else {
            'backend': 'local_swarm', 'mode': 'standard',
        }),
        'agents': [
            {
                'id':          s.id,
                'role':        s.role,
                # Full objective (not truncated): this handle is the only
                # persisted source the swarm panel re-parses to rebuild agent
                # cards after a reload (_recoverSwarmAgents in
                # streaming_swarm_panel.js), and the card renders it verbatim
                # with CSS wrapping. A [:N] cap here clips the displayed text
                # mid-sentence. The text is the model's own spawn input, so
                # echoing it in full is no new information in its context.
                # (output_file is deliberately absent: neither the model nor
                # the panel consumes the on-disk log path.)
                'objective':   s.objective,
                'required_for_completion': s.required_for_completion,
            }
            for s in specs
        ],
        'message': (
            f'Launched {len(specs)} agent(s) in the background. '
            'Continue with other work — completions arrive automatically '
            'as <swarm-update> user messages carrying the outcome. To wait, use '
            'poll(ids=[<returned agent ids>], wait_s=30); wait_s is bounded '
            'to 60 seconds. No-ID poll() is a snapshot, not a wait for all agents.'),
    }
    if deduped_dropped:
        handle['deduplicated'] = [
            {'id': s.id, 'objective': s.objective[:120]}
            for s in deduped_dropped
        ]
        handle['message'] += (
            f' Note: {len(deduped_dropped)} spec(s) were skipped '
            'because their objective duplicates an already-running '
            'or completed agent — see ``deduplicated`` field.')
    _launched_lane = str(orchestration.get('multiAgent') or '').lower()
    if specs and _launched_lane in ('read_only', 'full'):
        # A successfully launched wave is actual adoption.  Merely exposing
        # spawn_agents/native multi-agent on the wire is projection evidence
        # and is intentionally insufficient for benchmark promotion.  The
        # lane value is the effective session contract (``read_only`` or
        # ``full``); a spontaneous spawn with the lane off records nothing.
        from lib.orchestration.adoption import record_orchestration_execution
        record_orchestration_execution(
            task, lane='multi_agent', kind='agent_wave',
            backend='local_swarm', status='launched',
            agent_count=len(specs))
    return json.dumps(handle, ensure_ascii=False)


def _session_matches_task(session: MasterOrchestrator, task: dict | None,
                          task_id: str) -> bool:
    """Result reads and waits cannot borrow another principal's live session."""
    try:
        if session.user_id != task_user_id(task):
            return False
    except ValueError:
        return False
    conv_id = task.get('convId') or ''
    if conv_id and session.conv_id != conv_id:
        return False
    if not conv_id and session.task_id != task_id:
        return False
    tenant_id = task.get('_tenant_id', (task.get('config') or {}).get('_tenant_id'))
    return session._parent_task_proxy.get('_tenant_id') == tenant_id


# ═══════════════════════════════════════════════════════════
#  await_agents
# ═══════════════════════════════════════════════════════════

def _handle_await_agents(fn_args: dict, *, task_id: str,
                         task: dict | None = None,
                         abort_check: Callable | None = None,
                         pending_input_check: Callable[[], bool] | None = None) -> str:
    # Resolve the conversation-scoped session (Option A): a "continue" turn
    # has a fresh task_id but the live swarm lives under the conv key.
    swarm_key = swarm_key_for(task) if task is not None else task_id
    session = _get_session(swarm_key) or _get_session(task_id)
    if session is not None and not _session_matches_task(session, task, task_id):
        return _swarm_tool_error(
            'swarm_scope_denied', retryable=False,
            message='No accessible swarm in this task scope.',
            next_action='Use a handle returned in the current conversation.')
    if session is not None:
        add_session_alias(task_id, swarm_key)
    if session is None:
        # Preserve authorized durable evidence without claiming a log is a
        # successful final result or a lost worker is still running.
        disk = _await_from_disk(task_id, fn_args, task=task)
        if disk is not None:
            return disk
        return _swarm_tool_error(
            'swarm_not_found', retryable=False,
            message=(
                'No accessible live swarm or recovered outcome in this scope.'),
            next_action=(
                'Check the returned agent handles and conversation before '
                'launching replacement work. For a background command, call poll with its '
                'explicit id and wait_s between 0 and 60. No-ID poll() '
                'is only a snapshot.'))

    ids_in = fn_args.get('ids') or []
    if not isinstance(ids_in, list):
        ids_in = []
    mode = fn_args.get('mode', 'any')
    timeout = fn_args.get('timeout_seconds', 60)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError) as e:
        logger.debug('[Swarm] Bad await timeout %r, defaulting to 60s: %s', timeout, e)
        timeout = 60.0
    _requested_timeout = timeout
    timeout = max(1.0, min(timeout, AWAIT_AGENTS_HARD_CAP_SEC))
    if _requested_timeout > AWAIT_AGENTS_HARD_CAP_SEC:
        # The model asked to block longer than we allow in a single call.
        # This is the silent trap behind "await always times out": a sub-agent
        # whose runtime exceeds the cap can never be awaited to completion in
        # one call, and mode='all' then times out every time. Make it visible.
        logger.warning(
            '[Swarm:%s] await_agents requested timeout=%.0fs CLAMPED to hard '
            'cap %ss — a single await cannot block longer. If agents run '
            'longer than this, the call will time out (agents keep running); '
            'call poll with explicit agent ids and wait_s <= 60.',
            task_id, _requested_timeout, AWAIT_AGENTS_HARD_CAP_SEC)

    from lib.swarm.delivery import stage_tool_results
    result = session.await_agents(
        delivery_ack=lambda rows: stage_tool_results(task, session, rows),
        ids=[str(x) for x in ids_in] or None,
        mode=mode,
        timeout_seconds=timeout,

        **({'abort_check': abort_check} if abort_check is not None else {}),
        **({'pending_input_check': pending_input_check}
           if pending_input_check is not None else {}),
    )
    result['status'] = 'ok'
    return json.dumps(result, ensure_ascii=False)


def _await_from_disk(task_id: str, fn_args: dict, *,
                     task: dict | None = None) -> str | None:
    """Recover explicit ids without promoting unknown evidence to completion."""
    ids_in = fn_args.get('ids') or []
    if not isinstance(ids_in, list) or not ids_in:
        return None
    completed: list[dict] = []
    partial: list[dict] = []
    unknown: list[str] = []
    for sid in dict.fromkeys(str(raw_id) for raw_id in ids_in):
        payload = recover_agent_result(sid, task_id=task_id, task=task)
        if payload is None:
            unknown.append(sid)
            continue
        payload['status'] = payload.pop('agent_status')
        payload['result'] = payload.pop('final_answer')
        if payload['status'] == 'unknown':
            unknown.append(sid)
            partial.append(payload)
        else:
            # Compatibility: completed is the terminal-result batch, not a
            # success count. Each row retains failed/cancelled/etc. status.
            completed.append(payload)
    if not completed and not partial:
        return None
    return json.dumps({
        'status': 'ok',
        'awaited_ids': list(dict.fromkeys(str(x) for x in ids_in)),
        'completed': completed,
        'partial': partial,
        'unknown': unknown,
        'still_running': [],
        'mode': fn_args.get('mode', 'any'),
        'timed_out': False,
        'source': 'recovery',
        'note': ('No live swarm session; recovered evidence only. Terminal '
                 'results retain their actual statuses. Unknown ids are not '
                 'verified as completed or running, and partial_output is '
                 'not a final answer. No blocking wait occurred.'),
    }, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
#  get_agent_result
# ═══════════════════════════════════════════════════════════

def _handle_get_agent_result(fn_args: dict, *, task_id: str,
                             task: dict | None = None) -> str:
    # Batch mode: an ``agent_ids`` array fetches several agents' full bodies
    # in one call (mirrors read_files/web_search/fetch_url batching). Each id
    # is resolved independently — a running/unknown agent produces its own
    # per-entry notice and never aborts the rest of the batch.
    ids_in = fn_args.get('agent_ids')
    if isinstance(ids_in, list) and ids_in:
        results = []
        for raw_id in ids_in:
            sid = str(raw_id or '').strip()
            if not sid:
                continue
            results.append(_resolve_one_agent_result(sid, task_id=task_id, task=task))
        if not results:
            return _swarm_tool_error(
                'invalid_agent_id', retryable=True,
                message='agent_ids contained no usable ids',
                next_action='Provide at least one non-empty agent id.')
        return json.dumps({
            'status':  'ok',
            'results': results,
        }, ensure_ascii=False)

    agent_id = str(fn_args.get('agent_id') or '').strip()
    if not agent_id:
        return _swarm_tool_error(
            'invalid_agent_id', retryable=True,
            message='agent_id (or agent_ids) is required',
            next_action='Provide the agent_id (or agent_ids) to fetch.')
    return json.dumps(
        _resolve_one_agent_result(agent_id, task_id=task_id, task=task),
        ensure_ascii=False)


def _resolve_one_agent_result(agent_id: str, *, task_id: str,
                              task: dict | None = None) -> dict:
    """Resolve one outcome (live session → scoped durable recovery evidence).

    Returns the payload dict (with its own ``status`` field) — the caller
    JSON-encodes it, either standalone (single mode) or inside ``results``
    (batch mode).
    """
    swarm_key = swarm_key_for(task) if task is not None else task_id
    session = _get_session(swarm_key) or _get_session(task_id)
    if session is not None and not _session_matches_task(session, task, task_id):
        return json.loads(_swarm_tool_error(
            'swarm_scope_denied', retryable=False, found=False, agent_id=agent_id,
            message='No accessible agent in this task scope.',
            next_action='Use a handle returned in the current conversation.'))
    if session is not None:
        add_session_alias(task_id, swarm_key)
    if session is not None:
        from lib.swarm.delivery import stage_tool_results
        payload = session.get_agent_result(agent_id,
            delivery_ack=lambda rows: stage_tool_results(task, session, rows))
        if payload.get('found'):
            payload['agent_status'] = payload.get('status', 'unknown')
            payload['status'] = 'ok'
            return payload
        # The live session does not know this id (e.g. recycled session).
        # Consult authorized durable evidence, never unrelated task logs.

    recovered = recover_agent_result(agent_id, task_id=task_id, task=task)
    if recovered is not None:
        return recovered

    if session is None:
        return json.loads(_swarm_tool_error(
            'swarm_not_found', retryable=False, found=False, agent_id=agent_id,
            message=('No accessible live result or recovery evidence for '
                     f'agent {agent_id!r}. Its outcome is not verified.'),
            next_action=('Check the returned agent handle and conversation. '
                         'Do not infer success or duplicate an unverified run.')))
    # Session live but agent genuinely unknown and no log on disk.
    payload = session.get_agent_result(agent_id)
    payload['status'] = 'ok' if payload.get('found') else 'error'
    return payload


# ═══════════════════════════════════════════════════════════
#  Artifact passthrough (master → live session's store)
# ═══════════════════════════════════════════════════════════

def _handle_artifact_tool(fn_name: str, fn_args: dict, task_id: str) -> str:
    session = _get_session(task_id)
    if not session:
        return ('No active swarm session — artifacts require an active '
                'spawn_agents call.')

    store = session.artifact_store

    if fn_name == 'store_artifact':
        key = fn_args.get('key', '')
        content = fn_args.get('content', '')
        if not key:
            return 'Error: key is required'
        store.put(key, content, writer_id='orchestrator',
                  tags=fn_args.get('tags', []))
        return f'Stored artifact "{key}" ({len(content):,} chars)'

    if fn_name == 'read_artifact':
        key = fn_args.get('key', '')
        if not key:
            return 'Error: key is required'
        content = store.get(key)
        if not content:
            available = store.list_keys()
            return (f'Artifact "{key}" not found. '
                    f'Available: {", ".join(available) or "(none)"}')
        return content

    if fn_name == 'list_artifacts':
        return store.summary()

    return f'Unknown artifact tool: {fn_name}'
