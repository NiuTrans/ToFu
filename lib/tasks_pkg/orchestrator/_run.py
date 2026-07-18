# HOT_PATH — functions in this module are called per-request.
# Prefer logger.debug() over logger.info(). logger.info() is reserved
# for rare, high-signal events (e.g. content-filter injection, per-round diagnostics).
"""Orchestrator main loop — ``run_task`` kept as ONE whole function.

This is the hottest path in the codebase.  The phased Planner/tool loop
with SSE emission + round accounting lives here as a single function; the
per-turn / finalize helpers live in sibling modules (``_finalize``).

CRITICAL: ``build_body`` is resolved THROUGH the package facade at call
time so a reassignment of ``orchestrator.build_body`` steers the loop.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from lib.log import get_logger, set_req_id

logger = get_logger(__name__)


from lib.llm import AbortedError
from lib.tasks_pkg.attachments import compute_turn_attachments, inject_attachments
from lib.tasks_pkg.cache_tracking import (
    detect_cache_break,
    get_prev_turn_cache_read,
    log_round_cache_stats,
    sort_tool_results,
)
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.compaction import run_compaction_pipeline
from lib.tasks_pkg.llm_fallback import _llm_call_with_fallback
from lib.tasks_pkg.manager import (
    _strip_base64_for_snapshot,
    append_event,
    checkpoint_task_partial,
    persist_task_result,
    stream_llm_response,  # noqa: F401  (re-exported by the package facade)
)
from lib.tasks_pkg.commit_round import (  # noqa: E402
    _run_commit_round_async,  # noqa: F401  (re-export for back-comp)
    _spawn_async_commit_round,  # noqa: F401  (re-exported by the package facade)
    _spawn_async_profile_consolidation,  # noqa: F401  (re-exported by the facade)
    derive_round_modified_files,  # noqa: F401  (re-exported by the facade)
)
from lib.tasks_pkg.message_builder import inject_tool_history
from lib.tasks_pkg.model_config import (
    _assemble_tool_list,
    _resolve_model_config,
)
from lib.tasks_pkg.stream_handler import analyse_stream_result
from lib.tasks_pkg.system_context import (
    _inject_system_contexts,
    _disabled_prompt_blocks,
    inject_search_addendum_to_user,
)
from lib.tasks_pkg.wire_messages import apply_wire_sanitize
from lib.tasks_pkg.server_message_store import (
    rebuild_messages_with_history as _rebuild_messages_with_history,
    save_messages as _save_messages_to_store,
    estimate_token_overhead as _estimate_token_overhead,
)
from lib.tasks_pkg.tool_dispatch import (
    emit_tool_exec_phase,
    execute_tool_pipeline,
    parse_tool_calls,
    tool_label,  # noqa: F401  (re-exported by the package facade)
)



# Resolve the REBINDABLE ``build_body`` binding THROUGH the package facade
# at CALL time (never bind at import): a test/consumer that reassigns
# ``orchestrator.build_body`` MUST steer this loop.
import lib.tasks_pkg.orchestrator as _o

# Per-turn / finalize helpers live in the sibling ``_finalize`` module.
from lib.tasks_pkg.orchestrator._finalize import (
    _discard_pretool_prose,
    _emit_tool_round_phase,
    _finalize_and_emit_done,
    _maybe_auto_retry_turn,
    _compute_write_breakdown,
)





# ══════════════════════════════════════════════════════════
#  run_task — main orchestration loop
# ══════════════════════════════════════════════════════════
def run_task(task: dict[str, Any]) -> None:
    """Main orchestration loop: streams LLM response and dispatches tool calls.

    Parameters
    ----------
    task : dict[str, Any]
        Live task dict created by ``create_task()``.  Mutated in place
        throughout the run (content, usage, status, events, etc.).
    """
    if 'id' not in task:
        raise ValueError("run_task called with a task dict missing 'id' — did you forget to use create_task()?")
    tid = task['id'][:8]
    # Seed the thread-local request-id so audit_log / log_exception / log_context
    # (which auto-stamp req_id) correlate to THIS task. run_task executes on a
    # pooled background thread where req_id() would otherwise be empty, leaving
    # every audit line and swallowed-exception trace un-attributable.
    set_req_id(tid)
    # ★ Autopilot kick-from-idle: a carrier task that runs ONLY the virtual-user
    #   hook (no worker LLM turn).  The conversation already ended and the last
    #   message is the agent's reply, so the simulated user answers it directly.
    #   See lib.tasks_pkg.autopilot._run_autopilot_kick.
    if task.get('_autopilot_kick'):
        from lib.tasks_pkg.autopilot import _run_autopilot_kick
        _run_autopilot_kick(task)
        return
    # ★ Pristine turn-input snapshot for turn-level auto-retry.
    #   run_task mutates a LOCAL copy of messages (system-context injection,
    #   tool-history rebuild, completed tool rounds) and writes it back to
    #   task['messages'] on exit — so on a transient-error re-run we must
    #   restore the ORIGINAL input first, or the re-run would double-inject
    #   system blocks and replay a half-finished round. Captured ONCE and
    #   preserved across every retry attempt (see _maybe_auto_retry_turn).
    if not task.get('_endpoint_managed') and '_turn_input_messages' not in task:
        task['_turn_input_messages'] = list(task.get('messages') or [])
    # ★ Timing: thread picked the task up. Compare against '_t_created'
    #   (set in create_task) to measure how long the user "waited" before the
    #   background worker even started — i.e. thread-pool / queue latency.
    _t_run_start = time.time()
    _t_created = task.get('_t_created')
    if _t_created:
        logger.info('[Timing:%s] queue_wait=%.3fs (create→run_task)',
                    tid, _t_run_start - _t_created)
    # ★ Task START bracket — logged with the FULL task id (not the 8-char
    #   prefix) so a user can copy the id from the cost popover and grep the
    #   whole turn's lifecycle. Pairs with the '[Task:%s] ■ DONE' summary at
    #   completion. Every per-round line in between is tagged [<tid8>] via the
    #   thread-local req_id set just above.
    logger.info('[Task:%s] ▶ START conv=%s msgs=%d',
                task['id'], task.get('convId', '') or '-',
                len(task.get('messages') or []))
    try:
        cfg = task['config']

        # ── Autopilot VU startup attribution ──
        #   The VU sub-task's ``events`` is a _VUEventForwarder, so any PHASE
        #   emitted here auto-forwards into the synthetic-user bubble. The
        #   pre-stream prep window (tool assembly → tool-history rebuild →
        #   system-context injection → FUSE memory/project prefetch) is
        #   otherwise SILENT for up to tens of seconds on a large conversation
        #   (measured 2.9–4.7s typical, ~26s on a 3000-event conv), leaving the
        #   bubble on a vague placeholder. Naming each real sub-step keeps the
        #   display honest. Gated on ``_vu_subtask`` so the ordinary
        #   worker/endpoint startup path stays byte-identical (no new events).
        _vu_startup = bool(task.get('_vu_subtask'))

        def _vu_phase(detail):
            if not _vu_startup:
                return
            try:
                append_event(task, build_event(
                    EventType.PHASE, phase='working', detail=detail))
            except Exception as _e:
                logger.debug('[Task %s] vu startup phase emit failed: %s', tid, _e)

        # ── Reset swarm auto-continue chain on HUMAN turns ──
        # A human-initiated turn (NOT itself a swarm auto-continuation) means
        # the user is back in the loop, so the consecutive-auto-continue
        # ceiling should start fresh. Auto-continue turns carry
        # ``_swarmAutoContinue`` and must NOT reset the counter (that's what
        # bounds a runaway unattended loop). See lib/swarm/integration.py.
        if not cfg.get('_swarmAutoContinue'):
            try:
                from lib.swarm.integration import (reset_autocontinue_chain,
                                                    swarm_key_for)
                reset_autocontinue_chain(swarm_key_for(task))
            except Exception as _e:
                logger.debug('[Task %s] autocontinue chain reset failed: %s', tid, _e)

        # ── Capability profile: merge named profile defaults UNDER the
        #    explicit cfg (explicit caller values always win).  No-op when
        #    cfg has no 'profile' key or selects the empty 'default'.  Applied
        #    here — before model resolution + tool assembly — so every
        #    downstream consumer sees the merged values.
        from lib.agent_core.profiles import apply_profile, resolve_profile_name
        _profile_name = resolve_profile_name(cfg)
        if _profile_name != 'default':
            cfg = apply_profile(cfg)
            task['config'] = cfg

        # ── Per-client browser routing: set thread-local client ID so all
        #    browser commands (tools, fetch fallback, search fallback) from
        #    this task thread route to the correct device's extension. ──
        _browser_client_id = cfg.get('browserClientId')
        if _browser_client_id:
            from lib.browser import _set_active_client
            _set_active_client(_browser_client_id)
            logger.debug('[Task %s] Browser client routed to %s', tid, _browser_client_id[:12])

        # ── Hard provider pin (multi-tenant isolation) ──
        # When this task was created from an inline `provider` block or a
        # registered @prov_xxx BYO endpoint, bind THIS worker thread to that
        # provider so every LLM dispatch on it (main solve, L2/advanced
        # compaction summaries, endpoint replan turns) can only pick that
        # provider's slot — never silently falling back to an operator key
        # and eating a 429. Cleared in the finally block because worker
        # threads are pooled and reused. See lib/llm_dispatch/provider_pin.py.
        from lib.llm_dispatch.provider_pin import set_pinned_provider
        _pinned_provider_id = task.get('_pinned_provider_id') or ''
        if _pinned_provider_id:
            set_pinned_provider(_pinned_provider_id)
            logger.info('[Task %s] Provider-pinned to %s (hard isolation)',
                        tid, _pinned_provider_id)

        # ── Conversation-sticky routing ──
        # Bind this worker thread to the conversation so every LLM dispatch on
        # it prefers the API key that last served this conv — keeping the
        # Anthropic per-key prompt cache warm across rounds. Soft preference:
        # the picker still falls back to a healthy key if the sticky one is
        # cooled down. Cleared in the finally block (pooled threads).
        # See lib/llm_dispatch/conv_affinity.py.
        from lib.llm_dispatch.conv_affinity import set_conv_affinity
        set_conv_affinity(task.get('convId') or '')

        # ── Section 1: Config & Model Resolution ──
        mcfg = _resolve_model_config(cfg, task['id'])
        model           = mcfg['model']
        thinking_enabled = mcfg['thinking_enabled']
        thinking_depth  = mcfg['thinking_depth']
        preset          = mcfg['preset']
        max_tokens      = mcfg['max_tokens']
        temperature     = mcfg['temperature']
        search_mode     = mcfg['search_mode']
        response_format = mcfg.get('response_format')
        search_enabled  = mcfg['search_enabled']
        fetch_enabled   = mcfg['fetch_enabled']
        project_path    = mcfg['project_path']
        project_enabled = mcfg['project_enabled']
        if project_enabled and project_path:
            # ★ Extract extra root paths from projectPaths (frontend sends all roots).
            #   projectPaths[0] = primary (same as projectPath), rest are extras.
            _all_paths = cfg.get('projectPaths') or []
            _extra_paths = [p for p in _all_paths[1:] if p and p != project_path] if len(_all_paths) > 1 else []
            # ★ Read-only roots: a subset of the configured paths the user
            #   attached for reference only. Writes/edits/create_project and
            #   destructive run_command targeting these are refused; reads are
            #   always allowed. Empty list = today's all-writable behaviour.
            _readonly_paths = [p for p in (cfg.get('readOnlyPaths') or []) if p]
            logger.info('[Task:%s] project_path=%s extra_roots=%d readonly=%d',
                        task['id'], project_path, len(_extra_paths),
                        len(_readonly_paths))
            # ★ Ensure the server's global project state matches this task's
            # project path + extras.  Another conversation may have switched the
            # server to a different project, causing get_context_for_prompt to miss
            # the file tree (path mismatch → no tree in system prompt → LLM
            # doesn't know the project structure → "backend cannot use tools").
            from lib.project_mod import ensure_project_state
            # ★ Pass conv_id for per-conversation root isolation (2026-05-05).
            #   Prevents concurrent tasks from clobbering each other's
            #   workspace-root namespace when they call set_project with
            #   different primary paths. See lib/project_mod/config.py
            #   ::set_conv_roots docstring for background.
            _conv_id_for_roots = task.get('convId') or task.get('id') or ''
            ensure_project_state(project_path, extra_paths=_extra_paths,
                                 conv_id=_conv_id_for_roots,
                                 readonly_paths=_readonly_paths)
            # ── Presence: announce this conversation as a live peer of the
            #    project root (the "who is working here now" feed). Idempotent
            #    per convId — an autopilot follow-up turn refreshes the SAME
            #    peer rather than spawning a new one. Best-effort; a presence
            #    failure must never affect the task.
            if task.get('convId'):
                try:
                    from lib.presence import announce as _presence_announce
                    _presence_announce(
                        project_path, task['convId'],
                        task_id=task['id'],
                        run_id=cfg.get('autopilotRunId') or '',
                        title=cfg.get('convTitle') or '',
                        objective=cfg.get('autopilotObjective') or '',
                        phase='working',
                    )
                except Exception as _pe:
                    logger.debug('[Task:%s] presence announce failed: %s',
                                 task['id'][:8], _pe)
            # ── File-history: capture any external (IDE) edits made between rounds.
            #
            #   Runs SILENTLY in a background thread: no phase event, no UI
            #   status — the LLM response starts streaming immediately.  Cost
            #   is bounded by the size of the tracked-files set (files the
            #   assistant has touched this session), not the worktree, so
            #   this is cheap even on slow filesystems.
            #
            #   Correctness guard: if the round has already started mutating
            #   files by the time the probe finishes, we skip the synthetic
            #   external-edit snapshot to avoid misattribution.  The next
            #   round's probe catches the drift cleanly on top of a stable
            #   timeline.
            try:
                from lib import file_history as fh

                if fh.is_enabled() and fh.probe_enabled():
                    def _probe_external_edits():
                        try:
                            if task.get('modifiedFileList') or task.get('modifiedFiles'):
                                logger.debug('[Task:%s] skipping external-edit probe '
                                             '— round already mutated files',
                                             task['id'][:8])
                                return
                            # Pass the set of known Tofu task ids so the probe
                            # can tell a CONCURRENT conversation's write on the
                            # shared project root (last_writer_task_id ∈ known)
                            # from a genuine out-of-band IDE edit — the former
                            # must NOT surface as an "edited outside Tofu" toast.
                            try:
                                from lib.tasks_pkg.manager import (
                                    tasks as _known_tasks,
                                    tasks_lock as _known_tasks_lock,
                                )
                                with _known_tasks_lock:
                                    _known_task_ids = set(_known_tasks.keys())
                            except Exception as _kte:
                                logger.debug('[Task:%s] known-task-id snapshot '
                                             'failed: %s', task['id'][:8], _kte)
                                _known_task_ids = None
                            _ext = fh.detect_external_edits(
                                project_path, known_task_ids=_known_task_ids)
                            if _ext.get('siblingFiles'):
                                logger.info('[Task:%s] external-edit probe '
                                            'attributed %d drifted file(s) to '
                                            'concurrent Tofu task(s) — suppressed '
                                            'IDE toast', task['id'][:8],
                                            len(_ext.get('siblingFiles', [])))
                            if (task.get('modifiedFileList')
                                    or task.get('modifiedFiles')):
                                logger.debug('[Task:%s] external-edit probe '
                                             'completed after round started '
                                             'mutating files — not emitting '
                                             'SSE event (attribution ambiguous)',
                                             task['id'][:8])
                                return
                            if _ext.get('committed'):
                                append_event(task, build_event(
                                    EventType.PROJECT_EXTERNAL_EDIT,
                                    files=_ext.get('files', []),
                                    sha=_ext.get('snapshotId'),
                                ))
                                logger.info('[Task:%s] captured %d external edit(s) snap=%s',
                                            task['id'][:8], len(_ext.get('files', [])),
                                            (_ext.get('snapshotId') or '')[:8])
                        except Exception as e:
                            logger.warning('[Task:%s] external-edit detection failed: %s',
                                           task['id'][:8], e)

                    threading.Thread(
                        target=_probe_external_edits,
                        name=f'ext-edit-probe-{task["id"][:8]}',
                        daemon=True,
                    ).start()
            except Exception as e:
                logger.warning('[Task:%s] could not start external-edit probe: %s',
                               task['id'][:8], e)
        code_exec_enabled = mcfg['code_exec_enabled']
        memory_enabled  = mcfg['memory_enabled']
        browser_enabled = mcfg['browser_enabled']
        desktop_enabled = mcfg['desktop_enabled']
        swarm_enabled   = mcfg['swarm_enabled']
        image_gen_enabled = mcfg['image_gen_enabled']
        human_guidance_enabled = mcfg.get('human_guidance_enabled', False)
        scheduler_enabled = mcfg.get('scheduler_enabled', False)
        # ── Memory Prefetch: start loading project and memory contexts in
        #    background threads while tool assembly runs (FUSE I/O can be slow).
        #    Inspired by Claude Code's startRelevantMemoryPrefetch().
        from concurrent.futures import ThreadPoolExecutor as _PrefetchPool
        _prefetch_executor = _PrefetchPool(max_workers=2,
                                           thread_name_prefix='mem-prefetch')
        _prefetch_project_future = None
        _prefetch_memory_future = None

        if project_enabled and project_path:
            _prefetch_conv_id = task.get('convId') or task.get('id') or ''
            def _prefetch_project():
                from lib.project_mod import get_context_for_prompt
                return get_context_for_prompt(project_path,
                                              conv_id=_prefetch_conv_id or None)
            _prefetch_project_future = _prefetch_executor.submit(_prefetch_project)

        # Simple heuristic: if any tool-providing feature is enabled, we'll
        # have real tools → need memory injection + accumulation instructions.
        _has_real_tools_hint = (search_enabled or fetch_enabled or
                                project_enabled or browser_enabled or
                                desktop_enabled or swarm_enabled or
                                code_exec_enabled or image_gen_enabled)
        _pp = project_path if project_enabled else None
        # ★ Extra workspace roots for memory scoping (multi-root session).
        #   Memories are READ (listed / searched / prefetched) across the
        #   primary + every extra root, unioned and de-duplicated; NEW
        #   memories are still written only to the primary project_path.
        #   Mirrors the projectPaths[1:] extraction used for file tools.
        _mem_extra_paths = []
        if project_enabled and _pp:
            _all_mem_paths = cfg.get('projectPaths') or []
            _mem_extra_paths = [p for p in _all_mem_paths[1:]
                                if p and p != _pp] if len(_all_mem_paths) > 1 else []
        # Memory toggle gates EVERYTHING memory-related: the count-hint
        # background load, the per-turn prefetch (BM25 + cheap-LLM rerank),
        # and the accumulation instructions injected into the system prompt.
        # AI still accumulates memories in the background via the
        # search_memories / create_memory tools — only the proactive
        # injection path is muted.
        if memory_enabled:
            def _prefetch_memory():
                from lib.memory import build_memory_context
                return build_memory_context(project_path=_pp,
                                            extra_paths=_mem_extra_paths)
            _prefetch_memory_future = _prefetch_executor.submit(_prefetch_memory)

        # Store prefetch futures on the task for _inject_system_contexts to use
        task['_prefetch_project'] = _prefetch_project_future
        task['_prefetch_memory'] = _prefetch_memory_future

        # ── Section 2: Tool Assembly ──
        _vu_phase('Autopilot：装配工具、准备工作区…')
        tool_list, has_real_tools, max_tool_rounds = _assemble_tool_list(
            cfg, project_path, project_enabled, task['id'],
            search_mode, search_enabled, fetch_enabled,
            code_exec_enabled, browser_enabled, desktop_enabled,
            swarm_enabled,
            image_gen_enabled=image_gen_enabled,
            human_guidance_enabled=human_guidance_enabled,
            scheduler_enabled=scheduler_enabled,
            messages=task['messages'],
            conv_id=task.get('convId', ''),
        )

        # ★ Pending-swarm follow-up tools (root fix for the get_agent_result /
        #   await_agents "非真实工具" rejection desync — conv mr2ysg473scxv8).
        #   The swarm inbox drain below (~L1607) is UNGATED: it injects a
        #   <swarm-update> instructing the model to call await_agents /
        #   get_agent_result even when swarmEnabled is false (e.g. a manual
        #   "continue" turn after an interrupted spawn turn). If a swarm is
        #   live-or-pending for THIS conversation, those tools MUST be real for
        #   this turn, or the model obeys the injected instruction and gets
        #   rejected as a hallucinator — stranding the completed agent work.
        #   `_start_autocontinue_turn` already forces swarmEnabled=True; this
        #   covers the ordinary continue turn, which had no such protection.
        #   Runs AFTER assembly (and after latch_tool_list) so it BYPASSES the
        #   per-conversation tool-schema latch — correctness of the pending
        #   turn wins over prompt-cache stability.
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

        # (Planner no-tools override removed — all endpoint roles now
        #  get full tool access.  See endpoint_review._run_planner_turn.)

        messages = list(task['messages'])
        original_messages = list(messages)
        tool_round_num = 0
        all_search_results_text = []

        # ── Section 2.5: Server-side tool history restoration ──
        # If keepToolHistory is enabled AND we have stored full messages
        # from a previous turn, replace the frontend's summary-only messages
        # with the full tool_use/tool_result history.
        _keep_tool_history = cfg.get('keepToolHistory', True)
        _conv_id = task.get('convId', '')
        if _keep_tool_history and _conv_id:
            _vu_phase('Autopilot：重建工具调用历史…')
            rebuilt, _rebuild_stats = _rebuild_messages_with_history(_conv_id, messages)
            if _rebuild_stats['used_store']:
                # Log the overhead for monitoring
                _oh = _estimate_token_overhead(messages, rebuilt)
                logger.info(
                    '[%s] conv=%s ★ TOOL HISTORY RESTORED: '
                    'frontend=%d msgs → rebuilt=%d msgs '
                    '(tool_msgs=%d, overhead=+%d est_tokens, ratio=%.1fx)',
                    tid, _conv_id[:8],
                    _rebuild_stats['frontend_msg_count'], len(rebuilt),
                    _rebuild_stats['tool_msgs_restored'],
                    _oh['overhead_est_tokens'], _oh['ratio'],
                )
                messages = rebuilt
                original_messages = list(messages)
                # Emit a diagnostic event for the debug panel
                append_event(task, build_event(
                    EventType.PHASE,
                    phase='tool_history_restored',
                    detail=f'Restored {_rebuild_stats["tool_msgs_restored"]} tool messages from server store',
                    stats=_rebuild_stats,
                    overhead=_oh,
                ))
            else:
                logger.debug('[%s] conv=%s keepToolHistory enabled but no stored messages found',
                             tid, _conv_id[:8])

        # ── Section 3: Context Injection ──
        _vu_phase('Autopilot：注入系统上下文（项目结构、记忆检索）…')
        _tool_names = {
            (t.get('function') or {}).get('name')
            for t in (tool_list or [])
            if isinstance(t, dict)
        }
        _tool_names.discard(None)
        _inject_system_contexts(
            messages, project_path, project_enabled,
            memory_enabled, search_enabled, swarm_enabled,
            has_real_tools,
            conv_id=task.get('convId', ''),
            task=task,
            model=model,
            system_prompt_mode=cfg.get('systemPromptMode', 'append'),
            tool_names=_tool_names or None,
            disabled_blocks=_disabled_prompt_blocks(cfg),
        )
        # ★ Preferences-applied chip: if the bounded user-profile was injected
        #   onto the cache-safe _isMeta tail by _inject_system_contexts, emit a
        #   quiet event so the frontend can show "preferences applied" — making
        #   the assistant's awareness of the user's stored preferences VISIBLE.
        _applied_prefs = task.get('_appliedPreferences')
        if _applied_prefs:
            try:
                append_event(task, build_event(
                    EventType.PREFERENCES_APPLIED,
                    chars=_applied_prefs.get('chars', 0),
                    items=_applied_prefs.get('items', []),
                    core=_applied_prefs.get('core', []),
                    detail=_applied_prefs.get('detail', []),
                ))
                task['_preferencesApplied'] = dict(_applied_prefs)
            except Exception as _e:
                logger.debug('[orchestrator] preferences_applied emit failed: %s', _e)

        # ★ Related-conversations chip: if the cross-conversation project
        #   digest was injected by _inject_system_contexts (★4.4), emit a quiet
        #   event so the frontend can show which sibling conversations the
        #   model was made aware of — auditable ambient context.
        _related_convs = task.get('_relatedConversations')
        if _related_convs:
            try:
                append_event(task, build_event(
                    EventType.RELATED_CONVERSATIONS,
                    count=_related_convs.get('count', 0),
                    items=_related_convs.get('items', []),
                    toolsAvailable=_related_convs.get('toolsAvailable', False),
                ))
                task['_relatedConversations'] = dict(_related_convs)
            except Exception as _e:
                logger.debug('[orchestrator] related_conversations emit failed: %s', _e)

        # Cleanup prefetch futures (no longer needed)
        task.pop('_prefetch_project', None)
        task.pop('_prefetch_memory', None)
        _prefetch_executor.shutdown(wait=False)

        # ★ Timing: context assembly complete (config/model resolution, tool
        #   assembly, tool-history restoration, system-context injection — incl.
        #   the FUSE-slow memory/project prefetch). This is the bulk of the
        #   pre-LLM "waiting" window. Stash the anchor on the task so
        #   stream_llm_response can compute time-to-first-token (TTFT).
        _t_prep_done = time.time()
        task['_t_prep_done'] = _t_prep_done
        logger.info('[Timing:%s] prep=%.3fs (run_task→context-ready, '
                    'model=%s) — about to build first LLM request',
                    tid, _t_prep_done - _t_run_start, model)
        _vu_phase('Autopilot：上下文就绪，正在发送请求…')

        # NOTE: Auto-prefetch disabled — the model can fetch URLs on demand
        # via the fetch_url tool call when it deems them relevant, rather than
        # being forced to fetch every URL detected in the user message.
        # if fetch_enabled:
        #     prefetched = _prefetch_user_urls(messages, task)
        #     if prefetched:
        #         tool_round_num = inject_prefetched_urls(messages, prefetched, task)


        logger.debug('[Task %s] conv=%s Start model=%s think=%s search=%s fetch=%s project=%s code_exec=%s',
                    task['id'][:8], task.get('convId', ''), model, thinking_enabled, search_mode, fetch_enabled,
                    'yes' if project_enabled else 'no', 'yes' if code_exec_enabled else 'no')
        tool_call_happened = False
        last_finish_reason = None
        last_usage = None
        assistant_msg = None  # ★ Initialize before loop — prevents UnboundLocalError if loop breaks early
        accumulated_usage = {}  # ★ Accumulate usage across all tool rounds
        api_rounds = []  # ★ Track per-round usage for cost breakdown

        # ★ Inject toolHistory from continue — restore interrupted tool call context
        _injected_tool_calls = inject_tool_history(messages, cfg, task, model)
        if _injected_tool_calls:
            tool_call_happened = True
            tool_round_num = _injected_tool_calls  # offset so new roundNums don't conflict

        # ★ Memory Prefetch (proactive, per-user-turn, round 0 only):
        #   BM25 coarse → cheap-LLM precision → inject <relevant_memories>.
        #   This surfaces past lessons even when the model wouldn't have
        #   thought to call search_memories on its own. Emits SSE
        #   `memory_prefetch` events so the frontend can show an indicator.
        #   Skipped if:
        #     • Memory toggle disabled (memory_enabled=false)
        #     • feature flag disabled
        #     • continue/resume (tool_history was replayed → not a fresh turn)
        #     • no real tools (memory tools unavailable anyway)
        # Stash the consolidation gate for the post-done async spawner
        #   (_spawn_async_profile_consolidation reads this; memory_enabled +
        #   has_real_tools are run_task locals not in _finalize's scope).
        task['_profileConsolidateEligible'] = bool(memory_enabled and has_real_tools)

        if memory_enabled and has_real_tools and not _injected_tool_calls:
            try:
                from lib.memory.prefetch import run_memory_prefetch
                # Active-tools list lets the cheap-LLM filter drop memories
                # about subsystems the user can't currently use (e.g.
                # browser memories when browser is off).
                _active_tools = []
                for _t in (tool_list or []):
                    try:
                        _active_tools.append(_t['function']['name'])
                    except (KeyError, TypeError) as _e_audit:
                        logger.debug('[orchestrator] run_task caught %s: %s', type(_e_audit).__name__, _e_audit)
                        continue
                run_memory_prefetch(
                    messages,
                    project_path=project_path if project_enabled else None,
                    task=task,
                    emit_event=lambda ev: append_event(task, ev),
                    active_tools=_active_tools,
                    extra_paths=_mem_extra_paths,
                )
            except Exception as _e:
                # Advisory path — never block the task on prefetch failure.
                logger.warning('[Task %s] memory prefetch failed: %s',
                               task['id'][:8], _e, exc_info=True)

        # ★ Apply preserved content prefix from Continue — ensures backend checkpoints
        #   include text the LLM generated alongside completed tool rounds in the prior
        #   task, so page-refresh mid-stream doesn't lose that content.
        #
        #   ⚠ IMPORTANT: contentPrefix is NEVER re-injected into `messages` as a
        #   trailing assistant turn.  That would only work against OpenAI-compat
        #   endpoints — Anthropic Messages API rejects a trailing assistant turn
        #   ("This model does not support assistant message prefill. The
        #   conversation must end with a user message.").  Rather than branching
        #   by provider we keep the universal behaviour: use contentPrefix only
        #   as a bookkeeping seed for `task['content']` so the resumed response
        #   displays [preserved text] + [freshly generated continuation].  The
        #   freshly generated part begins from the tool-result checkpoint, which
        #   is replayed via `inject_tool_history` above — that shape every
        #   provider accepts.
        _content_prefix = cfg.get('contentPrefix') or ''
        if _content_prefix:
            with task['content_lock']:
                task['content'] = _content_prefix
            logger.debug('[%s] conv=%s Applied contentPrefix (%d chars) from continue checkpoint',
                         tid, task.get('convId', ''), len(_content_prefix))

        # ★ Resume-prefill (epic pt_cb8f98b0cb9b47fb): the capability-gated
        #   exception to the "never inject contentPrefix as a trailing assistant
        #   turn" rule above. resumePrefill is set ONLY when routes/chat.py's
        #   resume_prefill_from_segments already confirmed the target provider
        #   TOLERATES a trailing assistant prefill (model_supports_assistant_
        #   prefill → False for Claude, so Claude never reaches here). Injecting
        #   the terminal deliverable tail as a trailing assistant turn makes the
        #   model CONTINUE the same tokens (case 2: mid-prose after a tool batch;
        #   case 3: mid-answer no-tool turn) instead of regenerating from the
        #   checkpoint. The tool batch (if any) was already replayed by
        #   inject_tool_history above; the pre-tool prose lives on those
        #   assistant(tool_calls) turns, so the prefill (terminal deliverable
        #   only) never double-counts. task['content'] is seeded with the FULL
        #   prior content (contentPrefix) so display = full + continuation.
        #
        #   Defence in depth: even if a dispatcher model-swap routed this to
        #   Claude after the gate, _strip_trailing_assistant_for_claude() in
        #   build_body()/dispatch_stream() would neutralise the trailing turn
        #   (the Claude-4.6 prefill-removal guard) — so a leak degrades to
        #   today's regenerate-from-checkpoint, never an HTTP 400.
        _resume_prefill = cfg.get('resumePrefill') or ''
        from lib.model_info import model_supports_assistant_prefill
        if _resume_prefill and model_supports_assistant_prefill(model):
            messages.append({'role': 'assistant', 'content': _resume_prefill})
            task['_resumePrefill'] = _resume_prefill
            logger.info('[%s] conv=%s Injected resume prefill (%d chars) as trailing '
                        'assistant turn — model=%s will continue the same tokens',
                        tid, task.get('convId', ''), len(_resume_prefill), model)
        elif _resume_prefill:
            logger.info('[%s] conv=%s resumePrefill present but model=%s rejects prefill '
                        '— falling back to regenerate-from-checkpoint (contentPrefix seed only)',
                        tid, task.get('convId', ''), model)

        # ★ Stash checkpoint metadata for merging into done event and DB persistence.
        #   NOTE: we do NOT pre-populate task['toolRounds'] with checkpoint rounds
        #   because the frontend's state/delta handlers would double-count them
        #   (frontend does _continueToolRounds.concat(ev.toolRounds)).  Instead,
        #   checkpoint rounds are merged only when writing to DB and in the done event.
        _checkpoint_tr = cfg.get('checkpointToolRounds') or []
        if _checkpoint_tr:
            task['_checkpointToolRounds'] = list(_checkpoint_tr)
            logger.debug('[%s] conv=%s Stashed %d checkpoint toolRounds for DB merge',
                         tid, task.get('convId', ''), len(_checkpoint_tr))
        if cfg.get('checkpointUsage'):
            task['_checkpointUsage'] = cfg['checkpointUsage']
        if cfg.get('checkpointApiRounds'):
            task['_checkpointApiRounds'] = cfg['checkpointApiRounds']
        if cfg.get('checkpointModifiedFiles'):
            task['_checkpointModifiedFiles'] = cfg['checkpointModifiedFiles']
        if cfg.get('checkpointModifiedFileList'):
            task['_checkpointModifiedFileList'] = cfg['checkpointModifiedFileList']

        # ★ 禁止添加 anti-loop / 预算警告 / _force_stop 等机制。
        #   不允许在运行时向 messages 注入任何 [SYSTEM NOTE] 或 [SYSTEM:] 消息来
        #   干扰模型的正常生成。详见 max_tool_rounds 注释。

        _loop_exit_reason = 'max_rounds_exhausted'  # ★ DIAGNOSTIC: track why the loop ended
        _abort_detected_phase = None  # ★ Track exactly WHEN abort was detected
        _premature_retry_count = 0    # ★ Track retries for PREMATURE STREAM CLOSE
        _PREMATURE_RETRY_MAX = 2      # ★ Max premature-close retries (must match stream_handler)
        _consecutive_tool_timeouts = 0  # ★ Track consecutive tool-execution timeouts to prevent runaway loops
        _MAX_CONSECUTIVE_TOOL_TIMEOUTS = 3  # ★ Force-stop after this many consecutive tool timeouts
        _last_checkpoint = 0.0  # ★ Throttle crash-recovery checkpoints (epoch seconds)
        round_num = -1
        # ★ WHILE-loop instead of FOR — the ceiling expands when premature-close
        #   retries are used, so even max_tool_rounds=0 (no tools) gets retry
        #   iterations.  Without this, `continue` in a single-iteration for-loop
        #   exits immediately and the retry never actually fires.
        #   Ceiling: max_tool_rounds + 1 (base) + _premature_retry_count (bonus).
        #   Original for-loop was: range(max_tool_rounds + 1) = [0..max_tool_rounds].
        while round_num + 1 <= max_tool_rounds + _premature_retry_count:
            round_num += 1
            if task['aborted']:
                _abort_detected_phase = f'loop_start_round_{round_num}'
                _loop_exit_reason = f'aborted_at_round_{round_num}'
                _abort_ts = task.get('_abort_timestamp', 0)
                _now = time.time()
                _delay = f'{_now - _abort_ts:.1f}s ago' if _abort_ts else 'unknown'
                logger.debug('[%s] Task aborted at START of round %d model=%s '
                             '(abort signal arrived %s, content so far: %dchars)',
                             tid, round_num, model, _delay, len(task.get('content') or ''))
                # ★ RENDER_CONTRACT Phase 3: explicit round-end boundary even on
                #   the abort-at-start path (the round never opened, so no
                #   round_start was emitted for it — close nothing here; the
                #   PREVIOUS round's end was already emitted at its own exit).
                break

            # ★ RENDER_CONTRACT Phase 3: explicit ROUND boundary. Emitted at the
            #   TOP of every round the model actually runs — INCLUDING a
            #   prose-only round (no tool calls) which previously had NO signal
            #   the client could key round attribution off. The reducer opens
            #   the round here off the canonical roundNum instead of inferring
            #   it from the first tool_start / llmRound grouping.
            append_event(task, build_event(EventType.ROUND_START, roundNum=round_num))

            # ★ Emit phase event so the frontend knows what's happening
            _emit_tool_round_phase(task, assistant_msg if round_num > 0 else {}, round_num)

            # ★ Context compaction: two-layer pipeline
            #   L1: micro-compact cold tool results (every round, zero LLM cost)
            #   L2: smart summary as synthetic tool result (on context overflow)
            run_compaction_pipeline(messages, round_num, task=task)

            # ★ Per-turn attachments: dynamic context injection
            #   Inspired by Claude Code's getAttachments() — injects session
            #   memory, file reminders, tool discovery deltas each turn.
            #   Wrapped defensively: attachment building is advisory and must
            #   never crash an otherwise-healthy task. Any bug here (e.g. a
            #   malformed tool_call arg from the model) degrades to "no
            #   attachments this round" rather than aborting the task.
            if round_num > 0:  # skip round 0 (system contexts just injected)
                try:
                    _attachments = compute_turn_attachments(
                        messages, task, round_num,
                        conv_id=task.get('convId', ''),
                        project_path=project_path,
                        project_enabled=project_enabled,
                    )
                    if _attachments:
                        inject_attachments(messages, _attachments,
                                            conv_id=task.get('convId') or None)
                except Exception as e:
                    logger.error('[Task:%s] compute_turn_attachments failed '
                                 'round=%d: %s — continuing without attachments',
                                 tid, round_num, e, exc_info=True)

            # ★ Legacy cleanup: strip old "Current date and time:" from user
            #   messages.  Date is now injected in the system prompt (step 4.5)
            #   as date-only format.  This just ensures conversations with
            #   old-format timestamps get cleaned up for proper cache prefix.
            inject_search_addendum_to_user(messages, search_enabled,
                                           round_num=round_num)

            # ★ Drain swarm inbox — async sub-agent completions (and any other
            #   model-facing notifications) get injected as `user`-role
            #   `_isMeta` messages right before the LLM call. Drained AFTER
            #   attachments / search-addendum so it sits at the end of the
            #   message list (just before the model takes its next turn).
            #   Safe injection rule: if the previous turn ended with an
            #   assistant tool_call awaiting tool_result, postpone — the
            #   pair must close before another role can speak.
            try:
                _last_msg = messages[-1] if messages else None
                _has_unmatched_tool_call = (
                    bool(_last_msg)
                    and _last_msg.get('role') == 'assistant'
                    and _last_msg.get('tool_calls')
                )
                if not _has_unmatched_tool_call:
                    from lib.agent_inbox import drain as _drain_inbox
                    from lib.swarm.integration import swarm_key_for as _swarm_key_for
                    # NOTE: drain with the conversation-scoped SWARM KEY — the
                    # inbox is keyed by ``swarm_key_for(task)`` (conv id when
                    # present, else task id) so <swarm-update>s enqueued by a
                    # PRIOR turn's background agents are still drained on a
                    # later "continue" turn of the same conversation. ``tid`` is
                    # just the 8-char log prefix.
                    _swarm_key = _swarm_key_for(task)
                    # ── Peer key can DIFFER from the swarm key ──
                    #   A VU sub-task runs with convId='' (swarm key = sub-task
                    #   id) but its peer twin lives under the PARENT conv, passed
                    #   via ``_peer_drain_key``. And when a DRIVER loop (endpoint)
                    #   owns peer delivery at its OWN iteration boundary it sets
                    #   ``_peer_driver_owned`` — run_task must then NOT drain peer
                    #   here (only swarm), or the two paths would double-drain.
                    _peer_owned = bool(task.get('_peer_driver_owned'))
                    _peer_key = task.get('_peer_drain_key') or _swarm_key
                    # Swarm items (peer-msg AND user-steer excluded — both are
                    # drained separately below with their own de-dup / chip
                    # semantics; folding them into _swarm_items would render a
                    # human steer as a <swarm-update> chip and mark it delivered
                    # via the swarm path, which is the wrong lane).
                    _swarm_items = _drain_inbox(
                        _swarm_key, exclude_modes=['peer-msg', 'user-steer'])
                    _peer_items = ([] if _peer_owned
                                   else _drain_inbox(_peer_key, modes=['peer-msg']))
                    # Human steer messages (the operator interjecting into their
                    # own running turn). Keyed on the same conversation swarm key
                    # the send route enqueues under. Delivered exactly once via
                    # the deferred-confirm flush after the LLM call (mirrors peer).
                    _steer_items = _drain_inbox(_swarm_key, modes=['user-steer'])
                    _inbox_items = (list(_swarm_items) + list(_peer_items)
                                    + list(_steer_items))
                    if _inbox_items:
                        # Coalesce ALL drained items into a single user
                        # message — one message with N <swarm-update>
                        # blocks instead of N adjacent user messages.
                        # Reasons:
                        #   1. Cuts message count → cleaner cache prefix.
                        #   2. <swarm-update> is treated as factual data
                        #      (not a system reminder), so this is a real
                        #      user-role message — no _isMeta flag, no
                        #      <system-reminder> wrapper.  Mirrors Claude
                        #      Code's <task-notification> approach.
                        _payloads = [it.get('value', '') for it in _inbox_items
                                     if it.get('value')]
                        if _payloads:
                            messages.append({
                                'role':    'user',
                                'content': '\n\n'.join(_payloads),
                            })
                            # Items already partitioned by the two drains above:
                            # ``_swarm_items`` (sub-agent results, carry agent_id)
                            # and ``_peer_items`` (Pillar #6, carry queueId). They
                            # share the ONE coalesced user message but their de-dup
                            # + observability handling differ.
                            _swarm_items = [it for it in _swarm_items if it.get('value')]
                            _peer_items = [it for it in _peer_items if it.get('value')]
                            _steer_items = [it for it in _steer_items if it.get('value')]

                            # Swarm: persist the delivered flag so a restart
                            # mid-turn doesn't re-inject these <swarm-update>s.
                            if _swarm_items:
                                try:
                                    from lib.swarm import persistence as _swarm_persist
                                    _swarm_persist.mark_delivered(
                                        _swarm_key_for(task),
                                        [it.get('agent_id', '') for it in _swarm_items
                                         if it.get('agent_id')])
                                except Exception as _mde:
                                    logger.debug('[Task %s] swarm mark_delivered failed: %s',
                                                 tid, _mde)
                                _swarm_previews = [{
                                    'agentId': it.get('agent_id', ''),
                                    'text': (it.get('value') or '')[:1200],
                                } for it in _swarm_items]
                                append_event(task, build_event(
                                    EventType.SWARM_INBOX_INJECT,
                                    roundNum=round_num + 1,
                                    count=len(_swarm_items),
                                    agentIds=[it.get('agent_id', '')
                                              for it in _swarm_items],
                                    # ★ Carry the actual <swarm-update> payloads
                                    #   (truncated) so the frontend can render an
                                    #   in-timeline ptool-panel row showing exactly
                                    #   what the model received — not just a count.
                                    previews=_swarm_previews,
                                ))
                                # Display-only sidecar accumulation (shape mirrors
                                # the peer/steer inject records). Persisted by the
                                # sync layer as the underscore field
                                # ``msg['_inboxInjects']`` — NEVER into toolRounds
                                # (that is the wire-replay / prefix-cache source; a
                                # synthetic row there breaks tool-turn continuation
                                # and shifts wire bytes). Frontend rebuilds the
                                # in-timeline chip from this on reload.
                                task.setdefault('_inboxInjects', []).append({
                                    'round': round_num + 1,
                                    'count': len(_swarm_items),
                                    'agentIds': [it.get('agent_id', '')
                                                 for it in _swarm_items],
                                    'previews': _swarm_previews,
                                })

                            # Peer: the message is now in the in-memory
                            # `messages` list but NOT yet consumed by the model.
                            # The FORWARD-race de-dup (delete the durable row)
                            # and the PEER_INBOX_INJECT arrival chip are BOTH
                            # DEFERRED to just after the LLM call confirms
                            # consumption (see the flush below) — so an abort
                            # before the call leaves the durable row intact for a
                            # later fresh-turn redelivery (never zero-delivered).
                            if _peer_items:
                                # ── DEFERRED confirmed-delivery (never-zero fix) ──
                                # Do NOT emit the PEER_INBOX_INJECT chip NOR
                                # delete the durable message_queue rows here. At
                                # this point the message is only placed in the
                                # IN-MEMORY `messages` list — the model has not
                                # yet consumed it. If the task aborts / crashes
                                # between here and the LLM call, the inbox twin is
                                # already drained (gone) and the in-memory message
                                # dies with the task; deleting the durable row now
                                # would make the message render NOWHERE (zero
                                # delivery), and emitting the chip now would show
                                # a delivery that never happened. Instead stash
                                # the peer items and do BOTH — emit the chip AND
                                # delete the durable rows — only AFTER the LLM
                                # call returns (delivery confirmed), so
                                # chip-shown ⟺ model-consumed ⟺ durable-deleted
                                # is one atomic step. On an abort the durable row
                                # SURVIVES → it is re-dispatched later as a fresh
                                # turn (delivered late, never lost, and rendered
                                # exactly once).
                                task.setdefault(
                                    '_peer_inject_pending', []).extend(_peer_items)

                            # Steer: same deferred-confirm discipline as peer.
                            # The human steer is now in the in-memory `messages`
                            # list but the model has not consumed it yet. Do NOT
                            # emit the USER_STEER_INJECT chip here — stash it and
                            # emit AFTER the LLM call confirms consumption. On an
                            # abort before the call the steer is re-routed to the
                            # durable message_queue as a fresh next turn (see the
                            # flush + the finalize salvage), so it is delivered
                            # exactly once — never zero, never double.
                            if _steer_items:
                                task.setdefault(
                                    '_steer_inject_pending', []).extend(_steer_items)

                            logger.info(
                                '[Task %s] injected %d inbox item(s) '
                                '(%d swarm, %d peer, %d steer) as 1 user message '
                                'at round %d',
                                tid, len(_payloads), len(_swarm_items),
                                len(_peer_items), len(_steer_items), round_num + 1)
            except Exception as _e:
                logger.error(
                    '[Task %s] swarm inbox drain/inject failed at round %d: %s '
                    '— continuing without notifications',
                    tid, round_num + 1, _e, exc_info=True)

            _tools_this_round = tool_list if (tool_list and round_num < max_tool_rounds) else None

            # ★ Cache-aware tool result ordering: sort consecutive tool results
            #   by tool_call_id so the prefix is deterministic across rounds
            #   (important for automatic prefix caching on OpenAI/Qwen).
            sort_tool_results(messages, conv_id=task.get('convId', ''))

            # ★ Emit messages snapshot for the debug panel (AFTER sort_tool_results
            #   so the panel reflects the real outbound ordering). The snapshot is
            #   the WIRE-FORM view — apply_wire_sanitize on an INDEPENDENT copy
            #   reproduces build_body's OpenAI-form tail (strip/sanitize/orphan/
            #   merge/empty-fix) without mutating `messages` (build_body re-runs
            #   these on its own copy at request time). See lib/tasks_pkg/wire_messages.py.
            try:
                _wire = apply_wire_sanitize(
                    messages, conv_id=task.get('convId', ''),
                    provider_id=task.get('provider_id') or '')
                snapshot = _strip_base64_for_snapshot(_wire)
                snap_evt = build_event(
                    EventType.MESSAGES_SNAPSHOT,
                    roundNum=round_num + 1,
                    label=f'Round {round_num + 1} 请求前 · {len(snapshot)}条',
                    messages=snapshot,
                )
                if _tools_this_round:
                    snap_evt['tools'] = _tools_this_round
                append_event(task, snap_evt)
            except Exception:
                logger.warning('[Task %s] messages_snapshot failed at round %d model=%s', tid, round_num + 1, model, exc_info=True)

            body = _o.build_body(
                model, messages,
                max_tokens=max_tokens,
                temperature=temperature,
                thinking_enabled=thinking_enabled,
                preset=preset,
                thinking_depth=thinking_depth,
                tools=_tools_this_round,
                response_format=response_format,
                stream=True,
            )
            # ★ Attach task_id for session-stable TTL latch in
            #   add_cache_breakpoints (prevents mid-session cache key shift).
            body['_task_id'] = task['id']

            # ★ Streaming tool execution: pre-execute read-only tools while
            #   the model is still generating subsequent tool calls.
            #   Also emits tool_start events immediately during streaming so
            #   the frontend shows "Searching…" / "Running…" without delay.
            from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
            _stream_acc = StreamingToolAccumulator(
                task, project_path=cfg.get('projectPath'),
                tool_round_num=tool_round_num,
                round_num=round_num,
                project_enabled=project_enabled,
            )

            # ★ Per-round DB-connection checkpoint release.
            #   run_task runs on a long-lived pooled worker thread whose
            #   thread-local PG connection holds a _conn_semaphore slot from
            #   its first DB op until close_thread_db() runs. That release
            #   otherwise lives ONLY in the terminal finally, so if the LLM
            #   call below spins (e.g. a total gateway-5xx outage rotating
            #   slots), the stuck task pins a connection slot for the WHOLE
            #   outage — and that semaphore is shared with the frontend's data
            #   endpoints (/api/v1/conversations, /api/health SELECT 1), which
            #   then can't acquire and hang ("backend alive, frontend dead").
            #   The connection is provably DB-idle at this point: all per-round
            #   writes above committed (db_execute_with_retry commit=True), and
            #   the streaming-tool pool runs NO DB, so nothing spans the stream.
            #   Releasing here caps connection-hold at one round; the next DB op
            #   transparently re-acquires via get_thread_db. Best-effort — a
            #   release failure must never break an otherwise-healthy task.
            try:
                from lib.agent_core.store import get_conversation_store
                get_conversation_store().release_connection()
            except Exception as _rel_err:
                logger.debug('[Task:%s] per-round release_connection failed at '
                             'round %d: %s', tid, round_num, _rel_err)

            # ★ LLM call with automatic fallback to Opus on failure
            try:
                llm_result = _llm_call_with_fallback(
                    task, body, model, round_num, max_tokens,
                    tool_call_happened, tool_list, max_tool_rounds,
                    messages, preset, thinking_enabled,
                    accumulated_usage, api_rounds,
                    on_tool_call_ready=_stream_acc.on_tool_call_ready,
                )
                assistant_msg = llm_result['assistant_msg']
                last_finish_reason = llm_result['finish_reason']
                last_usage = llm_result['usage'] or last_usage
                model = llm_result['model']
                preset = llm_result['preset']
                thinking_enabled = llm_result['thinking_enabled']

                # ── Flush DEFERRED peer delivery (never-zero fix) ──
                #   The LLM call above succeeded, so the peer message injected
                #   into `messages` this round WAS consumed by the model. NOW —
                #   atomically — emit the PEER_INBOX_INJECT chip (the in-timeline
                #   arrival marker) AND delete the durable message_queue row(s)
                #   so dispatch_next_queued can't later re-dispatch them as a
                #   redundant fresh turn. If the task had aborted BEFORE this
                #   point, neither happened and the durable row SURVIVED → it is
                #   re-dispatched later as a fresh turn (delivered late, rendered
                #   exactly once — never zero, never double). Runs after a
                #   fallback too (delivery still happened). Best-effort: a delete
                #   failure only risks a rare double-delivery (reverse-race guard
                #   still applies), never a loss.
                _peer_inject = task.pop('_peer_inject_pending', None)
                if _peer_inject:
                    _peer_previews = [{
                        'fromConv': _pit.get('fromConv', ''),
                        'text': (_pit.get('peerText')
                                 or _pit.get('value') or '')[:1200],
                    } for _pit in _peer_inject]
                    try:
                        append_event(task, build_event(
                            EventType.PEER_INBOX_INJECT,
                            roundNum=round_num + 1,
                            count=len(_peer_inject),
                            previews=_peer_previews,
                        ))
                    except Exception as _pce:
                        logger.warning('[Task %s] peer inject chip emit failed: %s',
                                       tid, _pce)
                    # Display-only sidecar accumulation — persisted by the sync
                    # layer as ``msg['_peerInjects']`` (underscore field, NEVER
                    # into toolRounds). Delivery is confirmed here, so it is safe
                    # to record for the committed-message projection + reload.
                    task.setdefault('_peerInjects', []).append({
                        'round': round_num + 1,
                        'count': len(_peer_inject),
                        'previews': _peer_previews,
                    })
                    # Resolve the peer conv key: a VU sub-task runs with
                    # convId='' and carries the parent conv in _peer_drain_key,
                    # so dedup the durable rows under that key (the same key the
                    # twin was enqueued under), not the empty sub-task convId.
                    _conv_dd = (task.get('_peer_drain_key')
                                or task.get('convId', '') or '')
                    _dd_ids = [_pit.get('queueId') for _pit in _peer_inject
                               if _pit.get('queueId')]
                    if _conv_dd and _dd_ids:
                        try:
                            from lib.message_queue import dedup_peer_durable_rows
                            dedup_peer_durable_rows(_conv_dd, _dd_ids)
                        except Exception as _dde:
                            logger.warning(
                                '[Task %s] deferred peer de-dup failed (durable '
                                'row may re-deliver once): %s', tid, _dde)

                # ── Flush DEFERRED human-steer delivery (never-zero fix) ──
                #   Same discipline as the peer flush above: the LLM call
                #   succeeded, so the human steer injected into `messages` this
                #   round WAS consumed by the model. Emit the USER_STEER_INJECT
                #   chip now (delivery confirmed) and accumulate a DISPLAY-ONLY
                #   sidecar record on the task (task['_userSteerInjects']) so the
                #   sync layer can persist it onto the assistant message as an
                #   underscore field — NEVER into toolRounds (that is the
                #   wire-replay / prefix-cache source; a synthetic row there
                #   breaks tool-turn continuation and shifts wire bytes). On an
                #   abort BEFORE this point the chip is never emitted and the
                #   undelivered steer is salvaged back to the durable
                #   message_queue by finalize (see _finalize.py) → re-dispatched
                #   as a fresh turn, delivered exactly once.
                _steer_inject = task.pop('_steer_inject_pending', None)
                if _steer_inject:
                    _steer_previews = [{
                        'text': (_sit.get('value') or '')[:1200],
                    } for _sit in _steer_inject]
                    try:
                        append_event(task, build_event(
                            EventType.USER_STEER_INJECT,
                            roundNum=round_num + 1,
                            count=len(_steer_inject),
                            previews=_steer_previews,
                        ))
                    except Exception as _sce:
                        logger.warning('[Task %s] steer inject chip emit failed: %s',
                                       tid, _sce)
                    # Display-only sidecar accumulation (shape mirrors the swarm/
                    # peer inject records the sync layer persists as underscore
                    # fields). Delivery is confirmed here, so it is safe to
                    # record for the committed message projection.
                    task.setdefault('_userSteerInjects', []).append({
                        'round': round_num + 1,
                        'count': len(_steer_inject),
                        'previews': _steer_previews,
                    })

                # Surface the resolved model on the task AS SOON as it's known
                # (was only set at task finalization), so per-round telemetry
                # emitted during tool dispatch — e.g. report_hallucinated's
                # `tool_hallucinated` audit — records the real model instead of
                # an empty string and the optimizer can cluster by model.
                if model:
                    task['model'] = model

                if llm_result['_loop_action'] == 'break':
                    _loop_exit_reason = llm_result['_loop_exit_reason']
                    break
            except Exception as e:
                if isinstance(e, AbortedError):
                    logger.info('[%s] ✋ User abort caught at round %d', tid, round_num)
                    _loop_exit_reason = 'user_abort'
                    break
                raise

            # ★ Prompt cache break detection: track what changed between turns
            #   to diagnose unexpected cost spikes.
            #   Inspired by Claude Code's promptCacheBreakDetection.ts.
            if task.get('convId') and last_usage:
                _cache_break = detect_cache_break(
                    task['convId'], messages,
                    tools=_tools_this_round, model=model,
                    usage=last_usage,
                )
                # Stamp the break reason onto the round we just recorded so
                # the frontend cost popover can explain WHY cache_read dropped
                # (system-prompt change, tools change, TTL expiry, …). Guard on
                # the round number so we don't mis-attribute when this round
                # produced no usage and api_rounds[-1] is an earlier round.
                if _cache_break and api_rounds and api_rounds[-1].get('round') == round_num + 1:
                    api_rounds[-1]['cacheBreak'] = _cache_break
                # ★ Stamp WHAT the model did this round (the tool calls it
                #   emitted). This is the causal driver of the NEXT round's
                #   cache `write`: round N's assistant output (text + these
                #   tool_calls) PLUS the tool results fed back get appended to
                #   the prefix and cached on round N+1. Recording the tool
                #   names lets the cost popover explain why a round that
                #   "generated" only a few hundred output tokens leads to a
                #   multi-thousand-token write next round.
                if api_rounds and api_rounds[-1].get('round') == round_num + 1:
                    try:
                        _tcs = (assistant_msg or {}).get('tool_calls') or []
                        _names = [
                            (tc.get('function') or {}).get('name') or '?'
                            for tc in _tcs if isinstance(tc, dict)
                        ]
                        if _names:
                            api_rounds[-1]['toolCalls'] = _names
                    except Exception as _te:
                        logger.debug('[%s] tool-call stamp failed: %s', tid, _te)
                    # ★ Stamp the EXACT decomposition of this round's `write`
                    #   into {toolResults, prevOutput, envelope} computed from
                    #   real recorded usage (see _compute_write_breakdown). The
                    #   frontend renders these three sub-items — which sum to
                    #   exactly `write` — instead of doing the arithmetic (and
                    #   only proxying it) client-side.
                    try:
                        # On a turn's round-1 there is no within-turn predecessor
                        # (api_rounds has one entry), so the breakdown has no read
                        # baseline and would default the whole write to benign
                        # contextWrite — even when the PREVIOUS turn's cached
                        # prefix was partly evicted and re-billed this round. Feed
                        # the cross-turn baseline (prior turn's final cached-prefix
                        # read, recovered across the run_task thread boundary) so
                        # round-1 classifies an evicted-tail re-bill as recacheBody.
                        _prev_turn_read = (
                            get_prev_turn_cache_read(task['convId'])
                            if len(api_rounds) < 2 else 0)
                        _wb = _compute_write_breakdown(
                            task, api_rounds, round_num,
                            prev_turn_cache_read=_prev_turn_read)
                        if _wb:
                            api_rounds[-1]['writeBreakdown'] = _wb
                    except Exception as _we:
                        logger.debug('[%s] write-breakdown stamp failed: %s', tid, _we)
                # ★ Per-round cache stats at INFO level for production visibility
                log_round_cache_stats(
                    task['convId'], round_num, last_usage,
                    model=model, tid=task['id'],
                )

            # ★ Read back updated tool_round_num from streaming accumulator
            #   (tool_start events emitted during streaming already consumed
            #   round numbers, so parse_tool_calls must start from here).
            if _stream_acc.announced_tc_map:
                tool_round_num = _stream_acc.tool_round_num

            # ★ Inject pre-computed streaming tool results into dedup cache.
            #   execute_tool_pipeline will find these and skip re-execution.
            if _stream_acc.submitted_count > 0:
                _prefetch_hits = _stream_acc.inject_into_cache(task)
                if _prefetch_hits:
                    logger.info('[%s] Streaming tool exec: %d results pre-computed '
                                'and injected into cache', tid, _prefetch_hits)

            # ★ Post-stream analysis: premature close, abort, normal exit
            stream_decision = analyse_stream_result(
                assistant_msg, last_finish_reason, task, tid, model,
                round_num, _premature_retry_count, messages,
                usage=last_usage,
            )
            _premature_retry_count = stream_decision['premature_retry_count']
            last_finish_reason = stream_decision['last_finish_reason']
            if stream_decision['abort_detected_phase']:
                _abort_detected_phase = stream_decision['abort_detected_phase']
            if stream_decision['action'] == 'break':
                _loop_exit_reason = stream_decision['loop_exit_reason']
                break
            if stream_decision['action'] == 'continue':
                continue

            # ── Per-round diagnostic: log finish_reason for every tool round ──
            _round_content = len((assistant_msg or {}).get('content', '') or '')
            _round_tcs = len((assistant_msg or {}).get('tool_calls', []))
            logger.info('[%s] conv=%s Round %d result: finish_reason=%s model=%s '
                        'content=%dchars tool_calls=%d → proceeding to tool execution',
                        tid, task.get('convId', ''), round_num + 1, last_finish_reason, model,
                        _round_content, _round_tcs)

            # ── max_budget_usd gate (Claude Agent SDK parity) ──
            # Hard $ ceiling on accumulated cost.  0 / unset disables.
            _max_budget = float(cfg.get('maxBudgetUsd') or 0.0)
            if _max_budget > 0:
                from lib.cost_estimator import check_budget
                _exceeded, _cost, _reason = check_budget(
                    task, accumulated_usage, model, _max_budget,
                    round_num=round_num,
                )
                if _exceeded:
                    last_finish_reason = 'budget_exceeded'
                    from lib.error_envelope import make_envelope as _make_env
                    task['error'] = _make_env(
                        'budget_exceeded',
                        detail=_reason,
                        model=model,
                        context='budget-gate',
                        source='orchestrator',
                        raw=f'cost_usd={_cost:.6f} max={_max_budget:.6f}',
                    )
                    _loop_exit_reason = f'budget_exceeded_round_{round_num}_${_cost:.4f}'
                    append_event(task, build_event(EventType.ROUND_END,
                                                   roundNum=round_num, reason='budget'))
                    break

            # ── Tool round budget check ──
            if round_num >= max_tool_rounds:
                # Safety ceiling: tool round budget exhausted
                last_finish_reason = 'tool_rounds_exhausted'
                from lib.error_envelope import make_envelope as _make_env
                task['error'] = _make_env(
                    'tool_rounds_exhausted',
                    detail=f'Tool call limit reached ({max_tool_rounds} rounds).',
                    model=model,
                    context='tool-budget',
                    source='orchestrator',
                    raw=f'max_tool_rounds={max_tool_rounds}',
                )
                logger.warning('[Task %s] conv=%s ⚠️ Tool rounds exhausted at round %d/%d', task['id'][:8], task.get('convId', ''), round_num+1, max_tool_rounds)
                _loop_exit_reason = f'tool_rounds_exhausted_{round_num}'
                append_event(task, build_event(EventType.ROUND_END,
                                               roundNum=round_num, reason='budget'))
                break

            tool_call_happened = True
            # ★ SINGLE SOURCE: assemble the live-tail assistant/tool_call
            #   message through build_assistant_tool_call_message — the SAME
            #   function the replay path (_reconstruct_tool_call_messages) uses.
            #   This makes the live tail and every replay path emit byte-
            #   identical fields for the turn, structurally: content is STRIPPED
            #   (the pre-tool prose snapshot assistantContent is persisted
            #   stripped; a raw↔stripped flip was a WIRE PREFIX CHANGED miss),
            #   reasoning_content is carried whenever thinking is present, and
            #   the thinking-block signature only when present (so the NEXT
            #   tool-loop turn replays a signed thinking block). All those gates
            #   now live in ONE place, so a future field can never re-diverge
            #   between the two paths. See build_assistant_tool_call_message.
            from lib.tasks_pkg.conv_message_builder import (
                build_assistant_tool_call_message)
            clean_msg = build_assistant_tool_call_message(
                tool_calls=assistant_msg['tool_calls'],
                content=assistant_msg.get('content'),
                reasoning_content=assistant_msg.get('reasoning_content'),
                thinking_signature=assistant_msg.get('thinking_signature'))
            messages.append(clean_msg)

            # ★ Discard the inter-round narration this round streamed before
            #   its tool calls (backend reset + client DELTA_RESET). See
            #   _discard_pretool_prose for the full rationale.
            _discard_pretool_prose(task, round_num)

            # ★ Incremental auto-translate: this round's prose segment is now
            #   self-contained (the model finished its commentary and is about
            #   to call tools). Translate it in the background so it's ready by
            #   task end instead of one big translation stall. Gated + isolated
            #   inside the helper; a no-op when autoTranslate is off.
            try:
                from lib.translate import submit_round_segment
                submit_round_segment(task, round_num, assistant_msg.get('content') or '')
            except Exception as _ite:
                logger.debug('[%s] incremental translate submit failed (non-fatal): %s', tid, _ite)

            # ★ Expose live messages to context_compact tool handler
            task['_compact_messages'] = messages

            # ══════════════════════════════════════════
            #  Tool Execution Pipeline (delegated to tool_dispatch)
            # ══════════════════════════════════════════

            # ── Abort check before tool execution ──
            if task['aborted']:
                _abort_detected_phase = f'before_tool_exec_round_{round_num}'
                _loop_exit_reason = f'aborted_before_tools_round_{round_num}'
                # ★ Remove the assistant message with tool_calls that we just
                #   appended (line ~879) — since we're skipping tool execution,
                #   leaving it creates orphaned tool_use blocks without matching
                #   tool_result.  This causes HTTP 400 on the next turn when
                #   server_message_store replays the full message history.
                if messages and messages[-1].get('tool_calls'):
                    _popped = messages.pop()
                    logger.info('[%s] Removed trailing tool_calls message (abort) — '
                                'prevents orphaned tool_use in stored history', tid)
                    # If it had content alongside tool_calls, keep just the content
                    if _popped.get('content'):
                        messages.append({'role': 'assistant', 'content': _popped['content']})
                        logger.debug('[%s] Re-added assistant content without tool_calls', tid)
                logger.info('[%s] Task aborted before tool execution at round %d — skipping all tools', tid, round_num)
                append_event(task, build_event(EventType.ROUND_END,
                                               roundNum=round_num, reason='aborted'))
                break

            # ── Phase 1: Parse all tool_calls ──
            #   Pass early_announced so parse_tool_calls skips re-emitting
            #   tool_start events that were already sent during streaming.
            parsed_tcs, tool_round_num = parse_tool_calls(
                assistant_msg, task, round_num, tool_round_num, project_enabled,
                early_announced=_stream_acc.announced_tc_map,
            )

            # ── Phase 1b: Sanitize tool_calls in messages so the next API
            #   round doesn't carry malformed JSON args back to the gateway.
            #
            #   Background: when a model emits ``tool_calls=[{arguments: '...'}]``
            #   where ``arguments`` is invalid JSON (common with weaker models
            #   that mis-escape backslashes in regex args, e.g. ``\d`` instead
            #   of ``\\d``), parse_tool_calls() catches the JSONDecodeError and
            #   builds an error tool_result.  But the assistant message we
            #   already appended at line ~1361 still contains the RAW bad args.
            #
            #   On the next round, server_message_store / orchestrator replays
            #   ``assistant(tool_calls=[..bad args..]) + tool(error_msg)`` to
            #   the upstream gateway, which validates the JSON-string itself
            #   and rejects with HTTP 400 ``invalid function arguments json
            #   string``.  The whole conversation gets stuck — model never
            #   sees the error tool_result, can't recover, task ends in
            #   ``finishReason=error``.
            #
            #   Fix: walk parsed_tcs and any tc with non-None ``_args_parse_error``
            #   gets its ``arguments`` overwritten to ``'{}'`` in messages[-1].
            #   The error tool_result still teaches the model what went wrong;
            #   the gateway sees valid JSON and lets the next round through.
            #   See May 2026 incident memory.
            for tc, fn_name, tc_id, fn_args, rn, round_entry, args_parse_err in parsed_tcs:
                if not args_parse_err:
                    continue
                # Find the matching tool_call in messages[-1] by tc_id and
                # rewrite its arguments to a syntactically valid empty JSON.
                last_msg = messages[-1] if messages else {}
                for live_tc in last_msg.get('tool_calls', []) or []:
                    if live_tc.get('id') != tc_id:
                        continue
                    fn = live_tc.get('function') or {}
                    bad_args = fn.get('arguments', '')
                    fn['arguments'] = '{}'
                    logger.info(
                        '[%s] conv=%s Sanitized malformed tool_call args for '
                        'tool=%s tc_id=%s (was %d chars) — error fed back to '
                        'model in matching tool_result; gateway sees valid JSON',
                        tid, task.get('convId', ''), fn_name, tc_id[:12],
                        len(bad_args) if isinstance(bad_args, str) else 0)
                    break

            # ── Phase 2: Emit execution phase event ──
            emit_tool_exec_phase(task, parsed_tcs)

            # ── Phase 3: Execute tools (approval + parallel + result append) ──
            # ★ Reaper heartbeat: a long tool run (or a human-guidance/approval
            #   block inside it) emits no delta, so refresh the positive-
            #   liveness clock before entering the pipeline. See
            #   manager.reap_stuck_running_tasks.
            task['_dispatch_heartbeat'] = time.time()
            _tool_timed_out = execute_tool_pipeline(
                task, parsed_tcs, cfg, project_path, project_enabled,
                tool_list, messages, all_search_results_text, round_num, model,
            )

            # Clean up live messages ref after tool execution
            task.pop('_compact_messages', None)

            # ── Phase 4b: Consecutive tool-timeout circuit breaker ──
            if _tool_timed_out:
                _consecutive_tool_timeouts += 1
                logger.warning(
                    '[%s] conv=%s Tool timeout at round %d (%d/%d consecutive) model=%s',
                    tid, task.get('convId', ''), round_num + 1, _consecutive_tool_timeouts,
                    _MAX_CONSECUTIVE_TOOL_TIMEOUTS, model)
                if _consecutive_tool_timeouts >= _MAX_CONSECUTIVE_TOOL_TIMEOUTS:
                    logger.error(
                        '[%s] conv=%s ⚠️ FORCE STOP: %d consecutive tool timeouts — breaking loop to prevent runaway task. model=%s',
                        tid, task.get('convId', ''), _consecutive_tool_timeouts, model)
                    from lib.error_envelope import make_envelope as _make_env
                    task['error'] = _make_env(
                        'tool_timeout',
                        detail=f'{_consecutive_tool_timeouts} consecutive tool execution timeouts.',
                        model=model,
                        context='tool-loop',
                        source='orchestrator',
                        raw=f'consecutive_tool_timeouts={_consecutive_tool_timeouts}',
                    )
                    _loop_exit_reason = f'consecutive_tool_timeouts_{_consecutive_tool_timeouts}'
                    break
            else:
                _consecutive_tool_timeouts = 0  # Reset on successful tool execution

            # ══════════════════════════════════════════
            #  ★ Crash-recovery checkpoint: persist partial state to DB
            # ══════════════════════════════════════════
            # After each tool execution round, save current content/thinking
            # to task_results + conversation so data survives a server crash.
            # Throttled to at most once every 10 seconds to avoid DB pressure.
            _now = time.time()
            if _now - _last_checkpoint >= 5:
                try:
                    checkpoint_task_partial(task)
                    _last_checkpoint = _now
                except Exception as e:
                    logger.warning('[%s] Checkpoint after round %d failed (non-fatal): %s', tid, round_num + 1, e, exc_info=True)

            # ★ RENDER_CONTRACT Phase 3: explicit round-end boundary for a round
            #   that issued tool calls and is about to loop into the next round.
            #   Reached only at the natural end of a tools-executed iteration
            #   (an early `continue` for a premature-close retry does NOT reach
            #   here, so it never emits a spurious end for a round being re-run).
            append_event(task, build_event(EventType.ROUND_END,
                                           roundNum=round_num, reason='tools'))



        # ── Append final assistant reply to messages if it wasn't already ──
        # When the LLM returns text content WITHOUT tool_calls, the loop
        # breaks before appending the assistant message (tool_calls path at
        # line ~698 is the only place messages.append(clean_msg) happens).
        # Without this, _run_single_turn returns messages missing the
        # assistant's reply, and endpoint mode's critic never sees the
        # worker's output.
        if assistant_msg and not assistant_msg.get('tool_calls'):
            # ★ RENDER_CONTRACT Phase 3: explicit round-end for the TERMINAL
            #   round — the model finished with prose and no tool calls (this is
            #   the prose-only / final-answer round that never issued a
            #   round_end via the tools path). round_num is the final round
            #   index; a `done` still follows. Emitted before the message-append
            #   bookkeeping so the boundary lands even when the reply is empty.
            append_event(task, build_event(EventType.ROUND_END,
                                           roundNum=round_num, reason='final'))
            _final_content = assistant_msg.get('content') or ''
            _final_reasoning = assistant_msg.get('reasoning_content') or ''
            if _final_content or _final_reasoning:
                _final_assistant = {'role': 'assistant', 'content': _final_content}
                if _final_reasoning:
                    _final_assistant['reasoning_content'] = _final_reasoning
                messages.append(_final_assistant)
                logger.debug('[%s] Appended final assistant reply to messages '
                             '(%d content chars, %d reasoning chars)',
                             tid, len(_final_content), len(_final_reasoning))
                # ★ Incremental auto-translate: the closing prose segment (the
                #   model's final answer after the last tool round, or the
                #   whole reply when no tools were called). round_num here is
                #   the final round index — unique vs the in-loop submissions.
                try:
                    from lib.translate import submit_round_segment
                    submit_round_segment(task, round_num, _final_content)
                except Exception as _ite:
                    logger.debug('[%s] incremental translate submit (final) failed: %s', tid, _ite)
                # Emit a final snapshot so the debug panel shows the complete
                # message list. The only in-loop snapshots are "请求前" (before
                # the assistant reply exists) and the post-tool one (skipped on
                # a no-tool-call completion), so without this the panel is stuck
                # on [system?, user].
                try:
                    _wire = apply_wire_sanitize(
                        messages, conv_id=task.get('convId', ''),
                        provider_id=task.get('provider_id') or '')
                    snap = _strip_base64_for_snapshot(_wire)
                    snap_evt = build_event(
                        EventType.MESSAGES_SNAPSHOT,
                        roundNum='final',
                        label=f'最终回复后 · {len(snap)}条',
                        messages=snap)
                    # Carry the tool schema so the panel's tools section
                    # survives — showMessagesInDebug rebuilds _debugCache and
                    # drops the cached tools unless this snapshot re-supplies them.
                    if tool_list:
                        snap_evt['tools'] = tool_list
                    append_event(task, snap_evt)
                except Exception:
                    logger.warning('[Task %s] final messages_snapshot failed model=%s',
                                   tid, model, exc_info=True)

        # ── Write back updated messages to task so callers (e.g.
        #    _run_single_turn → endpoint.py) can access the complete
        #    conversation including assistant replies and tool results.
        #    Without this, task['messages'] still holds the PRE-run_task
        #    snapshot, and endpoint mode's critic never sees the worker's output.
        task['messages'] = messages

        # ── Save full messages to server store for next turn ──
        if _keep_tool_history and _conv_id:
            try:
                _save_messages_to_store(_conv_id, messages)
            except Exception as e:
                logger.warning('[%s] conv=%s Failed to save messages to store: %s',
                               tid, _conv_id[:8], e, exc_info=True)

        # ── Post-loop finalization: fallback, done event, persist ──
        _finalize_and_emit_done(
            task,
            model=model, preset=preset, thinking_depth=thinking_depth, cfg=cfg,
            last_finish_reason=last_finish_reason, last_usage=last_usage,
            accumulated_usage=accumulated_usage, api_rounds=api_rounds,
            tool_call_happened=tool_call_happened, messages=messages,
            original_messages=original_messages,
            all_search_results_text=all_search_results_text,
            max_tokens=max_tokens, thinking_enabled=thinking_enabled,
            temperature=temperature,
            _loop_exit_reason=_loop_exit_reason,
            _abort_detected_phase=_abort_detected_phase,
            project_path=project_path, project_enabled=project_enabled,
            round_num=round_num,
            assistant_msg=assistant_msg,
        )

        # ── Autopilot now runs INSIDE _finalize_and_emit_done (before
        #    the done SSE event is emitted), so its result can ride on
        #    the same event.  No standalone hook here.
    except Exception as e:
        logger.error('[Orchestrator] run_task FATAL error task=%s', task.get('id', '?')[:8], exc_info=True)
        # Prefer the user-friendly message attached by _llm_call_with_fallback;
        # otherwise format the raw exception here so the frontend error-block
        # always tells the user how to recover.
        _user_err = getattr(e, '_user_message', None)
        if not _user_err:
            try:
                from lib.llm_error_format import format_llm_error_for_user
                _user_err = format_llm_error_for_user(
                    e, model=task.get('config', {}).get('model', ''),
                    context='task-fatal', source='orchestrator')
            except Exception as _fmt_err:
                logger.warning('[Orchestrator] format_llm_error_for_user failed: %s', _fmt_err)
                from lib.error_envelope import make_envelope as _make_env
                _user_err = _make_env(
                    'internal',
                    detail=f'Task fatal: {e}',
                    model=task.get('config', {}).get('model', ''),
                    context='task-fatal',
                    source='orchestrator',
                    raw=str(e),
                )
        task['error'] = _user_err; task['status'] = 'error'; task['finishReason'] = 'error'
        if task.get('_endpoint_managed'):
            return   # let endpoint.py handle the error
        # ── Turn-level auto-retry (raise path) ──
        # A transient first-round error (e.g. a 429 with no fallback, or a
        # network reset before any tool ran) RAISES past _finalize_and_emit_done
        # to here rather than error-breaking, so it would otherwise never reach
        # the finalize-seam auto-retry guard. Apply the same self-heal here: if
        # the classified error is transient and the budget remains, re-run the
        # whole turn transparently instead of surfacing it for a manual click.
        if not task.get('aborted'):
            try:
                _rt_cfg = task.get('config') or {}
                if _maybe_auto_retry_turn(task, _rt_cfg):
                    return
            except Exception as _ar_err:
                logger.warning('[Orchestrator] fatal-path auto-retry check '
                               'failed (surfacing original error): %s', _ar_err,
                               exc_info=True)
        # ── Recovery-carrier internal-FATAL → PRESERVE recoverability ──
        # A carrier spawned by killed-turn recovery (task['_killed_recovery'])
        # that FATALs from a RECOVERY-INTERNAL cause (config-build bug,
        # message-assembly error, unhandled backend exception — kind
        # internal/generic) never reached the model, so it is NOT a completed
        # turn. Downgrading it to a terminal error would strand the turn (it is
        # no longer tagged 'killed', so the next boot won't re-recover it) — the
        # exact way my own maxTokens=None bug burned 6 turns. Instead re-stamp
        # the conv tail 'killed' so a later CALM boot re-dispatches it, bounded
        # by the SAME per-turn attempt cap (the counter already advanced before
        # this dispatch, so this can never loop). A REAL model error
        # (ratelimit/quota/permission/prompt_too_long/…) is a completed turn and
        # falls through to the normal terminal-error persist below.
        if (task.get('_killed_recovery') and not task.get('aborted')):
            try:
                from lib.tasks_pkg.killed_recovery import (
                    is_recovery_internal_fatal,
                    restamp_killed_after_internal_fatal,
                )
                if is_recovery_internal_fatal(_user_err):
                    if restamp_killed_after_internal_fatal(task):
                        logger.error(
                            '[Orchestrator] recovery carrier %s internal-FATAL '
                            '(kind=%s) — model never reached; re-stamped tail '
                            '"killed" for retry on a calm boot instead of a '
                            'terminal error. conv=%s',
                            task.get('id', '?')[:8],
                            _user_err.get('kind') if isinstance(_user_err, dict) else '?',
                            task.get('convId', '')[:8])
                        # Emit a non-error DONE so listeners settle without
                        # surfacing a scary terminal error for a turn we intend
                        # to retry. The persisted tail carries the 'killed' tag.
                        append_event(task, build_event(
                            EventType.DONE, finishReason='interrupted'))
                        return
            except Exception as _kr_err:
                logger.warning('[Orchestrator] recovery-carrier internal-FATAL '
                               'handling failed (surfacing error normally): %s',
                               _kr_err, exc_info=True)
        append_event(task, build_event(EventType.DONE, error=_user_err, finishReason='error'))
        persist_task_result(task)
    except BaseException as be:
        # ── Non-Exception fatal: cancel / kill / interpreter shutdown ──
        # KeyboardInterrupt, SystemExit, and asyncio.CancelledError derive from
        # BaseException, NOT Exception, so they slip past the handler above and
        # would otherwise leave the task NON-TERMINAL forever — stranding its
        # admission slot AND (on the headless API) its billing reservation
        # until the slot TTL / janitor reclaims them. Emit the terminal
        # DONE(error) so the terminal-callback chain (release slot + settle
        # billing via on_terminal) still fires, then RE-RAISE so the
        # cancel/shutdown semantics are preserved for the caller.
        logger.error('[Orchestrator] run_task FATAL BaseException task=%s: %s',
                     task.get('id', '?')[:8], type(be).__name__, exc_info=True)
        try:
            from lib.error_envelope import make_envelope as _make_env
            task['error'] = _make_env(
                'internal', detail=f'Task terminated: {type(be).__name__}',
                model=task.get('config', {}).get('model', ''),
                context='task-fatal-base', source='orchestrator', raw=str(be))
            task['status'] = 'error'
            task['finishReason'] = 'error'
            if not task.get('_endpoint_managed'):
                append_event(task, build_event(
                    EventType.DONE, error=task['error'], finishReason='error'))
                persist_task_result(task)
        except Exception as _fin_err:
            logger.error('[Orchestrator] BaseException terminal-finalize failed '
                         'task=%s: %s', task.get('id', '?')[:8], _fin_err,
                         exc_info=True)
        raise
    finally:
        # ── Presence: this conversation's turn ended — transition its peer to
        #    IDLE (keep it; the sweep fades it after the idle window, and an
        #    autopilot follow-up turn re-announces the SAME peer to ACTIVE, so
        #    we never flicker gone→active between back-to-back turns). Reads
        #    config defensively (an early fatal may precede cfg binding). ──
        try:
            _fin_cfg = task.get('config') or {}
            _fin_pp = _fin_cfg.get('projectPath') or ''
            _fin_cid = task.get('convId') or ''
            if _fin_pp and _fin_cid:
                from lib.presence import mark_idle as _presence_mark_idle
                _presence_mark_idle(_fin_pp, _fin_cid)
        except Exception as _pe:
            logger.debug('[Task:%s] presence mark_idle failed: %s', tid, _pe)
        # ── Clear the per-task request-id correlation tag (pooled threads are
        #    reused; a stale tid would mis-attribute the NEXT task's logs). ──
        set_req_id('')
        # ── Clear the hard provider pin so it can't bleed into the NEXT
        #    task that lands on this pooled worker thread. ──
        try:
            from lib.llm_dispatch.provider_pin import clear_pinned_provider
            clear_pinned_provider()
        except Exception as _pp_err:
            logger.debug('[Task:%s] clear_pinned_provider failed: %s', tid, _pp_err)
        # ── Clear the conversation binding (pooled threads are reused). ──
        try:
            from lib.llm_dispatch.conv_affinity import clear_conv_affinity
            clear_conv_affinity()
        except Exception as _ca_err:
            logger.debug('[Task:%s] clear_conv_affinity failed: %s', tid, _ca_err)
        # ── Release this worker thread's thread-local DB connection back to
        #    the shared pool.  run_task runs on long-lived threads (the
        #    asyncio.to_thread default pool, or daemon task threads); without
        #    this each one would pin a PG connection for its entire lifetime,
        #    exhausting the connection semaphore under high concurrency
        #    (see the "pool exhausted / tracked_threads ≫ active" symptom). ──
        try:
            from lib.agent_core.store import get_conversation_store
            get_conversation_store().release_connection()
        except Exception as _ctd_err:
            logger.debug('[Task:%s] release_connection on task end failed: %s',
                         tid, _ctd_err)

