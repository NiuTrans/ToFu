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

import time
# NOTE: ``import threading`` was removed 2026-07-23 (pt_03f4cdf1 slice 2).
# The only usage inside run_task was the daemon-thread spawn of the
# external-edit probe, which now lives in
# lib.tasks_pkg.orchestrator._vu_startup.start_external_edit_probe.
from typing import Any

from lib.log import get_logger, set_req_id

logger = get_logger(__name__)


from lib.llm import AbortedError
from lib.tasks_pkg.attachments import compute_turn_attachments, inject_attachments
from lib.tasks_pkg.cache_tracking import (
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
    save_messages as _save_messages_to_store,
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
)

# Startup helpers extracted 2026-07-23 (pt_03f4cdf1 slice 2) — the first
# real source movement out of run_task's 1813-line body. Kept as module-
# level callables so tests can drive them directly instead of via the
# whole run_task orchestration. ``_vu_phase`` is imported under an
# ``_extracted_vu_phase`` alias because run_task also defines a local
# ``_vu_phase(detail)`` closure adapter (its call sites in the loop keep
# the closure-style single-arg call).
from lib.tasks_pkg.orchestrator._vu_startup import (
    _probe_external_edits,  # noqa: F401  (imported for wire-parity guard + back-compat)
    _vu_phase as _extracted_vu_phase,
    setup_project_context,
    start_external_edit_probe,  # noqa: F401  (also invoked indirectly via setup_project_context)
)
from lib.tasks_pkg.orchestrator._prefetch import start_prefetches
from lib.tasks_pkg.orchestrator._context_inject import inject_context_and_emit_chips  # noqa: E501
from lib.tasks_pkg.orchestrator._round_state import RoundState
from lib.tasks_pkg.orchestrator._tool_history import restore_tool_history
from lib.tasks_pkg.orchestrator._memory_prefetch import (
    await_memory_prefetch,
    maybe_run_memory_prefetch,
)
from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
from lib.tasks_pkg.orchestrator._post_loop import (
    finalize_after_loop,
    handle_task_fatal,
)
from lib.tasks_pkg.orchestrator._teardown import finalize_task_lane
from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox
from lib.tasks_pkg.orchestrator._deferred_inbox_flush import (
    flush_deferred_peer_and_steer,
)
from lib.tasks_pkg.orchestrator._cache_round_accounting import (
    stamp_round_cache_accounting,
)
from lib.tasks_pkg.orchestrator._sanitize_tool_call_args import (
    sanitize_malformed_tool_call_args,
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
        #   The VU sub-task carries ``_vu_event_transform`` (the append_event
        #   facade seam), so any PHASE emitted here is wrapped as
        #   ``autopilot_vu_event`` and lands in the synthetic-user bubble on
        #   BOTH the carrier's own stream and the parent's. The
        #   pre-stream prep window (tool assembly → tool-history rebuild →
        #   system-context injection → FUSE memory/project prefetch) is
        #   otherwise SILENT for up to tens of seconds on a large conversation
        #   (measured 2.9–4.7s typical, ~26s on a 3000-event conv), leaving the
        #   bubble on a vague placeholder. Naming each real sub-step keeps the
        #   display honest. Gated on ``_vu_subtask`` so the ordinary
        #   worker/endpoint startup path stays byte-identical (no new events).
        _vu_startup = bool(task.get('_vu_subtask'))

        # Local shim: preserves the closure-style call sites throughout
        # run_task while delegating to the extracted module-level
        # ``_extracted_vu_phase``. Zero-cost — the closure just forwards
        # its args. The captured ``task`` + ``_vu_startup`` are stable
        # across the whole run_task invocation (no rebind), so a per-call
        # re-read is semantically identical to the previous inline
        # closure.
        def _vu_phase(detail):
            _extracted_vu_phase(task, detail, vu_startup=_vu_startup)

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
        # ★ Seed the resolved model on the task IMMEDIATELY (epic
        #   pt_8f6cbc753855415e). The loop tail stamps it again after each
        #   successful round, but a first-call DISPATCH failure (revoked-OAuth
        #   401, all keys cooling, endpoint-unreachable exhaustion) raises
        #   BEFORE any round succeeds — the error row then persisted with
        #   metadata.model NULL (40 such rows in 14 days), invisible to
        #   per-model failure stats. The post-round stamp still tracks
        #   fallback swaps; this seed is the floor.
        if model:
            task['model'] = model
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
        # ── One-shot project-scope startup (pt_03f4cdf1 slice 4):
        #    server-state reconcile + presence announce + external-edit
        #    probe kick. Gate lives INSIDE the extracted helper so
        #    non-project turns simply return.
        setup_project_context(task, cfg, project_path, project_enabled)
        code_exec_enabled = mcfg['code_exec_enabled']
        memory_enabled  = mcfg['memory_enabled']
        browser_enabled = mcfg['browser_enabled']
        desktop_enabled = mcfg['desktop_enabled']
        swarm_enabled   = mcfg['swarm_enabled']
        image_gen_enabled = mcfg['image_gen_enabled']
        human_guidance_enabled = mcfg.get('human_guidance_enabled', False)
        scheduler_enabled = mcfg.get('scheduler_enabled', False)
        # ── Memory Prefetch: start loading project and memory contexts in
        #    background threads while tool assembly runs. Extracted to
        #    lib.tasks_pkg.orchestrator._prefetch (pt_03f4cdf1 slice 3).
        #    Owner: this function creates the pool + futures; the finally
        #    block at end-of-run shuts it down.
        _prefetch_executor = start_prefetches(
            task, cfg=cfg, project_path=project_path,
            project_enabled=project_enabled, memory_enabled=memory_enabled)

        # Simple heuristic: if any tool-providing feature is enabled, we'll
        # have real tools → need memory injection + accumulation instructions.
        _has_real_tools_hint = (search_enabled or fetch_enabled or
                                project_enabled or browser_enabled or
                                desktop_enabled or swarm_enabled or
                                code_exec_enabled or image_gen_enabled)
        _pp = project_path if project_enabled else None

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
        # ── Round-loop cross-iteration state (pt_862771477a86 slice 1):
        #    the 14 locals that cross the stream-loop iteration boundary
        #    live on ONE flat carrier (docs/ROUND_STATE_LOCALS_INVENTORY.md).
        #    round_num / _premature_retry_count stay plain locals
        #    (chassis-owned at cutover). Pure container swap, byte-identical.
        rs = RoundState(model=model, preset=preset,
                        thinking_enabled=thinking_enabled)
        all_search_results_text = []

        # ── Section 2.5: Server-side tool history restoration ── (pt_03f4cdf1 slice 8)
        #   Extracted to lib.tasks_pkg.orchestrator._tool_history.
        #   Rebuilds messages with server-side tool history when
        #   keepToolHistory=True; returns (messages, original_messages,
        #   used_store) — caller reassigns its two locals from the tuple.
        _keep_tool_history = cfg.get('keepToolHistory', True)
        _conv_id = task.get('convId', '')
        messages, original_messages, _tool_history_used = restore_tool_history(
            task=task, cfg=cfg, messages=messages, tid=tid, vu_phase=_vu_phase,
        )

        # ── Section 3.5 (SPAWN) ── Memory Prefetch, started EARLY
        #   Runs on its own thread from HERE so it overlaps Section 3's
        #   context injection — the FUSE/DB-bound project + memory context
        #   loads — instead of the microseconds of checkpoint bookkeeping that
        #   follow it. Joined by await_memory_prefetch() just before the stream
        #   loop; see _memory_prefetch.py for why starting here is byte-safe
        #   (every context-inject mutation to the true tail is wrapped in
        #   <system-reminder>, which the rerank's query builder strips).
        #
        #   `injected_tool_calls` is read from cfg['toolHistory'] rather than
        #   inject_tool_history()'s return value further down: that call is the
        #   only producer of a non-zero count and it is driven entirely by that
        #   cfg key, so the eligibility answer is identical and available now.
        #   A parity test pins the two agreeing.
        maybe_run_memory_prefetch(
            task=task, cfg=cfg, messages=messages, tool_list=tool_list,
            project_path=project_path, project_enabled=project_enabled,
            memory_enabled=memory_enabled, has_real_tools=has_real_tools,
            injected_tool_calls=len(cfg.get('toolHistory') or []),
        )

        # ── Section 3: Context Injection ── (pt_03f4cdf1 slice 7)
        #   Extracted to lib.tasks_pkg.orchestrator._context_inject.
        #   The helper does: VU phase → _inject_system_contexts →
        #   PREFERENCES_APPLIED chip → RELATED_CONVERSATIONS chip →
        #   prefetch executor shutdown → _t_prep_done timing anchor →
        #   VU phase (context ready).
        _t_prep_done = inject_context_and_emit_chips(
            task=task, messages=messages, cfg=cfg,
            project_path=project_path, project_enabled=project_enabled,
            memory_enabled=memory_enabled, search_enabled=search_enabled,
            swarm_enabled=swarm_enabled, has_real_tools=has_real_tools,
            model=model, tool_list=tool_list,
            prefetch_executor=_prefetch_executor,
            tid=tid, t_run_start=_t_run_start,
            vu_phase=_vu_phase,
        )

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
        # (the six historical loop-state init lines — tool_call_happened /
        #  last_finish_reason / last_usage / assistant_msg / accumulated_usage
        #  / api_rounds — now live on `rs`, constructed above; slice 1)

        # ★ Inject toolHistory from continue — restore interrupted tool call context
        _injected_tool_calls = inject_tool_history(messages, cfg, task, model)
        if _injected_tool_calls:
            rs.tool_call_happened = True
            rs.tool_round_num = _injected_tool_calls  # offset so new roundNums don't conflict

        # ── Section 3.5 ── the memory prefetch was SPAWNED above, before
        #   Section 3, so it overlaps context injection. It is joined by
        #   await_memory_prefetch() just before the stream loop.
        #
        #   Parity guard: the spawn passed len(cfg['toolHistory']) as the
        #   eligibility input; assert it matches what inject_tool_history
        #   actually injected, so a future change to that function's counting
        #   cannot silently flip the prefetch's skip decision.
        if bool(_injected_tool_calls) != bool(cfg.get('toolHistory') or []):
            logger.warning(
                '[%s] memory-prefetch eligibility drift: injected=%s but '
                'cfg[toolHistory]=%s — the early spawn used the latter; '
                'inject_tool_history no longer derives its count from that '
                'key alone', tid, _injected_tool_calls,
                len(cfg.get('toolHistory') or []))

        # ── Resume-state hydration ── (pt_03f4cdf1 slice 10)
        #   Extracted to lib.tasks_pkg.orchestrator._resume_state. Applies
        #   the three continue-checkpoint sub-blocks: contentPrefix seed
        #   (bookkeeping only, NEVER re-injected as a trailing assistant
        #   turn — Anthropic Messages API rejects that shape), the
        #   capability-gated resumePrefill trailing-assistant append (Claude
        #   never reaches the append via model_supports_assistant_prefill),
        #   and the four checkpoint stashes merged by the post-loop finalize.
        apply_resume_state(task=task, cfg=cfg, messages=messages,
                           model=model, tid=tid)

        # ★ 禁止添加 anti-loop / 预算警告 / _force_stop 等机制。
        #   不允许在运行时向 messages 注入任何 [SYSTEM NOTE] 或 [SYSTEM:] 消息来
        #   干扰模型的正常生成。详见 max_tool_rounds 注释。

        # ── Join the background memory prefetch (epic pt_e92d3be4) ──
        #   Section 3.5 SPAWNS the BM25 + cheap-LLM rerank instead of running
        #   it inline, so it overlaps the turn prep above rather than adding
        #   its 800 ms deadline to TTFT. It mutates `messages` in place, so it
        #   has to land BEFORE the stream loop serializes them — this is the
        #   last point where that is still true. The wait is BOUNDED: on
        #   overrun the turn proceeds with no injection, because a late write
        #   into a body already on the wire is worse than a missing advisory
        #   memory. No-op when nothing was spawned.
        await_memory_prefetch(task)

        _premature_retry_count = 0    # ★ Track retries for PREMATURE STREAM CLOSE
        _PREMATURE_RETRY_MAX = 2      # ★ Max premature-close retries (must match stream_handler)
        _MAX_CONSECUTIVE_TOOL_TIMEOUTS = 3  # ★ Force-stop after this many consecutive tool timeouts
        round_num = -1
        # (exit_reason / abort_phase / consecutive_tool_timeouts /
        #  last_checkpoint_ts now live on `rs` — slice 1 container swap)
        # ★ WHILE-loop instead of FOR — the ceiling expands when premature-close
        #   retries are used, so even max_tool_rounds=0 (no tools) gets retry
        #   iterations.  Without this, `continue` in a single-iteration for-loop
        #   exits immediately and the retry never actually fires.
        #   Ceiling: max_tool_rounds + 1 (base) + _premature_retry_count (bonus).
        #   Original for-loop was: range(max_tool_rounds + 1) = [0..max_tool_rounds].
        while round_num + 1 <= max_tool_rounds + _premature_retry_count:
            round_num += 1
            if task['aborted']:
                rs.abort_phase = f'loop_start_round_{round_num}'
                rs.exit_reason = f'aborted_at_round_{round_num}'
                _abort_ts = task.get('_abort_timestamp', 0)
                _now = time.time()
                _delay = f'{_now - _abort_ts:.1f}s ago' if _abort_ts else 'unknown'
                logger.debug('[%s] Task aborted at START of round %d model=%s '
                             '(abort signal arrived %s, content so far: %dchars)',
                             tid, round_num, rs.model, _delay, len(task.get('content') or ''))
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
            _emit_tool_round_phase(task, rs.assistant_msg if round_num > 0 else {}, round_num)

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

            # ★ Drain swarm inbox (pt_03f4cdf1 slice 11):
            #   Extracted to lib.tasks_pkg.orchestrator._swarm_inbox.
            #   Drains async sub-agent completions + peer + human-steer
            #   lanes and injects them as ONE coalesced user-role message
            #   just before the next LLM call. Immediate delivery for
            #   swarm (chip + mark_delivered); DEFERRED confirm-flush for
            #   peer and steer (never-zero delivery — stashed on task
            #   sidecars and flushed after the LLM call returns). Guards
            #   unmatched-tool_call tail internally. Never raises.
            drain_and_inject_inbox(task=task, messages=messages,
                                   round_num=round_num, tid=tid)

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
                    # Request Inspector contract (docs/DEBUG_PANEL_REDESIGN.md
                    # §3): this is the ONLY kind='request' emission — the
                    # payload the model is about to receive. The other three
                    # snapshot sites (post-tool / final / fallback) are
                    # kind='state' (NOT LLM requests).
                    kind='request',
                    model=rs.model,
                    # Endpoint turns (Planner/Worker/Critic) each re-run
                    # run_task with their OWN round numbering — tag the
                    # driver's phase so the Request Inspector can tell
                    # same-numbered rounds apart (epic pt_e3dc7198e7e34bb1).
                    # '' for normal (non-endpoint) tasks.
                    turn=task.get('_endpoint_phase') or '',
                    params={
                        'maxTokens': max_tokens,
                        'temperature': temperature,
                        'thinkingEnabled': rs.thinking_enabled,
                        'thinkingDepth': thinking_depth,
                        'preset': rs.preset,
                        'responseFormat': response_format,
                        'stream': True,
                    },
                    roundNum=round_num + 1,
                    label=f'Round {round_num + 1} 请求前 · {len(snapshot)}条',
                    messages=snapshot,
                )
                if _tools_this_round:
                    snap_evt['tools'] = _tools_this_round
                append_event(task, snap_evt)
            except Exception:
                logger.warning('[Task %s] messages_snapshot failed at round %d model=%s', tid, round_num + 1, rs.model, exc_info=True)

            body = _o.build_body(
                rs.model, messages,
                max_tokens=max_tokens,
                temperature=temperature,
                thinking_enabled=rs.thinking_enabled,
                preset=rs.preset,
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
                tool_round_num=rs.tool_round_num,
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
                    task, body, rs.model, round_num, max_tokens,
                    rs.tool_call_happened, tool_list, max_tool_rounds,
                    messages, rs.preset, rs.thinking_enabled,
                    rs.accumulated_usage, rs.api_rounds,
                    on_tool_call_ready=_stream_acc.on_tool_call_ready,
                )
                rs.assistant_msg = llm_result['assistant_msg']
                rs.last_finish_reason = llm_result['finish_reason']
                rs.last_usage = llm_result['usage'] or rs.last_usage
                rs.model = llm_result['model']
                rs.preset = llm_result['preset']
                rs.thinking_enabled = llm_result['thinking_enabled']

                # ── Flush DEFERRED peer + steer inbox (pt_03f4cdf1 slice 12) ──
                #   Extracted to
                #   lib.tasks_pkg.orchestrator._deferred_inbox_flush.
                #   The LLM call above just succeeded, so the peer and
                #   human-steer messages injected into ``messages`` earlier
                #   this round WERE consumed by the model. The helper emits
                #   the PEER_INBOX_INJECT / USER_STEER_INJECT chips, records
                #   display-only sidecars on the task, and de-dups the
                #   durable message_queue rows so dispatch_next_queued can't
                #   later re-dispatch them as a redundant fresh turn.
                #   Never-zero-and-never-double invariants preserved.
                flush_deferred_peer_and_steer(task, round_num=round_num, tid=tid)

                # Surface the resolved model on the task AS SOON as it's known
                # (was only set at task finalization), so per-round telemetry
                # emitted during tool dispatch — e.g. report_hallucinated's
                # `tool_hallucinated` audit — records the real model instead of
                # an empty string and the optimizer can cluster by model.
                if rs.model:
                    task['model'] = rs.model

                if llm_result['_loop_action'] == 'break':
                    rs.exit_reason = llm_result['_loop_exit_reason']
                    break
            except Exception as e:
                if isinstance(e, AbortedError):
                    logger.info('[%s] ✋ User abort caught at round %d', tid, round_num)
                    rs.exit_reason = 'user_abort'
                    break
                raise

            # ── Per-round cache accounting (pt_03f4cdf1 slice 13) ──
            #   Extracted to
            #   lib.tasks_pkg.orchestrator._cache_round_accounting.
            #   Detects cross-round prompt-cache breaks, stamps causal
            #   metadata (cacheBreak / toolCalls / writeBreakdown) onto
            #   rs.api_rounds[-1] so the frontend cost popover can
            #   explain WHY cache_read dropped and WHERE next-round
            #   cache `write` comes from, plus logs the per-round cache
            #   stats at INFO for production visibility. Guarded by
            #   ``convId + last_usage`` INTERNALLY — safe to call
            #   unconditionally. All three stamps are round-match
            #   protected (api_rounds[-1].round == round_num + 1) and
            #   individually wrapped in try/except so a stamp bug on
            #   one field never blocks the other two.
            stamp_round_cache_accounting(
                task,
                round_num=round_num, tid=tid, model=rs.model,
                tools=_tools_this_round, usage=rs.last_usage,
                assistant_msg=rs.assistant_msg,
                api_rounds=rs.api_rounds, messages=messages,
            )

            # ★ Settle orphan early-announced rounds left by a discarded stream
            #   retry. stream_chat re-runs the SSE stream on a transient
            #   mid-stream error while reusing the same on_tool_call_ready
            #   callback, so a tool call whose args streamed far enough on an
            #   EARLIER attempt already got a 'searching' round + tool_start —
            #   but only the FINAL attempt's tool calls survive into
            #   assistant_msg. Any announced round whose tc_id isn't in the
            #   final message is orphaned at 'searching' forever (a permanently
            #   spinning tool row, live AND after reload). Reconcile here — the
            #   per-round complement of the task-end dangling sweep — BEFORE
            #   parse_tool_calls so the orphan never reaches the render/persist
            #   path unsettled.
            _stream_acc.reconcile_announced_rounds(rs.assistant_msg)

            # ★ Read back updated tool_round_num from streaming accumulator
            #   (tool_start events emitted during streaming already consumed
            #   round numbers, so parse_tool_calls must start from here).
            if _stream_acc.announced_tc_map:
                rs.tool_round_num = _stream_acc.tool_round_num

            # ★ Inject pre-computed streaming tool results into dedup cache.
            #   execute_tool_pipeline will find these and skip re-execution.
            if _stream_acc.submitted_count > 0:
                _prefetch_hits = _stream_acc.inject_into_cache(task)
                if _prefetch_hits:
                    logger.info('[%s] Streaming tool exec: %d results pre-computed '
                                'and injected into cache', tid, _prefetch_hits)

            # ★ Post-stream analysis: premature close, abort, normal exit
            stream_decision = analyse_stream_result(
                rs.assistant_msg, rs.last_finish_reason, task, tid, rs.model,
                round_num, _premature_retry_count, messages,
                usage=rs.last_usage,
            )
            _premature_retry_count = stream_decision['premature_retry_count']
            rs.last_finish_reason = stream_decision['last_finish_reason']
            if stream_decision['abort_detected_phase']:
                rs.abort_phase = stream_decision['abort_detected_phase']
            if stream_decision['action'] == 'break':
                rs.exit_reason = stream_decision['loop_exit_reason']
                break
            if stream_decision['action'] == 'continue':
                continue

            # ── Per-round diagnostic: log finish_reason for every tool round ──
            _round_content = len((rs.assistant_msg or {}).get('content', '') or '')
            _round_tcs = len((rs.assistant_msg or {}).get('tool_calls', []))
            logger.info('[%s] conv=%s Round %d result: finish_reason=%s model=%s '
                        'content=%dchars tool_calls=%d → proceeding to tool execution',
                        tid, task.get('convId', ''), round_num + 1, rs.last_finish_reason, rs.model,
                        _round_content, _round_tcs)

            # ── max_budget_usd gate (Claude Agent SDK parity) ──
            # Hard $ ceiling on accumulated cost.  0 / unset disables.
            _max_budget = float(cfg.get('maxBudgetUsd') or 0.0)
            if _max_budget > 0:
                from lib.cost_estimator import check_budget
                _exceeded, _cost, _reason = check_budget(
                    task, rs.accumulated_usage, rs.model, _max_budget,
                    round_num=round_num,
                )
                if _exceeded:
                    rs.last_finish_reason = 'budget_exceeded'
                    from lib.error_envelope import make_envelope as _make_env
                    task['error'] = _make_env(
                        'budget_exceeded',
                        detail=_reason,
                        model=rs.model,
                        context='budget-gate',
                        source='orchestrator',
                        raw=f'cost_usd={_cost:.6f} max={_max_budget:.6f}',
                    )
                    rs.exit_reason = f'budget_exceeded_round_{round_num}_${_cost:.4f}'
                    append_event(task, build_event(EventType.ROUND_END,
                                                   roundNum=round_num, reason='budget'))
                    break

            # ── Tool round budget check ──
            if round_num >= max_tool_rounds:
                # Safety ceiling: tool round budget exhausted
                rs.last_finish_reason = 'tool_rounds_exhausted'
                from lib.error_envelope import make_envelope as _make_env
                task['error'] = _make_env(
                    'tool_rounds_exhausted',
                    detail=f'Tool call limit reached ({max_tool_rounds} rounds).',
                    model=rs.model,
                    context='tool-budget',
                    source='orchestrator',
                    raw=f'max_tool_rounds={max_tool_rounds}',
                )
                logger.warning('[Task %s] conv=%s ⚠️ Tool rounds exhausted at round %d/%d', task['id'][:8], task.get('convId', ''), round_num+1, max_tool_rounds)
                rs.exit_reason = f'tool_rounds_exhausted_{round_num}'
                append_event(task, build_event(EventType.ROUND_END,
                                               roundNum=round_num, reason='budget'))
                break

            rs.tool_call_happened = True
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
                tool_calls=rs.assistant_msg['tool_calls'],
                content=rs.assistant_msg.get('content'),
                reasoning_content=rs.assistant_msg.get('reasoning_content'),
                thinking_signature=rs.assistant_msg.get('thinking_signature'))
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
                submit_round_segment(task, round_num, rs.assistant_msg.get('content') or '')
            except Exception as _ite:
                logger.debug('[%s] incremental translate submit failed (non-fatal): %s', tid, _ite)

            # ★ Expose live messages to context_compact tool handler
            task['_compact_messages'] = messages

            # ══════════════════════════════════════════
            #  Tool Execution Pipeline (delegated to tool_dispatch)
            # ══════════════════════════════════════════

            # ── Abort check before tool execution ──
            if task['aborted']:
                rs.abort_phase = f'before_tool_exec_round_{round_num}'
                rs.exit_reason = f'aborted_before_tools_round_{round_num}'
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
            parsed_tcs, rs.tool_round_num = parse_tool_calls(
                rs.assistant_msg, task, round_num, rs.tool_round_num, project_enabled,
                early_announced=_stream_acc.announced_tc_map,
            )

            # ── Phase 1b: Sanitize tool_calls in messages so the next API
            #   round doesn't carry malformed JSON args back to the gateway.
            #   Extracted 2026-07-31 (pt_03f4cdf1 slice 14) into
            #   lib.tasks_pkg.orchestrator._sanitize_tool_call_args — see
            #   that module's docstring for the HTTP-400 recovery rationale
            #   and the RAW-args log-line evidence trail.
            sanitize_malformed_tool_call_args(
                parsed_tcs, messages,
                tid=tid, conv_id=task.get('convId', ''), model=rs.model)

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
                tool_list, messages, all_search_results_text, round_num, rs.model,
            )

            # Clean up live messages ref after tool execution
            task.pop('_compact_messages', None)

            # ── Phase 4b: Consecutive tool-timeout circuit breaker ──
            if _tool_timed_out:
                rs.consecutive_tool_timeouts += 1
                logger.warning(
                    '[%s] conv=%s Tool timeout at round %d (%d/%d consecutive) model=%s',
                    tid, task.get('convId', ''), round_num + 1, rs.consecutive_tool_timeouts,
                    _MAX_CONSECUTIVE_TOOL_TIMEOUTS, rs.model)
                if rs.consecutive_tool_timeouts >= _MAX_CONSECUTIVE_TOOL_TIMEOUTS:
                    logger.error(
                        '[%s] conv=%s ⚠️ FORCE STOP: %d consecutive tool timeouts — breaking loop to prevent runaway task. model=%s',
                        tid, task.get('convId', ''), rs.consecutive_tool_timeouts, rs.model)
                    from lib.error_envelope import make_envelope as _make_env
                    task['error'] = _make_env(
                        'tool_timeout',
                        detail=f'{rs.consecutive_tool_timeouts} consecutive tool execution timeouts.',
                        model=rs.model,
                        context='tool-loop',
                        source='orchestrator',
                        raw=f'consecutive_tool_timeouts={rs.consecutive_tool_timeouts}',
                    )
                    rs.exit_reason = f'consecutive_tool_timeouts_{rs.consecutive_tool_timeouts}'
                    # ★ RENDER_CONTRACT Phase 3: close THIS round's boundary —
                    #   the FORCE-STOP break otherwise strands the ROUND_START
                    #   emitted at this round's top with no pairing ROUND_END
                    #   (the ONLY exit path that skipped it: budget x2 /
                    #   aborted / tools all pair). The frontend reducer does
                    #   not read `reason` (stream_reducer.js round_end case),
                    #   so the new 'tool_timeout' value is wire-safe.
                    append_event(task, build_event(
                        EventType.ROUND_END,
                        roundNum=round_num, reason='tool_timeout'))
                    break
            else:
                rs.consecutive_tool_timeouts = 0  # Reset on successful tool execution

            # ══════════════════════════════════════════
            #  ★ Crash-recovery checkpoint: persist partial state to DB
            # ══════════════════════════════════════════
            # After each tool execution round, save current content/thinking
            # to task_results + conversation so data survives a server crash.
            # Throttled to at most once every 10 seconds to avoid DB pressure.
            _now = time.time()
            if _now - rs.last_checkpoint_ts >= 5:
                try:
                    checkpoint_task_partial(task)
                    rs.last_checkpoint_ts = _now
                except Exception as e:
                    logger.warning('[%s] Checkpoint after round %d failed (non-fatal): %s', tid, round_num + 1, e, exc_info=True)

            # ★ RENDER_CONTRACT Phase 3: explicit round-end boundary for a round
            #   that issued tool calls and is about to loop into the next round.
            #   Reached only at the natural end of a tools-executed iteration
            #   (an early `continue` for a premature-close retry does NOT reach
            #   here, so it never emits a spurious end for a round being re-run).
            append_event(task, build_event(EventType.ROUND_END,
                                           roundNum=round_num, reason='tools'))



        # ── Post-loop success tail (pt_03f4cdf1 slice 6):
        #    append-final-assistant + write-back-messages + save-to-store
        #    + finalize-and-emit-done extracted to
        #    lib.tasks_pkg.orchestrator._post_loop.finalize_after_loop.
        #    Byte-identical event sequence + task mutations.
        finalize_after_loop(
            task,
            cfg=cfg, tid=tid,
            model=rs.model, preset=rs.preset,
            thinking_depth=thinking_depth,
            thinking_enabled=rs.thinking_enabled,
            temperature=temperature, max_tokens=max_tokens,
            messages=messages, original_messages=original_messages,
            tool_list=tool_list, assistant_msg=rs.assistant_msg,
            round_num=round_num,
            accumulated_usage=rs.accumulated_usage, api_rounds=rs.api_rounds,
            last_finish_reason=rs.last_finish_reason, last_usage=rs.last_usage,
            tool_call_happened=rs.tool_call_happened,
            all_search_results_text=all_search_results_text,
            project_path=project_path, project_enabled=project_enabled,
            keep_tool_history=_keep_tool_history, conv_id=_conv_id,
            loop_exit_reason=rs.exit_reason,
            abort_detected_phase=rs.abort_phase,
        )
    except Exception as e:
        # FATAL-path handling extracted to
        # lib.tasks_pkg.orchestrator._post_loop.handle_task_fatal
        # (pt_03f4cdf1 slice 6): user-error extraction, endpoint-managed
        # short-circuit, turn-level auto-retry, recovery-carrier
        # re-stamp, terminal-DONE + persist. Returns True when the
        # caller should return early (retry-in-progress or endpoint-
        # managed handoff), False when the fall-through path already
        # emitted terminal DONE + persisted.
        if handle_task_fatal(task, e):
            return
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
        # 5-step teardown lane extracted to
        # lib.tasks_pkg.orchestrator._teardown.finalize_task_lane
        # (pt_03f4cdf1 slice 5): presence.mark_idle + set_req_id('') +
        # clear_pinned_provider + clear_conv_affinity +
        # get_conversation_store().release_connection(). Each step is
        # its own try/except so one failure never blocks the others.
        finalize_task_lane(task, tid=tid)

