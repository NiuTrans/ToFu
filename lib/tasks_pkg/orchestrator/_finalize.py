# HOT_PATH — functions in this module are called per-request.
# Prefer logger.debug() over logger.info(). logger.info() is reserved
# for rare, high-signal events (e.g. content-filter injection, per-round diagnostics).
"""Orchestrator finalization + per-turn helpers.

Split out of the monolithic ``orchestrator.py`` (facade-preserving).
Holds the post-loop finalizer ``_finalize_and_emit_done`` plus the
inter-round narration discard, suspicious-completion detector, tool-round
phase emitter, dangling-round sweep, whole-turn auto-retry, and the
sources-footer backstop.  ``run_task`` (in ``_run.py``) calls these.

CRITICAL: ``build_body`` and ``run_task`` are resolved THROUGH the package
facade at call time (``import lib.tasks_pkg.orchestrator as _o``) so a
test/consumer reassignment of ``orchestrator.build_body`` steers this
module too, and the finalize->retry->run_task cycle stays import-safe.
"""

from __future__ import annotations

import re
import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


from lib.tasks_pkg.cache_tracking import (
    cleanup_stale_cache_states,
    get_session_cache_stats,
    release_ttl_latch,
)
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.executor import (
    _finalize_tool_round,
    _generate_tool_summary,
)
from lib.tasks_pkg.manager import (
    _strip_base64_for_snapshot,
    append_event,
    persist_task_result,
    stream_llm_response,
)
from lib.tasks_pkg.commit_round import (  # noqa: E402
    _run_commit_round_async,  # noqa: F401  (re-export for back-comp)
    _spawn_async_commit_round,
    derive_round_modified_files,
)
from lib.tasks_pkg.tool_dispatch import (
    tool_label,
)



# Resolve the REBINDABLE ``build_body`` binding + ``run_task`` THROUGH the
# package facade at CALL time (never bind at import): a test/consumer that
# reassigns ``orchestrator.build_body`` must steer this module too, and
# finalize->retry->run_task is a cycle that only closes at call time.
import lib.tasks_pkg.orchestrator as _o


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
    append_event(task, build_event(EventType.DELTA_RESET, roundNum=round_num))


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
        append_event(task, build_event(EventType.PHASE, phase='llm_thinking', detail='Generating response…', roundNum=1))
    else:
        tool_names = [tc['function']['name'] for tc in assistant_msg.get('tool_calls', [])]
        unique_names = list(dict.fromkeys(tool_names))
        labeled = [tool_label(n) for n in unique_names]
        summary = ', '.join(labeled)
        append_event(task, build_event(
            EventType.PHASE, phase='llm_thinking',
            detail=f'Analyzing results and planning next step… (round {round_num+1})',
            toolContext=summary,
            roundNum=round_num + 1,
        ))



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
        _o.run_task(task)
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


def _salvage_undelivered_steer(task: dict[str, Any]) -> int:
    """Re-route any UNDELIVERED human-steer message to the durable queue.

    A steer message the user sent WHILE this turn was running lives in the
    conversation-keyed ``agent_inbox`` under ``mode='user-steer'`` until a round
    boundary drains it. Two ways it can be undelivered when the task ends:

      1. Still queued in the inbox — the task finished (or aborted) before the
         next round-boundary drain reached it.
      2. Drained into ``messages`` but the task aborted/crashed BEFORE the LLM
         call confirmed consumption — it sits in ``task['_steer_inject_pending']``,
         the deferred-confirm chip/record never fired, and the in-memory
         message dies with the task.

    In BOTH cases we re-enqueue the steer into ``message_queue`` as a fresh next
    turn, so the message is delivered EXACTLY ONCE (never zero). MUST run BEFORE
    the swarm teardown clears()+tombstones the inbox slot — after that a drain
    returns nothing.

    Returns the number of steer messages salvaged (0 when there was nothing
    undelivered). Best-effort: an enqueue failure is logged, never raised.
    """
    salvaged = 0
    try:
        from lib.agent_inbox import drain as _drain_inbox
        from lib.swarm.integration import swarm_key_for as _swarm_key_for
        steer_key = _swarm_key_for(task)
        undelivered = list(task.pop('_steer_inject_pending', None) or [])
        if steer_key:
            undelivered.extend(_drain_inbox(steer_key, modes=['user-steer']))
        conv_id = task.get('convId') or ''
        if undelivered and conv_id:
            from lib.message_queue import enqueue_message
            for sit in undelivered:
                umsg = sit.get('_user_msg')
                scfg = sit.get('config') or {}
                if umsg:
                    payload = {'text': umsg.get('content', ''), '_user_msg': umsg}
                else:
                    payload = {'text': sit.get('value', '')}
                try:
                    enqueue_message(conv_id, payload, scfg)
                    salvaged += 1
                except Exception as _sqe:
                    logger.error('[Orchestrator] steer salvage enqueue failed '
                                 'conv=%s: %s', conv_id[:8], _sqe, exc_info=True)
            if salvaged:
                logger.info('[Orchestrator] salvaged %d undelivered steer msg(s) '
                            'to message_queue on task end (conv=%s)',
                            salvaged, conv_id[:8])
    except Exception as _e:
        logger.warning('[Orchestrator] steer salvage on task end failed: %s',
                       _e, exc_info=True)
    return salvaged


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
            append_event(task, build_event(EventType.MESSAGES_SNAPSHOT, roundNum='fallback', label=f'Fallback · {len(fb)}条', messages=snapshot))
        except Exception as e:
            logger.warning('[Task %s] messages_snapshot fallback failed, model=%s: %s', tid, model, e, exc_info=True)
        body = _o.build_body(
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

    # ── Human-steer exactly-once salvage (never-zero fix) ──
    _salvage_undelivered_steer(task)

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
