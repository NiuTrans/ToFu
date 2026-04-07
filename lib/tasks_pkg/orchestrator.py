# HOT_PATH — functions in this module are called per-request.
# Prefer logger.debug() over logger.info(). logger.info() is reserved
# for rare, high-signal events (e.g. content-filter injection, per-round diagnostics).
"""Task orchestrator — main run_task loop coordinating LLM calls and tool execution.

Also exposes ``_run_single_turn()`` — a reusable primitive that executes one
full LLM-tool cycle (setup → tool loop → finalization) on an existing task
dict.  ``endpoint.py`` uses it to drive the outer work→review→revise loop.
"""

from __future__ import annotations

import os
import time
from typing import Any

from lib.log import get_logger
from lib.protocols import BodyBuilder

logger = get_logger(__name__)

from lib.llm_client import build_body as _build_body_impl

build_body: BodyBuilder = _build_body_impl  # type: explicit protocol binding
from lib.llm_client import AbortedError
from lib.tasks_pkg.attachments import compute_turn_attachments, inject_attachments
from lib.tasks_pkg.cache_tracking import (
    detect_cache_break,
    log_round_cache_stats,
    release_ttl_latch,
    sort_tool_results,
)
from lib.tasks_pkg.compaction import run_compaction_pipeline
from lib.tasks_pkg.executor import (
    _generate_tool_summary,
)
from lib.tasks_pkg.llm_fallback import _llm_call_with_fallback
from lib.tasks_pkg.manager import (
    _strip_base64_for_snapshot,
    append_event,
    checkpoint_task_partial,
    persist_task_result,
    stream_llm_response,
)
from lib.tasks_pkg.message_builder import inject_tool_history
from lib.tasks_pkg.model_config import (
    _assemble_tool_list,
    _resolve_model_config,
)
from lib.tasks_pkg.session_memory import trigger_memory_extraction
from lib.tasks_pkg.stream_handler import analyse_stream_result
from lib.tasks_pkg.system_context import (
    _inject_system_contexts,
    inject_search_addendum_to_user,
    inject_skills_to_user,
)
from lib.tasks_pkg.tool_dispatch import (
    _TOOL_EXEC_LABELS,
    emit_tool_exec_phase,
    execute_tool_pipeline,
    parse_tool_calls,
)


# ── Suspicious-completion detection ────────────────────────────────────────
def _check_suspicious_completion(task, last_finish_reason, _loop_exit_reason,
                                  tool_call_happened, round_num, model,
                                  assistant_msg=None):
    """Check for suspicious completion patterns and return a list of reason strings.

    Returns an empty list if the completion looks normal.  Also emits
    appropriate warning logs for each detected suspicion.
    """
    tid = task['id'][:8]
    _content_len = len(task.get('content') or '')
    _thinking_len = len(task.get('thinking') or '')
    _elapsed = time.time() - task.get('created_at', time.time())

    suspicion_reasons = []

    if _content_len == 0 and _thinking_len == 0 and not task.get('error') and not task.get('aborted'):
        suspicion_reasons.append('empty_content_and_thinking_no_error')

    if last_finish_reason == 'stop' and tool_call_happened and _content_len < 50:
        suspicion_reasons.append(f'short_content_after_tool_calls({_content_len}chars)')

    if _loop_exit_reason == 'max_rounds_exhausted':
        suspicion_reasons.append('loop_fell_through_max_rounds')
        _tc_count = len((assistant_msg or {}).get('tool_calls', []))
        logger.warning('[%s] conv=%s ⚠️ MAX TOOL ROUNDS EXHAUSTED: ran %d rounds without model stopping. '
                       'last_finish_reason=%s final_content=%dchars tool_calls_in_last_round=%d '
                       'model=%s. Consider increasing max_tool_rounds or investigating infinite tool loop.',
                       tid, task.get('convId', ''), round_num + 1, last_finish_reason, _content_len, _tc_count, model)

    if last_finish_reason is None:
        suspicion_reasons.append('finish_reason_is_None')
        logger.error('[%s] ❓ finish_reason is None — stream_llm_response likely never returned normally. '
                     'loop_exit=%s error=%s', tid, _loop_exit_reason, task.get('error') or 'none')

    if _elapsed < 1.0 and _content_len == 0:
        suspicion_reasons.append(f'completed_too_fast({_elapsed:.1f}s)_with_no_content')

    if suspicion_reasons:
        logger.warning(
            '[Orchestrator] Task %s conv=%s ⚠️ SUSPICIOUS COMPLETION detected! '
            'Reasons: %s. '
            'This task may have stopped prematurely but appears as "completed" to the user.',
            tid, task.get('convId', ''), ', '.join(suspicion_reasons)
        )

    return suspicion_reasons


# ── JSON repair for truncated / malformed LLM tool-call arguments ──────────
# Canonical implementation lives in lib.utils.repair_json.
# Re-exported here for backward compatibility.
from lib.utils import repair_json as _repair_json  # noqa: F401


def _emit_tool_round_phase(task, assistant_msg, round_num):
    """Emit a 'phase' event describing the current tool round for the frontend."""
    if round_num == 0:
        append_event(task, {'type': 'phase', 'phase': 'llm_thinking', 'detail': 'Generating response…', 'round': 1})
    else:
        tool_names = [tc['function']['name'] for tc in assistant_msg.get('tool_calls', [])]
        unique_names = list(dict.fromkeys(tool_names))
        labeled = [_TOOL_EXEC_LABELS.get(n, n) for n in unique_names]
        summary = ', '.join(labeled)
        append_event(task, {
            'type': 'phase', 'phase': 'llm_thinking',
            'detail': f'Analyzing results and planning next step… (round {round_num+1})',
            'toolContext': summary,
            'round': round_num + 1,
        })


def _finalize_and_emit_done(task: dict[str, Any], *, model: str, preset: str, thinking_depth: str | None, cfg: dict[str, Any],
                            last_finish_reason, last_usage, accumulated_usage, api_rounds,
                            tool_call_happened, messages, original_messages,
                            all_search_results_text, max_tokens, thinking_enabled, temperature,
                            _loop_exit_reason, _abort_detected_phase, project_path, project_enabled,
                            round_num, assistant_msg):
    """Post-loop finalization: fallback synthesis, done-event construction, and emit.

    Handles the fallback LLM call when the main loop produced no content,
    determines the final finish reason, generates tool summaries, and emits
    the 'done' event with full diagnostic information.
    """
    tid = task['id'][:8]

    # ── Fallback: synthesize answer from search results if main loop produced nothing ──
    if not task['content'].strip() and tool_call_happened and all_search_results_text and not task['aborted']:
        combined = '\n\n---\n\n'.join(all_search_results_text)
        fb = list(original_messages)
        fb.append({'role':'assistant','content':"I've gathered the information. Let me analyze it."})
        fb.append({'role':'user','content':f'Here are fetched contents:\n\n{combined}\n\nProvide a comprehensive answer. Cite sources.'})
        try:
            snapshot = _strip_base64_for_snapshot(fb)
            append_event(task, {'type': 'messages_snapshot', 'round': 'fallback', 'label': f'Fallback · {len(fb)}条', 'messages': snapshot})
        except Exception as e:
            logger.warning('[Task %s] messages_snapshot fallback failed, model=%s: %s', tid, model, e, exc_info=True)
        body = build_body(
            model, fb,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking_enabled=thinking_enabled,
            preset=preset,
            thinking_depth=thinking_depth,
            stream=True,
        )
        try:
            _, fr, usg = stream_llm_response(task, body, tag='FALLBACK')
            last_finish_reason = fr
            if usg:
                last_usage = usg
                for k, v in usg.items():
                    if isinstance(v, (int, float)):
                        accumulated_usage[k] = accumulated_usage.get(k, 0) + v
                api_rounds.append({'round': 'fallback', 'model': model, 'usage': dict(usg), 'tag': 'FALLBACK'})
        except Exception as e:
            logger.error('[%s] ⚠️ Post-loop fallback failed: %s', tid, e, exc_info=True)
            task['error'] = f'Fallback failed: {e}'

    # ── Content-filter: give user a meaningful error instead of blank bubble ──
    if (not task['content'].strip()
            and not task['aborted']
            and (last_finish_reason == 'content_filter'
                 or (_loop_exit_reason and 'content_filter' in str(_loop_exit_reason).lower()))):
        task['content'] = '⚠️ 该回复被模型安全过滤器拦截，请尝试换一种方式提问。\n\n_The response was blocked by the model\'s safety filter. Please try rephrasing your question._'
        logger.info('[%s] Injected content_filter user-facing message (finish_reason=%s, loop_exit=%s)',
                    tid, last_finish_reason, _loop_exit_reason)

    # ── Determine final finish reason ──
    if task['aborted']:
        _pre_abort_finish = last_finish_reason
        last_finish_reason = 'aborted'
        if _abort_detected_phase:
            logger.debug('[%s] Abort was detected INSIDE loop at: %s model=%s '
                         '(original finish_reason was "%s")',
                         tid, _abort_detected_phase, model, _pre_abort_finish)
        else:
            logger.warning('[%s] LATE ABORT: loop exited normally (%s) model=%s '
                           'but task["aborted"] is True. Original finish_reason was "%s". '
                           'The user likely clicked Stop AFTER the model finished but BEFORE the response was fully rendered.',
                           tid, _loop_exit_reason, model, _pre_abort_finish)
    elif last_finish_reason in ('tool_use', 'tool_calls') and not task.get('error'):
        last_finish_reason = 'error'
        task['error'] = task['error'] or 'Model requested tool calls but the loop ended unexpectedly.'

    task['finishReason'] = last_finish_reason
    task['usage'] = accumulated_usage if accumulated_usage else last_usage
    task['preset'] = cfg.get('preset') or cfg.get('effort', 'medium')

    # ── Generate tool summary for cross-turn context (non-blocking) ──
    if tool_call_happened and not task['aborted']:
        try:
            summary = _generate_tool_summary(messages, model, task)
            if summary:
                task['toolSummary'] = summary
        except Exception as e:
            logger.warning('[Task %s] Tool summary generation failed model=%s (non-fatal): %s', task['id'][:8], model, e, exc_info=True)

    if not task.get('_endpoint_managed'):
        task['status'] = 'done'

    # ── Cleanup reactive compact tracking (prevent memory leak) ──
    from lib.tasks_pkg.llm_fallback import cleanup_reactive_compact_state
    cleanup_reactive_compact_state(task.get('id', ''))

    # ── Release session-stable TTL latch (prevent memory leak) ──
    release_ttl_latch(task.get('id', ''))

    # ── Diagnostic: log completion stats ──
    _content_len = len(task.get('content') or '')
    _thinking_len = len(task.get('thinking') or '')
    _elapsed = time.time() - task.get('created_at', time.time())
    logger.debug('[Orchestrator] Task %s conv=%s COMPLETED — content=%dchars thinking=%dchars '
                  'error=%s elapsed=%.1fs finishReason=%s toolCalls=%s',
                 task['id'][:8], task.get('convId', ''), _content_len, _thinking_len,
                 task.get('error') or 'none', _elapsed, last_finish_reason,
                 'yes' if tool_call_happened else 'no')
    if _content_len == 0 and _thinking_len == 0 and not task.get('error') and not task.get('aborted'):
        logger.warning('[Orchestrator] Task %s conv=%s ⚠️ COMPLETED WITH EMPTY CONTENT '
                      'and no error! This will appear as a blank message to the user.',
                      task['id'][:8], task.get('convId', ''))

    logger.debug(
        '[Orchestrator] Task %s LIFECYCLE SUMMARY:\n'
        '  loop_exit_reason   = %s\n'
        '  last_finish_reason = %s\n'
        '  rounds_completed   = %d\n'
        '  tool_call_happened = %s\n'
        '  content_length     = %d\n'
        '  thinking_length    = %d\n'
        '  error              = %s\n'
        '  model              = %s\n'
        '  elapsed            = %.1fs\n'
        '  api_rounds         = %d\n'
        '  aborted            = %s\n'
        '  abort_phase        = %s',
        tid, _loop_exit_reason, last_finish_reason, round_num + 1,
        tool_call_happened, _content_len, _thinking_len,
        task.get('error') or 'none', model, _elapsed,
        len(api_rounds), task.get('aborted', False),
        _abort_detected_phase or 'n/a',
    )

    # ── Flag suspicious completions ──
    _suspicion_reasons = _check_suspicious_completion(
        task, last_finish_reason, _loop_exit_reason,
        tool_call_happened, round_num, model,
        assistant_msg=assistant_msg,
    )

    # ── Build done event ──
    done_evt = {'type': 'done'}
    if last_finish_reason: done_evt['finishReason'] = last_finish_reason
    final_usage = accumulated_usage if accumulated_usage else last_usage
    if final_usage: done_evt['usage'] = final_usage
    if task.get('preset'): done_evt['preset'] = task['preset']
    done_evt['model'] = model
    task['model'] = model
    if task.get('provider_id'):
        done_evt['provider_id'] = task['provider_id']
    if thinking_depth:
        done_evt['thinkingDepth'] = thinking_depth
        task['thinkingDepth'] = thinking_depth
    if task.get('error'): done_evt['error'] = task['error']
    if task.get('toolSummary'): done_evt['toolSummary'] = task['toolSummary']
    if api_rounds:
        done_evt['apiRounds'] = api_rounds
        task['apiRounds'] = api_rounds
    if task.get('_fallback_model'):
        done_evt['fallbackModel'] = task['_fallback_model']
        done_evt['fallbackFrom'] = task.get('_fallback_from', '')
    if project_enabled and task['convId']:
        try:
            from lib.project_mod import get_modifications
            conv_mods = get_modifications(project_path, conv_id=task['convId'])
            if conv_mods:
                # Filter to only modifications from THIS task/round
                turn_mods = [m for m in conv_mods if m.get('taskId') == task['id']]
                if not turn_mods:
                    # Fallback for older mods without taskId tag: use timestamp
                    task_start = task.get('created_at', 0)
                    turn_mods = [m for m in conv_mods if m.get('timestamp', 0) >= task_start]
                done_evt['modifiedFiles'] = len(turn_mods)
                task['modifiedFiles'] = len(turn_mods)
                # ★ Include taskId so frontend can do per-round undo
                done_evt['taskId'] = task['id']
                # ★ Include per-file detail so frontend can show which files changed
                seen = {}  # path → action  (dedup, keep latest)
                for m in turn_mods:
                    p = m.get('path', '?')
                    t = m.get('type', '')
                    if t == 'write_file':
                        action = 'created' if not m.get('existed', True) else 'written'
                    elif t == 'apply_diff':
                        action = 'patched'
                    elif t == 'run_command':
                        # run_command changes carry granular action based on existed flag
                        if not m.get('existed', True):
                            action = 'created'
                        elif 'originalContent' in m and not os.path.exists(os.path.join(project_path, p)):
                            action = 'deleted'
                        else:
                            action = 'modified'
                    else:
                        action = t
                    seen[p] = action
                file_list = [
                    {'path': p, 'action': a} for p, a in seen.items()
                ]
                done_evt['modifiedFileList'] = file_list
                task['modifiedFileList'] = file_list
        except Exception as e:
            logger.warning('[Task %s] get_modifications failed for conv=%s model=%s: %s',
                      task['id'][:8], task.get('convId', ''), model, e, exc_info=True)
    if _suspicion_reasons:
        done_evt['_diagnostics'] = {
            'loop_exit_reason': _loop_exit_reason,
            'rounds_completed': round_num + 1,
            'finish_reason': last_finish_reason,
            'content_len': _content_len,
            'thinking_len': _thinking_len,
            'suspicions': _suspicion_reasons,
        }

    # ── Emit done event (unless endpoint-managed) ──
    if task.get('_endpoint_managed'):
        return
    append_event(task, done_evt)
    persist_task_result(task)


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
    try:
        cfg = task['config']

        # ── Per-client browser routing: set thread-local client ID so all
        #    browser commands (tools, fetch fallback, search fallback) from
        #    this task thread route to the correct device's extension. ──
        _browser_client_id = cfg.get('browserClientId')
        if _browser_client_id:
            from lib.browser import _set_active_client
            _set_active_client(_browser_client_id)
            logger.debug('[Task %s] Browser client routed to %s', tid, _browser_client_id[:12])

        # ── Section 1: Config & Model Resolution ──
        mcfg = _resolve_model_config(cfg, task['id'])
        model           = mcfg['model']
        thinking_enabled = mcfg['thinking_enabled']
        thinking_depth  = mcfg['thinking_depth']
        preset          = mcfg['preset']
        max_tokens      = mcfg['max_tokens']
        temperature     = mcfg['temperature']
        search_mode     = mcfg['search_mode']
        search_enabled  = mcfg['search_enabled']
        fetch_enabled   = mcfg['fetch_enabled']
        project_path    = mcfg['project_path']
        project_enabled = mcfg['project_enabled']
        if project_enabled and project_path:
            logger.info('[Task:%s] project_path=%s', task['id'], project_path)
            # ★ Ensure the server's global project state matches this task's
            # project path.  Another conversation may have switched the server
            # to a different project, causing get_context_for_prompt to miss
            # the file tree (path mismatch → no tree in system prompt → LLM
            # doesn't know the project structure → "backend cannot use tools").
            from lib.project_mod import ensure_project_state
            ensure_project_state(project_path)
        code_exec_enabled = mcfg['code_exec_enabled']
        skills_enabled  = mcfg['skills_enabled']
        browser_enabled = mcfg['browser_enabled']
        desktop_enabled = mcfg['desktop_enabled']
        swarm_enabled   = mcfg['swarm_enabled']
        image_gen_enabled = mcfg['image_gen_enabled']
        human_guidance_enabled = mcfg.get('human_guidance_enabled', False)
        scheduler_enabled = mcfg.get('scheduler_enabled', False)
        # ── Memory Prefetch: start loading project and skills contexts in
        #    background threads while tool assembly runs (FUSE I/O can be slow).
        #    Inspired by Claude Code's startRelevantMemoryPrefetch().
        from concurrent.futures import ThreadPoolExecutor as _PrefetchPool
        _prefetch_executor = _PrefetchPool(max_workers=2,
                                           thread_name_prefix='mem-prefetch')
        _prefetch_project_future = None
        _prefetch_skills_future = None

        if project_enabled and project_path:
            def _prefetch_project():
                from lib.project_mod import get_context_for_prompt
                return get_context_for_prompt(project_path)
            _prefetch_project_future = _prefetch_executor.submit(_prefetch_project)

        # Simple heuristic: if any tool-providing feature is enabled, we'll
        # have real tools → need skills injection + accumulation instructions.
        _has_real_tools_hint = (search_enabled or fetch_enabled or
                                project_enabled or browser_enabled or
                                desktop_enabled or swarm_enabled or
                                code_exec_enabled or image_gen_enabled)
        _pp = project_path if project_enabled else None
        if skills_enabled or _has_real_tools_hint:
            def _prefetch_skills():
                from lib.skills import build_skills_context
                return build_skills_context(project_path=_pp)
            _prefetch_skills_future = _prefetch_executor.submit(_prefetch_skills)

        # Store prefetch futures on the task for _inject_system_contexts to use
        task['_prefetch_project'] = _prefetch_project_future
        task['_prefetch_skills'] = _prefetch_skills_future

        # ── Section 2: Tool Assembly ──
        tool_list, deferred_tools, has_real_tools, max_tool_rounds = _assemble_tool_list(
            cfg, project_path, project_enabled, task['id'],
            search_mode, search_enabled, fetch_enabled,
            code_exec_enabled, browser_enabled, desktop_enabled,
            swarm_enabled,
            image_gen_enabled=image_gen_enabled,
            human_guidance_enabled=human_guidance_enabled,
            scheduler_enabled=scheduler_enabled,
            messages=task['messages'],
        )

        # (Planner no-tools override removed — all endpoint roles now
        #  get full tool access.  See endpoint_review._run_planner_turn.)

        # Store deferred tools on the task for the executor to access during tool_search
        task['_deferred_tools'] = deferred_tools

        messages = list(task['messages'])
        original_messages = list(messages)
        search_round_num = 0
        all_search_results_text = []

        # ── Section 3: Context Injection ──
        _inject_system_contexts(
            messages, project_path, project_enabled,
            skills_enabled, search_enabled, swarm_enabled,
            has_real_tools,
            conv_id=task.get('convId', ''),
            task=task,
        )
        # Cleanup prefetch futures (no longer needed)
        task.pop('_prefetch_project', None)
        task.pop('_prefetch_skills', None)
        _prefetch_executor.shutdown(wait=False)

        # NOTE: Auto-prefetch disabled — the model can fetch URLs on demand
        # via the fetch_url tool call when it deems them relevant, rather than
        # being forced to fetch every URL detected in the user message.
        # if fetch_enabled:
        #     prefetched = _prefetch_user_urls(messages, task)
        #     if prefetched:
        #         search_round_num = inject_prefetched_urls(messages, prefetched, task)


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
            search_round_num = _injected_tool_calls  # offset so new roundNums don't conflict

        # ★ Apply preserved content prefix from Continue — ensures backend checkpoints
        #   include text the LLM generated alongside completed tool rounds in the prior
        #   task, so page-refresh mid-stream doesn't lose that content.
        _content_prefix = cfg.get('contentPrefix') or ''
        if _content_prefix:
            with task['content_lock']:
                task['content'] = _content_prefix
            logger.debug('[%s] conv=%s Applied contentPrefix (%d chars) from continue checkpoint',
                         tid, task.get('convId', ''), len(_content_prefix))

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
                break

            # ★ Emit phase event so the frontend knows what's happening
            _emit_tool_round_phase(task, assistant_msg if round_num > 0 else {}, round_num)

            # ★ Context compaction: two-layer pipeline
            #   L1: micro-compact cold tool results (every round, zero LLM cost)
            #   L2: smart summary as synthetic tool result (on context overflow)
            run_compaction_pipeline(messages, round_num, task=task)

            # ★ Per-turn attachments: dynamic context injection
            #   Inspired by Claude Code's getAttachments() — injects session
            #   memory, file reminders, tool discovery deltas each turn.
            if round_num > 0:  # skip round 0 (system contexts just injected)
                _attachments = compute_turn_attachments(
                    messages, task, round_num,
                    conv_id=task.get('convId', ''),
                    project_path=project_path,
                    project_enabled=project_enabled,
                )
                if _attachments:
                    inject_attachments(messages, _attachments)

            # ★ Legacy cleanup: strip old "Current date and time:" from user
            #   messages.  Date is now injected in the system prompt (step 4.5)
            #   as date-only format.  This just ensures conversations with
            #   old-format timestamps get cleaned up for proper cache prefix.
            inject_search_addendum_to_user(messages, search_enabled,
                                           round_num=round_num)

            # ★ Skills listing: inject into the last user message (NOT system)
            #   to avoid cache-breaking on skill CRUD. Uses BM25 relevance
            #   filtering to show only ~30 most relevant skills per turn.
            #   MUST be the LAST user-message injection — after attachments,
            #   after search addendum, after any planner/critic replacements.
            #   Only on round 0 — subsequent rounds skip to preserve cache.
            inject_skills_to_user(
                messages,
                project_path=project_path,
                project_enabled=project_enabled,
                skills_enabled=skills_enabled,
                has_real_tools=has_real_tools,
                conv_id=task.get('convId', ''),
                task=task,
                round_num=round_num,
            )

            # ★ Emit messages snapshot for debug panel (before LLM call)
            try:
                snapshot = _strip_base64_for_snapshot(messages)
                append_event(task, {
                    'type': 'messages_snapshot',
                    'round': round_num + 1,
                    'label': f'Round {round_num + 1} 请求前 · {len(messages)}条',
                    'messages': snapshot,
                })
            except Exception:
                logger.warning('[Task %s] messages_snapshot failed at round %d model=%s', tid, round_num + 1, model, exc_info=True)

            _tools_this_round = tool_list if (tool_list and round_num < max_tool_rounds) else None

            # ★ Cache-aware tool result ordering: sort consecutive tool results
            #   by tool_call_id so the prefix is deterministic across rounds
            #   (important for automatic prefix caching on OpenAI/Qwen).
            sort_tool_results(messages)

            body = build_body(
                model, messages,
                max_tokens=max_tokens,
                temperature=temperature,
                thinking_enabled=thinking_enabled,
                preset=preset,
                thinking_depth=thinking_depth,
                tools=_tools_this_round,
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
                search_round_num=search_round_num,
                round_num=round_num,
                project_enabled=project_enabled,
            )

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
                detect_cache_break(
                    task['convId'], messages,
                    tools=_tools_this_round, model=model,
                    usage=last_usage,
                )
                # ★ Per-round cache stats at INFO level for production visibility
                log_round_cache_stats(
                    task['convId'], round_num, last_usage,
                    model=model, tid=task['id'],
                )

            # ★ Read back updated search_round_num from streaming accumulator
            #   (tool_start events emitted during streaming already consumed
            #   round numbers, so parse_tool_calls must start from here).
            if _stream_acc.announced_tc_map:
                search_round_num = _stream_acc.search_round_num

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

            # ── Tool round budget check ──
            if round_num >= max_tool_rounds:
                # Safety ceiling: tool round budget exhausted
                last_finish_reason = 'tool_rounds_exhausted'
                task['error'] = f'⚠️ Tool call limit reached ({max_tool_rounds} rounds). The response may be incomplete.'
                logger.warning('[Task %s] conv=%s ⚠️ Tool rounds exhausted at round %d/%d', task['id'][:8], task.get('convId', ''), round_num+1, max_tool_rounds)
                _loop_exit_reason = f'tool_rounds_exhausted_{round_num}'
                break

            tool_call_happened = True
            clean_msg = {'role': 'assistant'}
            clean_msg['tool_calls'] = assistant_msg['tool_calls']
            if assistant_msg.get('content'): clean_msg['content'] = assistant_msg['content']
            if assistant_msg.get('reasoning_content'): clean_msg['reasoning_content'] = assistant_msg['reasoning_content']
            messages.append(clean_msg)

            # ★ Expose live messages to context_compact tool handler
            task['_compact_messages'] = messages

            # ══════════════════════════════════════════
            #  Tool Execution Pipeline (delegated to tool_dispatch)
            # ══════════════════════════════════════════

            # ── Abort check before tool execution ──
            if task['aborted']:
                _abort_detected_phase = f'before_tool_exec_round_{round_num}'
                _loop_exit_reason = f'aborted_before_tools_round_{round_num}'
                logger.info('[%s] Task aborted before tool execution at round %d — skipping all tools', tid, round_num)
                break

            # ── Phase 1: Parse all tool_calls ──
            #   Pass early_announced so parse_tool_calls skips re-emitting
            #   tool_start events that were already sent during streaming.
            parsed_tcs, search_round_num = parse_tool_calls(
                assistant_msg, task, round_num, search_round_num, project_enabled,
                early_announced=_stream_acc.announced_tc_map,
            )

            # ── Phase 2: Emit execution phase event ──
            emit_tool_exec_phase(task, parsed_tcs)

            # ── Phase 3: Execute tools (approval + parallel + result append) ──
            _tool_timed_out = execute_tool_pipeline(
                task, parsed_tcs, cfg, project_path, project_enabled,
                tool_list, messages, all_search_results_text, round_num, model,
            )

            # Clean up live messages ref after tool execution
            task.pop('_compact_messages', None)

            # ── Phase 4a: emit_to_user terminal detection ──
            # If any tool in this round was emit_to_user, the model wants to
            # end its turn by pointing the user to an existing tool result.
            # Set the comment as final content and break the loop immediately
            # — no further LLM calls needed.
            #
            # ★ The referenced tool round is tagged with _emit_ref=True so the
            #   frontend can auto-expand it (making the tool output visible
            #   without requiring the user to click "Preview").
            _emit_detected = False
            for _ptc in parsed_tcs:
                _ptc_tc, _ptc_fn, _ptc_id, _ptc_args, _ptc_rn, _ptc_re, _ptc_pe = _ptc
                if _ptc_re and _ptc_re.get('_emit_to_user'):
                    _emit_comment = _ptc_re.get('_emit_comment', '')
                    _emit_round = _ptc_re.get('_emit_tool_round', '?')

                    # ★ Tag the referenced tool round so the frontend
                    #   auto-expands it — the user should see the output
                    #   without having to click "Preview".
                    if isinstance(_emit_round, int):
                        for _sr in task.get('searchRounds', []):
                            if _sr.get('roundNum') == _emit_round:
                                _sr['_emit_ref'] = True
                                # Notify frontend to tag its copy too
                                append_event(task, {
                                    'type': 'emit_ref',
                                    'roundNum': _emit_round,
                                })
                                break

                    with task['content_lock']:
                        task['content'] = _emit_comment
                    append_event(task, {'type': 'delta', 'content': _emit_comment})
                    last_finish_reason = 'stop'
                    _loop_exit_reason = f'emit_to_user_round_{_emit_round}'
                    _emit_detected = True
                    logger.info(
                        '[%s] conv=%s emit_to_user: referencing tool_round=%s, '
                        'comment=%d chars — breaking loop (no further LLM calls)',
                        tid, task.get('convId', ''), _emit_round, len(_emit_comment))
                    break
            if _emit_detected:
                break

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
                    task['error'] = (
                        f'⚠️ Task stopped: {_consecutive_tool_timeouts} consecutive tool execution timeouts. '
                        f'The tool keeps timing out — try a simpler approach or increase the timeout.'
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



        # ── Append final assistant reply to messages if it wasn't already ──
        # When the LLM returns text content WITHOUT tool_calls, the loop
        # breaks before appending the assistant message (tool_calls path at
        # line ~698 is the only place messages.append(clean_msg) happens).
        # Without this, _run_single_turn returns messages missing the
        # assistant's reply, and endpoint mode's critic never sees the
        # worker's output.
        if assistant_msg and not assistant_msg.get('tool_calls'):
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

        # ── Write back updated messages to task so callers (e.g.
        #    _run_single_turn → endpoint.py) can access the complete
        #    conversation including assistant replies and tool results.
        #    Without this, task['messages'] still holds the PRE-run_task
        #    snapshot, and endpoint mode's critic never sees the worker's output.
        task['messages'] = messages

        # ★ Session memory extraction: background thread extracts
        #   persistent session notes after tool-heavy turns.
        #   Inspired by Claude Code's SessionMemory system.
        if tool_call_happened and task.get('convId'):
            trigger_memory_extraction(
                task['convId'], messages,
                tool_call_happened=tool_call_happened,
            )

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
    except Exception as e:
        logger.error('[Orchestrator] run_task FATAL error task=%s', task.get('id', '?')[:8], exc_info=True)
        task['error'] = str(e); task['status'] = 'error'; task['finishReason'] = 'error'
        if task.get('_endpoint_managed'):
            return   # let endpoint.py handle the error
        append_event(task, {'type': 'done', 'error': str(e), 'finishReason': 'error'})
        persist_task_result(task)


# ══════════════════════════════════════════════════════════
#  _run_single_turn — reusable building block for endpoint mode
# ══════════════════════════════════════════════════════════

def _run_single_turn(
    task: dict[str, Any],
    messages_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute ONE full work turn (LLM + tool loop) and return the results.

    This wrapper:
    1. Resets per-turn accumulation fields (content, thinking, usage, etc.)
    2. Optionally replaces the messages list
    3. Delegates to the full ``run_task`` machinery
    4. Returns dict with keys: content, thinking, usage, finishReason, messages, error

    **Note:** This mutates ``task`` in place (content, thinking, status, etc.).
    It does NOT emit 'done' events — the caller (endpoint.py) decides when the
    overall session is done.

    Parameters
    ----------
    task : dict
        The live task dict (from ``create_task``).  Must already be in ``tasks``.
    messages_override : list | None
        If provided, replaces ``task['messages']`` before calling.

    Returns
    -------
    dict  with keys: content, thinking, usage, finishReason, messages, error
    """
    if 'id' not in task:
        raise ValueError("_run_single_turn called with a task dict missing 'id' — did you forget to use create_task()?")
    tid = task['id'][:8]
    logger.debug('[Endpoint] _run_single_turn %s ENTRY — messages_override=%s',
                 tid, 'yes' if messages_override is not None else 'no')

    # Override messages if supplied
    if messages_override is not None:
        task['messages'] = list(messages_override)

    # Reset per-turn accumulation fields so run_task starts clean
    with task['content_lock']:
        task['content']  = ''
        task['thinking'] = ''
    task['usage']        = {}
    task['status']       = 'running'
    task['error']        = None
    task['finishReason'] = None
    task['searchRounds'] = []    # fresh tool rounds per turn

    # Flag to tell run_task NOT to emit final 'done' event
    task['_endpoint_managed'] = True

    try:
        run_task(task)
    finally:
        task.pop('_endpoint_managed', None)

    result = {
        'content':      task.get('content', ''),
        'thinking':     task.get('thinking', ''),
        'usage':        task.get('usage', {}),
        'finishReason': task.get('finishReason', 'stop'),
        'messages':     list(task.get('messages', [])),
        'error':        task.get('error'),
    }
    # ★ Propagate fallback info so endpoint mode can surface it to the frontend
    if task.get('_fallback_model'):
        result['fallbackModel'] = task['_fallback_model']
        result['fallbackFrom']  = task.get('_fallback_from', '')

    logger.debug('[Endpoint] _run_single_turn %s → %d chars, finish=%s',
                 tid, len(result['content']), result['finishReason'])
    return result
