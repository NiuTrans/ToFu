# HOT_PATH — functions in this module are called per-request.
# Prefer logger.debug() over logger.info(). logger.info() is reserved
# for rare, high-signal events (e.g. content-filter injection, per-round diagnostics).
"""Orchestrator finalization + per-turn helpers.

Holds the post-loop finalizer ``_finalize_and_emit_done`` plus the
inter-round narration discard, suspicious-completion detector, tool-round
phase emitter, dangling-round sweep, whole-turn auto-retry, and the
sources-footer backstop. ``run_task`` (in ``_run.py``) calls these. Shared
outbound dependencies are resolved through the explicit ``_ports`` module.
"""

from __future__ import annotations

import copy
import time
from typing import Any
from urllib.parse import urlsplit

from lib.conversation_sync.settled_rows import is_settled
from lib.tool_results.tool_round_status import (
    AWAITING_ACTOR_TOOL_ROUND_STATUSES,
    TERMINAL_TOOL_ROUND_STATUSES,
)
from lib.tasks_pkg.round_costs import (
    stamp_round_costs, stamp_terminal_cost, usage_with_round_costs,
)
from ._completion_receipt import build_done_event
from ._completion_observers import observe_completion
from lib.tasks_pkg.failure_attribution import append_turn_retry_receipt
from lib.tasks_pkg.stream_handler._canned_greeting import is_canned_greeting_reply
from lib.turn_verdict import (
    CONTENT_FILTER_SETTLEMENT_KEY,
    DISPATCH_AUTHORITY_ABORT_REASONS,
    INTERNAL_FENCE_ABORT_REASONS,
    MODEL_REFUSAL_CAUSE,
    dispatch_authority_stop_detail,
)
from lib.tasks_pkg.llm_fallback._refusal import (
    model_refusal_envelope,
    model_refusal_error,
    stamp_content_filter_settlement,
    turn_input_is_automatic,
)
from lib.llm.stream_result import (
    ProviderStreamResult,
    ProviderStreamState,
    ensure_provider_stream_result,
)
from lib.log import get_logger

logger = get_logger(__name__)


from lib.tasks_pkg.cache_tracking._state import cleanup_stale_cache_states
from lib.tasks_pkg.cache_tracking._roi import get_session_cache_stats
from lib.tasks_pkg.cache_tracking._ttl import release_ttl_latch
from lib.agent_core.events import EventType, Phase, build_event, build_phase
from lib.tasks_pkg.executor import (
    _finalize_tool_round,
    _generate_tool_summary,
)
from lib.tasks_pkg.manager import (
    append_event,
    persist_task_result,
    reset_task_text,
    stamp_chat_task_terminal,
    stream_llm_response,
)
from lib.tasks_pkg.manager._events import _strip_base64_for_snapshot
from lib.tasks_pkg.llm_fallback._usage import (
    record_attempt_usage,
)
from lib.tasks_pkg.commit_round._commit import (  # noqa: E402
    _spawn_async_commit_round,
)
from lib.tasks_pkg.commit_round._profile import (  # noqa: E402
    _spawn_async_profile_consolidation,
)
from lib.tasks_pkg.tool_dispatch._labels import tool_label
from lib.tasks_pkg.tool_dispatch._pipeline import bounded_search_results_text


import lib.tasks_pkg.orchestrator._ports as orchestrator_ports


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
# WHY this exists (2026-07-28 09:51–10:45 incident): the example-corp gateway
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
# a blank terminal body or a confirmed incongruent canned greeting, we
# rebuild the delivered content from those snapshots instead of shipping the
# short residue, so the user gets the real accumulated work back.
#
# Gates (each one is a distinct no-false-positive boundary, all pinned by
# tests/tasks_pkg/test_suspicious_short_completion_guard.py):
#   * finish_reason == 'stop'   — only a completed turn is an overwrite
#     candidate (tool_use/aborted/error finishes are not "the model stopped");
#   * tool_call_happened        — no tool rounds means nothing accumulated;
#   * content is short (<50)    — mirrors the detector's own threshold, so the
#     recovery considers only that diagnostic shape, never a normal long
#     answer (complement test);
#   * blank or confirmed incongruent canned greeting — shortness alone is
#     diagnostic, never permission to overwrite a real final answer;
#   * recovered narration is substantial (> accumulated chars) — a genuinely
#     short turn with no real prior prose has nothing to recover, so it is
#     left untouched (no mangling a real "好的。");
#   * not aborted               — a user Stop legitimately truncates the
#     accumulator and must never be "recovered" over.
_SHORT_CONTENT_MAX = 50
_MIN_RECOVERED_NARRATION = 80


def _maybe_preserve_accumulated_on_suspicion(task, last_finish_reason,
                                             tool_call_happened,
                                             messages=None) -> bool:
    """Recover accumulated inter-round narration when the terminal round is a
    blank or confirmed canned-greeting ``stop`` after tool work.

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
    if content.strip() and not is_canned_greeting_reply(content, messages):
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
        'terminal stop round left %d chars (blank or confirmed canned/upstream '
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
    _settled = TERMINAL_TOOL_ROUND_STATUSES | AWAITING_ACTOR_TOOL_ROUND_STATUSES | {
        'skipped',
        # Effect-ledger refusal (prior attempt may have applied) is settled
        # at claim time with a receipt — sweeping it to 'aborted' would lie.
        'unknown',
    }
    tid = (task.get('id', '?') or '?')[:8]
    finalized = 0
    for index, entry in enumerate(rounds):
        if not isinstance(entry, dict):
            continue
        status = entry.get('status')
        if status in _settled:
            continue
        if is_settled(entry):
            # Settled history is replaced copy-on-write, never edited.
            entry = copy.deepcopy(entry)
            rounds[index] = entry
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
            # 'aborted' goes THROUGH the seam so the emitted tool_result
            # frame and the persisted round agree (a post-hoc downgrade would
            # ship a 'done' frame for an 'aborted' round).
            _finalize_tool_round(task, rn, entry, [meta], query_override=query,
                                 status='aborted')
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
      4. carries forward paid usage, restores the pristine invocation input,
         and resets per-invocation accumulators to a clean 'running' state;
      5. re-enters the existing invocation owner.

    Whole-turn regeneration is admitted only before any tool entry. Completed
    tool histories are not replayed by this path.

    Returns True iff a retry was launched (caller must ``return`` without
    finalizing).  Returns False when the error is not auto-retryable, the
    budget is exhausted, the caller opted out, or the user aborted during the
    backoff — in which case the caller finalizes and surfaces the error as
    usual, so manual Retry still works.
    """
    if (task.get('_suppress_whole_turn_retry_to_preserve_partial')
            and task.get('error')):
        logger.warning(
            '[%s] whole-turn auto-retry suppressed: a partial stream reply is '
            'being preserved across a later terminal error', task['id'][:8])
        return False

    from lib.tasks_pkg.turn_retry import (
        auto_turn_retry_max,
        should_auto_retry_turn,
    )

    # A whole-invocation reset is safe only before tool admission. Persistence
    # pressure retries its exact write elsewhere; it must never buy another
    # model request or replay an already-started external effect.
    if task.get('toolCallCount') or any(
            row.get('status') not in {'pending', 'queued', 'searching'}
            for row in task.get('toolRounds') or [] if isinstance(row, dict)):
        return False

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
    # Cross-attempt traceability: the discarded attempt's error (with its
    # failure attribution) is linked to this retry decision BEFORE the task
    # error slot is cleared, so the terminal settlement can persist the whole
    # chain instead of orphaning attempt N when attempt N+1 begins.
    append_turn_retry_receipt(
        task, attempt=_next, kind=_kind, backoff_s=backoff_s, error=err)
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
        from lib.tasks_pkg.stream_handler._budget import _interruptible_sleep
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
    # HONEST ACCOUNTING — the discarded attempt was BILLED.
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
        orchestrator_ports.rerun_task(task)
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


#: Tools whose settled result rows name the page they delivered to the model.
#: A ``fetch_url`` row is ``fetched`` once page content reached the model on
#: any path (handler, streaming prefetch, dedup replay, native gateway); a
#: ``browser_read_page`` row carries the tab's accepted URL only for a
#: successful text read. Search-hit rows are engine candidates, not openings.
_PAGE_READING_TOOLS = frozenset({'fetch_url', 'browser_read_page'})

#: Markdown delimiters percent-encoded so a model-chosen URL stays one inert
#: list item: no link, image or code span can open inside the footer.
_FOOTER_URL_ESCAPES = str.maketrans({
    '(': '%28', ')': '%29', '[': '%5B', ']': '%5D', '<': '%3C', '>': '%3E',
    '"': '%22', '`': '%60', '\\': '%5C'})


def _opened_page_url(value: Any) -> str:
    """Fragment-free http(s) URL of one opened page, or '' if it names none."""
    if not isinstance(value, str):
        return ''
    url = value.split('#', 1)[0]
    if (not url.startswith(('http://', 'https://')) or not url.isprintable()
            or any(ch.isspace() for ch in url)):
        return ''
    try:
        return url if urlsplit(url).netloc else ''
    except ValueError:
        return ''


def _opened_page_urls(tool_rounds: Any) -> list[str]:
    """Pages this turn's settled page-reading rounds opened, first-open order."""
    opened: list[str] = []
    for round_entry in tool_rounds or ():
        if (not isinstance(round_entry, dict)
                or round_entry.get('toolName') not in _PAGE_READING_TOOLS
                or round_entry.get('status') != 'done'):
            continue
        for row in round_entry.get('results') or ():
            if not isinstance(row, dict) or not row.get('fetched'):
                continue
            url = _opened_page_url(row.get('url'))
            if url and url not in opened:
                opened.append(url)
    return opened


def _maybe_append_sources_footer(task: dict[str, Any]) -> None:
    """Deterministic citation backstop for answers built on opened pages.

    When this turn opened web pages but the final answer cites none of them,
    append a compact "来源 / Sources" footer listing up to five opened pages
    in first-open order. Pairs with the system-prompt citation nudge as the
    half a non-compliant model cannot ignore.

    Evidence is the turn's settled page-reading rounds (``task['toolRounds']``,
    see ``_PAGE_READING_TOOLS``), which every execution path settles alike.
    Search-hit listings and links inside an opened page are never sources, so
    a turn that only searched gets no footer. No-op for an empty or aborted
    answer, a turn that opened nothing, or an answer citing an opened page.
    """
    content = task.get('content') or ''
    if not content.strip() or task.get('aborted'):
        return
    opened = _opened_page_urls(task.get('toolRounds'))
    if not opened or any(url.rstrip('/') in content for url in opened):
        return
    footer_urls = [url.translate(_FOOTER_URL_ESCAPES) for url in opened[:5]]
    task['content'] = (content + "\n\n---\n**来源 / Sources**（本轮检索所用，供核对）：\n"
                       + "\n".join(f"- {url}" for url in footer_urls))
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
            from lib.tasks_pkg.manager import task_user_id
            owner_user_id = int(task_user_id(task))
            for sit in undelivered:
                umsg = sit.get('_user_msg')
                scfg = sit.get('config') or {}
                if umsg:
                    payload = {'text': umsg.get('content', ''), '_user_msg': umsg}
                else:
                    payload = {'text': sit.get('value', '')}
                try:
                    queued = enqueue_message(
                        conv_id, payload, scfg, user_id=owner_user_id)
                    salvaged += 1
                    # The accepted steer already has a durable pending chip
                    # on this assistant Turn. Once the *next* Turn is queued,
                    # its old chip must no longer claim injection here. Keep
                    # the transition on the executor until the terminal Turn
                    # projection commits; never retract on an enqueue error.
                    block_id = sit.get('blockId')
                    if isinstance(block_id, str) and block_id:
                        task.setdefault('_userSteerInjects', []).append({
                            'blockId': block_id,
                            'deliveryState': 'queued',
                            'queueId': queued.get('queueId'),
                        })
                    # A timer that delivered this steer recorded ``steered``
                    # as its receipt; the same transition applies to it.
                    injection_id = sit.get('injectionId')
                    if isinstance(injection_id, str) and injection_id:
                        from lib.scheduler.timer import record_requeued_continuation
                        record_requeued_continuation(
                            injection_id, user_id=owner_user_id)
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


def _run_post_loop_fallback(
    task: dict[str, Any],
    *,
    model: str,
    preset: str,
    thinking_depth: str | None,
    cfg: dict[str, Any],
    messages: list,
    round_num: int,
    all_search_results_text: list[str],
    max_tokens: int,
    thinking_enabled: bool,
    temperature: float,
    last_finish_reason: Any,
    last_usage: Any,
    last_stream_result: ProviderStreamResult | None,
    accumulated_usage: dict,
    api_rounds: list,
) -> tuple[Any, Any, ProviderStreamResult | None]:
    """Synthesize a terminal answer when tools returned data but prose did not."""
    tid = task['id'][:8]
    prior_error = task.get('error')
    from lib.llm import AbortedError
    from lib.tasks_pkg.context_composer import append_injection
    from lib.tasks_pkg.orchestrator._root_agent_loop import admit_round_context

    try:
        # Original invocation input may be far larger than the working view.
        # Admission can transform nested carriers, so isolate its candidate
        # from the accepted context and the task's durable source.
        fallback_messages = copy.deepcopy(messages)
        combined = bounded_search_results_text(all_search_results_text)
        fallback_messages.append({
            'role': 'assistant',
            'content': "I've gathered the information. Let me analyze it.",
        })
        append_injection(
            fallback_messages, 'fallback_synthesis',
            f'Here are fetched contents:\n\n{combined}\n\n'
            'Provide a comprehensive answer. Cite sources.', task=task)

        # Preserve the live task identity required by summary authorization,
        # while counting against the effective destination after model fallback.
        configured_selection = task.get('config')
        task['config'] = dict(
            configured_selection or cfg, model=model, maxTokens=max_tokens)
        try:
            admitted_input_tokens = admit_round_context(
                task, fallback_messages, [], round_num=round_num, model=model,
            ).get('totalTokens')
        finally:
            if configured_selection is None:
                task.pop('config', None)
            else:
                task['config'] = configured_selection
        if task.get('aborted'):
            raise AbortedError('Owner stopped post-loop synthesis before dispatch')

        body = orchestrator_ports.build_request_body(
            model,
            fallback_messages,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking_enabled=thinking_enabled,
            preset=preset,
            thinking_depth=thinking_depth,
            response_format=cfg.get('responseFormat'),
            defer_reasoning=True,
            stream=True,
            precomputed_input_tokens=admitted_input_tokens,
            context_window=(task.get('_contextBudgetDecision') or {}).get(
                'contextWindowTokens', 0),
        )
        try:
            snapshot = _strip_base64_for_snapshot(
                body.get('messages', fallback_messages))
            append_event(task, build_event(
                EventType.MESSAGES_SNAPSHOT,
                kind='state',
                model=model,
                roundNum='fallback',
                label=f'Fallback · {len(fallback_messages)}条',
                messages=snapshot,
                contextManifest=list(task.get('_contextManifest') or []),
            ))
        except Exception as error:
            logger.warning(
                '[Task %s] messages_snapshot fallback failed, model=%s: %s',
                tid, model, error, exc_info=True,
            )
        last_stream_result = ensure_provider_stream_result(
            stream_llm_response(task, body, tag='FALLBACK'))
        _, finish_reason, usage = last_stream_result
        last_finish_reason = finish_reason
        if (prior_error and task.get('error') is prior_error
                and last_stream_result.is_verified_complete
                and finish_reason in ('stop', 'end_turn')
                and (last_stream_result.message.get('content') or '').strip()):
            task['error'] = None
        if usage:
            last_usage = usage
            record_attempt_usage(
                task, usage, model, 'fallback', accumulated_usage, api_rounds,
                tag='FALLBACK')
    except AbortedError:
        if not task.get('aborted'):
            raise
    except Exception as error:
        last_finish_reason = 'error'
        logger.error(
            '[%s] ⚠️ Post-loop fallback failed: %s',
            tid, error, exc_info=True,
        )
        try:
            from lib.llm.llm_error_format import format_llm_error_for_user
            task['error'] = format_llm_error_for_user(
                error,
                model=model,
                context='post-loop-fallback',
                source='orchestrator',
            )
        except Exception as format_error:
            logger.warning(
                '[%s] format_llm_error_for_user failed: %s',
                tid, format_error,
            )
            from lib.error_envelope import make_envelope as _make_env
            task['error'] = _make_env(
                'internal',
                detail=f'Post-loop fallback failed: {error}',
                model=model,
                context='post-loop-fallback',
                source='orchestrator',
                raw=str(error),
            )
    return last_finish_reason, last_usage, last_stream_result


def _settle_post_loop_finish_reason(
    task: dict[str, Any],
    last_finish_reason: Any,
    *,
    loop_exit_reason: Any,
    abort_detected_phase: Any,
    model: str,
    tid: str,
) -> Any:
    """Converge user aborts, system reaps, and dangling tool exits."""
    from lib.tasks_pkg.tool_delivery import has_committed_delivery
    if task['aborted']:
        pre_abort_finish = last_finish_reason
        _abort_reason = str(task.get('_abort_reason') or '')
        if task.get('_invocationStopReason') in {'stalled', 'deadline'}:
            # Keep physical aborted/client_aborted for resource teardown, but
            # persist an error finish rather than a fabricated user Stop.
            last_finish_reason = 'error'
        elif _abort_reason == 'stuck_no_progress':
            # A stuck-task reap is a system failure, not a user Stop. Both the
            # reaper and finalizer must persist the same terminal verdict.
            last_finish_reason = 'error'
            logger.warning(
                '[%s] REAPED task reached finalize (system kill, '
                '_abort_reason=stuck_no_progress) — settling '
                'finishReason=error, NOT aborted. Loop exit was "%s" '
                'model=%s.', tid, loop_exit_reason, model,
            )
        elif _abort_reason in INTERNAL_FENCE_ABORT_REASONS:
            # An internal storage/attempt fence is an authority-side system
            # failure, not a user Stop: the turn's event writes were rejected,
            # so settling as 'aborted' would record a user_abort verdict and
            # render the turn as an unexplained stop. Settle as error with an
            # explicit envelope instead.
            last_finish_reason = 'error'
            if not task.get('error'):
                from lib.error_envelope import make_envelope as _make_env
                task['error'] = _make_env(
                    'storage_fence',
                    detail=(
                        'The server storage authority rejected this turn\'s '
                        'event writes and stopped the in-flight request '
                        f'(internal fence: {_abort_reason}).'),
                    model=model,
                    context='post-loop-settle',
                    source='turn-finalize',
                    raw=(f'abort_reason={_abort_reason} '
                         f'finish={pre_abort_finish}'),
                )
            logger.warning(
                '[%s] INTERNAL FENCE abort reached finalize '
                '(_abort_reason=%s) — settling finishReason=error, NOT '
                'user_abort. Loop exit was "%s" model=%s.',
                tid, _abort_reason, loop_exit_reason, model,
            )
        elif _abort_reason in DISPATCH_AUTHORITY_ABORT_REASONS:
            # The project-dispatch authority that admitted this task ended it
            # (revoked grant or lost authority); no person stopped it. The
            # typed envelope is non-retryable: the same grant refuses again.
            last_finish_reason = 'error'
            if not task.get('error'):
                from lib.error_envelope import make_envelope as _make_env
                task['error'] = _make_env(
                    _abort_reason,
                    detail=dispatch_authority_stop_detail(
                        _abort_reason, str(task.get('_abortReasonDetail') or '')),
                    model=model,
                    context='post-loop-settle',
                    source='turn-finalize',
                    raw=(f'abort_reason={_abort_reason} '
                         f'finish={pre_abort_finish}'),
                    retryable=False,
                )
            logger.warning(
                '[%s] DISPATCH AUTHORITY stop reached finalize '
                '(_abort_reason=%s) — settling finishReason=error, NOT '
                'user_abort. Loop exit was "%s" model=%s.',
                tid, _abort_reason, loop_exit_reason, model,
            )
        else:
            last_finish_reason = 'aborted'
            if abort_detected_phase:
                logger.debug(
                    '[%s] Abort was detected INSIDE loop at: %s model=%s '
                    '(original finish_reason was "%s")',
                    tid, abort_detected_phase, model, pre_abort_finish,
                )
            else:
                logger.warning(
                    '[%s] LATE ABORT: loop exited normally (%s) model=%s '
                    'but task["aborted"] is True. Original finish_reason '
                    'was "%s". The user likely clicked Stop AFTER the model '
                    'finished but BEFORE the response was fully rendered.',
                    tid, loop_exit_reason, model, pre_abort_finish,
                )
    elif (not task.get('error') and task.get('_toolLoopIncompleteStop')):
        # A repeated successful check is not proof of task completion. Use the
        # existing incomplete outcome so the user retains Continue.
        last_finish_reason = 'incomplete'
    elif has_committed_delivery(task, last_finish_reason, loop_exit_reason):
        # Keep the actual verified provider marker. Explicit final text was
        # supplied in that same tool payload; no synthetic stop or model round.
        pass
    elif (last_finish_reason in ('tool_use', 'tool_calls')
            and not task.get('error')
            and task.get('_toolLoopCleanFinish')):
        # Background acceptance deliberately ends on a tool round; its
        # acknowledgement is not a dangling tool call.
        last_finish_reason = 'stop'
        logger.info(
            '[%s] Tool-loop breaker clean finish (%s) at round %s — '
            'settling finishReason=stop model=%s.',
            tid,
            (task.get('_toolLoopCleanFinish') or {}).get('reason'),
            (task.get('_toolLoopCleanFinish') or {}).get('round'),
            model,
        )
    elif last_finish_reason in ('tool_use', 'tool_calls') and not task.get('error'):
        last_finish_reason = 'error'
        from lib.error_envelope import make_envelope as _make_env
        task['error'] = _make_env(
            'internal',
            detail='Model requested tool calls but the loop ended unexpectedly.',
            model=model,
            context='post-loop',
            source='orchestrator',
            raw=(f'finish_reason={last_finish_reason} but loop exited '
                 'without further tool execution'),
        )
    return last_finish_reason


def _log_terminal_runtime_stats(
    task,
    *,
    tid,
    last_finish_reason,
    tool_call_happened,
    model,
    loop_exit_reason,
    round_num,
    api_rounds,
    abort_detected_phase,
):
    """Clean bounded terminal caches and emit one lifecycle diagnostic."""
    conv_id = task.get('convId', '')
    if conv_id:
        from lib.tasks_pkg.manager import task_user_id
        session_stats = get_session_cache_stats(
            conv_id, user_id=task_user_id(task))
        if session_stats and session_stats['calls'] > 1:
            logger.info(
                '[CacheSession] %s conv=%s END — %d calls, '
                'total_read=%d total_write=%d overall_hit=%d%% '
                'breaks=%d duration=%.1fs model=%s',
                tid, conv_id[:8], session_stats['calls'],
                session_stats['total_cache_read'],
                session_stats['total_cache_write'],
                session_stats['overall_hit_pct'],
                session_stats['total_breaks'],
                session_stats['session_duration_s'],
                session_stats['model'],
            )
    try:
        cleanup_stale_cache_states(max_age_s=3600)
    except Exception as exc:
        logger.debug('[Orchestrator] stale cache cleanup failed: %s', exc,
                     exc_info=True)

    for label, cache_key, eviction_key in (
        ('DedupCache', '_tool_result_cache', '_tool_result_cache_evictions'),
        ('UnchangedResult', '_unchanged_tool_result_receipts',
         '_unchanged_tool_result_receipt_evictions'),
    ):
        cache = task.get(cache_key)
        try:
            evictions = max(0, int(task.get(eviction_key) or 0))
        except (TypeError, ValueError, OverflowError):
            evictions = 0
        if isinstance(cache, dict) and (cache or evictions > 0):
            logger.info(
                '[%s] %s conv=%s task END — %d entries, %d evicted',
                label, tid, conv_id[:8] if conv_id else '???', len(cache),
                evictions,
            )

    content_len = len(task.get('content') or '')
    thinking_len = len(task.get('thinking') or '')
    elapsed = time.time() - task.get('created_at', time.time())
    logger.debug(
        '[Orchestrator] Task %s conv=%s COMPLETED — content=%dchars '
        'thinking=%dchars error=%s elapsed=%.1fs finishReason=%s toolCalls=%s',
        task['id'][:8], conv_id, content_len, thinking_len,
        task.get('error') or 'none', elapsed, last_finish_reason,
        'yes' if tool_call_happened else 'no')
    if (content_len == 0 and thinking_len == 0 and not task.get('error')
            and not task.get('aborted')):
        logger.warning(
            '[Orchestrator] Task %s conv=%s ⚠️ COMPLETED WITH EMPTY CONTENT '
            'and no error! This will appear as a blank message to the user.',
            task['id'][:8], conv_id)
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
        tid, loop_exit_reason, last_finish_reason, round_num + 1,
        tool_call_happened, content_len, thinking_len,
        task.get('error') or 'none', model, elapsed, len(api_rounds),
        task.get('aborted', False), abort_detected_phase or 'n/a')
    return content_len, thinking_len, elapsed


def _close_finalize_window(task):
    """Retire the LATE-done latch after settlement and event delivery.

    Continuation publication belongs to the lifecycle's successful settlement
    boundary, before it releases queued successors or returns the done frame.
    """
    # The real done is queued. Readers can drain it normally; a reader that
    # missed the queue can still synthesize LATE done from the terminal task.
    task.pop('_finalize_started_at', None)


def _settle_unstamped_content_filter(
    task: dict[str, Any],
    *,
    last_finish_reason: Any,
    loop_exit_reason: Any,
    last_stream_result: ProviderStreamResult,
    original_messages: list,
    model: str,
    tid: str,
) -> None:
    """Settle an unstamped content_filter finish as a typed model refusal."""
    # The fallback layer stamps refusals and gateway blocks where it decides
    # them; a content_filter finish reaching here unstamped came through a
    # pool rescue, reactive retry or post-loop synthesis and is the model's.
    if (not task['aborted']
            and not task.get(CONTENT_FILTER_SETTLEMENT_KEY)
            and (last_finish_reason == 'content_filter'
                 or (loop_exit_reason and 'content_filter' in str(loop_exit_reason).lower()))):
        stamp_content_filter_settlement(
            task, cause=MODEL_REFUSAL_CAUSE,
            envelope=model_refusal_envelope(
                model_refusal_error(model, last_stream_result.usage),
                automatic=turn_input_is_automatic(original_messages)))
        logger.info('[%s] Settled unstamped content_filter as a model refusal '
                    '(finish_reason=%s, loop_exit=%s)',
                    tid, last_finish_reason, loop_exit_reason)


def _finalize_and_emit_done(task: dict[str, Any], *, model: str, preset: str, thinking_depth: str | None, cfg: dict[str, Any],
                            last_finish_reason, last_usage, last_stream_result,
                            accumulated_usage, api_rounds,
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
    # transient transport/dispatch failure admitted by the shared retry policy.
    # If so — and the budget
    # is not exhausted and the caller didn't opt out — reset per-turn state,
    # tell the client to clear the partial bubble (retry_reset), back off, and
    # re-run run_task.  This spares the user from manually clicking Retry on
    # each of many parallel conversations that hit a passing gateway blip.
    # Every invocation uses this decision before tool admission. Graph retry/
    # replan remains a separate domain transition, never a creator-based bypass.
    if not task.get('aborted'):
        if _maybe_auto_retry_turn(task, cfg):
            return

    # A clean empty response can exhaust terminal-response recovery. Preserve
    # that synthesis path using typed provider evidence, never the broad
    # abnormal_stop kind alone (which also includes broken transports).
    terminal_error = task.get('error')
    recoverable_empty_stop = (
        isinstance(terminal_error, dict)
        and terminal_error.get('kind') == 'abnormal_stop'
        and last_finish_reason == 'abnormal_stop'
        and isinstance(last_stream_result, ProviderStreamResult)
        and last_stream_result.state in {
            ProviderStreamState.EMPTY_RESPONSE, ProviderStreamState.PROVIDER_FINISHED}
        and last_stream_result.provider_finish_reason == 'stop'
        and not (last_stream_result.message.get('content') or '').strip()
        and not last_stream_result.message.get('tool_calls')
    )
    # ── Fallback: synthesize answer from search results if main loop produced nothing ──
    if (not task['content'].strip() and tool_call_happened
            and all_search_results_text and not task['aborted']
            and (recoverable_empty_stop or (
                not terminal_error and last_finish_reason in ('stop', 'end_turn')))):
        (last_finish_reason, last_usage,
         last_stream_result) = _run_post_loop_fallback(
            task,
            model=model,
            preset=preset,
            thinking_depth=thinking_depth,
            cfg=cfg,
            messages=messages,
            round_num=round_num + 1,
            all_search_results_text=all_search_results_text,
            max_tokens=max_tokens,
            thinking_enabled=thinking_enabled,
            temperature=temperature,
            last_finish_reason=last_finish_reason,
            last_usage=last_usage,
            last_stream_result=last_stream_result,
            accumulated_usage=accumulated_usage,
            api_rounds=api_rounds,
        )

    if not isinstance(last_stream_result, ProviderStreamResult):
        last_stream_result = ProviderStreamResult.from_legacy(
            assistant_msg,
            last_finish_reason,
            last_usage if isinstance(last_usage, dict) else None,
        )

    # ── Content filter: settle typed evidence, never canned answer text ──
    _settle_unstamped_content_filter(
        task, last_finish_reason=last_finish_reason, loop_exit_reason=_loop_exit_reason,
        last_stream_result=last_stream_result, original_messages=original_messages,
        model=model, tid=tid)

    # ── Strip the END_TURN control token from the delivered answer ──
    # The intent-stall nudge TEACHES `[END_TURN: <reason>]` so a model can
    # declare *why* it stopped instead of being mistaken for an interrupted
    # turn. That makes it a MACHINE signal on the same channel as the prose:
    # the classifier reads it, then it must not reach the user. Stripped HERE,
    # at the single point where task['content'] becomes the delivered answer,
    # rather than at each render site — the durable turn, done event, and every
    # reload path all read this one
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
    # If the model opened web pages but cited none of them, append a compact
    # Sources footer of those pages (never search hits). Pairs with the
    # system-prompt citation nudge (section_tone_and_style web_tools path).
    try:
        _maybe_append_sources_footer(task)
    except Exception as _src_e:  # never let the backstop break finalization
        logger.warning('[%s] sources-footer backstop failed: %s', tid, _src_e)

    last_finish_reason = _settle_post_loop_finish_reason(
        task,
        last_finish_reason,
        loop_exit_reason=_loop_exit_reason,
        abort_detected_phase=_abort_detected_phase,
        model=model,
        tid=tid,
    )

    task['finishReason'] = last_finish_reason
    if isinstance(last_stream_result, ProviderStreamResult):
        task['streamState'] = last_stream_result.state.value
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

    # ── Visible outcome for a terminal pre-execution rejection ──
    # A rejected tool never ran.  If it is the turn's last terminal act, expose
    # the typed cause instead of settling a substantively incomplete task as a
    # silent success.  Only ``kind='hallucinated'`` means the tool was absent;
    # checkpoint, approval, mode, hook, and argument refusals are distinct.
    # User/task abort always outranks a preceding refusal and must not be
    # rewritten into a tool error during the final sweep.
    if not task.get('error'):
        try:
            _task_was_aborted = bool(task.get('aborted')) or str(
                task.get('status') or '').lower() in {
                    'aborted', 'interrupted',
                } or str(task.get('finishReason') or '').lower() in {
                    'aborted', 'interrupted', 'user_abort',
                }
            if not _task_was_aborted:
                from lib.tool_results.tool_rejection import (
                    is_unavailable_tool_rejection,
                    rejection_reason,
                    rejection_tool_name,
                    terminal_tool_rejection,
                )
                _rej_tail, _rj = terminal_tool_rejection(
                    task.get('toolRounds') or [])

                # A successful sibling cannot prove a guarded command in the
                # same execution batch ran. Preserve that conditional blocker.
                from lib.tasks_pkg.stream_handler._intent_stall import (
                    known_external_blocker,
                )
                from lib.tool_results.tool_rejection import tool_rejection_descriptor
                _blocked = known_external_blocker(task)
                if _rj is None and _blocked is not None:
                    _rj = _blocked
                    _rej_tail = next(
                        entry for entry in reversed(task.get('toolRounds') or [])
                        if tool_rejection_descriptor(entry) == _blocked)
            else:
                _rej_tail, _rj = None, None
            if _rej_tail is not None and _rj is not None:
                _bad = rejection_tool_name(_rej_tail, _rj) or '(unnamed tool)'
                _reason = rejection_reason(_rej_tail, _rj)
                _unavailable = is_unavailable_tool_rejection(_rej_tail)
                _error_kind = (
                    'tool_not_available' if _unavailable
                    else 'tool_call_rejected')
                if _unavailable:
                    _detail = (
                        f'The model tried to call `{_bad}`, which is not in '
                        f'this turn\'s toolset, and the turn ended without '
                        f'any tool running afterwards.')
                else:
                    _detail = _reason or (
                        f'`{_bad}` was blocked before execution '
                        f'({_rj.get("kind") or "unspecified_rejection"}).')
                from lib.error_envelope import make_envelope
                from lib.turn_lifecycle import automatic_recovery_blocker
                _extensions = {'rejection': dict(_rj)}
                _blocker = automatic_recovery_blocker(_extensions)
                if _blocker is not None:
                    _extensions.update(
                        retryPolicy=_blocker['retryPolicy'],
                        condition=_blocker.get('condition'),
                        autoRetryExhausted=True,
                    )
                task['error'] = make_envelope(
                    _error_kind,
                    detail=_detail,
                    model=model or '',
                    context='tool-dispatch',
                    source='orchestrator._finalize',
                    raw=(f'kind={_rj.get("kind")!r}, tool={_bad!r}'),
                    retryable=bool(_rj.get('retryable', False)),
                    extensions=_extensions,
                )
                logger.warning(
                    '[%s] conv=%s Task ended on tool rejection kind=%s '
                    'tool=%r — surfacing %s instead of a silent success',
                    tid, task.get('convId', '') or '', _rj.get('kind'),
                    _bad, _error_kind)
        except Exception as _e_tna:
            logger.warning('[%s] terminal tool-rejection classification failed '
                           '(task still settles, reason renders generic): %s',
                           tid, _e_tna, exc_info=True)

    try:
        from lib.tasks_pkg.compaction._settlement import settle_compaction_usage
        settle_compaction_usage(task, accumulated_usage=accumulated_usage,
                                api_rounds=api_rounds, model=model)
    except Exception as exc:
        logger.warning('[Usage] compaction costs retained for settlement: %s', exc)

    # Accounting and terminal construction are shared. A presentation port
    # chooses where evidence goes, never a weaker retry/settlement lifecycle.
    detached_presentation = callable(task.get('_invocationEventSink'))
    from lib.tasks_pkg.segments import assemble_segments
    task['segments'] = assemble_segments(task)
    stamp_round_costs(task, api_rounds, model=model)
    accumulated_usage = usage_with_round_costs(accumulated_usage, api_rounds)

    # ── Generate tool summary for cross-turn context (non-blocking) ──
    if not detached_presentation and tool_call_happened and not task['aborted']:
        try:
            summary = _generate_tool_summary(messages, model, task)
            if summary:
                task['toolSummary'] = summary
        except Exception as e:
            logger.warning('[Task %s] Tool summary generation failed model=%s (non-fatal): %s', task['id'][:8], model, e, exc_info=True)

    if not detached_presentation and not task.get('_flow_managed'):
        # Finalize-window latch: stamp BEFORE the terminal flip. From here
        # until append_event(done) and diagnostic handoff complete,
        # a LATE-done tick must hold — the successor stamp is not
        # on the index yet, so a premature LATE done would close the stream
        # with no successor on the wire (see lib/chat_dispatch.py branch 3).
        task['_finalize_started_at'] = time.time()
        # Fence TTL/capacity/memory-pressure eviction before the terminal
        # settlement begins. persist_task_result clears this receipt only
        # after both authoritative Turn settlement and diagnostics acknowledge.
        task['_terminalPersistencePending'] = True
        stamp_chat_task_terminal(
            task,
            status='done',
            finish_reason=str(last_finish_reason or 'unknown'),
        )

    # ── Cleanup reactive compact tracking (prevent memory leak) ──
    from lib.tasks_pkg.llm_fallback._state import cleanup_reactive_compact_state
    cleanup_reactive_compact_state(task.get('id', ''))

    # ── Release session-stable TTL latch (prevent memory leak) ──
    release_ttl_latch(task.get('id', ''))

    # ── Human-steer exactly-once salvage (never-zero fix) ──
    _salvage_undelivered_steer(task)

    # Child evidence outlives this parent, including a completion racing the
    # final inbox drain. Stop signals workers; natural settlement never deletes
    # unread results. Continuation is rechecked AFTER the durable done below.
    try:
        from lib.swarm.turn_handoff import prepare_swarm_turn_end
        from lib.tasks_pkg.manager import task_user_id
        if not detached_presentation:
            prepare_swarm_turn_end(
                task, user_id=int(task_user_id(task)),
                finish_reason=str(last_finish_reason or 'unknown'))
    except Exception as _e:
        logger.warning('[Orchestrator] swarm parent handoff failed: %s', _e, exc_info=True)

    _content_len, _thinking_len, _elapsed = _log_terminal_runtime_stats(
        task,
        tid=tid,
        last_finish_reason=last_finish_reason,
        tool_call_happened=tool_call_happened,
        model=model,
        loop_exit_reason=_loop_exit_reason,
        round_num=round_num,
        api_rounds=api_rounds,
        abort_detected_phase=_abort_detected_phase,
    )

    # ── Flag suspicious completions ──
    _suspicion_reasons = _check_suspicious_completion(
        task, last_finish_reason, _loop_exit_reason,
        tool_call_happened, round_num, model,
        assistant_msg=assistant_msg,
    )

    # ── ACT on the suspicious-short shape, not just warn (2026-07-28 canned
    #    greeting incident). When the terminal stop body is blank or a confirmed
    #    incongruent canned greeting
    #    after real tool work, recover the accumulated narration BEFORE the
    #    pre-emit conv sync and the done event read task['content'] — so the
    #    committed message and the client both get the real work back, not
    #    the 29-char residue.  Must run after the fallback/synthesis paths
    #    above (which can legitimately fill empty content) and after the
    #    detector has logged its shape.
    _maybe_preserve_accumulated_on_suspicion(
        task, last_finish_reason, tool_call_happened, messages)

    done_evt = build_done_event(
        task,
        cfg=cfg,
        api_rounds=api_rounds,
        last_finish_reason=last_finish_reason,
        last_stream_result=last_stream_result,
        accumulated_usage=accumulated_usage,
        last_usage=last_usage,
        model=model,
        thinking_depth=thinking_depth,
    )
    if _suspicion_reasons:
        done_evt['_diagnostics'] = {
            'loop_exit_reason': _loop_exit_reason,
            'rounds_completed': round_num + 1,
            'finish_reason': last_finish_reason,
            'content_len': _content_len,
            'thinking_len': _thinking_len,
            'suspicions': _suspicion_reasons,
        }

    if detached_presentation:
        stamp_chat_task_terminal(task, status='done',
                                 finish_reason=str(last_finish_reason or 'unknown'))
        append_event(task, done_evt)
        return

    # ── Emit done event (unless Flow-managed) ──
    #
    # The file-history snapshot for this round runs in a daemon thread
    # AFTER ``persist_task_result`` so queue-dispatch is never blocked
    # by snapshot I/O.  When the snapshot completes we emit a separate
    # ``round_committed`` SSE event carrying ``snapshotId`` (and the
    # legacy ``gitSha`` field, kept for frontend backward-compat) plus
    # any side-channel ``modifiedFileList`` additions discovered by
    # ``diff_name_status``.
    if task.get('_flow_managed'):
        _spawn_async_commit_round(task, project_enabled, project_path,
                                  project_paths=cfg.get('projectPaths'))
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

    stamp_terminal_cost(task, done_evt)
    observe_completion(task, done_evt, model=model,
                       finish_reason=last_finish_reason)

    # ── Supersede-successor stamp ( wire completion) ──
    # Same contract as the LATE-done synthesis (lib/chat_dispatch.py): when
    # another lifecycle owner already admitted a successor, the
    # conv→latest-task index names it HERE, before append_event — ship it so
    # the client's attach reducer can hop transport-agnostically. Absent on
    # the normal no-successor path (index points at this task → '').
    try:
        from lib.tasks_pkg.manager.runtime import _live_successor_info
        _succ_tid, _succ_is_vu = _live_successor_info(
            task.get('convId') or '', exclude_task_id=task.get('id', ''))
        if _succ_tid:
            done_evt['latestLiveTaskId'] = _succ_tid
            if _succ_is_vu:
                done_evt['latestLiveTaskIsVu'] = True
    except Exception as _succ_err:
        logger.debug('[Task:%s] successor stamp failed: %s', tid, _succ_err)

    try:
        append_event(task, done_evt)
    finally:
        # The enriched done owns Turn settlement. Diagnostics and once-only
        # listeners cannot acknowledge while that authority still owns a live
        # attempt. A failed handoff retains the existing maintenance debt.
        try:
            _terminal_persisted = persist_task_result(task, _defer_heavy_release=True)
        finally:
            _close_finalize_window(task)
    if not _terminal_persisted:
        logger.error('[Task:%s] retained terminal retry state after persistence debt', tid)
        return
    from lib.tasks_pkg.orchestrator._post_commit import finish_committed_task
    finish_committed_task(task, messages=messages, cfg=cfg,
        project_enabled=project_enabled, project_path=project_path,
        commit_round=_spawn_async_commit_round,
        consolidate_profile=_spawn_async_profile_consolidation)
