# HOT_PATH — functions in this module are called per-request.
# Prefer logger.debug() over logger.info(). logger.info() is reserved
# for rare, high-signal events (e.g. content-filter injection, per-round diagnostics).
"""Task orchestrator — main run_task loop coordinating LLM calls and tool execution.

Also exposes ``_run_single_turn()`` — a reusable primitive that executes one
full LLM-tool cycle (setup → tool loop → finalization) on an existing task
dict.  ``endpoint.py`` uses it to drive the outer work→review→revise loop.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from lib.log import get_logger, set_req_id
from lib.protocols import BodyBuilder

logger = get_logger(__name__)

from lib.llm import build_body as _build_body_impl

build_body: BodyBuilder = _build_body_impl  # type: explicit protocol binding
from lib.llm import AbortedError
from lib.tasks_pkg.attachments import compute_turn_attachments, inject_attachments
from lib.tasks_pkg.cache_tracking import (
    cleanup_stale_cache_states,
    detect_cache_break,
    get_session_cache_stats,
    log_round_cache_stats,
    release_ttl_latch,
    sort_tool_results,
)
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.compaction import run_compaction_pipeline
from lib.tasks_pkg.executor import (
    _finalize_tool_round,
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
from lib.tasks_pkg.commit_round import (  # noqa: E402
    _run_commit_round_async,  # noqa: F401  (re-export for back-comp)
    _spawn_async_commit_round,
    _spawn_async_profile_consolidation,
    derive_round_modified_files,
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
    tool_label,
)


# ── Inter-round narration discard ──────────────────────────────────────────
def _discard_pretool_prose(task: dict[str, Any], round_num: int) -> None:
    """Drop the prose an LLM round streamed BEFORE issuing tool calls.

    The chat loop accumulates every content delta into a single
    ``task['content']`` via ``_on_content``. When a round ends in TOOL CALLS
    (not a final answer), any text it streamed first is inter-round narration
    ("Now let me check the utility functions.") — NOT the deliverable. It is
    already preserved elsewhere: in the tool-call message's ``content``
    (replayed to the API next round) and snapshotted onto the tool round's
    ``assistantContent`` (UI / Continue replay). If we leave it in
    ``task['content']`` it gets concatenated in front of the terminal round's
    real answer, leaking scaffolding into the deliverable.

    Two things are required (mirrors the paper-engine ``delta_reset``
    precedent):

    1. **Backend**: clear ``task['content']`` / ``task['thinking']`` so the
       final assembled answer contains ONLY the terminal round's text.
    2. **Client**: the DELTA frames for this prose were ALREADY streamed and
       mirrored into the live bubble, so a backend-only reset would leave the
       narration on screen (and a racing sync could persist it). Emit
       ``DELTA_RESET`` so the client clears accumulated content/thinking —
       but KEEPS this turn's tool rounds (unlike ``retry_reset``).

    Continue's ``contentPrefix`` path re-seeds ``task['content']`` explicitly
    (and later), so this reset is safe there too.
    """
    with task['content_lock']:
        task['content'] = ''
        task['thinking'] = ''
    append_event(task, build_event(EventType.DELTA_RESET, round=round_num))


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
        append_event(task, build_event(EventType.PHASE, phase='llm_thinking', detail='Generating response…', round=1))
    else:
        tool_names = [tc['function']['name'] for tc in assistant_msg.get('tool_calls', [])]
        unique_names = list(dict.fromkeys(tool_names))
        labeled = [tool_label(n) for n in unique_names]
        summary = ', '.join(labeled)
        append_event(task, build_event(
            EventType.PHASE, phase='llm_thinking',
            detail=f'Analyzing results and planning next step… (round {round_num+1})',
            toolContext=summary,
            round=round_num + 1,
        ))


# Prompt-cache `write` decomposition lives in lib.tasks_pkg.write_breakdown
# (extracted 2026-06 — a self-contained pure computation). Re-exported here
# for backward compatibility; the sole call site is run_task below.
from lib.tasks_pkg.write_breakdown import (  # noqa: E402
    _compute_write_breakdown,  # noqa: F401
    _ENVELOPE_MAX_TOKENS,  # noqa: F401  (re-export for back-compat)
    _READ_DROP_WASTE_TOKENS,  # noqa: F401  (re-export for back-compat)
)


def _finalize_dangling_tool_rounds(task: dict[str, Any]) -> int:
    """Finalize any tool round left in a non-terminal state at task end.

    The abort short-circuits in ``_execute_tool_one`` (executor.py top) and
    the streaming executor (its abort early-return + cancelled pending
    futures) return WITHOUT calling ``_finalize_tool_round``, so a round that
    was announced via ``tool_start`` (``status='searching'``) but never
    executed keeps ``status='searching'`` with empty ``results``. The frontend
    renders that as a permanent "Running…" spinner — live AND after reload,
    because the stale round is persisted verbatim into ``conversations.messages``.

    This sweep — run once at task termination — is the single source of truth:
    for every dangling round it stamps a terminal ``aborted`` status and emits a
    ``tool_result``-class event (via ``_finalize_tool_round``) so the live
    stream and the persisted/reloaded DB state agree.

    A round is considered dangling when its ``status`` is not one of the known
    terminal/interactive-resolved states AND it has no ``results``. Returns the
    number of rounds finalized.
    """
    rounds = task.get('toolRounds') or []
    if not rounds:
        return 0

    # States that are already settled (terminal) or are legitimately waiting
    # on an external actor and resolved elsewhere — never sweep these.
    _settled = {
        'done', 'error', 'aborted', 'rejected', 'skipped',
        'awaiting_human', 'awaiting_stdin', 'awaiting_client_tool',
        'submitted', 'pending_approval',
    }
    tid = (task.get('id', '?') or '?')[:8]
    finalized = 0
    for entry in rounds:
        if not isinstance(entry, dict):
            continue
        status = entry.get('status')
        if status in _settled:
            continue
        if entry.get('results'):
            # Has results but a non-terminal status (rare) — normalize the
            # status to 'done' so it doesn't render as running, but don't
            # fabricate an error meta over real results.
            entry['status'] = 'done'
            continue
        rn = entry.get('roundNum') or (rounds.index(entry) + 1)
        tool_name = entry.get('toolName') or 'tool'
        query = entry.get('query') or tool_name
        meta = {
            'toolName': tool_name,
            'title': query,
            'snippet': 'Interrupted — stopped before completion.',
            'source': 'Interrupted',
            'fetched': False,
            'fetchedChars': 0,
            'badge': 'interrupted',
            'interrupted': True,
        }
        try:
            _finalize_tool_round(task, rn, entry, [meta], query_override=query)
            # _finalize_tool_round sets status='done'; downgrade to the more
            # accurate 'aborted' so the renderer shows the interrupted state.
            entry['status'] = 'aborted'
            finalized += 1
            logger.info('[%s] Finalized dangling tool round %s (tool=%s status=%s→aborted) '
                        'at task end — was left "searching" by abort short-circuit',
                        tid, rn, tool_name, status)
        except Exception as e:
            # Best-effort: a failed finalize must not block task termination.
            # Still stamp the status so the round doesn't render as running.
            entry['status'] = 'aborted'
            logger.warning('[%s] _finalize_tool_round failed for dangling round %s '
                           '(tool=%s): %s — status stamped aborted anyway',
                           tid, rn, tool_name, e, exc_info=True)
            finalized += 1
    if finalized:
        logger.info('[%s] Dangling-tool-round sweep finalized %d round(s) at task end',
                    tid, finalized)
    return finalized


def _maybe_auto_retry_turn(task: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Auto-re-run a settled-but-transiently-failed turn; return True if retrying.

    Reads the typed error envelope on ``task['error']`` and asks
    :func:`lib.tasks_pkg.turn_retry.should_auto_retry_turn` whether a whole-turn
    re-run is worthwhile.  When it is, this:

      1. emits ``retry_reset`` so the client clears the failed attempt's partial
         bubble (deltas append client-side — without this the re-streamed
         output would stack on the old text);
      2. emits ``phase:retrying`` with the attempt/backoff detail (the same
         transient-status contract the inner stream-retry path uses);
      3. sleeps the backoff (interruptible by user abort);
      4. resets the per-turn task accumulators to a clean 'running' state
         (mirroring ``_run_single_turn``) while KEEPING ``task['messages']`` —
         which already holds every completed tool round as history, so the
         re-run resumes after them and never double-executes a tool;
      5. re-invokes :func:`run_task`.

    Returns True iff a retry was launched (caller must ``return`` without
    finalizing).  Returns False when the error is not auto-retryable, the
    budget is exhausted, the caller opted out, or the user aborted during the
    backoff — in which case the caller finalizes and surfaces the error as
    usual, so manual Retry still works.
    """
    from lib.tasks_pkg.turn_retry import (
        auto_turn_retry_max,
        should_auto_retry_turn,
    )

    tid = task['id'][:8]
    err = task.get('error')
    attempt = int(task.get('_auto_turn_retry_count') or 0)
    retry, backoff_s = should_auto_retry_turn(err, attempt, cfg)
    if not retry:
        return False

    _kind = err.get('kind', '?') if isinstance(err, dict) else '?'
    _cap = auto_turn_retry_max(cfg)
    _next = attempt + 1

    from lib.log import audit_log
    audit_log('turn_auto_retry', tid=tid, conv=task.get('convId', ''),
              kind=_kind, attempt=_next, max=_cap,
              backoff_s=round(backoff_s, 2),
              model=task.get('model', '') or (cfg.get('model', '')))
    logger.warning(
        '[%s] ⟳ TURN AUTO-RETRY (%d/%d): transient error kind=%s — re-running '
        'the whole turn after %.1fs backoff (transparent, no user action). '
        'conv=%s',
        tid, _next, _cap, _kind, backoff_s, task.get('convId', ''))

    # ── (1) Tell the client to clear the failed attempt's partial bubble ──
    #     retry_reset is non-terminal; the task stays 'running'.
    _detail = (f'⚠️ 请求失败（{_kind}），正在自动重试整轮 '
               f'({_next}/{_cap}）… / Transient error — auto-retrying the turn')
    try:
        append_event(task, build_event(
            EventType.RETRY_RESET, attempt=_next, max=_cap, kind=_kind))
        # (2) transient status bar (same contract as inner stream retries)
        append_event(task, build_event(
            EventType.PHASE, phase='retrying', detail=_detail,
            attempt=_next, max=_cap, bucket='turn'))
    except Exception as _ev_err:
        logger.debug('[%s] auto-retry event emit failed (non-fatal): %s',
                     tid, _ev_err)

    # ── (3) Backoff (abort-aware) ──
    if backoff_s > 0:
        from lib.tasks_pkg.stream_handler import _interruptible_sleep
        _interruptible_sleep(backoff_s, task)
    if task.get('aborted'):
        # User hit Stop during the backoff — do NOT re-run; let the caller
        # finalize (it will render as aborted).
        logger.info('[%s] auto-retry aborted during backoff — finalizing', tid)
        return False

    # ── (4) Reset per-turn accumulators to a clean running state ──
    #     Restore the PRISTINE turn input (run_task mutated task['messages']
    #     with injected system context + the failed attempt's partial rounds
    #     on write-back; re-running from that would double-inject and replay a
    #     half-finished round). Re-running from the original input is exactly
    #     the semantics of a manual Retry. Keep the retry counter (bumped
    #     below). Mirrors _run_single_turn's reset otherwise.
    _pristine = task.get('_turn_input_messages')
    if _pristine is not None:
        task['messages'] = list(_pristine)
    task['_auto_turn_retry_count'] = _next
    with task['content_lock']:
        task['content'] = ''
        task['thinking'] = ''
    task['usage'] = {}
    task['status'] = 'running'
    task['error'] = None
    task['finishReason'] = None
    task['toolRounds'] = []
    # Clear per-phase inner-retry counters so the re-run starts with a fresh
    # inner budget (the stream-anomaly retries are per-attempt, not lifetime).
    task.pop('_premature_retry_count_phase', None)
    task.pop('_force_rotate_pair', None)

    # ── (5) Re-run the whole turn ──
    try:
        run_task(task)
    except Exception as _rerun_err:
        # A re-run that raises lands here (the recursive run_task's own FATAL
        # handler already emitted a done+error for it in most cases, but a
        # raise that escapes must not crash this frame). Surface a generic
        # error so the turn still terminates cleanly.
        logger.error('[%s] auto-retry re-run raised: %s', tid, _rerun_err,
                     exc_info=True)
        from lib.error_envelope import make_envelope as _make_env
        task['error'] = _make_env(
            'internal',
            detail=f'Auto-retry re-run failed: {_rerun_err}',
            model=task.get('model', ''),
            context='turn-auto-retry',
            source='orchestrator',
            raw=str(_rerun_err),
        )
        task['status'] = 'error'
        task['finishReason'] = 'error'
        try:
            append_event(task, build_event(
                EventType.DONE, error=task['error'], finishReason='error'))
            persist_task_result(task)
        except Exception as _fin_err:
            logger.error('[%s] auto-retry terminal finalize failed: %s',
                         tid, _fin_err, exc_info=True)
    return True


_SRC_URL_RE = re.compile(r'https?://[^\s<>\]\)"`）】]+')


def _maybe_append_sources_footer(task: dict[str, Any], all_search_results_text: list[str]) -> None:
    """Deterministic guard: if the model actually consulted web pages this turn
    (``all_search_results_text`` non-empty) but its final answer cites NONE of
    the URLs it opened, append a compact "来源 / Sources" footer listing the
    deduped URLs it actually retrieved.

    Mechanism-first backstop for the system-prompt citation nudge: a
    non-compliant model can ignore the prompt, but this footer can't be
    ignored — it directly closes the 有据性=0 gap on web-research turns.

    Rules (never fabricate): only URLs that literally appeared in the fetched
    search-result text this turn; http(s) only; deduped preserving order;
    capped at 5. No-op when the answer already contains ≥1 opened URL, when no
    web results exist, or when the answer is empty/aborted.
    """
    content = task.get('content') or ''
    if not content.strip() or task.get('aborted'):
        return
    if not all_search_results_text:
        return
    # URLs the model actually saw (format.py emits "URL: <url>" per result).
    seen = _SRC_URL_RE.findall('\n'.join(all_search_results_text))
    if not seen:
        return

    def _norm(u: str) -> str:
        return u.split('#')[0].rstrip('/.,);：、')

    opened, order = set(), []
    for u in seen:
        n = _norm(u)
        if n and n not in opened:
            opened.add(n)
            order.append(n)
    # Already cited at least one opened source? Then respect the model's choice.
    if any(n in content for n in opened):
        return
    footer_urls = order[:5]
    if not footer_urls:
        return
    footer = "\n\n---\n**来源 / Sources**（本轮检索所用，供核对）：\n" + \
             "\n".join(f"- {u}" for u in footer_urls)
    task['content'] = content + footer
    logger.info('[%s] appended Sources footer (%d urls) — answer cited none of the '
                'pages it opened this turn', task['id'][:8], len(footer_urls))


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

    # ── Turn-level auto-retry over TRANSIENT terminal errors ──
    # Before finalizing a failed turn, check whether the settled error is a
    # transient transport/dispatch failure worth transparently re-running the
    # WHOLE turn for (429 / no_slot / timeout / network / premature_close /
    # abnormal_stop / server_offline / tool_timeout).  If so — and the budget
    # is not exhausted and the caller didn't opt out — reset per-turn state,
    # tell the client to clear the partial bubble (retry_reset), back off, and
    # re-run run_task.  This spares the user from manually clicking Retry on
    # each of many parallel conversations that hit a passing gateway blip.
    # Endpoint-managed turns are excluded (the Planner→Worker→Critic loop owns
    # its own retry/replan semantics); the raise/FATAL path is also excluded
    # because it never reaches finalize.
    if not task.get('_endpoint_managed') and not task.get('aborted'):
        if _maybe_auto_retry_turn(task, cfg):
            return

    # ── Fallback: synthesize answer from search results if main loop produced nothing ──
    if not task['content'].strip() and tool_call_happened and all_search_results_text and not task['aborted']:
        combined = '\n\n---\n\n'.join(all_search_results_text)
        fb = list(original_messages)
        fb.append({'role':'assistant','content':"I've gathered the information. Let me analyze it."})
        fb.append({'role':'user','content':f'Here are fetched contents:\n\n{combined}\n\nProvide a comprehensive answer. Cite sources.'})
        try:
            snapshot = _strip_base64_for_snapshot(fb)
            append_event(task, build_event(EventType.MESSAGES_SNAPSHOT, round='fallback', label=f'Fallback · {len(fb)}条', messages=snapshot))
        except Exception as e:
            logger.warning('[Task %s] messages_snapshot fallback failed, model=%s: %s', tid, model, e, exc_info=True)
        body = build_body(
            model, fb,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking_enabled=thinking_enabled,
            preset=preset,
            thinking_depth=thinking_depth,
            response_format=cfg.get('responseFormat'),
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
                from lib.tasks_pkg.llm_fallback import _emit_round_usage
                _emit_round_usage(task, 'fallback', model, usg, tag='FALLBACK')
        except Exception as e:
            logger.error('[%s] ⚠️ Post-loop fallback failed: %s', tid, e, exc_info=True)
            try:
                from lib.llm_error_format import format_llm_error_for_user
                task['error'] = format_llm_error_for_user(
                    e, model=model, context='post-loop-fallback',
                    source='orchestrator')
            except Exception as _fmt_err:
                logger.warning('[%s] format_llm_error_for_user failed: %s', tid, _fmt_err)
                from lib.error_envelope import make_envelope as _make_env
                task['error'] = _make_env(
                    'internal',
                    detail=f'Post-loop fallback failed: {e}',
                    model=model,
                    context='post-loop-fallback',
                    source='orchestrator',
                    raw=str(e),
                )

    # ── Content-filter: give user a meaningful error instead of blank bubble ──
    if (not task['content'].strip()
            and not task['aborted']
            and (last_finish_reason == 'content_filter'
                 or (_loop_exit_reason and 'content_filter' in str(_loop_exit_reason).lower()))):
        task['content'] = '⚠️ 该回复被模型安全过滤器拦截，请尝试换一种方式提问。\n\n_The response was blocked by the model\'s safety filter. Please try rephrasing your question._'
        logger.info('[%s] Injected content_filter user-facing message (finish_reason=%s, loop_exit=%s)',
                    tid, last_finish_reason, _loop_exit_reason)

    # ── Deterministic source-citation backstop (web-research turns) ──
    # If the model consulted web pages but cited none of them, append a compact
    # Sources footer of the URLs it actually opened. Pairs with the system-prompt
    # citation nudge (section_tone_and_style web_tools path) as the robust half.
    try:
        _maybe_append_sources_footer(task, all_search_results_text)
    except Exception as _src_e:  # never let the backstop break finalization
        logger.warning('[%s] sources-footer backstop failed: %s', tid, _src_e)

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
        from lib.error_envelope import make_envelope as _make_env
        task['error'] = _make_env(
            'internal',
            detail='Model requested tool calls but the loop ended unexpectedly.',
            model=model,
            context='post-loop',
            source='orchestrator',
            raw='finish_reason=%s but loop exited without further tool execution' % last_finish_reason,
        )

    task['finishReason'] = last_finish_reason
    task['usage'] = accumulated_usage if accumulated_usage else last_usage
    task['preset'] = cfg.get('preset') or cfg.get('effort', 'medium')

    # ── Finalize any tool round left "searching" by an abort short-circuit ──
    #   The executor / streaming-executor abort early-returns (and cancelled
    #   pending futures) skip _finalize_tool_round, leaving the round in
    #   'searching' with no terminal tool_result → permanent "Running…" in the
    #   UI, live and after reload. Sweep them once here so the live stream and
    #   the persisted DB state agree. Runs on EVERY exit (not just abort): a
    #   cancelled future can dangle on a normal-error exit too.
    try:
        _finalize_dangling_tool_rounds(task)
    except Exception as _sweep_err:
        logger.warning('[%s] dangling-tool-round sweep failed (non-fatal): %s',
                       tid, _sweep_err, exc_info=True)

    # ── Fold in compaction's OWN LLM usage ──
    # L2 smart-summary and the advanced-host summarizers (OpenCode/Hermes/
    # OpenClaw arms) call the LLM but historically discarded that usage, so
    # task['usage'] (→ reported cost) under-counted exactly the summary-based
    # strategies. Drain the per-conv accumulator and add it in, while also
    # exposing it separately so a cost breakdown can show compaction overhead.
    try:
        from lib.tasks_pkg.compaction._compaction_usage import pop_compaction_usage
        _comp_usage = pop_compaction_usage(task.get('convId', ''))
        if _comp_usage:
            task['compactionUsage'] = _comp_usage
            _u = task['usage'] or {}
            for _k, _v in _comp_usage.items():
                if _k == 'n_calls':
                    continue
                if isinstance(_v, (int, float)) and isinstance(_u.get(_k), (int, float)):
                    _u[_k] = _u[_k] + _v
                elif isinstance(_v, (int, float)) and _k not in _u:
                    _u[_k] = _v
            task['usage'] = _u
            logger.info('[Usage] conv=%s folded compaction usage (%d calls) into total: %s',
                        (task.get('convId') or '')[:8], _comp_usage.get('n_calls', 0),
                        {k: v for k, v in _comp_usage.items() if k != 'n_calls'})
    except Exception as _cu_e:
        logger.debug('[Usage] compaction-usage fold failed: %s', _cu_e)

    # ── Generate tool summary for cross-turn context (non-blocking) ──
    if tool_call_happened and not task['aborted']:
        try:
            summary = _generate_tool_summary(messages, model, task)
            if summary:
                task['toolSummary'] = summary
        except Exception as e:
            logger.warning('[Task %s] Tool summary generation failed model=%s (non-fatal): %s', task['id'][:8], model, e, exc_info=True)

    if not task.get('_endpoint_managed'):
        # Latch the autopilot decision window BEFORE flipping status to 'done'.
        # The status flip makes _task_terminal() true for the SSE generator and
        # chat_poll; setting the marker first closes the gap where they'd
        # observe 'done' before the autopilot hook (which can take several
        # seconds for the VU LLM call) has a chance to set it — otherwise a
        # late synthetic done closes the stream without the follow-up baton.
        try:
            from lib.tasks_pkg.autopilot import is_autopilot_enabled
            if is_autopilot_enabled(task):
                task['_autopilot_deciding'] = True
        except Exception as _ap_latch_err:
            logger.debug('[Autopilot] pre-flip decision latch skipped: %s',
                         _ap_latch_err)
        task['status'] = 'done'

    # ── Project-brain Activity Feed: 'completed' / 'aborted' pulse ──
    #   Emitted at the terminal seam, EXCEPT for autopilot follow-up turns
    #   (config.autopilotRunId set) — those collapse to one 'run_concluded'
    #   event at run close-out, mirroring the 'started' suppression in
    #   create_task. Best-effort: a feed failure must NEVER break finalization.
    try:
        _cfg_feed = task.get('config') or {}
        _proj_feed = (project_path or '').strip() if project_enabled else ''
        if (_proj_feed and task.get('convId')
                and not (_cfg_feed.get('autopilotRunId') or '').strip()):
            from lib.agent_core.activity import emit_activity_event
            _kind_feed = 'aborted' if task.get('aborted') else 'completed'
            emit_activity_event(
                _proj_feed, task['convId'], _kind_feed,
                (task.get('lastUserQuery') or '').strip() or ('Turn ' + _kind_feed),
                task_id=task['id'])
    except Exception as _feed_e:
        logger.debug('[%s] project-feed terminal emit skipped: %s', tid, _feed_e)

    # ── Cleanup reactive compact tracking (prevent memory leak) ──
    from lib.tasks_pkg.llm_fallback import cleanup_reactive_compact_state
    cleanup_reactive_compact_state(task.get('id', ''))

    # ── Release session-stable TTL latch (prevent memory leak) ──
    release_ttl_latch(task.get('id', ''))

    # ── Swarm session teardown (Option A — conversation-scoped) ──
    #
    # A swarm now outlives the single turn that spawned it: its lifetime is
    # bounded by the CONVERSATION, not this task. So on a NORMAL turn end we
    # must NOT abort a swarm whose agents are still running — the user's
    # background work would be discarded (the exact bug this fixes). We only
    # tear down when:
    #   (a) the user explicitly aborted this task (Stop button), OR
    #   (b) the swarm has already terminated on its own.
    # Otherwise we DETACH: leave the live session + its inbox intact so the
    # next turn in this conversation drains pending <swarm-update>s and can
    # await / fetch results. TTL eviction (conv-aware ``_key_is_live``)
    # reaps it only once the conversation goes quiet.
    try:
        from lib.agent_inbox import clear as _clear_inbox
        from lib.swarm.integration import _remove_session as _remove_swarm_session
        from lib.swarm.integration import get_active_session as _get_swarm_session
        from lib.swarm.integration import swarm_key_for as _swarm_key_for
        _swarm_key = _swarm_key_for(task)
        _swarm_sess = _get_swarm_session(_swarm_key)
        _user_aborted = bool(task.get('aborted'))
        if _swarm_sess is not None and (_user_aborted or _swarm_sess.is_terminated):
            try:
                _swarm_sess.abort()
            except Exception as _e:
                logger.debug('[Orchestrator] swarm abort on task end: %s', _e)
            _remove_swarm_session(_swarm_key)
            _clear_inbox(_swarm_key)
            logger.info('[Orchestrator] swarm torn down on task end '
                        '(key=%s reason=%s)', _swarm_key,
                        'user_abort' if _user_aborted else 'terminated')
        elif _swarm_sess is not None:
            logger.info('[Orchestrator] swarm DETACHED on normal turn end — '
                        'still running, will deliver on later turns (key=%s)',
                        _swarm_key)
    except Exception as _e:
        logger.warning('[Orchestrator] swarm/inbox cleanup on task end failed: %s', _e, exc_info=True)

    # ── Log session-level aggregate cache stats ──
    _conv_id = task.get('convId', '')
    if _conv_id:
        _session_stats = get_session_cache_stats(_conv_id)
        if _session_stats and _session_stats['calls'] > 1:
            logger.info(
                '[CacheSession] %s conv=%s END — %d calls, '
                'total_read=%d total_write=%d overall_hit=%d%% '
                'breaks=%d duration=%.1fs model=%s',
                tid, _conv_id[:8],
                _session_stats['calls'],
                _session_stats['total_cache_read'],
                _session_stats['total_cache_write'],
                _session_stats['overall_hit_pct'],
                _session_stats['total_breaks'],
                _session_stats['session_duration_s'],
                _session_stats['model'],
            )

    # ── Periodic stale cache state cleanup (every task completion) ──
    # Lightweight: only scans and removes entries older than 1 hour.
    try:
        cleanup_stale_cache_states(max_age_s=3600)
    except Exception as e:
        logger.debug('[Orchestrator] stale cache cleanup failed: %s', e)

    # ── Tool dedup cache stats (logged at task completion) ──
    _dedup_cache = task.get('_tool_result_cache')
    if _dedup_cache:
        _dedup_size = len(_dedup_cache)
        if _dedup_size > 0:
            logger.info(
                '[DedupCache] %s conv=%s task END — %d cached entries',
                tid, _conv_id[:8] if _conv_id else '???', _dedup_size)

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
    done_evt = build_event(EventType.DONE)
    # ★ Always expose the task ID (the whole user→assistant turn, across ALL
    #   tool rounds). The frontend shows it in the cost popover so the user
    #   can quote ONE id back to us for root-cause analysis — and it's the
    #   key every [Task:id] log line is tagged with. Previously taskId was
    #   only set inside the project-modifications block below, so chat-only
    #   turns (no file changes) never received it.
    done_evt['taskId'] = task['id']
    if last_finish_reason: done_evt['finishReason'] = last_finish_reason
    final_usage = accumulated_usage if accumulated_usage else last_usage
    if final_usage: done_evt['usage'] = final_usage
    if task.get('preset'): done_evt['preset'] = task['preset']
    done_evt['model'] = model
    task['model'] = model
    if thinking_depth:
        done_evt['thinkingDepth'] = thinking_depth
        task['thinkingDepth'] = thinking_depth
    if task.get('error'): done_evt['error'] = task['error']
    if task.get('toolSummary'): done_evt['toolSummary'] = task['toolSummary']
    # Tool-schema latch: a mid-conversation tool toggle was held back to keep
    # the prompt cache intact. Tell the frontend so it can offer "Apply now".
    if cfg.get('_toolsetDiverged'):
        done_evt['toolsetDiverged'] = True
        _ts_diff = cfg.get('_toolsetDiff')
        if _ts_diff and (_ts_diff.get('added') or _ts_diff.get('removed')):
            done_evt['toolsetDiff'] = _ts_diff
    if api_rounds:
        done_evt['apiRounds'] = api_rounds
        task['apiRounds'] = api_rounds
    if task.get('_fallback_model'):
        done_evt['fallbackModel'] = task['_fallback_model']
        done_evt['fallbackFrom'] = task.get('_fallback_from', '')
        if task.get('_fallback_reason'):
            done_evt['fallbackReason'] = task['_fallback_reason']
        if task.get('_fallback_kind'):
            done_evt['fallbackKind'] = task['_fallback_kind']
    if project_enabled and task['convId']:
        try:
            # Authoritative source of truth: this round's OWN journalled
            # writes, aggregated across EVERY workspace root the task may
            # have touched (primary + extras).  See
            # ``derive_round_modified_files`` for why scanning the primary
            # alone leaked a concurrent conversation's edit.
            file_list, _n_mods, _used_ts_fallback = derive_round_modified_files(
                task, project_path, cfg.get('projectPaths'))
            if file_list:
                done_evt['modifiedFiles'] = _n_mods
                task['modifiedFiles'] = _n_mods
                # ★ Include taskId so frontend can do per-round undo
                done_evt['taskId'] = task['id']
                done_evt['modifiedFileList'] = file_list
                task['modifiedFileList'] = file_list
                _n_roots = 1 + len([p for p in (cfg.get('projectPaths') or [])[1:]
                                    if p and p != project_path])
                if _n_roots > 1:
                    logger.info('[Task %s] modifiedFileList derived across %d roots: '
                                '%d file(s)%s', task['id'][:8], _n_roots,
                                len(file_list), ' (ts-fallback)' if _used_ts_fallback else '')
                # ── Presence: merge this turn's touched files into the peer
                #    and run notify-only overlap detection against other active
                #    peers on the same root. Best-effort.
                try:
                    from lib.presence import record_files as _presence_record
                    _presence_record(project_path, task['convId'], file_list)
                except Exception as _pe:
                    logger.debug('[Task %s] presence record_files failed: %s',
                                 task['id'][:8], _pe)
        except Exception as e:
            logger.warning('[Task %s] get_modifications failed for conv=%s model=%s: %s',
                      task['id'][:8], task.get('convId', ''), model, e, exc_info=True)
    # ── Continue checkpoint merging: merge pre-checkpoint metadata into
    #   both the done event and the task dict so that:
    #   (a) the frontend done handler sees merged data (even though it also
    #       merges client-side, this makes poll fallback consistent), and
    #   (b) _sync_result_to_conversation writes the full merged set to DB. ──
    _cp_usage = task.get('_checkpointUsage')
    if _cp_usage and done_evt.get('usage'):
        merged_usage = {}
        for k in set(list(_cp_usage.keys()) + list(done_evt['usage'].keys())):
            cv = _cp_usage.get(k)
            nv = done_evt['usage'].get(k)
            merged_usage[k] = (cv + nv) if isinstance(cv, (int, float)) and isinstance(nv, (int, float)) else (nv if nv is not None else cv)
        done_evt['usage'] = merged_usage
        task['usage'] = merged_usage
    elif _cp_usage and not done_evt.get('usage'):
        done_evt['usage'] = _cp_usage
        task['usage'] = _cp_usage

    _cp_api_rounds = task.get('_checkpointApiRounds')
    if _cp_api_rounds:
        merged_api = list(_cp_api_rounds) + (done_evt.get('apiRounds') or [])
        done_evt['apiRounds'] = merged_api
        task['apiRounds'] = merged_api

    _cp_mod_files = task.get('_checkpointModifiedFiles')
    if _cp_mod_files is not None and done_evt.get('modifiedFiles') is not None:
        done_evt['modifiedFiles'] = _cp_mod_files + done_evt['modifiedFiles']
        task['modifiedFiles'] = done_evt['modifiedFiles']

    _cp_mod_list = task.get('_checkpointModifiedFileList')
    if _cp_mod_list:
        # Merge: old + new, dedup by (root, path) so same relative path in
        # different workspace roots stays distinct in multi-root setups.
        merged_map = {}
        def _key(f):
            if isinstance(f, dict):
                return (f.get('root', '') or '', f.get('path', ''))
            return ('', str(f))
        for f in _cp_mod_list:
            merged_map[_key(f)] = f
        for f in (done_evt.get('modifiedFileList') or []):
            merged_map[_key(f)] = f
        merged_list = list(merged_map.values())
        done_evt['modifiedFileList'] = merged_list
        task['modifiedFileList'] = merged_list

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
    #
    # The file-history snapshot for this round runs in a daemon thread
    # AFTER ``persist_task_result`` so queue-dispatch is never blocked
    # by snapshot I/O.  When the snapshot completes we emit a separate
    # ``round_committed`` SSE event carrying ``snapshotId`` (and the
    # legacy ``gitSha`` field, kept for frontend backward-compat) plus
    # any side-channel ``modifiedFileList`` additions discovered by
    # ``diff_name_status``.
    if task.get('_endpoint_managed'):
        _spawn_async_commit_round(task, project_enabled, project_path)
        return
    # ── Producer B: scan the finalized assistant content for inline
    #    renderable artifacts (large fenced ```html / ```markdown blocks,
    #    bare <!doctype html> documents).  Best-effort — failures here
    #    must NOT block the done event or persistence.
    try:
        import lib as _lib_artifacts_gate
        if getattr(_lib_artifacts_gate, 'ARTIFACTS_ENABLED', True):
            from lib.artifacts import scan_message
            scan_message(
                task.get('convId') or '',
                task.get('content') or '',
                msg_id=task.get('_assistantMsgId') or '',
                task_id=task.get('id') or '',
                task=task,
            )
    except Exception as e:
        logger.debug('[Artifacts:scan] orchestrator hook failed (non-fatal): %s',
                     e, exc_info=True)

    # ── Autopilot hook (runs BEFORE the done event so its result can
    #    ride along on the same SSE message).  When autopilot is on and
    #    the VU produces a reply, this also writes the synthetic user
    #    message to the conversation DB and spawns the follow-up task.
    #    The frontend reads ``autopilotNextTaskId`` + ``autopilotVuMessage``
    #    from the done event and connects directly — no polling race.
    # ``task['status']`` was flipped to 'done' before this hook (see the
    # _run_loop tail), but the VU LLM call below can take several seconds.
    # Mark the autopilot decision as in-flight so chat_poll keeps reporting
    # 'running' until the baton exists — otherwise a poll landing in this
    # window would finalize the stream WITHOUT the follow-up handoff and
    # strand the already-spawned successor task.
    #
    # ── Commit the parent's FINAL assistant message to the conversation
    #    DB BEFORE running autopilot.  The autopilot hook appends the
    #    virtual-user turn AND spawns the follow-up task, which registers
    #    as the conversation's latest task and rebuilds its context from
    #    the DB.  The trailing persist_task_result → _sync_result_to_conversation
    #    would then be REJECTED by the freshness guard (superseded by the
    #    autopilot follow-up), freezing the parent reply at its last
    #    streaming checkpoint (truncated content, finishReason=None) and
    #    feeding that truncated copy to the follow-up.  Syncing here first
    #    makes the VU and follow-up layer on top of the complete reply; the
    #    later persist sync becomes a harmless no-op skip.
    # ── Phase 1 (parity-gap closure): commit the parent's FINAL assistant
    #    message to the conversation DB *before* the done event is emitted,
    #    for EVERY path — not only when autopilot is on.  Historically this
    #    sync ran only in the autopilot branch here; the non-autopilot path
    #    committed later via persist_task_result() AFTER append_event(done),
    #    so the terminal event a client received was NOT the committed record
    #    (it was a parallel reconstruction, and the DB row did not yet exist).
    #    `_sync_result_to_conversation` stamps `task['_committedMsg']` with the
    #    EXACT dict it wrote (re-SELECT-post-CAS), which the done event then
    #    ships verbatim below.  The trailing persist_task_result() sync becomes
    #    a harmless idempotent no-op (freshness/content guard).  Skip paths
    #    (freshness/inline/CAS-exhaustion) leave `_committedMsg` unset → no
    #    committedMessage rides the event → the client keeps its transient
    #    buffer (the Phase-2 offline fallback).
    if task.get('convId'):
        try:
            from lib.tasks_pkg.manager import (
                _sync_result_to_conversation,
                build_result_meta,
            )
            _sync_result_to_conversation(task, build_result_meta(task))
        except Exception as _pre_emit_err:
            logger.warning('[Task %s] pre-emit conv sync failed: %s — '
                           'terminal event will fall back to transient buffer',
                           tid, _pre_emit_err, exc_info=True)
    task['_autopilot_deciding'] = True
    try:
        from lib.tasks_pkg.autopilot import maybe_run_autopilot
        ap_result = maybe_run_autopilot(task)
        if ap_result:
            done_evt['autopilotNextTaskId'] = ap_result['next_task_id']
            done_evt['autopilotVuMessage'] = ap_result['vu_msg']
            # Stash on the task dict too so the baton is transport-agnostic:
            # the poll route surfaces the SAME handoff, so a client that fell
            # back to /api/chat/poll (SSE stripped / timed out) still attaches
            # to the follow-up instead of stranding it (sidebar dot / pause
            # button / translation desync until manual refresh).
            task['_autopilot_followup'] = ap_result
    except Exception as _ap_err:
        # On failure the deciding window is over and no baton will arrive —
        # clear the latch so _task_terminal() can finalize the stream.  The
        # SUCCESS path deliberately keeps the latch set until AFTER
        # append_event(done_evt) below, so the SSE generator never sees a
        # terminal task before the baton-carrying done event is buffered.
        task['_autopilot_deciding'] = False
        logger.warning('[Autopilot] hook raised: %s — continuing without '
                       'follow-up (this turn will still be persisted)',
                       _ap_err, exc_info=True)

    # ── Stamp cost snapshot on the done event ──
    # Mirrors the persisted-cost write in
    # lib.tasks_pkg.manager._sync_result_to_conversation: cost depends only
    # on usage + model + provider + the active pricing table, all of which
    # are final at this point. Sending it on the done event eliminates the
    # per-render `/api/v1/messages/cost` round-trips on the LIVE path —
    # the persisted-cost write covers reload paths.
    try:
        from lib.cost import compute_cost as _compute_cost
        if done_evt.get('usage'):
            _msg_cost = _compute_cost(
                done_evt['usage'],
                model_id=done_evt.get('model') or task.get('model') or '',
                provider_id=task.get('provider_id') or None,
            )
            if _msg_cost:
                done_evt['cost'] = _msg_cost
        for _rd in done_evt.get('apiRounds') or []:
            if not isinstance(_rd, dict) or _rd.get('cost'):
                continue
            _ru = _rd.get('usage') or {}
            if not _ru:
                continue
            _rc = _compute_cost(
                _ru,
                model_id=_rd.get('model') or done_evt.get('model') or '',
                provider_id=(_rd.get('provider_id')
                              or _rd.get('providerId')
                              or task.get('provider_id') or None),
            )
            if _rc:
                _rd['cost'] = _rc
    except Exception as _ce:
        logger.warning('[Cost] done-event stamp failed (non-fatal): %s', _ce)

    # ★ Comprehensive task-completion summary — keyed on the FULL task id so a
    #   user who quotes the id from the cost popover can grep ONE line that
    #   spans the whole turn (all tool rounds). Includes the per-round cache
    #   miss count so "why did cache break" is answerable straight from the
    #   log without re-deriving it. INFO level → lands in logs/app.log.
    try:
        _rounds = done_evt.get('apiRounds') or []
        _miss_rounds = [r.get('round') for r in _rounds
                        if isinstance(r, dict) and r.get('cacheBreak')]
        _u = done_evt.get('usage') or {}
        _cw = (_u.get('cache_write_tokens')
               or _u.get('cache_creation_input_tokens') or 0)
        _cr = (_u.get('cache_read_tokens')
               or _u.get('cache_read_input_tokens') or 0)
        _cost = (done_evt.get('cost') or {}).get('costCny')
        logger.info(
            '[Task:%s] ■ DONE conv=%s model=%s rounds=%d finish=%s '
            'cache_write=%d cache_read=%d cost=%s elapsed=%.1fs%s',
            task['id'], task.get('convId', '') or '-', model, len(_rounds),
            last_finish_reason or '-', _cw, _cr,
            (f'\u00a5{_cost:.3f}' if isinstance(_cost, (int, float)) else '?'),
            time.time() - task.get('created_at', time.time()),
            (f' \u26a0 CACHE_MISS rounds={_miss_rounds}' if _miss_rounds else ''),
        )
    except Exception as _se:
        logger.debug('[Task:%s] completion summary log failed: %s',
                     task['id'][:8], _se)

    # ── Phase 1: ship the EXACT committed conversation dict on the terminal
    #    event so the frontend can project the settled bubble verbatim
    #    (single source of truth — no keep-longer / snapshot reconstruction).
    #    `_committedMsg` was stamped by the pre-emit sync above with the row
    #    actually written (or the fresh row's authoritative tail on a genuine
    #    frontend-won race). Absent only on skip paths, where the client keeps
    #    its transient buffer.
    if task.get('_committedMsg'):
        done_evt['committedMessage'] = task['_committedMsg']

    append_event(task, done_evt)
    # The baton-carrying done event is now in task['events'] — only NOW is it
    # safe to let _task_terminal() (routes/chat.py) report the task finished.
    # Clearing this latch earlier (the old `finally`) opened a window where
    # status=='done' + _autopilot_deciding==False but the real done event was
    # not yet buffered, so the SSE generator synthesized a baton-LESS done from
    # extract_task_meta() and closed the stream → the spawned autopilot
    # follow-up was stranded and the conversation went idle until manual regen.
    task['_autopilot_deciding'] = False
    persist_task_result(task)

    _spawn_async_commit_round(task, project_enabled, project_path)

    # ★ Layer-3 preference consolidation — OFF the hot path.
    #   Mirrors _spawn_async_commit_round: runs AFTER the done event +
    #   persist, in a daemon thread, so the user sees the turn finish
    #   WITHOUT waiting on a cheap-LLM round-trip (most turns yield no
    #   preference change, and under rate-limiting the cheap call can stall
    #   for seconds). Any learned/staged preference is delivered as a
    #   post-done `preference_learned` event (best-effort live via the same
    #   SSE/push fan-out) + persisted to the conversation DB for reload.
    #   Gated on the Memory toggle inside the spawner (reads cfg), so a
    #   chat-only / memory-off turn spawns nothing.
    _spawn_async_profile_consolidation(task, messages, cfg)




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
                    # later "continue" turn of the same conversation. (Before
                    # Option A this used ``task['id']`` and cross-turn updates
                    # were stranded.) ``tid`` is just the 8-char log prefix.
                    _inbox_items = _drain_inbox(_swarm_key_for(task))
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
                            # ── Partition the drained items by mode ──
                            #   Peer messages (Pillar #6 fast-path lane) carry
                            #   mode='peer-msg' + a durable-row queueId; swarm
                            #   sub-agent results carry an agent_id. Both share
                            #   the ONE coalesced user message above, but their
                            #   de-dup + observability handling differ.
                            _peer_items = [it for it in _inbox_items
                                           if it.get('mode') == 'peer-msg'
                                           and it.get('value')]
                            _swarm_items = [it for it in _inbox_items
                                            if it.get('mode') != 'peer-msg'
                                            and it.get('value')]

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
                                append_event(task, build_event(
                                    EventType.SWARM_INBOX_INJECT,
                                    round=round_num + 1,
                                    count=len(_swarm_items),
                                    agentIds=[it.get('agent_id', '')
                                              for it in _swarm_items],
                                    # ★ Carry the actual <swarm-update> payloads
                                    #   (truncated) so the frontend can render an
                                    #   in-timeline ptool-panel row showing exactly
                                    #   what the model received — not just a count.
                                    previews=[{
                                        'agentId': it.get('agent_id', ''),
                                        'text': (it.get('value') or '')[:1200],
                                    } for it in _swarm_items],
                                ))

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

                            logger.info(
                                '[Task %s] injected %d inbox item(s) '
                                '(%d swarm, %d peer) as 1 user message at round %d',
                                tid, len(_payloads), len(_swarm_items),
                                len(_peer_items), round_num + 1)
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
                    round=round_num + 1,
                    label=f'Round {round_num + 1} 请求前 · {len(snapshot)}条',
                    messages=snapshot,
                )
                if _tools_this_round:
                    snap_evt['tools'] = _tools_this_round
                append_event(task, snap_evt)
            except Exception:
                logger.warning('[Task %s] messages_snapshot failed at round %d model=%s', tid, round_num + 1, model, exc_info=True)

            body = build_body(
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
                    try:
                        append_event(task, build_event(
                            EventType.PEER_INBOX_INJECT,
                            round=round_num + 1,
                            count=len(_peer_inject),
                            previews=[{
                                'fromConv': _pit.get('fromConv', ''),
                                'text': (_pit.get('peerText')
                                         or _pit.get('value') or '')[:1200],
                            } for _pit in _peer_inject],
                        ))
                    except Exception as _pce:
                        logger.warning('[Task %s] peer inject chip emit failed: %s',
                                       tid, _pce)
                    _conv_dd = task.get('convId', '') or ''
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
                        _wb = _compute_write_breakdown(task, api_rounds, round_num)
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
                break

            tool_call_happened = True
            clean_msg = {'role': 'assistant'}
            clean_msg['tool_calls'] = assistant_msg['tool_calls']
            if assistant_msg.get('content'): clean_msg['content'] = assistant_msg['content']
            if assistant_msg.get('reasoning_content'): clean_msg['reasoning_content'] = assistant_msg['reasoning_content']
            # ★ Carry the Claude thinking-block signature so the NEXT tool-loop
            #   turn replays a signed thinking block (build_body rebuilds
            #   reasoning_details from it). Without this, every in-loop turn
            #   after the first is a lossy continuation against Claude.
            if assistant_msg.get('thinking_signature'): clean_msg['thinking_signature'] = assistant_msg['thinking_signature']
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
                        round='final',
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
    task['toolRounds'] = []    # fresh tool rounds per turn

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
        if task.get('_fallback_reason'):
            result['fallbackReason'] = task['_fallback_reason']
        if task.get('_fallback_kind'):
            result['fallbackKind'] = task['_fallback_kind']

    logger.debug('[Endpoint] _run_single_turn %s → %d chars, finish=%s',
                 tid, len(result['content']), result['finishReason'])
    return result
