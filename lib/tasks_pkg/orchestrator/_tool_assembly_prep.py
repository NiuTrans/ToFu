"""Section 2 tool assembly (pt_03f4cdf1 slice 29).

Extracted 2026-07-31 from ``lib/tasks_pkg/orchestrator/_run.py``
run_task's pre-stream prep, where the block ran inline once per
invocation after config resolution (Section 1) and prefetch kick.
Byte-identical behaviour.

Three steps:

1. ``_assemble_tool_list`` — builds the per-turn tool schema from cfg
   + the mcfg feature flags. All flags are read from the ``mcfg``
   dict exactly as the inline original did (subscript for guaranteed
   keys, ``.get(..., False)`` for human_guidance / scheduler).
2. Pending-swarm force-enable guard — the root fix for the
   get_agent_result / await_agents "非真实工具" rejection desync
   (conv mr2ysg473scxv8). The swarm inbox drain is UNGATED: it injects
   a <swarm-update> instructing the model to call await_agents /
   get_agent_result even when swarmEnabled is false (e.g. a manual
   "continue" turn after an interrupted spawn turn). If a swarm is
   live-or-pending for THIS conversation, those tools MUST be real
   for this turn, or the model obeys the injected instruction and
   gets rejected as a hallucinator — stranding the completed agent
   work. Runs AFTER normal assembly because pending swarm work is itself an
   availability signal even when the composer toggle is currently off.
3. ``task['_tool_schema'] = tool_list`` stash — the compaction
   token-gate accounts for the tool-schema cost (the schema JSON
   ships in every request and the gateway tokenizes all of it, but
   the proactive gate only saw `messages`).

Returns ``(tool_list, has_real_tools)``.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.model_config import _assemble_tool_list


logger = get_logger(__name__)


def assemble_round_tools(cfg, task, mcfg):
    """Assemble this turn's tool schema + apply the pending-swarm guard.

    ``cfg`` / ``task`` / ``mcfg`` are positional carriers (mcfg is the
    resolved model-config dict from Section 1). Returns the 2-tuple
    ``(tool_list, has_real_tools)``. Tool availability remains stable for the
    entire turn and is not tied to a round counter.
    """
    tool_list, has_real_tools = _assemble_tool_list(
        cfg, mcfg['project_path'], mcfg['project_enabled'], task['id'],
        mcfg['search_mode'], mcfg['search_enabled'],
        mcfg['fetch_enabled'],
        mcfg['code_exec_enabled'], mcfg['browser_enabled'],
        mcfg['desktop_enabled'],
        mcfg['swarm_enabled'],
        image_gen_enabled=mcfg['image_gen_enabled'],
        human_guidance_enabled=mcfg.get('human_guidance_enabled', False),
        scheduler_enabled=mcfg.get('scheduler_enabled', False),
        messages=task['messages'],
        conv_id=task.get('convId', ''),
    )

    # ★ Pending-swarm follow-up tools (root fix for the get_agent_result /
    #   await_agents "非真实工具" rejection desync — conv mr2ysg473scxv8).
    #   The swarm inbox drain is UNGATED: it injects a <swarm-update>
    #   instructing the model to call await_agents / get_agent_result even
    #   when swarmEnabled is false (e.g. a manual "continue" turn after an
    #   interrupted spawn turn). If a swarm is live-or-pending for THIS
    #   conversation, those tools MUST be real for this turn, or the model
    #   obeys the injected instruction and gets rejected as a hallucinator —
    #   stranding the completed agent work. Runs after ordinary assembly so a
    #   pending swarm remains recoverable even when the composer toggle is off.
    swarm_enabled = mcfg['swarm_enabled']
    if not swarm_enabled:
        try:
            from lib.swarm.integration import (
                has_live_or_pending_swarm as _has_pending_swarm,
            )
            from lib.swarm.tools import (
                resolve_turn_swarm_tools as _resolve_turn_swarm_tools,
            )
            _pending = _has_pending_swarm(task)
            tool_list, _forced_swarm = _resolve_turn_swarm_tools(
                tool_list, swarm_enabled=False,
                has_pending_or_live=_pending)
            if _forced_swarm:
                has_real_tools = True
                # The pending-swarm correctness override is an execution-catalog
                # override too; otherwise the now-visible tools would be
                # rejected by the task authority check below dispatch.
                _enabled = list(cfg.get(
                    '_executableToolCatalog',
                    cfg.get('_enabledToolCatalog')) or [])
                _enabled_names = {
                    str(((t.get('function') or {}).get('name') or ''))
                    for t in _enabled if isinstance(t, dict)}
                for _tool in tool_list or ():
                    _name = str(((_tool.get('function') or {}).get('name') or '')) \
                        if isinstance(_tool, dict) else ''
                    if _name in _forced_swarm and _name not in _enabled_names:
                        _enabled.append(_tool)
                        _enabled_names.add(_name)
                cfg['_executableToolCatalog'] = list(_enabled)
                cfg['_enabledToolCatalog'] = list(_enabled)
                logger.warning(
                    '[Task %s] conv=%s 🐝 swarm_enabled=False but a '
                    'live-or-pending swarm exists — force-enabling swarm '
                    'tools %s for this turn so the injected <swarm-update> '
                    'can be acted on',
                    task['id'][:8], task.get('convId', '') or '',
                    _forced_swarm)
        except Exception as _e:
            logger.warning('[Task %s] pending-swarm tool force-enable '
                           'skipped: %s', task['id'][:8], _e)

    # Stash the assembled tool schema on the task so the compaction
    # token-gate can account for its cost. The tool-schema JSON ships
    # in every request and the gateway tokenizes all of it, but the
    # proactive gate (_count_tokens_authoritative) only saw `messages`
    # — under-counting by the full tool-schema size. Stashing here
    # (rather than threading through run_compaction_pipeline →
    # force_compact_if_needed → _should_force_compact) keeps the
    # pipeline signatures untouched.
    task['_tool_schema'] = tool_list
    _executable_catalog = cfg.get(
        '_executableToolCatalog', cfg.get('_enabledToolCatalog'))
    task['_executable_tool_catalog'] = list(
        tool_list or []
        if not isinstance(_executable_catalog, list)
        else _executable_catalog)
    # Compatibility for extensions and older task helpers.  Both names point
    # at copies of the same authority snapshot; neither is a wire projection.
    task['_enabled_tool_catalog'] = list(
        task['_executable_tool_catalog'])
    task['_toolExecutionScope'] = str(
        cfg.get('_toolExecutionScope') or 'available')
    task['_toolDiscoveryPolicyByName'] = dict(
        cfg.get('_toolDiscoveryPolicyByName') or {})
    _catalog_size = cfg.get('_toolSearchCatalogSize')
    task['_toolSearchCatalogSize'] = int(
        len(task['_executable_tool_catalog'])
        if _catalog_size is None else _catalog_size)
    task['_toolSearchableCount'] = int(
        cfg.get('_toolSearchableCount') or 0)
    task['_toolSearchMode'] = str(cfg.get('_toolSearchMode') or '')
    task['_toolScriptSafeByName'] = dict(
        cfg.get('_toolScriptSafeByName') or {})
    task['_enabledToolNamespaceByName'] = dict(
        cfg.get('_enabledToolNamespaceByName') or
        cfg.get('_toolNamespaceByName') or {})
    task['_enabledToolSearchTextByName'] = dict(
        cfg.get('_enabledToolSearchTextByName') or {})
    try:
        from lib.context_experiment_flags import normalize_context_experiment_flags
        from lib.tools.gateway import EXECUTE_TOOLS_NAME, SEARCH_TOOLS_NAME
        _search_mode = (task['_toolSearchMode']
                        or normalize_context_experiment_flags(
                            cfg)['tools']['toolSearch'])
        # Provider-boundary strategy decides whether the stable gateway schema
        # is visible (local search only). Admission remains task-owned for
        # recovery/native compatibility: every nested name is resolved against
        # the immutable executable catalog and enters the ordinary approval /
        # hook / executor pipeline.
        task['_tool_gateway_names'] = [EXECUTE_TOOLS_NAME]
        if _search_mode != 'off':
            task['_tool_gateway_names'].append(SEARCH_TOOLS_NAME)
    except Exception as _gateway_exc:
        logger.debug('[Task %s] gateway-name assembly skipped: %s',
                     task['id'][:8], _gateway_exc)
    if isinstance(cfg.get('_toolExposureTelemetry'), dict):
        task['_toolExposureTelemetry'] = dict(cfg['_toolExposureTelemetry'])
    task['_frontendSelectedToolNames'] = list(
        cfg.get('_frontendSelectedToolNames') or [])
    task['_toolNamespaceByName'] = dict(
        cfg.get('_toolNamespaceByName') or {})

    return tool_list, has_real_tools
