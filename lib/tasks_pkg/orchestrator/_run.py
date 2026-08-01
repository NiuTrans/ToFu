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


from lib.llm import AbortedError  # noqa: F401  (re-exported by the package facade)
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.manager import (
    _strip_base64_for_snapshot,  # noqa: F401  (re-exported by the package facade after slice 15)
    append_event,
    checkpoint_task_partial,  # noqa: F401  (re-exported by the package facade)
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
from lib.tasks_pkg.system_context import (
    _inject_system_contexts,
    _disabled_prompt_blocks,
)
from lib.tasks_pkg.server_message_store import (
    save_messages as _save_messages_to_store,
)
from lib.tasks_pkg.tool_dispatch import (
    tool_label,  # noqa: F401  (re-exported by the package facade)
)

# Per-turn / finalize helpers live in the sibling ``_finalize`` module.
from lib.tasks_pkg.orchestrator._finalize import (
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
from lib.tasks_pkg.orchestrator._cache_round_accounting import (
    stamp_round_cache_accounting,
)
from lib.tasks_pkg.orchestrator._tool_call_prelude import (
    append_assistant_tool_call_message,
)
from lib.tasks_pkg.orchestrator._round_gates import check_round_gates
from lib.tasks_pkg.orchestrator._round_message_hygiene import (
    run_round_message_hygiene,
)
from lib.tasks_pkg.orchestrator._abort_before_tools import (
    handle_abort_before_tools,
)
from lib.tasks_pkg.orchestrator._round_checkpoint import (
    run_round_checkpoint_and_close,
)
from lib.tasks_pkg.orchestrator._tool_timeout_breaker import (
    handle_tool_timeout_circuit_breaker,
)
from lib.tasks_pkg.orchestrator._tool_dispatch_round import (
    run_tool_dispatch,
)
from lib.tasks_pkg.orchestrator._abort_round_start import (
    handle_abort_at_round_start,
)
from lib.tasks_pkg.orchestrator._stream_acc_settle import (
    settle_stream_accumulator,
)
from lib.tasks_pkg.orchestrator._stream_decision import (
    apply_stream_decision,
)
from lib.tasks_pkg.orchestrator._llm_round_call import (
    run_llm_call_with_fallback,
)
from lib.tasks_pkg.orchestrator._db_conn_release import (
    release_db_conn_checkpoint,
)
from lib.tasks_pkg.orchestrator._round_request_prep import (
    build_round_request,
)
from lib.tasks_pkg.orchestrator._tool_assembly_prep import (
    assemble_round_tools,
)
from lib.tasks_pkg.orchestrator._config_resolution import (
    resolve_and_seed_model_config,
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
        #   _resolve_model_config + immediate task['model'] seed (the
        #   floor for first-call dispatch failures — epic
        #   pt_8f6cbc753855415e). Extracted 2026-07-31 (pt_03f4cdf1
        #   slice 30) to lib.tasks_pkg.orchestrator._config_resolution.
        #   The 17-field unpack below stays inline as local binding.
        mcfg = resolve_and_seed_model_config(cfg, task)
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
        mcfg.get('human_guidance_enabled', False)
        mcfg.get('scheduler_enabled', False)
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
        #   VU phase line → _assemble_tool_list → pending-swarm
        #   force-enable guard → task['_tool_schema'] stash. Extracted
        #   2026-07-31 (pt_03f4cdf1 slice 29) to
        #   lib.tasks_pkg.orchestrator._tool_assembly_prep — see that
        #   module's docstring for the force-enable contract (the
        #   get_agent_result / await_agents rejection-desync root fix)
        #   and the compaction token-gate stash rationale. All feature
        #   flags travel via mcfg; vu_phase is the local closure above.
        tool_list, has_real_tools, max_tool_rounds = assemble_round_tools(
            cfg, task, mcfg, vu_phase=_vu_phase)

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
            # ── Abort-at-round-start gate ──
            #   Extracted 2026-07-31 (pt_03f4cdf1 slice 23) to
            #   lib.tasks_pkg.orchestrator._abort_round_start — see that
            #   module's docstring for the abort-signal-age forensics and
            #   the no-ROUND_END contract (the round never opened, so
            #   there is nothing to pair). Returns True → break.
            if handle_abort_at_round_start(task, rs,
                                           round_num=round_num, tid=tid):
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

            # ★ Per-round message hygiene: two-layer compaction +
            #   per-turn attachments + legacy search-addendum cleanup.
            #   Extracted 2026-07-31 (pt_03f4cdf1 slice 18) to
            #   lib.tasks_pkg.orchestrator._round_message_hygiene — see that
            #   module's docstring for the step ordering and the advisory
            #   (never-fatal) attachments contract.
            run_round_message_hygiene(
                task, messages,
                round_num=round_num, tid=tid,
                project_path=project_path, project_enabled=project_enabled,
                search_enabled=search_enabled,
            )

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

            # ★ Round-request preamble: gate the tool list for this
            #   round → cache-aware tool-result sort → messages-snapshot
            #   debug event → late-bound facade build_body → attach
            #   body['_task_id'] (cache-TTL latch). Extracted 2026-07-31
            #   (pt_03f4cdf1 slice 28) to
            #   lib.tasks_pkg.orchestrator._round_request_prep — see that
            #   module's docstring for the step ordering and the
            #   late-binding contract. Returns (_tools_this_round, body);
            #   the tool list is still needed by the round checkpoint.
            _tools_this_round, body = build_round_request(
                task, rs, messages, tool_list,
                round_num=round_num, tid=tid,
                max_tool_rounds=max_tool_rounds,
                thinking_depth=thinking_depth, temperature=temperature,
                max_tokens=max_tokens, response_format=response_format,
            )

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

            # ★ Per-round DB-connection checkpoint release. Extracted
            #   2026-07-31 (pt_03f4cdf1 slice 27) to
            #   lib.tasks_pkg.orchestrator._db_conn_release — see that
            #   module's docstring for the _conn_semaphore slot-pinning /
            #   frontend-starvation rationale and the best-effort contract.
            release_db_conn_checkpoint(round_num=round_num, tid=tid)

            # ★ LLM call with automatic fallback + deferred-inbox flush +
            #   early model surface + abort handling. Extracted 2026-07-31
            #   (pt_03f4cdf1 slice 26) to
            #   lib.tasks_pkg.orchestrator._llm_round_call — see that
            #   module's docstring for the writeback / flush / early-model /
            #   break-action / AbortedError contracts. Returns 'break'
            #   (fallback-requested break or user abort) → break.
            if run_llm_call_with_fallback(
                    task, rs, body, messages, tool_list, _stream_acc,
                    round_num=round_num, tid=tid,
                    max_tokens=max_tokens,
                    max_tool_rounds=max_tool_rounds) == 'break':
                break

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

            # ★ Post-LLM streaming-accumulator settle: reconcile orphan
            #   early-announced rounds + read back tool_round_num + inject
            #   pre-computed results into the dedup cache. Extracted
            #   2026-07-31 (pt_03f4cdf1 slice 24) to
            #   lib.tasks_pkg.orchestrator._stream_acc_settle — see that
            #   module's docstring for the orphan-retry / round-readback /
            #   cache-inject contracts.
            settle_stream_accumulator(_stream_acc, task, rs, tid=tid)

            # ★ Post-stream analysis: premature close / abort / normal
            #   exit. Extracted 2026-07-31 (pt_03f4cdf1 slice 25) to
            #   lib.tasks_pkg.orchestrator._stream_decision — see that
            #   module's docstring for the action taxonomy and the
            #   chassis-owned premature_retry_count return contract.
            _stream_action, _premature_retry_count = apply_stream_decision(
                task, rs, round_num=round_num, tid=tid,
                premature_retry_count=_premature_retry_count,
                messages=messages)
            if _stream_action == 'break':
                break
            if _stream_action == 'continue':
                continue

            # ── Per-round gates: per-round diagnostic + max_budget_usd
            #   ceiling + tool-rounds ceiling. Extracted 2026-07-31
            #   (pt_03f4cdf1 slice 17) to
            #   lib.tasks_pkg.orchestrator._round_gates.check_round_gates —
            #   see that module's docstring for the gate ordering and the
            #   ROUND_END(reason='budget') / error-envelope contracts.
            #   Returns True when a gate fired (task['error'] stamped,
            #   rs.exit_reason set, ROUND_END emitted) and the loop must
            #   break.
            if check_round_gates(task, rs, round_num=round_num, tid=tid,
                                 max_tool_rounds=max_tool_rounds, cfg=cfg):
                break

            rs.tool_call_happened = True
            # ★ Assemble the live-tail assistant/tool_call message + discard
            #   pre-tool prose + submit incremental auto-translate. Extracted
            #   2026-07-31 (pt_03f4cdf1 slice 16) to
            #   lib.tasks_pkg.orchestrator._tool_call_prelude — see that
            #   module's docstring for the SINGLE SOURCE / DELTA_RESET /
            #   best-effort translate contracts.
            append_assistant_tool_call_message(
                task, messages,
                round_num=round_num, tid=tid,
                assistant_msg=rs.assistant_msg)

            # ══════════════════════════════════════════
            #  Tool Execution Pipeline (delegated to tool_dispatch)
            # ══════════════════════════════════════════

            # ── Abort check before tool execution ──
            #   Extracted 2026-07-31 (pt_03f4cdf1 slice 19) to
            #   lib.tasks_pkg.orchestrator._abort_before_tools — see that
            #   module's docstring for the orphaned-tool_use HTTP-400
            #   rationale and the prose-content re-append contract.
            #   Returns True (task aborted: trailing tool_calls message
            #   popped, ROUND_END(reason='aborted') emitted) → break.
            if handle_abort_before_tools(task, rs, messages,
                                         round_num=round_num, tid=tid):
                break

            # ── Per-round tool dispatch: parse → sanitize → emit →
            #   heartbeat → execute → pop live ref. Extracted 2026-07-31
            #   (pt_03f4cdf1 slice 22) to
            #   lib.tasks_pkg.orchestrator._tool_dispatch_round — see that
            #   module's docstring for the early_announced / reaper-
            #   heartbeat / timeout-flag contracts. Returns the pipeline's
            #   _tool_timed_out flag for the circuit breaker below.
            _tool_timed_out = run_tool_dispatch(
                task, rs, messages, all_search_results_text,
                round_num=round_num, tid=tid,
                cfg=cfg, project_path=project_path,
                project_enabled=project_enabled, tool_list=tool_list,
                announced_tc_map=_stream_acc.announced_tc_map,
            )

            # ── Phase 4b: Consecutive tool-timeout circuit breaker ──
            #   Extracted 2026-07-31 (pt_03f4cdf1 slice 21) to
            #   lib.tasks_pkg.orchestrator._tool_timeout_breaker — see that
            #   module's docstring for the FORCE-STOP envelope / exit_reason
            #   / ROUND_END(reason='tool_timeout') contracts. Returns True
            #   (ceiling reached: task['error'] stamped, ROUND_END emitted)
            #   → break; False on success (counter reset) or below-ceiling
            #   timeout (counter incremented, round proceeds).
            if handle_tool_timeout_circuit_breaker(
                    task, rs, round_num=round_num, tid=tid,
                    tool_timed_out=_tool_timed_out,
                    max_consecutive_tool_timeouts=_MAX_CONSECUTIVE_TOOL_TIMEOUTS):
                break

            # ★ Crash-recovery checkpoint (throttled) + RENDER_CONTRACT
            #   Phase 3 round close. Extracted 2026-07-31 (pt_03f4cdf1
            #   slice 20) to
            #   lib.tasks_pkg.orchestrator._round_checkpoint — see that
            #   module's docstring for the 5s throttle / non-fatal /
            #   ROUND_END(reason='tools') contracts.
            run_round_checkpoint_and_close(task, rs,
                                           round_num=round_num, tid=tid)



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

