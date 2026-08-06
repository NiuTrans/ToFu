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

# NOTE: ``import threading`` was removed 2026-07-23 (pt_03f4cdf1 slice 2).
# The only usage inside run_task was the daemon-thread spawn of the
# external-edit probe, which now lives in
# lib.tasks_pkg.orchestrator._vu_startup.start_external_edit_probe.
# NOTE: ``import time`` was removed 2026-08-01 (pt_03f4cdf1 slice 35).
# The queue-wait timing moved to _task_open.log_task_open.
from typing import Any

from lib.log import get_logger, set_req_id

logger = get_logger(__name__)


from lib.llm import AbortedError  # noqa: F401  (re-exported by the package facade)
from lib.agent_core.events import EventType, build_event  # noqa: F401  (re-exported by the package facade)
from lib.tasks_pkg.manager import (
    _strip_base64_for_snapshot,  # noqa: F401  (re-exported by the package facade after slice 15)
    append_event,  # noqa: F401  (re-exported by the package facade)
    checkpoint_task_partial,  # noqa: F401  (re-exported by the package facade)
    persist_task_result,  # noqa: F401  (re-exported by the package facade after slice 34)
    stream_llm_response,  # noqa: F401  (re-exported by the package facade)
)
from lib.tasks_pkg.commit_round import (  # noqa: E402
    _run_commit_round_async,  # noqa: F401  (re-export for back-comp)
    _spawn_async_commit_round,  # noqa: F401  (re-exported by the package facade)
    _spawn_async_profile_consolidation,  # noqa: F401  (re-exported by the facade)
    derive_round_modified_files,  # noqa: F401  (re-exported by the facade)
)
from lib.tasks_pkg.tool_dispatch import (
    tool_label,  # noqa: F401  (re-exported by the package facade)
)

# Per-turn / finalize helpers live in the sibling ``_finalize`` module.

# Startup helpers extracted 2026-07-23 (pt_03f4cdf1 slice 2) — the first
# real source movement out of run_task's 1813-line body. The VU closure
# adapter moved to make_vu_phase (slice 37); call sites keep the
# closure-style single-arg call.
from lib.tasks_pkg.orchestrator._vu_startup import (
    _probe_external_edits,  # noqa: F401  (imported for wire-parity guard + back-compat)
    make_vu_phase,
    setup_project_context,
    start_external_edit_probe,  # noqa: F401  (also invoked indirectly via setup_project_context)
)
from lib.tasks_pkg.orchestrator._prefetch import start_prefetches
from lib.tasks_pkg.orchestrator._context_inject import inject_context_and_emit_chips  # noqa: E501
from lib.tasks_pkg.orchestrator._round_state import RoundState
from lib.tasks_pkg.orchestrator._tool_history import restore_tool_history
from lib.tasks_pkg.orchestrator._tool_history import (
    inject_continue_tool_history,
)
from lib.tasks_pkg.orchestrator._memory_prefetch import (
    await_memory_prefetch,
    maybe_run_memory_prefetch,
)
from lib.tasks_pkg.orchestrator._resume_state import apply_resume_state
from lib.tasks_pkg.orchestrator._post_loop import (
    finalize_after_loop,
    handle_task_base_exception,
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
from lib.tasks_pkg.orchestrator._abort_prep import (
    handle_abort_during_prep,
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
from lib.tasks_pkg.orchestrator._provider_binding import (
    bind_provider_and_affinity,
)
from lib.tasks_pkg.orchestrator._round_open import (
    build_stream_accumulator,
    emit_round_open,
)
from lib.tasks_pkg.orchestrator._turn_prelude import run_turn_prelude
from lib.tasks_pkg.orchestrator._task_open import (
    check_autopilot_kick,
    log_task_open,
    snapshot_turn_input,
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
    # ★ Task open (slice 35 → _task_open: kick / snapshot / open-log).
    if check_autopilot_kick(task):
        return
    snapshot_turn_input(task)
    _t_run_start = log_task_open(task, tid)
    try:
        cfg = task['config']

        # ── VU phase closure (slice 37 → _vu_startup.make_vu_phase).
        _vu_phase = make_vu_phase(task)

        # ── Turn prelude (slice 33 → _turn_prelude; returns the rebound cfg).
        cfg = run_turn_prelude(task, cfg, tid)

        # ── Provider binding: hard pin + conv affinity
        #    (slice 31 → _provider_binding; cleared in finally).
        bind_provider_and_affinity(task, tid)

        # ── Section 1: Config & Model Resolution
        #    (slice 30 → _config_resolution; the 17-field unpack below
        #    stays inline as local binding).
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
        # ── One-shot project-scope startup (slice 4 → setup_project_context).
        setup_project_context(task, cfg, project_path, project_enabled)
        code_exec_enabled = mcfg['code_exec_enabled']
        memory_enabled  = mcfg['memory_enabled']
        browser_enabled = mcfg['browser_enabled']
        desktop_enabled = mcfg['desktop_enabled']
        swarm_enabled   = mcfg['swarm_enabled']
        image_gen_enabled = mcfg['image_gen_enabled']
        # ── Memory/project prefetch pool (slice 3 → _prefetch;
        #    shut down in the context-inject helper).
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

        # ── Section 2: Tool Assembly (slice 29 → _tool_assembly_prep;
        #    force-enable guard + _tool_schema stash).
        tool_list, has_real_tools, max_tool_rounds = assemble_round_tools(
            cfg, task, mcfg, vu_phase=_vu_phase)

        # (Planner no-tools override removed — all endpoint roles now
        #  get full tool access.  See endpoint_review._run_planner_turn.)

        messages = list(task['messages'])
        original_messages = list(messages)
        # ── Round-loop cross-iteration state (slice 1): the 14 locals
        #    crossing the stream-loop boundary live on ONE flat carrier
        #    (docs/ROUND_STATE_LOCALS_INVENTORY.md).
        rs = RoundState(model=model, preset=preset,
                        thinking_enabled=thinking_enabled)
        all_search_results_text = []

        # ★ Abort-during-prep gates (2026-08-06 conv msftgnt3 incident →
        #   _abort_prep): one sticky-flag check per expensive stage boundary —
        #   the FIRST tripped stage owns exit_reason and skips the loop below.
        _prep_aborted = handle_abort_during_prep(task, rs, stage='startup',
                                                 tid=tid)

        # ── Section 2.5: tool history restoration
        #    (slice 8 → _tool_history.restore_tool_history).
        _keep_tool_history = cfg.get('keepToolHistory', True)
        _conv_id = task.get('convId', '')
        messages, original_messages, _tool_history_used = restore_tool_history(
            task=task, cfg=cfg, messages=messages, tid=tid, vu_phase=_vu_phase,
        )
        if not _prep_aborted:
            _prep_aborted = handle_abort_during_prep(task, rs,
                                                     stage='tool_setup',
                                                     tid=tid)

        # ── Section 3.5 (SPAWN): memory prefetch started EARLY so it
        #   overlaps Section 3 (joined by await_memory_prefetch before the
        #   stream loop). Eligibility reads cfg['toolHistory'] — the drift
        #   guard below pins it against the actual injected count.
        maybe_run_memory_prefetch(
            task=task, cfg=cfg, messages=messages, tool_list=tool_list,
            project_path=project_path, project_enabled=project_enabled,
            memory_enabled=memory_enabled, has_real_tools=has_real_tools,
            injected_tool_calls=len(cfg.get('toolHistory') or []),
        )

        # ── Section 3: Context Injection → _t_prep_done
        #    (slice 7 → _context_inject).
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
        if not _prep_aborted:
            _prep_aborted = handle_abort_during_prep(task, rs,
                                                     stage='context_inject',
                                                     tid=tid)

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

        # ★ Continue-toolHistory injection + drift guard
        #   (slice 36 → _tool_history.inject_continue_tool_history).
        _injected_tool_calls = inject_continue_tool_history(
            task=task, rs=rs, messages=messages, cfg=cfg, model=model, tid=tid)

        # ── Resume-state hydration (slice 10 → _resume_state).
        apply_resume_state(task=task, cfg=cfg, messages=messages,
                           model=model, tid=tid)

        # ★ 禁止添加 anti-loop / 预算警告 / _force_stop 等机制。
        #   不允许在运行时向 messages 注入任何 [SYSTEM NOTE] 或 [SYSTEM:] 消息来
        #   干扰模型的正常生成。详见 max_tool_rounds 注释。

        # ── Join the background memory prefetch (BOUNDED wait — a late
        #   write into a body already on the wire is worse than a missing
        #   advisory memory; no-op when nothing was spawned).
        await_memory_prefetch(task)
        if not _prep_aborted:
            _prep_aborted = handle_abort_during_prep(task, rs, stage='prefinal',
                                                     tid=tid)

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
        while (not _prep_aborted
               and round_num + 1 <= max_tool_rounds + _premature_retry_count):
            round_num += 1
            # ★ Abort-at-round-start gate (slice 23 → _abort_round_start; True → break).
            if handle_abort_at_round_start(task, rs,
                                           round_num=round_num, tid=tid):
                break

            # ★ Per-round open: ROUND_START + phase emit
            #   (slice 32 → _round_open).
            emit_round_open(task, rs, round_num)

            # ★ Per-round message hygiene: compaction + attachments + cleanup
            #   (slice 18 → _round_message_hygiene).
            run_round_message_hygiene(
                task, messages,
                round_num=round_num, tid=tid,
                project_path=project_path, project_enabled=project_enabled,
                search_enabled=search_enabled,
            )

            # ★ Drain swarm inbox — coalesced user-role inject before the
            #   LLM call (slice 11 → _swarm_inbox; never raises).
            drain_and_inject_inbox(task=task, messages=messages,
                                   round_num=round_num, tid=tid)

            # ★ Round-request preamble → (_tools_this_round, body)
            #   (slice 28 → _round_request_prep).
            _tools_this_round, body = build_round_request(
                task, rs, messages, tool_list,
                round_num=round_num, tid=tid,
                max_tool_rounds=max_tool_rounds,
                thinking_depth=thinking_depth, temperature=temperature,
                max_tokens=max_tokens, response_format=response_format,
            )

            # ★ Streaming-accumulator construction
            #   (slice 32 → _round_open).
            _stream_acc = build_stream_accumulator(
                task, rs, cfg, round_num, project_enabled)

            # ★ Per-round DB-connection checkpoint release
            #   (slice 27 → _db_conn_release; best-effort).
            release_db_conn_checkpoint(round_num=round_num, tid=tid)

            # ★ LLM call with fallback + inbox flush + abort handling
            #   (slice 26 → _llm_round_call; 'break' → break).
            if run_llm_call_with_fallback(
                    task, rs, body, messages, tool_list, _stream_acc,
                    round_num=round_num, tid=tid,
                    max_tokens=max_tokens,
                    max_tool_rounds=max_tool_rounds) == 'break':
                break

            # ★ Per-round cache accounting — cacheBreak/toolCalls/
            #   writeBreakdown stamps (slice 13 → _cache_round_accounting;
            #   internally guarded, unconditional call).
            stamp_round_cache_accounting(
                task,
                round_num=round_num, tid=tid, model=rs.model,
                tools=_tools_this_round, usage=rs.last_usage,
                assistant_msg=rs.assistant_msg,
                api_rounds=rs.api_rounds, messages=messages,
            )

            # ★ Post-LLM streaming-accumulator settle
            #   (slice 24 → _stream_acc_settle).
            settle_stream_accumulator(_stream_acc, task, rs, tid=tid)

            # ★ Post-stream decision (slice 25 → _stream_decision;
            #   'break'/'continue' + premature_retry_count rebind).
            _stream_action, _premature_retry_count = apply_stream_decision(
                task, rs, round_num=round_num, tid=tid,
                premature_retry_count=_premature_retry_count,
                messages=messages)
            if _stream_action == 'break':
                break
            if _stream_action == 'continue':
                continue

            # ── Per-round gates: budget + tool-rounds ceilings
            #   (slice 17 → _round_gates; True → break).
            if check_round_gates(task, rs, round_num=round_num, tid=tid,
                                 max_tool_rounds=max_tool_rounds, cfg=cfg):
                break

            rs.tool_call_happened = True
            # ★ Live-tail assistant/tool_call message + translate
            #   (slice 16 → _tool_call_prelude).
            append_assistant_tool_call_message(
                task, messages,
                round_num=round_num, tid=tid,
                assistant_msg=rs.assistant_msg)

            # ══════════════════════════════════════════
            #  Tool Execution Pipeline (delegated to tool_dispatch)
            # ══════════════════════════════════════════

            # ── Abort check before tool execution
            #   (slice 19 → _abort_before_tools; True → break).
            if handle_abort_before_tools(task, rs, messages,
                                         round_num=round_num, tid=tid):
                break

            # ── Per-round tool dispatch → _tool_timed_out flag
            #   (slice 22 → _tool_dispatch_round).
            _tool_timed_out = run_tool_dispatch(
                task, rs, messages, all_search_results_text,
                round_num=round_num, tid=tid,
                cfg=cfg, project_path=project_path,
                project_enabled=project_enabled, tool_list=tool_list,
                announced_tc_map=_stream_acc.announced_tc_map,
            )

            # ── Consecutive tool-timeout circuit breaker
            #   (slice 21 → _tool_timeout_breaker; True → break).
            if handle_tool_timeout_circuit_breaker(
                    task, rs, round_num=round_num, tid=tid,
                    tool_timed_out=_tool_timed_out,
                    max_consecutive_tool_timeouts=_MAX_CONSECUTIVE_TOOL_TIMEOUTS):
                break

            # ★ Crash-recovery checkpoint (throttled) + round close
            #   (slice 20 → _round_checkpoint).
            run_round_checkpoint_and_close(task, rs,
                                           round_num=round_num, tid=tid)



        # ── Post-loop success tail (slice 6 → _post_loop.finalize_after_loop).
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
        # FATAL-path handling (slice 6 → _post_loop.handle_task_fatal;
        # True → return early).
        if handle_task_fatal(task, e):
            return
    except BaseException as be:
        # ── Non-Exception fatal (slice 34 → _post_loop
        #    .handle_task_base_exception; finalizes + re-raises).
        handle_task_base_exception(task, be)
    finally:
        # 5-step teardown lane (slice 5 → _teardown.finalize_task_lane;
        # each step fail-soft).
        finalize_task_lane(task, tid=tid)

