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

from lib.cost import normalize_usage
from lib.log import get_logger

logger = get_logger(__name__)


from lib.tasks_pkg.cache_tracking import (
    cleanup_stale_cache_states,
    get_session_cache_stats,
    release_ttl_latch,
)
from lib.agent_core.events import EventType, Phase, build_event, build_phase
from lib.tasks_pkg.executor import (
    _finalize_tool_round,
    _generate_tool_summary,
)
from lib.tasks_pkg.manager import (
    _strip_base64_for_snapshot,
    append_event,
    persist_task_result,
    reset_task_text,
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
    content_epoch = reset_task_text(task)
    append_event(task, build_event(
        EventType.DELTA_RESET, roundNum=round_num,
        contentEpoch=content_epoch))


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

    # ── Content-track divergence guard (bug-class net for the FloorRetry
    #    3411→215 silent loss, commit 6464592) ──
    # task['content'] is the APPEND-ONLY delta accumulator (_on_content) that
    # _sync persists; assistant_msg['content'] is the AUTHORITATIVE returned
    # answer. They must agree. Any retry/resend/fallback path that swaps in a
    # fresh authoritative msg WITHOUT reconciling the accumulator (as FloorRetry
    # did by streaming the adopted resend with on_content=None) re-creates the
    # exact silent loss — the model was billed for a long answer but the DB
    # stores a short residue. This check is PATH-AGNOSTIC: it fires at finalize
    # regardless of which door caused the divergence, so a FUTURE regression
    # surfaces LOUDLY in error.log + audit instead of shipping silently. We only
    # flag a genuine, non-marginal shortfall (msg has real prose AND the
    # accumulator kept <60% of it AND the gap is >200 chars) so normal
    # server-side footer/sanitize tweaks to task['content'] never trip it.
    _auth_content_len = len((assistant_msg or {}).get('content') or '')
    if (_auth_content_len > 200
            and _content_len < _auth_content_len * 0.6
            and (_auth_content_len - _content_len) > 200
            and not task.get('aborted')):
        suspicion_reasons.append(
            f'content_track_divergence(persisted={_content_len}<<'
            f'authoritative={_auth_content_len}chars)')
        logger.error(
            '[%s] conv=%s ⚠️ CONTENT-TRACK DIVERGENCE: persisted task[content]=%dchars '
            'but the authoritative assistant_msg[content]=%dchars — the DB will store '
            'the SHORT residue and the model output is silently LOST. This is the '
            'FloorRetry-class bug (6464592): a retry/resend produced the real answer '
            'without reconciling the delta accumulator. finish=%s model=%s',
            tid, task.get('convId', ''), _content_len, _auth_content_len,
            last_finish_reason, model)
        try:
            from lib.log import audit_log
            audit_log('content_track_divergence',
                      task_id=task['id'], conv=task.get('convId', ''),
                      persisted_chars=_content_len,
                      authoritative_chars=_auth_content_len,
                      finish_reason=last_finish_reason, model=model)
        except Exception as _ae:
            logger.debug('[%s] content_track_divergence audit failed: %s', tid, _ae)

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


# ── Suspicious-completion INTERCEPTION (act, not just warn) ───────────────
#
# WHY this exists (2026-07-28 09:51–10:45 incident): the sankuai gateway
# intermittently returned a 29-char canned greeting ("Hi! How can I help you
# today?") with ``finish_reason=stop`` for every Opus 5 request — 68 times,
# each carrying a real M-TraceId. Tasks that had just spent N rounds doing real
# tool work then had their TERMINAL round replaced by that greeting, and
# finalize persisted ``task['content']`` (the last round's 29 chars) over the
# accumulated deliverable. ``_check_suspicious_completion`` SAW it
# (``short_content_after_tool_calls`` fired 54×) — but only logged. Nothing
# stopped the overwrite.
#
# What is recoverable: ``_discard_pretool_prose`` zeroes ``task['content']``
# after every tool round, but the prose the model streamed ALONGSIDE those
# calls is kept verbatim on the tool round as ``assistantContent`` (see
# tool_dispatch/_parse.py). That is the accumulated inter-round work — the
# "已做的工作 + 阶段性结论" the user watched happen. When the terminal round is
# a suspicious short ``stop`` (the exact shape the detector already flags), we
# rebuild the delivered content from those snapshots instead of shipping the
# short residue, so the user gets the real accumulated work back.
#
# Gates (each one is a distinct no-false-positive boundary, all pinned by
# tests/test_suspicious_short_completion_guard.py):
#   * finish_reason == 'stop'   — only a completed turn is an overwrite
#     candidate (tool_use/aborted/error finishes are not "the model stopped");
#   * tool_call_happened        — no tool rounds means nothing accumulated;
#   * content is short (<50)    — mirrors the detector's own threshold, so the
#     interception fires on exactly the shape the detector flags, never on a
#     normal long answer (complement test);
#   * recovered narration is substantial (> accumulated chars) — a genuinely
#     short turn with no real prior prose has nothing to recover, so it is
#     left untouched (no mangling a real "好的。");
#   * not aborted               — a user Stop legitimately truncates the
#     accumulator and must never be "recovered" over.
_SHORT_CONTENT_MAX = 50
_MIN_RECOVERED_NARRATION = 80


def _maybe_preserve_accumulated_on_suspicion(task, last_finish_reason,
                                             tool_call_happened) -> bool:
    """Recover accumulated inter-round narration when the terminal round is a
    suspicious short ``stop`` after tool work.

    Returns ``True`` when the content was replaced with recovered narration,
    ``False`` when the turn was left untouched. See the module note above for
    the incident this closes and the gate boundaries.
    """
    tid = task['id'][:8]
    if last_finish_reason != 'stop':
        return False
    if not tool_call_happened:
        return False
    if task.get('aborted'):
        return False
    content = task.get('content') or ''
    if len(content) >= _SHORT_CONTENT_MAX:
        return False

    parts = [
        (r.get('assistantContent') or '').strip()
        for r in (task.get('toolRounds') or [])
        if isinstance(r, dict) and (r.get('assistantContent') or '').strip()
    ]
    recovered = '\n\n'.join(parts)
    if len(recovered) < _MIN_RECOVERED_NARRATION:
        return False

    task['content'] = recovered
    logger.warning(
        '[Orchestrator] %s conv=%s ⚠️ SUSPICIOUS SHORT COMPLETION intercepted: '
        'terminal stop round left only %d chars (likely a canned/upstream '
        'residue), so the delivered content was rebuilt from %d prior tool '
        'round(s)\' accumulated narration (%d chars) instead of overwriting '
        'the turn\'s real work. finish=%s',
        tid, task.get('convId', ''), len(content), len(parts),
        len(recovered), last_finish_reason)
    try:
        from lib.log import audit_log
        audit_log('suspicious_short_completion_intercepted',
                  task_id=task['id'], conv=task.get('convId', ''),
                  residue_chars=len(content),
                  recovered_chars=len(recovered),
                  rounds=len(parts))
    except Exception as _ae:
        logger.debug('[%s] suspicious-intercept audit failed: %s', tid, _ae)
    return True



# ── JSON repair for truncated / malformed LLM tool-call arguments ──────────
# Canonical implementation lives in lib.utils.repair_json.
# Re-exported here for backward compatibility.
from lib.utils import repair_json as _repair_json  # noqa: F401

def _emit_tool_round_phase(task, assistant_msg, round_num):
    """Emit a 'phase' event describing the current tool round for the frontend.

    Ships a stable ``detailKey`` (+ ``detailArgs``) alongside the legacy English
    ``detail``: modern clients pass ``detailKey`` through their i18n table so
    the label reads in the UI language (zh by default); older / headless
    clients that don't localize keep rendering ``detail`` unchanged.
    """
    if round_num == 0:
        if task.get('_vu_subtask'):
            detail = 'Autopilot is composing your reply…'
            detail_key = 'autopilot.composing'
        else:
            detail = 'Generating response…'
            detail_key = 'stream.phase.generatingResponse'
        append_event(task, build_phase(
            Phase.LLM_THINKING,
            detail=detail,
            detailKey=detail_key,
            roundNum=1))
    else:
        tool_names = [tc['function']['name'] for tc in assistant_msg.get('tool_calls', [])]
        unique_names = list(dict.fromkeys(tool_names))
        labeled = [tool_label(n) for n in unique_names]
        summary = ', '.join(labeled)
        append_event(task, build_phase(
            Phase.LLM_THINKING,
            detail=f'Analyzing results and planning next step… (round {round_num+1})',
            detailKey='stream.phase.analyzingRound',
            detailArgs={'round': round_num + 1},
            toolContext=summary,
            # Structured raw tool NAMES so an i18n client composes the
            # suffix in the UI language (toolContext stays the English
            # fallback for headless / non-i18n clients).
            toolContextTools=unique_names,
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
        content_epoch = reset_task_text(task)
        append_event(task, build_event(
            EventType.RETRY_RESET, attempt=_next, max=_cap, kind=_kind,
            contentEpoch=content_epoch))
        # (2) transient status bar (same contract as inner stream retries)
        append_event(task, build_phase(
            Phase.RETRYING, detail=_detail,
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
    # Text was cleared atomically with retry_reset before the backoff. Keeping
    # one reset seam prevents a poll snapshot from pairing the new epoch with
    # the discarded attempt's old text.
    # ★ HONEST ACCOUNTING — the discarded attempt was BILLED.
    #   By the time a turn settles into abnormal_stop / premature_close, the
    #   inner stream-anomaly loop may already have burned up to 16 attempts'
    #   worth of tokens, all correctly folded into task['usage'] / apiRounds.
    #   Wiping them here made the gateway's charge invisible: the re-run starts
    #   a fresh accumulated_usage (_run.py) and the wallet settles from the
    #   FINAL task['usage'] (lib/billing/request_flow.settle_task), so the user
    #   paid for up to 4 attempts and was charged for one. Same failure mode as
    #   the FloorRetry accounting bug — a real billed request hidden from the
    #   cost report.
    #
    #   Fold it into the SAME carry-forward slots the continue-checkpoint path
    #   uses; _finalize_task merges them into the terminal usage / apiRounds.
    #   Semantically exact ("billed before this run_task invocation") and
    #   ADDITIVE, so a task that is both resumed-from-checkpoint and
    #   auto-retried keeps both bills, and repeated retries accumulate.
    _spent = task.get('usage') or {}
    if _spent:
        from lib.cost import merge_usage_totals as _merge_usage
        _carry = _merge_usage(task.get('_checkpointUsage'), _spent)
        if _carry:
            task['_checkpointUsage'] = _carry
        _spent_rounds = task.get('apiRounds') or []
        if _spent_rounds:
            task['_checkpointApiRounds'] = (
                list(task.get('_checkpointApiRounds') or []) + list(_spent_rounds))
        logger.warning(
            '[%s] auto-retry: carrying forward the discarded attempt\'s billed '
            'usage (%s) so the wallet and cost popover see every request the '
            'gateway charged for', tid,
            {k: v for k, v in _spent.items() if isinstance(v, (int, float))})
    # The re-run must still START clean — it accumulates its own usage from
    # zero; the carry-forward above is merged back only at finalize.
    task['usage'] = {}
    task['apiRounds'] = []
    task['status'] = 'running'
    task['error'] = None
    task['finishReason'] = None
    task['toolRounds'] = []
    task['programRuns'] = []
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
            append_event(task, build_event(
                EventType.MESSAGES_SNAPSHOT, kind='state', model=model,
                roundNum='fallback', label=f'Fallback · {len(fb)}条',
                messages=snapshot,
                contextManifest=list(task.get('_contextManifest') or [])))
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
                # ★ HONEST ACCOUNTING (identical seam as _call.py): the
                #   post-loop fallback path is another consumer of the same
                #   stream_llm_response return contract — any FloorRetry
                #   discards it produces MUST also be billed. Without this the
                #   post-loop fallback would re-open the very leak we just
                #   closed in the primary/reactive/fallback branches.
                for _bill in (usg.get('_extra_billing_rounds') or []):
                    _bu = _bill.get('usage') or {}
                    for k, v in _bu.items():
                        if isinstance(v, (int, float)):
                            accumulated_usage[k] = accumulated_usage.get(k, 0) + v
                    api_rounds.append({
                        'round': 'fallback',
                        'model': _bill.get('model') or model,
                        'usage': dict(_bu),
                        'tag': _bill.get('tag') or 'FALLBACK-DISCARDED',
                    })
                    _emit_round_usage(task, 'fallback',
                                      _bill.get('model') or model, _bu,
                                      tag=_bill.get('tag') or 'FALLBACK-DISCARDED')
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

    # ── Strip the END_TURN control token from the delivered answer ──
    # The intent-stall nudge TEACHES `[END_TURN: <reason>]` so a model can
    # declare *why* it stopped instead of being mistaken for an interrupted
    # turn. That makes it a MACHINE signal on the same channel as the prose:
    # the classifier reads it, then it must not reach the user. Stripped HERE,
    # at the single point where task['content'] becomes the delivered answer,
    # rather than at each render site — the persisted row, the done event, the
    # committedMessage projection and every reload path all read this one
    # value, so a per-renderer strip would inevitably miss one.
    #
    # Runs BEFORE the sources footer so a stripped trailing token can never
    # sit between the prose and an appended footer.
    try:
        from lib.tasks_pkg.stream_handler._intent_stall import (
            strip_end_turn_marker,
        )
        _pre_strip = task.get('content') or ''
        if _pre_strip:
            _stripped = strip_end_turn_marker(_pre_strip)
            if _stripped != _pre_strip:
                task['content'] = _stripped
                logger.info('[%s] stripped [END_TURN:…] control token from the '
                            'delivered answer (%d → %d chars)',
                            tid, len(_pre_strip), len(_stripped))
    except Exception as _et_e:  # never let display hygiene break finalization
        logger.warning('[%s] END_TURN strip failed: %s', tid, _et_e)

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
        if task.get('_abort_reason') == 'stuck_no_progress':
            # ★ SYSTEM REAP, not a user Stop (pt_bf93496e98b9441e). The
            #   stuck-task reaper already settled this task's verdict —
            #   finishReason='error' + a worker_lost envelope — BEFORE the
            #   wedged worker thread unwound and reached this finalize. The
            #   unconditional 'aborted' collapse did two kinds of harm: it
            #   rendered a server-side kill as "已停止" (user Stop), hiding
            #   the one event that needs investigation; and because the
            #   reaper's own conv sync had already written 'error', the two
            #   terminal writers RACED — measured 4 'error' / 2 'aborted' on
            #   6 same-path reaps, the winner decided purely by timing.
            #   Converge on the reaper's verdict so BOTH writers land the
            #   same terminal state regardless of ordering.
            last_finish_reason = 'error'
            logger.warning('[%s] REAPED task reached finalize (system kill, '
                           '_abort_reason=stuck_no_progress) — settling '
                           'finishReason=error, NOT aborted. Loop exit was '
                           '"%s" model=%s.', tid, _loop_exit_reason, model)
        else:
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

    # ── Visible outcome for an unavailable-tool dead end (pt_88791cb08cb2495c) ──
    #   A tool the model does NOT have this turn is hard-rejected and never
    #   runs. If the turn then ends with that rejection as its last act, the
    #   task substantively FAILED — yet it used to settle status=done,
    #   error=none, so the user saw only the conversation stopping mid-thought
    #   while the API reported success. docs/INTENT_STALL_MEASUREMENT.md §4
    #   measured 3 such tasks in 7 days (project_board_complete / code_exec);
    #   the standing CLOSURE-PENDING note atop JOURNAL.md is a live victim —
    #   work finished, only the impossible call missing, nobody informed.
    #
    #   The criterion is STRUCTURAL, deliberately not the loop-breaker's
    #   no-suggestion gate: measurement showed `code_exec` DOES get near-miss
    #   suggestions, so gating on their absence would have missed 2 of the 3
    #   real cases. What makes it unrecoverable is that the tool is absent for
    #   the whole turn AND the turn produced no tool result afterwards.
    #
    #   §4 classifies this as NON-RETRYABLE and excludes it from the
    #   intent-stall nudge on purpose: re-prompting can only make the model
    #   reach for the same absent tool again.
    if not task.get('error'):
        try:
            _rej_tail = None
            for _entry in reversed(task.get('toolRounds') or []):
                if _entry.get('status') == 'rejected' and _entry.get('_rejected'):
                    _rej_tail = _entry
                    break
                if _entry.get('status') in ('done', 'error'):
                    # A real tool ran after the rejection — the model recovered.
                    break
            if _rej_tail is not None:
                _rj = _rej_tail.get('_rejected') or {}
                _bad = _rj.get('attempted') or _rej_tail.get('tool') or 'unknown'
                from lib.error_envelope import make_envelope
                task['error'] = make_envelope(
                    'tool_not_available',
                    detail=(f'The model tried to call `{_bad}`, which is not in '
                            f'this turn\'s toolset, and the turn ended without '
                            f'any tool running afterwards.'),
                    model=model or '',
                    context='tool-dispatch',
                    source='orchestrator._finalize',
                    raw=f'attempted={_bad!r}',
                )
                logger.warning('[%s] conv=%s Task ended on an unavailable-tool '
                               'rejection (%r) — surfacing tool_not_available '
                               'instead of a silent success',
                               tid, task.get('convId', '') or '', _bad)
        except Exception as _e_tna:
            logger.warning('[%s] tool_not_available classification failed '
                           '(task still settles, reason renders generic): %s',
                           tid, _e_tna, exc_info=True)

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
        # Finalize-window latch: stamp BEFORE the terminal flip. From here
        # until append_event(done) (autopilot hook + pre-emit sync in
        # between), a LATE-done tick must hold — the successor stamp is not
        # on the index yet, so a premature LATE done would close the stream
        # with no successor on the wire (see lib/chat_dispatch.py branch 3).
        task['_finalize_started_at'] = time.time()
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
            _kind_feed = (
                'aborted' if task.get('aborted') else
                ('blocked' if task.get('_todo_blocked') else 'completed'))
            emit_activity_event(
                _proj_feed, task['convId'], _kind_feed,
                (task.get('lastUserQuery') or '').strip() or ('Turn ' + _kind_feed),
                task_id=task['id'],
                payload={'todoBlocked': task.get('_todo_blocked')}
                if task.get('_todo_blocked') else None)
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

    # ── ACT on the suspicious-short shape, not just warn (2026-07-28 canned
    #    greeting incident).  When the terminal round is a short ``stop``
    #    after real tool work, recover the accumulated narration BEFORE the
    #    pre-emit conv sync and the done event read task['content'] — so the
    #    committed message and the client both get the real work back, not
    #    the 29-char residue.  Must run after the fallback/synthesis paths
    #    above (which can legitimately fill empty content) and after the
    #    detector has logged its shape.
    _maybe_preserve_accumulated_on_suspicion(
        task, last_finish_reason, tool_call_happened)

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
    if task.get('_todoState'):
        from lib.tools.todo import public_todo_state
        done_evt['todoState'] = public_todo_state(task['_todoState'])
    if task.get('_todo_blocked'):
        done_evt['todoBlocked'] = task['_todo_blocked']
    # ── Turn-ctx capsule fact-card contract ────────────────────────────
    # The per-turn note in the message gutter (static/js/info-rail.js) is
    # captured from the LIVE toolbar at send time — model / depth / modes
    # are a client-side SNAPSHOT that goes stale the moment the user
    # switches a preset in the pause between send and stream-start (or
    # when the dispatcher falls back to a different provider). Ship the
    # server-authoritative FACT for this turn so the frontend can
    # overwrite the capsule at done time and stop misleading the user.
    #
    # `actualModel` = the model the answer actually came from — the
    # mid-turn fallback wins over the initial pick, mirroring the
    # existing `fallbackModel` semantics one level up.
    # `actualDepth` = the thinking depth actually applied.
    # `actualModes` = the run-mode set that was live server-side
    # (autopilot / endpoint / swarm / flow name), same shape the info-rail
    # capsule renders ({label, tone:'mode'}). The frontend reconcile
    # OVERWRITES `snap.model` / `snap.depth` / `snap.modes` from these
    # — see info-rail.js::reconcileTurnCtxCapsule.
    done_evt['actualModel'] = task.get('_fallback_model') or model
    if thinking_depth:
        done_evt['actualDepth'] = thinking_depth
    # Anchor for the frontend turn-ctx capsule reconcile: the stable _msgId
    # of the user turn that TRIGGERED this task, stamped by
    # ``_start_task_for_conv`` (send/regenerate/continue) or inherited by
    # autopilot VU sub-tasks from their parent. Frontend prefers this over
    # "the last user in conv" — the latter is wrong under autopilot VU,
    # concurrent conv, or a regenerate targeting a historical user turn.
    # Empty string when a headless / external / legacy caller never stamped
    # it → frontend falls back to the last-user heuristic (safe default).
    if task.get('_userMsgId'):
        done_evt['userMsgId'] = task['_userMsgId']
    _actual_modes: list[dict[str, str]] = []
    _flow = cfg.get('activeFlow') if isinstance(cfg.get('activeFlow'), str) else ''
    if _flow:
        _actual_modes.append({'label': _flow, 'tone': 'mode'})
    else:
        if cfg.get('endpointMode'):
            _actual_modes.append({'label': 'Endpoint', 'tone': 'mode'})
        if cfg.get('autopilot'):
            _actual_modes.append({'label': 'Autopilot', 'tone': 'mode'})
    if cfg.get('swarmEnabled'):
        _actual_modes.append({'label': 'Swarm', 'tone': 'mode'})
    done_evt['actualModes'] = _actual_modes
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
    # ── Persist the parent's TERMINAL record BEFORE the autopilot hook ──
    #   (pt_5f0262fc). The VU sub-task runs INLINE inside maybe_run_autopilot
    #   (_run_single_turn on THIS thread) and can hang on ANYTHING — a wedged
    #   tool, a stalled LLM. Measured 2026-07-31: task 752273db finished at
    #   20:38:27 (message committed fr=stop, status='done' since line ~973)
    #   but its task_results row stayed 'running' for 2h57m because the VU
    #   sub-task sat in a run_command crawling a FUSE parent dir — the
    #   persist below the hook never ran. Everything the parent owes the
    #   world (terminal row, conv sync, queue drain, proactive status) must
    #   land BEFORE the VU can hang. The baton's own assumption comment
    #   ("persist_task_result runs _dispatch_queued_message before our hook
    #   fires") already expected this order — the code disagreed. The
    #   heavy-state release is deferred past the hook (the VU inherits
    #   task['messages']); it runs below, right after append_event(done_evt).
    persist_task_result(task, _defer_heavy_release=True)
    try:
        from lib.tasks_pkg.autopilot import maybe_run_autopilot
        maybe_run_autopilot(task)
    except Exception as _ap_err:
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

    # Same real-cost outcome sent on the live terminal event. The durable copy
    # was produced by the pre-emit conversation sync above; this closes the
    # live/reload parity gap without making experiment telemetry critical-path.
    try:
        from lib.cost_experiments import build_cost_experiment_outcome
        _outcome = build_cost_experiment_outcome(
            task.get('_costExperiment'),
            usage=done_evt.get('usage'),
            cost=done_evt.get('cost'),
            api_rounds=done_evt.get('apiRounds'),
            finish_reason=(done_evt.get('finishReason')
                           or task.get('finishReason')),
            error=done_evt.get('error') or task.get('error'),
            elapsed_ms=(time.time() - task.get('created_at', time.time())) * 1000,
            compactions=task.get('_costExperimentCompactions', 0),
            model=done_evt.get('model') or task.get('model') or '',
            provider_id=task.get('provider_id') or '',
            task=task,
        )
        if _outcome:
            done_evt['costExperiment'] = _outcome
            task['costExperiment'] = _outcome
            _round_tag = {
                'experiment_id': _outcome.get('experiment_id'),
                'arm': _outcome.get('arm'),
            }
            for _rd in done_evt.get('apiRounds') or []:
                if isinstance(_rd, dict) and _outcome.get('arm'):
                    _rd.setdefault('costExperiment', _round_tag)
    except Exception as _xe:
        logger.warning('[CostExperiment] done-event outcome failed '
                       '(non-fatal): %s', _xe, exc_info=True)

    # Terminal SLO sample. Correlation ids stay in events/logs only; the
    # Prometheus dimensions are deliberately limited to provider/model/outcome.
    try:
        from lib.observability import record_llm_task
        _metric_error = done_evt.get('error') or task.get('error')
        _metric_code = (_metric_error or {}).get('code') \
            if isinstance(_metric_error, dict) else ''
        if 'budget' in str(_metric_code):
            _metric_outcome = 'budget'
        elif task.get('aborted') or last_finish_reason == 'aborted':
            _metric_outcome = 'aborted'
        elif _metric_error:
            _metric_outcome = 'error'
        else:
            _metric_outcome = 'success'
        record_llm_task(
            done_evt.get('model') or task.get('model') or '',
            task.get('provider_id') or '',
            time.time() - task.get('created_at', time.time()),
            len(done_evt.get('apiRounds') or []),
            done_evt.get('usage') or {},
            float((done_evt.get('cost') or {}).get('costUsd') or 0.0),
            _metric_outcome,
        )
    except Exception as _metric_err:
        logger.debug('[Metrics] terminal LLM sample skipped: %s', _metric_err)

    # ★ Comprehensive task-completion summary — keyed on the FULL task id so a
    #   user who quotes the id from the cost popover can grep ONE line that
    #   spans the whole turn (all tool rounds). Includes the per-round cache
    #   miss count so "why did cache break" is answerable straight from the
    #   log without re-deriving it. INFO level → lands in logs/app.log.
    try:
        _rounds = done_evt.get('apiRounds') or []
        _miss_rounds = [r.get('round') for r in _rounds
                        if isinstance(r, dict) and r.get('cacheBreak')]
        _nu = normalize_usage(done_evt.get('usage') or {})
        _cw = _nu['cache_write']
        _cr = _nu['cache_read']
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

    # ── Supersede-successor stamp (pt_8dc03017 wire completion) ──
    # Same contract as the LATE-done synthesis (lib/chat_dispatch.py): when
    # the autopilot hook already spawned the successor (VU / follow-up), the
    # conv→latest-task index names it HERE, before append_event — ship it so
    # the client's attach reducer can hop transport-agnostically. Absent on
    # the normal no-successor path (index points at this task → '').
    try:
        from lib.tasks_pkg.manager import _live_successor_info
        _succ_tid, _succ_is_vu = _live_successor_info(
            task.get('convId') or '', exclude_task_id=task.get('id', ''))
        if _succ_tid:
            done_evt['latestLiveTaskId'] = _succ_tid
            if _succ_is_vu:
                done_evt['latestLiveTaskIsVu'] = True
    except Exception as _succ_err:
        logger.debug('[Task:%s] successor stamp failed: %s', tid, _succ_err)

    append_event(task, done_evt)
    # Finalize-window closed — the real done is now in the event queue, so a
    # tick drains it as a normal event instead of needing the LATE-done
    # synthesis. Clearing is safe even if a reader missed every event (the
    # task is terminal and the latch is gone → LATE done resumes its role).
    task.pop('_finalize_started_at', None)
    # Terminal busy-state broadcast (pt_3ea0e045): the CLEAR half of the busy
    # channel must be event-driven like the SET half is, not ride the next
    # incidental write. At this point the hook has concluded (a spawned VU
    # carrier projects itself as <tid>#vu; an ordinary settle projects IDLE),
    # the status is terminal and the latch is gone — this frame is the truth.
    from lib.tasks_pkg.manager import notify_terminal_busy_state
    notify_terminal_busy_state(task)
    # persist_task_result already ran BEFORE the autopilot hook (see above);
    # the heavy-state release was deferred because the VU inherits
    # task['messages'] — release it here, at the same point the old trailing
    # persist would have released.
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    _release_heavy_task_state(task)

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
