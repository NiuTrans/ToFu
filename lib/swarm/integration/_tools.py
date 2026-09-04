"""lib/swarm/integration/_tools.py — swarm tool dispatch + handlers.

Routes the swarm-control tools the master LLM may call:

  * ``spawn_agents``      — fire-and-forget; returns a handle dict
  * ``await_agents``      — blocking wait (capped at 60 s)
  * ``get_agent_result``  — pull one agent's full final answer
  * artifact tools        — proxied to the live session's ArtifactStore

There is **no** synchronous "run swarm and return synthesised answer" path
anymore. The async swarm handle is a JSON object the LLM sees as the tool
result; sub-agent completions arrive on subsequent turns as auto-injected
``<swarm-update>`` user messages.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable

from lib import agent_inbox
from lib.log import get_logger
from lib.swarm.integration._config import (
    AWAIT_AGENTS_HARD_CAP_SEC,
    MAX_SESSIONS,
    _persist_config,
    swarm_key_for,
)
from lib.swarm.integration._logs import (
    _read_agent_log,
    _resolve_output_dir,
)
from lib.swarm.integration._state import (
    _cleanup_stale_sessions,
    _get_session,
    _remove_session,
    _sessions_lock,
    _set_session,
    SwarmSessionCapacityExceeded,
    add_session_alias,
)
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubTaskSpec
from lib.swarm.resource_policy import (
    swarm_max_agents_per_session,
    swarm_max_agents_per_wave,
    swarm_max_parallel,
    swarm_max_retries,
)
from lib.swarm.scheduler import SwarmAgentCapacityExceeded
from lib.tasks_pkg.manager import task_user_id

logger = get_logger(__name__)


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
                       thinking_enabled: bool = False,
                       search_mode: str = 'multi',
                       abort_check: Callable | None = None,
                       on_event: Callable | None = None,
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
    if thinking_enabled:
        cfg['thinking_enabled'] = thinking_enabled
    if search_mode:
        cfg['search_mode'] = search_mode
    model = cfg.get('model', '')
    thinking_enabled = cfg.get('thinking_enabled', False)

    logger.info('[Swarm:%s] tool=%s args_keys=%s', task_id, fn_name, list(fn_args.keys()))

    try:
        if fn_name == 'spawn_agents':
            return _handle_spawn_agents(
                fn_args, task_id=task_id, task=task, cfg=cfg,
                all_tools=all_tools, model=model,
                thinking_enabled=thinking_enabled,
                project_path=project_path,
                abort_check=abort_check, on_event=on_event,
            )

        if fn_name == 'await_agents':
            return _handle_await_agents(fn_args, task_id=task_id, task=task)

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
                         thinking_enabled: bool,
                         project_path: str,
                         abort_check: Callable | None,
                         on_event: Callable | None) -> str:
    # Work on request-local copies: clamping the read-only wave must not mutate
    # the parent task's sticky config or authority catalog for later rounds.
    cfg = dict(cfg or {})
    all_tools = list(all_tools or [])
    agents_data = fn_args.get('agents') or []
    if not agents_data:
        return json.dumps({'error': 'no agents specified', 'status': 'error'})
    if not isinstance(agents_data, list):
        return json.dumps({
            'status': 'error',
            'error': 'invalid_agents',
            'message': 'agents must be an array.',
        })
    wave_limit = swarm_max_agents_per_wave()
    if len(agents_data) > wave_limit:
        return json.dumps({
            'status': 'error',
            'error': 'swarm_wave_capacity',
            'limit': wave_limit,
            'requested': len(agents_data),
            'message': (
                f'This deployment allows at most {wave_limit} agents in one '
                'wave. Combine or prioritize workstreams and retry once.'),
        })

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
    try:
        max_orchestrated_agents = max(1, min(
            int(orchestration.get('maxConcurrentAgents') or 3),
            8,
            wave_limit,
        ))
    except (TypeError, ValueError):
        max_orchestrated_agents = 3
    if (read_only_orchestration
            and len(agents_data) > max_orchestrated_agents):
        return json.dumps({
            'status': 'error',
            'error': 'multi_agent_wave_limit',
            'limit': max_orchestrated_agents,
            'requested': len(agents_data),
            'message': (
                f'This read-only orchestration round allows at most '
                f'{max_orchestrated_agents} agents in one wave. Combine or '
                'prioritize the independent workstreams and retry once.'),
        })
    if read_only_orchestration:
        from lib.swarm.routing import read_only_swarm_tools
        all_tools = read_only_swarm_tools(task, all_tools)
        cfg['max_parallel'] = max(
            1, min(configured_parallel, max_orchestrated_agents))

    specs: list[SubTaskSpec] = []
    for agent_def in agents_data:
        if not isinstance(agent_def, dict):
            return json.dumps({
                'status': 'error',
                'error': 'invalid_agent',
                'message': 'Every agents entry must be an object.',
            })
        try:
            requested_retries = int(
                agent_def.get('max_retries')
                if agent_def.get('max_retries') is not None
                else cfg['max_retries'])
        except (TypeError, ValueError, OverflowError):
            requested_retries = cfg['max_retries']
        spec = SubTaskSpec(
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
        )
        specs.append(spec)

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
        return json.dumps({
            'status': 'error',
            'error': 'swarm_not_found',
            'message': 'No swarm session exists for this conversation.',
        })
    if session is not None and session.is_terminated:
        logger.info('[Swarm:%s] previous session terminated — recycling key', swarm_key)
        _remove_session(swarm_key)
        session = None
    if session is not None:
        session_limit = swarm_max_agents_per_session()
        retained_agents = len(session.specs)
        if retained_agents + len(specs) > session_limit:
            return json.dumps({
                'status': 'error',
                'error': 'swarm_session_capacity',
                'limit': session_limit,
                'retained': retained_agents,
                'requested': len(specs),
                'message': (
                    f'This live swarm can retain at most {session_limit} '
                    'agents. Wait for it to settle, then start a new wave.'),
            })
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
            return json.dumps({
                'status': 'error',
                'error': 'orchestration_session_mode_conflict',
                'message': (
                    'An existing standard-authority swarm is still active for '
                    'this conversation. Wait for it to settle before starting '
                    'a read-only orchestration wave.'),
            })
        if existing_read_only:
            # Follow-up waves inherit the session's original authority even
            # when the current root round no longer carries the first-round
            # auto-routing decision.
            read_only_orchestration = True
            orchestration = existing_contract
            try:
                max_orchestrated_agents = max(1, min(
                    int(orchestration.get('maxConcurrentAgents') or 3),
                    8,
                    wave_limit,
                ))
            except (TypeError, ValueError):
                max_orchestrated_agents = 3
            if len(agents_data) > max_orchestrated_agents:
                return json.dumps({
                    'status': 'error',
                    'error': 'multi_agent_wave_limit',
                    'limit': max_orchestrated_agents,
                    'requested': len(agents_data),
                    'message': (
                        f'This read-only swarm allows at most '
                        f'{max_orchestrated_agents} agents in one wave.'),
                })

    # A fresh wave must be able to enqueue <swarm-update>s even if a prior
    # wave on this key was explicitly aborted (which tombstoned the inbox).
    agent_inbox.untombstone(swarm_key)

    swarm_id_existing = bool(session)
    # Per-agent log streams stay task-scoped on disk (one dir per spawning
    # turn) — the cross-task glob in _read_agent_log already finds them
    # from any later turn, so durable result recovery is unaffected.
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
                push_event('swarm', push_conv_id, ev, user_id=owner_id)
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
    def _on_settled(k=swarm_key, owner=owner_id, source=task_id):
        import lib.swarm.integration as _pkg
        return _pkg._maybe_autocontinue(k, owner, source_id=source)

    deduped_dropped: list[SubTaskSpec] = []
    if session is None:
        conv_id = task.get('convId', cfg.get('convId', '')) or ''
        # Forward only routing-relevant parent config (browserClientId is
        # the main one — per-device playwright pool selection).  Other
        # cfg fields like model / thinking are already passed via direct
        # kwargs above, so no need to duplicate them into parent_config.
        parent_cfg = {}
        for _k in ('browserClientId',):
            if _k in (task.get('config') or {}):
                parent_cfg[_k] = task['config'][_k]
            elif _k in cfg:
                parent_cfg[_k] = cfg[_k]
        # Forward the hard provider pin so sub-agents (which run on their
        # OWN threads) stay bound to the same BYO endpoint as the parent
        # solve — they must not leak onto operator keys either. See
        # lib/llm_dispatch/provider_pin.py.
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
            abort_check=abort_check,
            all_tools=all_tools,
            max_parallel=cfg['max_parallel'],
            max_retries=cfg['max_retries'],
            output_dir=output_dir,
            parent_config=parent_cfg,
            inbox_key=swarm_key,
            on_settled=_on_settled,
        )
        try:
            _set_session(swarm_key, session, task_id=task_id)
        except SwarmSessionCapacityExceeded as exc:
            session.abort()
            return json.dumps({
                'status': 'error',
                'error': 'swarm_server_busy',
                'limit': MAX_SESSIONS,
                'message': str(exc),
            })

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
                                       user_id=owner_id),
                status='running')
        except Exception as e:
            logger.debug('[Swarm:%s] session persist failed (non-fatal): %s',
                         swarm_key, e)

        try:
            session.run_in_background()
        except SwarmAgentCapacityExceeded as e:
            _remove_session(swarm_key)
            return json.dumps({
                'status': 'error',
                'error': 'swarm_session_capacity',
                'message': str(e),
            })
        except ValueError as e:
            # Cycle detection raised by add_specs
            logger.warning('[Swarm:%s] spawn_agents rejected: %s',
                           swarm_key, e)
            _remove_session(swarm_key)
            return json.dumps({
                'status': 'error',
                'error':  str(e),
                'message': (
                    'Cycle detected in agent dependencies. Re-issue '
                    'spawn_agents without circular depends_on entries.'),
            })
        # On a fresh session, run_in_background's add_specs accepted everything
        # (no prior state to dedup against). ``specs`` is already correct.
    else:
        # Existing live session — inject new specs into the running scheduler.
        # A live session reached from a later turn: alias this task_id so
        # await_agents / get_agent_result on THIS turn resolve to it.
        add_session_alias(task_id, swarm_key)
        try:
            accepted_specs = session._scheduler.add_specs(specs)  # type: ignore[union-attr]
        except SwarmAgentCapacityExceeded as e:
            return json.dumps({
                'status': 'error',
                'error': 'swarm_session_capacity',
                'message': str(e),
            })
        except ValueError as e:
            logger.warning('[Swarm:%s] follow-up spawn rejected: %s',
                           swarm_key, e)
            return json.dumps({
                'status': 'error',
                'error':  str(e),
                'message': 'Cycle detected when adding specs; existing swarm unchanged.',
            })
        if accepted_specs:
            # Track followup specs in MasterOrchestrator so ``get_status``
            # (and the /api/v1/swarm/status route) sees the full agent list.
            session.register_followup_specs(accepted_specs)
            # Update the persisted spec set so a restart rehydrates the full
            # roster (first wave + this follow-up wave).
            try:
                from lib.swarm import persistence
                persistence.save_session(
                    swarm_key,
                    conv_id=session.conv_id, task_id=task_id,
                    specs=[s.to_dict() for s in session.specs],
                    config=_persist_config(cfg, model, thinking_enabled,
                                           project_path, {},
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
            'maxConcurrentAgents': max_orchestrated_agents,
        } if read_only_orchestration else {
            'backend': 'local_swarm', 'mode': 'standard',
        }),
        'agents': [
            {
                'id':          s.id,
                'role':        s.role,
                # Full objective (not truncated): this handle is the only
                # persisted source the swarm panel re-parses to rebuild agent
                # cards after a reload (_recoverSwarmAgents in streaming_ui.js),
                # and the card renders it verbatim with CSS wrapping. A [:N]
                # cap here clips the displayed text mid-sentence. The text is
                # the model's own spawn input, so echoing it in full is no new
                # information in its context.
                'objective':   s.objective,
                'output_file': os.path.join(output_dir, f'{s.id}.log'),
            }
            for s in specs
        ],
        'message': (
            f'Launched {len(specs)} agent(s) in the background. '
            'Continue with other work — completions will arrive automatically '
            'as <swarm-update> user messages on later turns. Use '
            'await_agents() if you have nothing else to do, or '
            'get_agent_result(id) when a preview was insufficient.'),
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
    if read_only_orchestration and specs:
        # A successfully launched wave is actual adoption.  Merely exposing
        # spawn_agents/native multi-agent on the wire is projection evidence
        # and is intentionally insufficient for benchmark promotion.
        from lib.orchestration_adoption import record_orchestration_execution
        record_orchestration_execution(
            task, lane='multi_agent', kind='agent_wave',
            backend='local_swarm', status='launched',
            agent_count=len(specs))
    return json.dumps(handle, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
#  await_agents
# ═══════════════════════════════════════════════════════════

def _handle_await_agents(fn_args: dict, *, task_id: str,
                         task: dict | None = None) -> str:
    # Resolve the conversation-scoped session (Option A): a "continue" turn
    # has a fresh task_id but the live swarm lives under the conv key.
    swarm_key = swarm_key_for(task) if task is not None else task_id
    session = _get_session(swarm_key) or _get_session(task_id)
    if session is not None:
        add_session_alias(task_id, swarm_key)
    if session is None:
        # No live session anywhere for this conversation. Fall back to the
        # durable on-disk transcripts so a cross-turn await still returns
        # whatever the (now torn-down) agents produced, instead of a hard
        # "no active swarm" error that strands the user's results.
        disk = _await_from_disk(task_id, fn_args)
        if disk is not None:
            return disk
        return json.dumps({
            'status': 'error',
            'error':  'no active swarm session',
            'message': (
                'No active swarm — call spawn_agents first, or you may have '
                'aborted / let the session expire.'),
        })

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
            'call await_agents again or await specific ids.',
            task_id, _requested_timeout, AWAIT_AGENTS_HARD_CAP_SEC)

    result = session.await_agents(
        ids=[str(x) for x in ids_in] or None,
        mode=mode,
        timeout_seconds=timeout,
    )
    result['status'] = 'ok'
    return json.dumps(result, ensure_ascii=False)


def _await_from_disk(task_id: str, fn_args: dict) -> str | None:
    """Best-effort ``await_agents`` fallback when no live session exists.

    Only works when the caller named explicit ``ids`` — we look each one up
    via the cross-task on-disk transcript glob (``_read_agent_log``). With no
    ids there's nothing to scope to (the spawning task dir is unknown once
    the session is gone), so we return None and let the caller emit the
    standard "no active swarm" error.

    Returns a JSON string shaped like ``await_agents`` output (so the model
    sees a uniform contract), or None if nothing could be recovered.
    """
    ids_in = fn_args.get('ids') or []
    if not isinstance(ids_in, list) or not ids_in:
        return None
    completed: list[dict] = []
    missing: list[str] = []
    for raw_id in ids_in:
        sid = str(raw_id)
        found = _read_agent_log(task_id, sid)
        if found is None:
            missing.append(sid)
            continue
        log_text, _src = found
        completed.append({
            'agent_id':     sid,
            'status':       'completed',
            'source':       'disk',
            'preview':      log_text[:200],
            'final_answer': log_text,
        })
    if not completed:
        return None
    logger.info('[Swarm:%s] await_agents served %d agent(s) from disk '
                '(no live session)', task_id, len(completed))
    out = {
        'status':        'ok',
        'completed':     completed,
        'still_running': [],
        'mode':          fn_args.get('mode', 'any'),
        'timed_out':     False,
        'source':        'disk',
        'note': ('No live swarm session — these results were recovered from '
                 'the durable on-disk transcripts. Metadata (tokens/elapsed) '
                 'is unavailable; the full output is in final_answer.'),
    }
    if missing:
        out['still_running'] = []
        out['unknown'] = missing
        out['note'] += (f' Could not find on-disk transcripts for: '
                        f'{", ".join(missing)}.')
    return json.dumps(out, ensure_ascii=False)


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
            return json.dumps({
                'status': 'error',
                'error':  'agent_ids contained no usable ids',
            })
        return json.dumps({
            'status':  'ok',
            'results': results,
        }, ensure_ascii=False)

    agent_id = (fn_args.get('agent_id') or '').strip()
    if not agent_id:
        return json.dumps({
            'status': 'error',
            'error':  'agent_id (or agent_ids) is required',
        })
    return json.dumps(
        _resolve_one_agent_result(agent_id, task_id=task_id, task=task),
        ensure_ascii=False)


def _resolve_one_agent_result(agent_id: str, *, task_id: str,
                              task: dict | None = None) -> dict:
    """Resolve one agent's full result payload (live session → on-disk log).

    Returns the payload dict (with its own ``status`` field) — the caller
    JSON-encodes it, either standalone (single mode) or inside ``results``
    (batch mode).
    """
    swarm_key = swarm_key_for(task) if task is not None else task_id
    session = _get_session(swarm_key) or _get_session(task_id)
    if session is not None:
        add_session_alias(task_id, swarm_key)
    if session is not None:
        payload = session.get_agent_result(agent_id)
        if payload.get('found'):
            payload['status'] = 'ok'
            return payload
        # Session is live but doesn't know this agent_id (e.g. recycled
        # session). Fall through to the on-disk fallback before giving up.

    # No live session, OR the live session lost this result — recover the
    # full transcript from the durable per-agent log file. This also covers
    # the common cross-task case: the result is asked for on a LATER turn
    # (fresh task_id) than the one that spawned the swarm.
    found = _read_agent_log(task_id, agent_id)
    if found is not None:
        log_text, source_path = found
        cross_task = os.path.dirname(source_path) != _resolve_output_dir(task_id)
        logger.info('[Swarm:%s] get_agent_result(%s) served from disk '
                    '(session %s, %s)', task_id, agent_id,
                    'gone' if session is None else 'lacked result',
                    'cross-task' if cross_task else 'same-task')
        return {
            'status':       'ok',
            'found':        True,
            'agent_id':     agent_id,
            'source':       'disk',
            'final_answer': log_text,
            'note': ('Served from the on-disk transcript — the live swarm '
                     'session for this result is no longer in memory, so '
                     'metadata (tokens/elapsed/role) is unavailable. The '
                     'full streamed output is the final_answer field.'),
        }

    if session is None:
        return {
            'status': 'error',
            'error':  'no active swarm session',
            'message': ('No active swarm and no on-disk transcript for '
                        f'agent {agent_id!r} — perhaps it ended. Use '
                        'spawn_agents to start a new one.'),
        }
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
