# HOT_PATH — functions in this module are called per-request.
# Prefer logger.debug() over logger.info(). logger.info() is reserved
# for rare, high-signal events (e.g. content-filter injection, per-round diagnostics).
"""Orchestrator main loop — ``run_task`` kept as ONE whole function.

This is the hottest path in the codebase.  The phased Planner/tool loop
with SSE emission + round accounting lives here as a single function; the
per-turn / finalize helpers live in sibling modules (``_finalize``).

Outbound dependencies shared by multiple phases live in ``_ports``.
"""

from __future__ import annotations

# NOTE: ``import threading`` was removed 2026-07-23 ( slice 2).
# The only usage inside run_task was the daemon-thread spawn of the
# external-edit probe, which now lives in
# lib.tasks_pkg.orchestrator._vu_startup.start_external_edit_probe.
# NOTE: ``import time`` was removed 2026-08-01 ( slice 35).
# The queue-wait timing moved to _task_open.log_task_open.
from typing import Any

from lib.log import clear_log_context, get_logger, set_log_context, set_req_id

logger = get_logger(__name__)


from lib.agent_core.events import (
    Phase,
    emit_phase,
)

# Startup helpers extracted 2026-07-23 ( slice 2) — the first
# real source movement out of run_task's 1813-line body. The VU closure
# adapter moved to make_vu_phase (slice 37); call sites keep the
# closure-style single-arg call.
from lib.tasks_pkg.orchestrator._vu_startup import (
    _probe_external_edits,  # noqa: F401  (imported for wire-parity guard + back-compat)
    make_vu_phase,
    setup_project_context,
    start_external_edit_probe,  # noqa: F401  (also invoked indirectly via setup_project_context)
)
from lib.tasks_pkg.orchestrator._prefetch import (
    start_prefetches,
    stop_prefetches,
)
from lib.tasks_pkg.orchestrator._context_inject import inject_context_and_emit_chips  # noqa: E501
from lib.tasks_pkg.orchestrator._round_state import RoundState
from lib.tasks_pkg.orchestrator._root_agent_loop import (
    RootLoopRequest,
    run_root_agent_loop,
)
from lib.tasks_pkg.orchestrator._tool_history import (
    inject_continue_tool_history,
    prepare_continue_tool_history,
)
from lib.tasks_pkg.orchestrator._memory_prefetch import (
    maybe_run_memory_prefetch,
)
from lib.tasks_pkg.orchestrator._resume_state import (
    apply_resume_state,
    prepare_resume_state,
)
from lib.tasks_pkg.orchestrator._post_loop import (
    finalize_after_loop,
    handle_task_base_exception,
    handle_task_fatal,
)
from lib.tasks_pkg.orchestrator._teardown import finalize_task_lane
from lib.tasks_pkg.orchestrator._abort_prep import (
    handle_abort_during_prep,
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
from lib.tasks_pkg.orchestrator._turn_prelude import run_turn_prelude
from lib.tasks_pkg.orchestrator._task_open import (
    check_autopilot_kick,
    log_task_open,
    snapshot_turn_input,
)


_STARTUP_PHASES = {
    'config': (
        'Resolving model and workspace settings…',
        'stream.phase.startupConfig',
    ),
    'tools': (
        'Preparing tools and workspace…',
        'stream.phase.startupTools',
    ),
    'context': (
        'Loading project context and relevant memory…',
        'stream.phase.startupContext',
    ),
}


def _emit_startup_phase(task: dict[str, Any], stage: str) -> None:
    detail, detail_key = _STARTUP_PHASES[stage]
    try:
        emit_phase(
            task,
            Phase.WORKING,
            detail=detail,
            detailKey=detail_key,
        )
    except Exception as exc:
        logger.debug('[Task %s] startup phase emit failed stage=%s: %s',
                     (task.get('id') or '')[:8], stage, exc)


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
    set_req_id(task.get('_requestId') or tid)
    # Task open (slice 35 → _task_open: kick / snapshot / open-log).
    try:
        autopilot_kicked = check_autopilot_kick(task)
    except BaseException:
        set_req_id('')
        clear_log_context()
        raise
    if autopilot_kicked:
        # This early return precedes the main try/finally. Pooled worker lanes
        # must not leak kickoff correlation into their next task.
        set_req_id('')
        clear_log_context()
        return
    set_log_context(
        task_id=task.get('id') or '',
        conversation_id=task.get('convId') or '',
        trace_id=task.get('_requestId') or '',
        user_id=task.get('_userId') or '',
    )
    _prefetch_executor = None
    try:
        # Keep the prelude inside the same no-escape teardown boundary as the
        # main loop. Snapshot/log/presence helpers are fault-injectable; if one
        # fails, a pooled thread still must clear task/conv/trace/user context.
        snapshot_turn_input(task)
        _t_run_start = log_task_open(task, tid)
        _vu_phase = make_vu_phase(task)
        _emit_startup_phase(task, 'config')
        cfg = task['config']

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
        code_exec_enabled = mcfg['code_exec_enabled']
        memory_enabled  = mcfg['memory_enabled']
        messages = list(task['messages'])
        original_messages = list(messages)
        # ── Round-loop cross-iteration state (slice 1): the 14 locals
        #    crossing the stream-loop boundary live on ONE flat carrier
        #    (docs/modules/task_engine.md).
        rs = RoundState(model=model, preset=preset,
                        thinking_enabled=thinking_enabled)
        all_search_results_text = []
        tool_list = []
        has_real_tools = False

        # Abort-during-prep gates (2026-08-06 conv msftgnt3 incident →
        #   _abort_prep): check BEFORE each expensive stage as well as after
        #   non-interruptible work. The first tripped stage owns exit_reason.
        _prep_aborted = handle_abort_during_prep(task, rs, stage='startup',
                                                 tid=tid)

        # Validate every Continue/checkpoint authority before project setup,
        # prefetch, context mutation, or provider dispatch. Keep the detached
        # result so the hydration seam does not parse/copy a large snapshot a
        # second time.
        prepared_resume_state = None
        prepared_tool_history = None
        if not _prep_aborted:
            prepared_resume_state = prepare_resume_state(cfg)
            prepared_tool_history = prepare_continue_tool_history(
                task=task, cfg=cfg, model=model)

        if not _prep_aborted:
            # ── One-shot project-scope startup (slice 4 →
            #    setup_project_context). This may touch FUSE-backed state, so
            #    never enter it for a task already cancelled in the queue.
            setup_project_context(task, cfg, project_path, project_enabled)
            _prep_aborted = handle_abort_during_prep(
                task, rs, stage='project_setup', tid=tid)

        if not _prep_aborted:
            # ── Memory/project prefetch pool (slice 3 → _prefetch).
            #    Context injection consumes it; the outer finally is the
            #    no-escape cleanup for aborts and exceptions.
            _prefetch_executor = start_prefetches(
                task, cfg=cfg, project_path=project_path,
                project_enabled=project_enabled,
                memory_enabled=memory_enabled)

            # ── Section 2: Tool Assembly (slice 29 →
            #    _tool_assembly_prep; force-enable guard + schema stash).
            _emit_startup_phase(task, 'tools')
            tool_list, has_real_tools = assemble_round_tools(cfg, task, mcfg)
            _prep_aborted = handle_abort_during_prep(task, rs,
                                                     stage='tool_setup',
                                                     tid=tid)

        if _prep_aborted and _prefetch_executor is not None:
            stop_prefetches(
                task, _prefetch_executor, cancel_pending=True)
            _prefetch_executor = None

        if not _prep_aborted:
            # ── Section 3.5: local high-confidence memory selection. It
            #    completes synchronously before Composer and never mutates
            #    messages.
            _emit_startup_phase(task, 'context')
            maybe_run_memory_prefetch(
                task=task, cfg=cfg, messages=messages, tool_list=tool_list,
                project_path=project_path, project_enabled=project_enabled,
                memory_enabled=memory_enabled,
                has_real_tools=has_real_tools,
                injected_tool_calls=prepared_tool_history.injected_calls,
            )

            # ── Section 3: Context Injection → _t_prep_done
            #    (slice 7 → _context_inject).
            inject_context_and_emit_chips(
                task=task, messages=messages, cfg=cfg,
                project_path=project_path, project_enabled=project_enabled,
                memory_enabled=memory_enabled,
                search_enabled=search_enabled,
                has_real_tools=has_real_tools,
                model=model, tool_list=tool_list,
                prefetch_executor=_prefetch_executor,
                tid=tid, t_run_start=_t_run_start,
                vu_phase=_vu_phase,
            )
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

        if not _prep_aborted:
            # Tool history is already reconstructed from the owner-scoped,
            # turn-native transcript before task creation. There is
            # deliberately no second in-process message authority here.
            _injected_tool_calls = inject_continue_tool_history(
                task=task, rs=rs, messages=messages, cfg=cfg, model=model,
                tid=tid, prepared_history=prepared_tool_history)
            if _injected_tool_calls != prepared_tool_history.injected_calls:
                raise RuntimeError(
                    'Prepared Continue tool-history count changed at injection')

            # ── Resume-state hydration (slice 10 → _resume_state).
            apply_resume_state(task=task, cfg=cfg, messages=messages,
                               model=model, tid=tid,
                               prepared_state=prepared_resume_state)

            _prep_aborted = handle_abort_during_prep(task, rs, stage='prefinal',
                                                     tid=tid)

        round_num = -1
        if not _prep_aborted:
            loop_result = run_root_agent_loop(RootLoopRequest(
                task=task,
                state=rs,
                messages=messages,
                tool_list=tool_list,
                all_search_results_text=all_search_results_text,
                cfg=cfg,
                tid=tid,
                thinking_depth=thinking_depth,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                project_path=project_path,
                project_enabled=project_enabled,
                search_enabled=search_enabled,
            ))
            round_num = loop_result.last_round_num
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
            last_stream_result=rs.last_stream_result,
            tool_call_happened=rs.tool_call_happened,
            all_search_results_text=all_search_results_text,
            project_path=project_path, project_enabled=project_enabled,
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
        # No-escape teardown lane (slice 5 → _teardown.finalize_task_lane;
        # each step fail-soft).
        stop_prefetches(task, _prefetch_executor, cancel_pending=True)
        finalize_task_lane(task, tid=tid)
