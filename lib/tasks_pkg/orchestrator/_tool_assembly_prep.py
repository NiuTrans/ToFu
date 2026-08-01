"""Section 2 tool assembly (pt_03f4cdf1 slice 29).

Extracted 2026-07-31 from ``lib/tasks_pkg/orchestrator/_run.py``
run_task's pre-stream prep, where the block ran inline once per
invocation after config resolution (Section 1) and prefetch kick.
Byte-identical behaviour.

Four steps:

1. VU startup phase line — emitted via the passed ``vu_phase``
   closure (the run_task-local adapter over
   ``_vu_startup._vu_phase``; same seam style as
   ``restore_tool_history``).
2. ``_assemble_tool_list`` — builds the per-turn tool schema from cfg
   + the mcfg feature flags. All flags are read from the ``mcfg``
   dict exactly as the inline original did (subscript for guaranteed
   keys, ``.get(..., False)`` for human_guidance / scheduler).
3. Pending-swarm force-enable guard — the root fix for the
   get_agent_result / await_agents "非真实工具" rejection desync
   (conv mr2ysg473scxv8). The swarm inbox drain is UNGATED: it injects
   a <swarm-update> instructing the model to call await_agents /
   get_agent_result even when swarmEnabled is false (e.g. a manual
   "continue" turn after an interrupted spawn turn). If a swarm is
   live-or-pending for THIS conversation, those tools MUST be real
   for this turn, or the model obeys the injected instruction and
   gets rejected as a hallucinator — stranding the completed agent
   work. Runs AFTER assembly so it BYPASSES the per-conversation
   tool-schema latch — correctness of the pending turn wins over
   prompt-cache stability.
4. ``task['_tool_schema'] = tool_list`` stash — the compaction
   token-gate accounts for the tool-schema cost (the schema JSON
   ships in every request and the gateway tokenizes all of it, but
   the proactive gate only saw `messages`).

Returns ``(tool_list, has_real_tools, max_tool_rounds)``.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.model_config import _assemble_tool_list


logger = get_logger(__name__)


def assemble_round_tools(cfg, task, mcfg, *, vu_phase):
    """Assemble this turn's tool schema + apply the pending-swarm guard.

    ``cfg`` / ``task`` / ``mcfg`` are positional carriers (mcfg is the
    resolved model-config dict from Section 1); ``vu_phase`` is the
    keyword-only VU progress closure. Returns the 3-tuple
    ``(tool_list, has_real_tools, max_tool_rounds)`` exactly as the
    inline original produced them.
    """
    vu_phase('Autopilot：装配工具、准备工作区…')
    tool_list, has_real_tools, max_tool_rounds = _assemble_tool_list(
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
    #   stranding the completed agent work. Runs AFTER assembly (and after
    #   latch_tool_list) so it BYPASSES the per-conversation tool-schema
    #   latch — correctness of the pending turn wins over prompt-cache
    #   stability.
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
                # If assembly produced NO tools (max_tool_rounds=0), the
                # forced swarm tools would be dead on arrival — lift the
                # cap to the same "unlimited" the assembler uses.
                if not max_tool_rounds:
                    max_tool_rounds = 999_999_999
                logger.warning(
                    '[Task %s] conv=%s 🐝 swarm_enabled=False but a '
                    'live-or-pending swarm exists — force-enabling swarm '
                    'tools %s for this turn so the injected <swarm-update> '
                    'can be acted on (bypassing tool-schema latch)',
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

    return tool_list, has_real_tools, max_tool_rounds
