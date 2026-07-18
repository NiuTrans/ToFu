"""Autopilot mode — virtual user that auto-replies when the LLM stops.

When the model would normally hand control back to the user (either by
calling ``ask_human`` or by emitting a final assistant message with
``finish_reason='stop'``), Autopilot runs a one-shot LLM as the *user*
and feeds its reply back to the orchestrator as a brand-new turn.

Design constraints (locked in by the user, do not relax silently):

  • **Runs BEFORE the ``done`` SSE event.**  The hook fires inside
    ``_finalize_and_emit_done`` after the post-loop work but *before*
    ``append_event(done_evt)`` / ``persist_task_result``.  This lets
    the ``done`` event carry ``autopilotNextTaskId`` +
    ``autopilotVuMessage`` so the frontend attaches to the follow-up
    task directly instead of polling ``/api/chat/active`` after the
    SSE stream has already closed.  (Earlier design ran autopilot
    *after* persist; the SSE pipe was closed by the time the VU
    finished, so the synthetic user msg was invisible until manual
    refresh.)

  • **Independent of endpoint mode.**  Autopilot and endpoint mode are
    mutually exclusive — both share the same termination boundary
    ("the model stopped") so running them together would double-loop.
    The frontend hides one toggle when the other is on; this module
    additionally bails out when ``task['_endpoint_managed']`` is set.

  • **Reuse the conversation's main model.**  No separate VU model.

  • **Same tools as the worker.**  The VU runs through the full
    orchestrator (``_run_single_turn``) so it has access to every tool
    the parent task has — read_files, search, project edits, browser,
    memory, MCP, etc.  This lets the simulated user investigate before
    composing its reply (e.g. "let me check the file the assistant
    referenced before answering").  Inherited from
    ``task['config']`` verbatim — same tool list as
    ``_assemble_tool_list`` would build for the parent.

  • **Role override via a trailing directive turn**, mirroring how
    endpoint-mode's planner/critic announce their role.  We do NOT
    role-swap the conversation history: the LLM sees the real
    conversation and a final user-turn that says "for THIS turn play
    the simulated user".  Prefix-cache-friendly and avoids the
    swapped-history confusion with the orchestrator's injected
    system prompt.

  • **Full conversation passed through.**  We do NOT trim history
    here; the orchestrator's compaction layer (run_compaction_pipeline)
    handles bounding.  This keeps tool_calls / tool_result pairs
    contiguous and removes one place where context choices can drift
    between the worker and the simulated user.

  • **No turn cap, no state-change watchdog.**  The only graceful stop
    signal is the VU itself emitting ``[VU: TASK_DONE]``.  Other stops
    are: real-user abort, real-user sending a new message (handled
    automatically by ``abort_running_tasks_for_conv``), an error path,
    or the queue having a real queued message waiting (deferred to).

  • **Empty VU output does NOT stop the loop.**  An empty reply is
    treated as a valid "yeah, keep going" — the orchestrator just
    starts a fresh turn with that empty user message.  This is the
    user's explicit choice — see the design discussion in
    docs/ARCHITECTURE.md if rebooting decisions.

The "don't stop on empty output" rule means the only correctness escape
hatch is the real user clicking Stop or sending a new message.  Both are
already wired through ``task['aborted']`` and the freshness guard in
``manager._conv_latest_task``, so we don't need extra plumbing here.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid

from lib.agent_core.events import EventType, build_event
from lib.log import audit_log, get_logger

logger = get_logger(__name__)


from lib.agent_verdict import VU_DONE_SENTINEL as _VU_DONE_SENTINEL
from lib.agent_verdict import VU_ROLE_PROMPT as _VU_ROLE_PROMPT
from lib.agent_verdict import classify_verdict as _classify_verdict


# ``_VU_ROLE_PROMPT`` is the SINGLE-SOURCE virtual-user persona, defined once
# in ``lib.agent_verdict._handoff.VU_ROLE_PROMPT`` and imported here (the live
# standalone loop) AND by ``lib.swarm.registry`` (the FlowExecutor engine
# path).  It was previously an inline ~2000-char copy in this module that the
# registry paraphrased and drifted from; consolidating it kills that
# hand-copy.  The module-level ``_VU_ROLE_PROMPT`` name is retained (existing
# call sites + tests reference it) as an alias so nothing else changes, and
# ``VU_PROMPT_VERSION`` below still hashes the identical byte string.


# Content-derived prompt version marker.  Stamped into every VU directive turn
# so a stale-vs-live prompt mismatch is mechanically detectable (one glance at
# the directive text / message dict) instead of relying on eyeballing the prose.
# Derived from the prompt body itself, so it changes AUTOMATICALLY whenever the
# prompt text changes — no one can forget to bump it.  A directive carrying an
# old marker (or none) was produced by a server still running a pre-edit
# import-time constant; restart picks up the new prompt.  See
# tests/test_autopilot_verify.py for the regression assertion.
VU_PROMPT_VERSION = hashlib.sha256(
    _VU_ROLE_PROMPT.encode('utf-8')).hexdigest()[:8]

# Emit the loaded prompt version once at import so the RUNNING process's
# prompt is greppable in app.log without needing a directive paste to infer
# it ("is the live process current?" answered directly from logs).
logger.info('[Autopilot] VU prompt v%s loaded', VU_PROMPT_VERSION)


def _extract_objective(messages: list) -> str:
    """Return the original objective = the FIRST real user message text.

    Skips VU directive turns (``_isVuDirective``) and synthetic virtual-user
    turns (``_isVirtualUser``) so the anchor is always the human's opening
    ask, never an autopilot-generated reply.  Returns '' when none found.
    """
    for m in messages or []:
        if not isinstance(m, dict) or m.get('role') != 'user':
            continue
        # Skip synthetic injected turns, not just autopilot's own VU turns:
        # ``_isMeta`` marks the runtime context carriers (CLAUDE.md / per-turn
        # attachments) the context builder prepends — never a human ask.
        if m.get('_isVuDirective') or m.get('_isVirtualUser') or m.get('_isMeta'):
            continue
        content = m.get('content')
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            # Multimodal content blocks — concatenate the text parts.
            parts = [b.get('text', '') for b in content
                     if isinstance(b, dict) and b.get('type') == 'text']
            text = ' '.join(p for p in parts if p).strip()
        else:
            text = ''
        if text:
            return text
    return ''


def _extract_objective_from_db(conv_id: str) -> str:
    """Return the objective derived from the PERSISTED conversation messages.

    The DB row is the source of truth for what the human actually typed — it
    never contains the per-turn context the runtime injects into the in-memory
    ``task['messages']`` (user-preference profile, CLAUDE.md carrier, memory
    prefetch, per-turn attachments). Deriving the pinned objective from here
    keeps it to the human's ask, independent of how injected context is wrapped
    (``<system-reminder>`` today, an XML block tomorrow).

    Returns '' when the conversation can't be loaded (caller falls back to the
    live message list).
    """
    if not conv_id:
        return ''
    try:
        from lib.tasks_pkg.conv_message_builder import _load_messages_from_db
        raw = _load_messages_from_db(conv_id)
    except Exception as e:
        logger.debug('[Autopilot] objective DB read failed conv=%s: %s',
                     conv_id[:8], e)
        return ''
    if not raw:
        return ''
    return _extract_objective(raw)


def _get_or_persist_objective(conv_id: str, messages: list) -> str:
    """Resolve the immutable autopilot objective for a conversation.

    The objective is the north star the virtual user measures the assistant
    against.  It is captured ONCE (the first real user message) and pinned to
    ``settings.autopilotObjective`` so every follow-up task's VU sees the SAME
    anchor even after compaction has trimmed the early conversation history.

    Read-through cache: returns the persisted value if present; otherwise
    derives it from ``messages``, persists it, and returns it.  All failures
    are non-fatal — the caller falls back to deriving from the live messages.
    """
    if not conv_id:
        return _extract_objective(messages)
    try:
        from lib.conversations import update_conversation_settings
        # Serialized read-through mint (settings_store): re-read under the lock,
        # keep an existing pin, else derive + write — never clobbering a
        # concurrent settings write (e.g. autopilotRunId / activeTaskId).
        out = {'objective': ''}

        def _mut(settings):
            existing = (settings.get('autopilotObjective') or '').strip()
            if existing:
                out['objective'] = existing
                return False  # keep the pin; skip the write
            # Derive from the PERSISTED conversation, the source of truth for
            # human input: the DB row never carries per-turn injected context
            # (user-preference profile, CLAUDE.md, memory prefetch), whereas the
            # live ``messages`` handed to us is the runtime-augmented copy whose
            # first user turn has those <system-reminder> blocks spliced in.
            # Deriving from ``messages`` would pin ~2KB of boilerplate as the
            # objective. Fall back to the live list only if the DB read fails.
            objective = _extract_objective_from_db(conv_id) or _extract_objective(messages)
            out['objective'] = objective
            if not objective:
                return False  # nothing worth pinning
            settings['autopilotObjective'] = objective
            logger.info('[Autopilot] conv=%s pinned objective (%d chars)',
                        conv_id[:8], len(objective))
            return None  # proceed with the write

        # notify=False: autopilotObjective is internal run-bookkeeping, never
        # rendered — invalidate the (now-stale) cache blob but don't push.
        res = update_conversation_settings(conv_id, _mut, notify=False)
        if res is None:
            # Conv row absent — derive without persisting (original behaviour).
            return _extract_objective(messages)
        return out['objective']
    except Exception as e:
        logger.warning('[Autopilot] objective resolve failed conv=%s: %s — '
                       'deriving from live messages', conv_id[:8], e)
        return _extract_objective(messages)


def _get_or_persist_run_id(conv_id: str) -> str:
    """Resolve the immutable autopilot run id for a conversation.

    The run id is the EXPLICIT boundary that lets the frontend group a whole
    autopilot run ``[VU turn … summary]`` into one collapsible fold without
    role-scanning the flat message list (which breaks on edits, branches, and
    back-to-back runs). It is minted ONCE per run and pinned to
    ``settings.autopilotRunId`` alongside ``settings.autopilotObjective`` — both
    are cleared together when the run concludes (``disarm`` / TASK_DONE), so the
    next run gets a fresh id.

    Read-through cache: returns the persisted value if present; otherwise mints
    a new uuid, persists it, and returns it. Failures are non-fatal — returns a
    fresh (unpersisted) id so stamping still works for the current turn.
    """
    new_id = 'ar-' + uuid.uuid4().hex[:12]
    if not conv_id:
        return new_id
    try:
        from lib.conversations import update_conversation_settings
        # Serialized read-through mint (settings_store): re-read under the lock,
        # keep an existing runId, else mint + write — never clobbering a
        # concurrent autopilotObjective / activeTaskId write.
        out = {'id': new_id}

        def _mut(settings):
            existing = (settings.get('autopilotRunId') or '').strip()
            if existing:
                out['id'] = existing
                return False  # keep the id; skip the write
            settings['autopilotRunId'] = new_id
            logger.info('[Autopilot] conv=%s minted runId=%s', conv_id[:8], new_id)
            return None

        # notify=False: autopilotRunId is internal run-bookkeeping, not rendered.
        res = update_conversation_settings(conv_id, _mut, notify=False)
        if res is None:
            return new_id  # conv row absent → ephemeral id (original behaviour)
        return out['id']
    except Exception as e:
        logger.warning('[Autopilot] runId resolve failed conv=%s: %s — '
                       'using ephemeral id', conv_id[:8], e)
        return new_id


# Per-run budget state lives in settings alongside the run pins so it is
# DURABLE across the recursive follow-up tasks (the loop spans separate tasks,
# not one function) AND across a server crash + kick-resume: the counters are
# keyed to ``autopilotRunId`` and cleared together with it in ``_clear_run_id``,
# so a resumed run CONTINUES its count rather than restarting at 0 (a
# crash-looping run must not evade the cap).  Bounded history keeps the settings
# blob small.
_VU_HISTORY_CAP = 6


_PROGRESS_LEDGER_CAP = 8


def _record_vu_turn_and_check_budget(conv_id: str, vu_text: str,
                                     targets: list | None = None) -> dict:
    """Increment the run's VU turn count + append its request text, then verdict.

    Serialized read-merge-write through ``update_conversation_settings`` (never
    a bare RMW — see settings-column convention) so the increment doesn't
    clobber a concurrent ``activeTaskId`` / objective / summaries write on the
    same row.  The counters are pinned under ``autopilotTurnCount`` +
    ``autopilotVuHistory`` + ``autopilotProgress``, all cleared with the run
    pins in ``_clear_run_id``.

    ``targets`` is the set of files the WORKER touched this turn
    (``task['modifiedFileList']`` paths) — the churn signal for the
    diminishing-returns guard.  The VU reply's ``[PROGRESS: resolved=X
    remaining=Y]`` line supplies the hard net-progress signal.

    Returns ``{'stop': bool, 'reason': str, 'turn': int}`` — ``reason`` is
    ``'budget_exhausted'`` (turn ceiling), ``'stuck'`` (``AUTOPILOT_STUCK_WINDOW``
    near-identical VU nudges), or ``'no_progress'`` (``window`` edit-shipping
    turns re-touching the same targets without resolving new objective items),
    else ''.  FAIL-OPEN: any error resolving/persisting returns no-stop so a
    settings glitch never wedges a healthy loop, and the no_progress guard
    never fires without the hard ``[PROGRESS]`` signal.
    """
    out = {'stop': False, 'reason': '', 'turn': 0}
    if not conv_id:
        return out
    try:
        from lib.agent_verdict import (
            AUTOPILOT_STUCK_WINDOW,
            autopilot_max_turns,
            autopilot_progress_window,
            detect_diminishing_returns,
            detect_stuck,
            parse_progress,
        )
        from lib.conversations import update_conversation_settings

        max_turns = autopilot_max_turns()
        prog_window = autopilot_progress_window()
        resolved, _remaining = parse_progress(vu_text)
        turn_targets = sorted({str(t) for t in (targets or []) if t})

        def _mut(settings):
            count = int(settings.get('autopilotTurnCount') or 0) + 1
            settings['autopilotTurnCount'] = count
            hist = settings.get('autopilotVuHistory')
            if not isinstance(hist, list):
                hist = []
            hist.append(vu_text or '')
            if len(hist) > _VU_HISTORY_CAP:
                hist = hist[-_VU_HISTORY_CAP:]
            settings['autopilotVuHistory'] = hist

            # ── Progress ledger: per-turn (resolved_delta, targets) ──
            # resolved_delta = NEW items verified this turn = cumulative
            # resolved now minus cumulative resolved last turn (never negative;
            # None when the VU emitted no parseable [PROGRESS] line → fail open).
            ledger = settings.get('autopilotProgress')
            if not isinstance(ledger, list):
                ledger = []
            prev_cum = None
            for e in reversed(ledger):
                if isinstance(e, dict) and e.get('cum_resolved') is not None:
                    prev_cum = e['cum_resolved']
                    break
            if resolved is None:
                delta = None
                cum = prev_cum
            else:
                delta = resolved - prev_cum if prev_cum is not None else resolved
                if delta < 0:
                    delta = 0
                cum = resolved
            ledger.append({'resolved_delta': delta, 'cum_resolved': cum,
                           'targets': turn_targets})
            if len(ledger) > _PROGRESS_LEDGER_CAP:
                ledger = ledger[-_PROGRESS_LEDGER_CAP:]
            settings['autopilotProgress'] = ledger

            out['turn'] = count
            if max_turns and count >= max_turns:
                out['stop'] = True
                out['reason'] = 'budget_exhausted'
            elif detect_stuck(hist, window=AUTOPILOT_STUCK_WINDOW):
                out['stop'] = True
                out['reason'] = 'stuck'
            elif prog_window and detect_diminishing_returns(
                    ledger, window=prog_window):
                out['stop'] = True
                out['reason'] = 'no_progress'
            return None  # always persist the incremented counters

        # notify=False: turn-count / VU-history / progress ledger are internal
        # budget bookkeeping, not rendered — invalidate cache but don't push.
        res = update_conversation_settings(conv_id, _mut, notify=False)
        if res is None:
            return {'stop': False, 'reason': '', 'turn': 0}
        if out['stop']:
            logger.warning('[Autopilot] conv=%s run budget guard fired: '
                           'reason=%s turn=%d (max_turns=%s)',
                           conv_id[:8], out['reason'], out['turn'], max_turns)
            audit_log('autopilot_budget_stop', conv_id=conv_id,
                      reason=out['reason'], turn=out['turn'], max_turns=max_turns)
        return out
    except Exception as e:
        logger.warning('[Autopilot] budget check failed conv=%s: %s — '
                       'failing open (no stop)', conv_id[:8], e)
        return {'stop': False, 'reason': '', 'turn': 0}


def _clear_run_id(conv_id: str) -> None:
    """Clear the pinned run id + budget counters when a run concludes.

    Called on TASK_DONE (after the summary is generated) so the NEXT autopilot
    run on the same conversation mints a fresh ``autopilotRunId`` AND resets its
    turn budget / VU history / progress ledger.  Clearing the budget counters
    ATOMICALLY with the run id (one serialized write) is what guarantees a fresh
    run always starts clean — and, conversely, that a run still in progress
    keeps its accumulated count.

    ★ Hole A — ``autopilotObjective`` is DELIBERATELY NOT cleared here.  The
    objective is the first real user message (the conversation's north star);
    clearing it forced the next run to RE-DERIVE by re-scanning the live
    messages, and after compaction that re-scan could return a later,
    now-oldest-surviving turn instead of the true original — objective drift
    across run boundaries.  Keeping the pin durable means a subsequent run
    reuses the authoritative original objective rather than a re-scan.  This is
    consistent with the existing "objective = first user message" semantics
    (the pin equals what a clean re-scan WOULD return) and robust when the
    first turn has aged out of the window.  Best-effort — failures are swallowed
    at debug level.
    """
    if not conv_id:
        return
    try:
        from lib.conversations import update_conversation_settings
        # Serialized read-clear-write (settings_store): pop the run pins under
        # the lock so a concurrent settings write isn't clobbered.  NOTE:
        # autopilotObjective is intentionally absent — see docstring (Hole A).
        def _mut(settings):
            changed = False
            for k in ('autopilotRunId',
                      'autopilotTurnCount', 'autopilotVuHistory',
                      'autopilotProgress'):
                if settings.pop(k, None) is not None:
                    changed = True
            if not changed:
                return False  # nothing to clear; skip the write
            logger.info('[Autopilot] conv=%s cleared runId+budget '
                        '(run concluded; objective pin retained)', conv_id[:8])
            return None

        # notify=False: clearing internal run pins/counters is not rendered.
        update_conversation_settings(conv_id, _mut, notify=False)
    except Exception as e:
        logger.debug('[Autopilot] _clear_run_id failed conv=%s: %s', conv_id[:8], e)


def _store_run_record(conv_id: str, run_id: str, *,
                      reason: str,
                      text: str = '',
                      translated: str = '') -> dict | None:
    """Persist the SINGLE authoritative per-run record in the SIDECAR.

    ONE record per run carries BOTH facts the frontend needs — that the run has
    ``status='concluded'`` (with its ``reason``) AND the optional close-out
    ``content`` (a manual stop has none). There is deliberately no second map
    and no ``status='running'`` record: only the TERMINAL fact is persisted, so
    the frontend's ``syncConversationToServer`` (which rebuilds the whole
    ``settings`` column from a whitelist) can never clobber a mid-run marker it
    doesn't yet hold.

    Stored under ``settings.autopilotSummaries[run_id]`` (kept for the existing
    read/write/IDB whitelist + reload round-trip), which surfaces to the
    frontend as ``conv.autopilotSummaries[run_id]`` without ever becoming a chat
    turn. The frontend gates the run fold on ``rec.status==='concluded'`` and
    renders ``content`` (when present) as the fold's read-only report panel.

    Read-modify-write is idempotent per run: re-concluding merges (a later
    ``task_done`` with a report supersedes an earlier bare ``stopped`` marker,
    and never downgrades a report back to empty). Returns the stored record
    (``{runId, status, reason, content?, translatedContent?, ts, _summaryId}``),
    or ``None`` on failure. The record has NO ``role`` and NO ``_msgId`` — it is
    not a message.
    """
    try:
        from lib.agent_verdict import autopilot_summary_retention
        from lib.conversations import update_conversation_settings
    except Exception as e:
        logger.warning('[Autopilot] run record: import failed: %s', e)
        return None
    # Resolve the run's boundary turn's stable _msgId ONCE, server-side — the
    # backend authority for report placement. Read outside the settings mutation
    # (a separate column) so the frontend never has to re-derive placement by
    # scanning run-id stamps. '' when the run's turns aren't on disk yet.
    anchor_msgid = _resolve_run_anchor_msgid(conv_id, run_id)
    try:
        # Serialized read-merge-write (settings_store): autopilotSummaries
        # ACCRETES run records — a bare RMW would drop a concurrently-stored
        # sibling run. Merge the run under the per-conv lock on the freshest
        # blob so no run record is lost.
        out = {'record': None}

        def _mut(settings):
            summaries = settings.get('autopilotSummaries')
            if not isinstance(summaries, dict):
                summaries = {}
            prior = summaries.get(run_id) if isinstance(summaries.get(run_id), dict) else {}
            # Never downgrade a run that already carries a report to an empty
            # one: a later manual-stop conclude on a run that cleanly reported
            # keeps the report + upgrades the reason only if new one is task_done.
            content = text or (prior.get('content') or '')
            translated_final = translated or (prior.get('translatedContent') or '')
            # Reason precedence (a later conclude never downgrades a stronger
            # prior — manual stop / task_done can race): 'task_done'
            # (verified-complete) is strongest; everything else
            # (stopped / budget_exhausted / …) is weaker.
            _RANK = {'task_done': 3}
            _prior_reason = prior.get('reason') or ''
            reason_final = (_prior_reason
                            if _RANK.get(_prior_reason, 0) > _RANK.get(reason, 0)
                            else reason)
            record = {
                'runId': run_id,
                'status': 'concluded',
                'reason': reason_final,
                'ts': int(time.time() * 1000),
                '_summaryId': prior.get('_summaryId') or str(uuid.uuid4()),
            }
            # ★ BACKEND-AUTHORITATIVE PLACEMENT — the stable _msgId of the run's
            #   boundary turn. Resolved server-side (above) where the run's turns
            #   are known; the frontend docks the report at this id (a pure
            #   lookup) instead of inferring the boundary from run-id stamps.
            #   ★ STICKY: a prior anchor ALWAYS wins over a fresh re-resolution.
            #   The anchor is resolved+persisted ONCE, synchronously, at the
            #   conclude point (the TASK_DONE sync pre-stamp below, or the
            #   manual-stop conclude_run) — while the run's boundary is known
            #   and stable. A later write (the async report-content fill ~63s
            #   later, which may run AFTER the user started a new round) must
            #   NEVER move it: recomputing then could drift the boundary past a
            #   newer turn and offset the report to the transcript tail. So once
            #   stamped, keep it; only compute fresh when there is no prior.
            anchor_final = (prior.get('anchorMsgId') or '') or anchor_msgid
            if anchor_final:
                record['anchorMsgId'] = anchor_final
            # ★ A run cut off by a safety cap (budget_exhausted / stuck /
            #   no_progress) is UNFINISHED — the objective is unverified. Flag
            #   it so the fold renders "stopped early — needs review" instead of
            #   a clean conclusion. A later clean task_done supersedes it (the
            #   reason_final no-downgrade rule above), so re-concluding clears it.
            from lib.agent_verdict import is_incomplete_stop
            if is_incomplete_stop(reason_final):
                record['incomplete'] = True
            if content:
                record['content'] = content
            if translated_final:
                record['translatedContent'] = translated_final
            summaries[run_id] = record
            # ★ RETENTION — the map accretes one record per run and re-serializes
            #   into every settings PUT + IDB write, so on a year-scale
            #   conversation an unbounded map makes each turn's write cost grow
            #   O(n).  Keep the N most-recent runs by ``ts``; the run currently
            #   being concluded (``run_id``) is ALWAYS retained (it's the
            #   freshest ts, but we also force-keep it so a clock skew can't
            #   evict the live fold's own record).
            retain = autopilot_summary_retention()
            if retain and len(summaries) > retain:
                def _rec_ts(item):
                    rid, rec = item
                    if rid == run_id:
                        return float('inf')  # never evict the current run
                    try:
                        return float((rec or {}).get('ts') or 0)
                    except (TypeError, ValueError) as e:
                        logger.debug('[Autopilot] float ts parse failed, using fallback: %s', e)
                        return 0.0
                kept = sorted(summaries.items(), key=_rec_ts, reverse=True)[:retain]
                evicted = len(summaries) - len(kept)
                summaries = dict(kept)
                logger.info('[Autopilot] conv=%s pruned autopilotSummaries: '
                            'evicted %d oldest run record(s), kept %d (cap=%d)',
                            conv_id[:8], evicted, len(summaries), retain)
            settings['autopilotSummaries'] = summaries
            out['record'] = record
            logger.info('[Autopilot] conv=%s ✅ Stored concluded run record in sidecar '
                        '(reason=%s, %d report chars, run=%s, NOT a message)',
                        conv_id[:8], reason_final, len(content), run_id)
            return None

        res = update_conversation_settings(conv_id, _mut)
        if res is None:
            logger.warning('[Autopilot] run record: conv=%s not found', conv_id[:8])
            return None
        return out['record']
    except Exception as e:
        logger.error('[Autopilot] conv=%s run record sidecar store failed: %s',
                     conv_id[:8], e, exc_info=True)
        return None


def is_autopilot_enabled(task: dict) -> bool:
    """True iff autopilot is active for this task AND endpoint mode is not.

    Autopilot is "active" when EITHER:
      • ``config['autopilot']`` is set (config-driven — toggle was ON at the
        real send, propagated into the task and its follow-ups), OR
      • a persistent autopilot armed-marker exists for the conversation
        (the mid-stream / idle "arm" gesture; survives page reload and is
        cancellable from the queue bar).

    Endpoint mode wins the mutual exclusion (both share the same
    "model stopped" boundary).  The VU sub-task (``_vu_subtask``) and
    inline tasks never consult the marker — only DB-backed parent/follow-up
    tasks do, so the cheap config flag covers the hot recursion guard.
    """
    cfg = task.get('config') or {}
    if cfg.get('endpointMode') or task.get('_endpoint_managed'):
        return False
    if cfg.get('autopilot'):
        return True
    # Persistent armed-marker fallback (mid-stream arm / reload survival).
    if task.get('_vu_subtask') or task.get('_inline_messages'):
        return False
    conv_id = task.get('convId') or ''
    if not conv_id:
        return False
    try:
        from lib.message_queue import has_autopilot_marker
        return has_autopilot_marker(conv_id)
    except Exception as e:
        logger.debug('[Autopilot] marker probe failed (non-fatal): %s', e)
        return False


_VU_FORWARD_TYPES = frozenset({
    'delta', 'phase',
    'tool_start', 'tool_result', 'tool_progress', 'tool_complete',
    'tool_compacted',
    'stdin_request', 'stdin_resolved',
    'write_approval_request',
    'human_guidance_request', 'human_guidance_response',
})


class _VUEventForwarder(list):
    """List subclass that forwards the VU sub-task's events to the parent.

    The orchestrator drives all SSE updates by calling
    ``manager.append_event(task, ev)`` which does
    ``task['events'].append(ev)`` under the task's events_lock.  By
    swapping ``sub_task['events']`` with this subclass we get a hook on
    every event the VU sub-task emits, without monkey-patching
    ``append_event`` globally.

    For each VU event we still append it to the underlying list (so the
    sub-task's own SSE stream stays intact for any reader that ever
    connects to it), and additionally forward two flavours of derived
    events onto the PARENT task's stream:

      1. ``autopilot_vu_event`` — wraps the original VU sub-task event
         (delta / tool_start / tool_result / tool_progress / tool_complete /
         tool_compacted / stdin_* / write_approval_request /
         human_guidance_*) so the frontend can render the VU's reply +
         tool calls into the synthetic-user bubble *as they happen*,
         instead of materializing the whole bubble after the VU
         finishes.  The wrapper carries ``vuMsgId`` so the frontend can
         target the right message.

    The synthetic-user bubble itself is created eagerly by the
    ``autopilot_vu_start`` event (emitted from ``maybe_run_autopilot``
    BEFORE the VU sub-task runs), so the user sees an "Autopilot ·
    composing…" bubble in the USER lane the moment the worker stops —
    NOT a phase chip glued to the worker bubble.  All VU thinking, tool
    calls, and reply text then stream into that bubble via the wrapped
    events above.
    """

    def __init__(self, parent_task, vu_msg_id):
        super().__init__()
        self._parent = parent_task
        self._vu_msg_id = vu_msg_id

    def append(self, ev):
        super().append(ev)
        try:
            self._forward_to_parent(ev)
        except Exception as e:
            logger.debug('[Autopilot] event forward failed (non-fatal): %s', e)

    def _forward_to_parent(self, ev):
        from lib.tasks_pkg.manager import append_event as _ap_event
        et = (ev or {}).get('type')

        # Forward the inner event verbatim, wrapped so the frontend
        # routes it into the VU bubble (by vuMsgId) instead of the
        # parent's worker bubble.  We re-emit the parent-stream phase
        # chip below as well; the two are not mutually exclusive (one
        # paints the VU bubble, the other annotates the parent's chip).
        if et in _VU_FORWARD_TYPES:
            _ap_event(self._parent, build_event(
                EventType.AUTOPILOT_VU_EVENT,
                vuMsgId=self._vu_msg_id,
                inner=ev,
            ))


def _emit_vu_setup_phase(task: dict, vu_msg_id: str | None, detail: str) -> None:
    """Surface a pre-stream Autopilot setup step in the VU bubble.

    Diagnosis (task_events probe, debug/autopilot_warmup_window_probe.py):
    between ``autopilot_vu_start`` and the VU sub-task's first orchestrator
    phase (``llm_thinking`` / ``waiting_model``) there is a genuinely SILENT
    window — measured 2.5–26.7s across 12 real runs — during which
    ``run_virtual_user`` resolves the objective (DB read), assembles the
    message list and builds the sub-task. Nothing was emitted, so the bubble
    sat on the bare "Autopilot…" placeholder with no attribution of what was
    blocking.

    This emits a ``working`` phase wrapped as ``autopilot_vu_event`` — the
    SAME envelope ``_VUEventForwarder`` uses for the sub-task's own events —
    so it routes into the VU bubble by ``vuMsgId`` and renders through the
    existing ``updateStreamingUI`` ``working`` branch (``phase.detail`` shown
    verbatim). No new event type; the frontend already handles it.

    Emitted directly on the PARENT task because the sub-task (and its
    forwarding event list) does not exist yet at these steps.
    """
    if not vu_msg_id:
        return
    try:
        from lib.tasks_pkg.manager import append_event
        append_event(task, build_event(
            EventType.AUTOPILOT_VU_EVENT,
            vuMsgId=vu_msg_id,
            inner={'type': 'phase', 'phase': 'working', 'detail': detail},
        ))
    except Exception as e:
        logger.debug('[Autopilot] vu setup-phase emit failed (non-fatal): %s', e)


def run_virtual_user(task: dict, vu_msg_id: str | None = None) -> dict | None:
    """Run the VU LLM (with full tools) and return its reply + investigation.

    The VU runs as a fresh sub-task through the orchestrator's
    ``_run_single_turn``, inheriting the parent task's config so the
    same tools (read_files, search, project edits, memory, MCP, …)
    are available.  The trailing directive user-turn announces the
    "simulated user" role for THIS turn only — the conversation
    history itself is not role-swapped.

    Returns ``{'text': str, 'rounds': list}`` on success, where
    ``rounds`` is the VU sub-task's tool round history (suitable for
    attaching to the persisted synthetic user message so the user can
    see what Autopilot probed).  Returns ``None`` when the loop should
    stop — either because the VU emitted ``[VU: TASK_DONE]``, the
    sub-task failed, or the parent task was aborted while the VU was
    thinking.  An empty ``text`` is a valid "keep going" reply.
    """
    tid = task['id'][:8]
    if task.get('aborted'):
        logger.info('[Autopilot %s] Skip — task aborted', tid)
        return None

    parent_messages = task.get('messages') or []
    if not parent_messages:
        logger.warning('[Autopilot %s] No messages — stopping', tid)
        return None

    # Append the role-override directive as a trailing user turn —
    # same pattern as endpoint_review._run_planner_turn / _run_critic_turn.
    # We pass the parent's full message list verbatim so the VU sees the
    # entire conversation (including tool_calls / tool_result pairs);
    # the orchestrator's compaction layer handles context bounding.
    # Resolve the immutable objective anchor (north star).  Pinned to
    # settings.autopilotObjective so it survives across follow-up tasks and
    # compaction; falls back to deriving from the live messages.
    # ★ Attribute the (silent, up to tens of seconds) pre-stream window so the
    #   VU bubble names what's blocking instead of a bare "Autopilot…".
    _emit_vu_setup_phase(task, vu_msg_id, 'Autopilot：核对助手回答、确定下一步…')
    objective = _get_or_persist_objective(task.get('convId') or '',
                                           parent_messages)
    objective_block = ''
    if objective:
        objective_block = (
            '=== ORIGINAL OBJECTIVE (your north star — does NOT change '
            'across turns) ===\n'
            f'{objective}\n'
            '=== The assistant works for YOU toward this objective. '
            'Hold it to this, not to its own self-report. ===\n\n'
        )

    vu_messages = [dict(m) for m in parent_messages]
    vu_messages.append({
        'role': 'user',
        'content': (
            f'{objective_block}'
            f'=== Your role for THIS turn: Simulated User '
            f'[prompt v{VU_PROMPT_VERSION}] ===\n'
            f'{_VU_ROLE_PROMPT}\n'
            '=== End simulated-user role ===\n\n'
            'Based on the conversation above, produce the simulated '
            'user\'s reply now.  Verify the assistant\'s key claims with '
            'tools first, then output the reply text only.'
        ),
        '_isVuDirective': True,
        '_vuPromptVersion': VU_PROMPT_VERSION,
    })

    # Build a fresh sub-task that inherits the parent's config so
    # _assemble_tool_list constructs the same tool list the worker had.
    # ``_inline_messages=True`` keeps it out of the conv DB sync path,
    # ``convId=''`` keeps it out of the latest-task registry, and
    # ``_endpoint_managed=True`` suppresses the orchestrator's done
    # event + autopilot recursion.
    _emit_vu_setup_phase(task, vu_msg_id, 'Autopilot：整理对话上下文，准备生成回复…')
    from lib.tasks_pkg import create_task
    from lib.tasks_pkg.orchestrator import _run_single_turn

    sub_cfg = dict(task.get('config') or {})
    # Strip checkpoint/continue flags so the sub-task starts clean.
    for stale_key in (
        'excludeLast', 'toolHistory', 'contentPrefix',
        'checkpointToolRounds', 'checkpointUsage', 'checkpointApiRounds',
        'checkpointModifiedFiles', 'checkpointModifiedFileList',
    ):
        sub_cfg.pop(stale_key, None)
    # Endpoint mode is gated by is_autopilot_enabled but be defensive —
    # the sub-task must never re-enter endpoint mode.
    sub_cfg['endpointMode'] = False
    # Autopilot must NOT recurse (the parent's hook already runs us).
    sub_cfg['autopilot'] = False
    # Disable ask_human for the simulated user — the VU IS the user, so
    # asking another human makes no sense and would block forever (the
    # in-handler autopilot fallback is gated on cfg.autopilot which we
    # just turned off above).
    sub_cfg['humanGuidanceEnabled'] = False

    sub_task = create_task('', vu_messages, sub_cfg)
    sub_task['_inline_messages'] = True
    sub_task['_vu_subtask'] = True
    sub_task['_autopilotParent'] = task.get('id', '')
    # ── Peer-message fast path (Pillar #6) ──
    #   The VU sub-task runs with convId='' (to stay out of the latest-task
    #   registry), so its swarm/inbox key would be the sub-task id — but a peer
    #   twin for this conversation is enqueued under the PARENT conv id. Carry
    #   the parent conv as the peer-drain key so the sub-task's nested run_task
    #   round loop drains + flushes peer messages under the SAME key the twin
    #   was enqueued under (otherwise a peer message arriving mid-VU-turn would
    #   strand). This is what makes an endpoint/VU "big task" deliver a sibling's
    #   message at its next round boundary instead of the input-box queue.
    sub_task['_peer_drain_key'] = task.get('convId') or ''

    # Swap in a forwarding event list so the VU sub-task's events
    # surface live on the parent stream:
    #   • inner events (delta / tool_start / tool_result / tool_progress
    #     / tool_complete / tool_compacted / stdin_* /
    #     write_approval_request / human_guidance_*) are wrapped as
    #     `autopilot_vu_event` and routed by the frontend into the VU
    #     bubble (created eagerly by the `autopilot_vu_start` event
    #     above) identified by `vuMsgId` — so the user sees the VU's
    #     tool calls and reply STREAM in, not "pop in" once the VU
    #     finishes.
    sub_task['events'] = _VUEventForwarder(task, vu_msg_id or '')

    # Mirror parent abort onto the sub-task so user-clicked Stop while
    # the VU is mid-tool-loop tears the sub-task down too.  Single
    # threaded poll is fine — the sub-task is short-lived and the
    # orchestrator already polls task['aborted'] each round.
    _stop_mirror = threading.Event()

    def _mirror_abort():
        while not _stop_mirror.is_set():
            if task.get('aborted') and not sub_task.get('aborted'):
                sub_task['aborted'] = True
                sub_task['_abort_timestamp'] = time.time()
                sub_task['_abort_reason'] = 'parent_aborted'
                logger.info('[Autopilot %s] Mirroring parent abort onto '
                            'VU sub-task %s', tid, sub_task['id'][:8])
                return
            _stop_mirror.wait(0.5)

    _mirror_thread = threading.Thread(
        target=_mirror_abort,
        name=f'autopilot-abort-mirror-{tid}',
        daemon=True,
    )
    _mirror_thread.start()

    try:
        result = _run_single_turn(sub_task)
    except Exception as e:
        logger.warning('[Autopilot %s] VU sub-task raised: %s — '
                       'stopping autopilot for this conv', tid, e,
                       exc_info=True)
        return None
    finally:
        _stop_mirror.set()

    if task.get('aborted'):
        logger.info('[Autopilot %s] Aborted during VU sub-task — stopping', tid)
        return None

    err = result.get('error')
    if err:
        logger.warning('[Autopilot %s] VU sub-task error: %.200s — '
                       'stopping autopilot for this conv', tid, err)
        return None

    text = (result.get('content') or '').strip()
    rounds = list(sub_task.get('toolRounds') or [])
    # Route the stop decision through the single source of truth.  The
    # virtual_user policy ends the loop only on an explicit TASK_DONE/STOP
    # AND downgrades that to "keep going" when the reply itself still flags
    # unresolved work (❌ / "NOT met" / "still failing" / "unresolved") — the
    # anti-premature-done guard lives in lib/agent_verdict.py, NOT here.
    verdict = _classify_verdict(text, verifier_role='virtual_user')
    if verdict['phase'] == 'stop':
        logger.info('[Autopilot %s] VU emitted TASK_DONE — stopping loop', tid)
        # Signal the hook to clear the persistent armed-marker (disarm) so the
        # loop ends and the queue-bar sentinel disappears.
        task['_vu_emitted_done'] = True
        audit_log('autopilot_stop',
                  task_id=task.get('id', ''),
                  conv_id=task.get('convId', ''),
                  reason='vu_task_done')
        return None

    # The verdict downgraded a premature TASK_DONE to "keep going": the reply
    # may still literally carry the sentinel token.  Strip it so the
    # synthetic user message we feed back is clean instructional text, not a
    # stray sentinel the next turn would mis-read.
    if _VU_DONE_SENTINEL in text:
        text = text.replace(_VU_DONE_SENTINEL, '').strip()

    # ── Segment timeline (epic pt_cb8f98b0cb9b47fb) ──
    # The VU turn must render with the IDENTICAL agent inline per-tool timeline.
    # `_run_single_turn` runs the sub-task with `_endpoint_managed=True`, which
    # SKIPS the `persist_task_result` path where `assemble_segments` normally
    # runs — so the sub-task never got a `segments` list. Assemble it here, off
    # the SAME finished sub_task (its terminal content/thinking + merged
    # toolRounds), so it can be persisted onto the VU message. This is the ONLY
    # source; `sub_task.get('segments')` is always empty at this point.
    #
    # Persist the THIN form (segments_to_json strips the `_round` mirror) —
    # `toolRounds` is co-persisted on the same VU row, so the renderer + any
    # rehydration path recover the full round. DISPLAY-ONLY: segments on a
    # role=user VU row never reach the next agent (conv_message_builder's
    # _build_user_message reads ONLY `content`; the segment-first reconstruction
    # is assistant-only), so the VU provenance-split invariant holds.
    seg_thin: list = []
    try:
        from lib.tasks_pkg.segments import assemble_segments, segments_to_json
        seg_thin = segments_to_json(assemble_segments(sub_task))
    except Exception as e:
        logger.warning('[Autopilot %s] VU segment assembly failed (timeline will '
                       'fall back to grouped render): %s', tid, e)
        seg_thin = []

    logger.info('[Autopilot %s] VU reply: %.200s%s (used %d tool round(s), %d segment(s))',
                tid, text, ' …' if len(text) > 200 else '', len(rounds), len(seg_thin))
    return {'text': text, 'rounds': rounds, 'segments': seg_thin}


# ──────────────────────────────────────────────────────────────────
#  Follow-up scheduling — append a synthetic user msg + start a task
# ──────────────────────────────────────────────────────────────────

def _presync_parent_reply(task: dict) -> None:
    """Commit the parent task's FINAL assistant message to the conv DB.

    MUST run before this hook appends the VU turn / spawns the follow-up:
    once a follow-up registers as ``_conv_latest_task`` the freshness guard
    in ``manager._sync_result_to_conversation`` rejects the parent's final
    write, freezing the reply at its last streaming checkpoint (truncated
    content, ``finishReason=None``) and feeding that truncated copy to the
    follow-up.

    The orchestrator already calls this once before the hook when autopilot
    was enabled at task-creation time.  We repeat it here so the RUNTIME-ARM
    path (autopilot flipped on mid-stream via ``arm_autopilot``) is equally
    safe regardless of whether the arm landed before or after the
    orchestrator's gate — ``_sync_result_to_conversation`` only FILLS the
    trailing assistant slot (find-or-append), so a second call is an
    idempotent no-op when the orchestrator already synced.
    """
    conv_id = task.get('convId') or ''
    if not conv_id or task.get('_inline_messages'):
        return
    try:
        from lib.tasks_pkg.manager import (
            _sync_result_to_conversation,
            build_result_meta,
        )
        _sync_result_to_conversation(task, build_result_meta(task))
    except Exception as e:
        logger.warning('[Autopilot] parent pre-sync failed: %s — follow-up '
                       'may see a truncated parent reply', e, exc_info=True)


def _has_pending_real_message(conv_id: str) -> bool:
    """True if a real user message is queued — autopilot must defer."""
    if not conv_id:
        return False
    try:
        from lib.message_queue import get_queue_depth
        return get_queue_depth(conv_id) > 0
    except Exception as e:
        logger.debug('[Autopilot] queue depth probe failed (non-fatal): %s', e)
        return False


def _successor_already_running(task: dict, conv_id: str) -> bool:
    """True if another task has already taken over for this conversation.

    ``persist_task_result`` runs ``_dispatch_queued_message`` before our
    hook fires, so a queued real-user message will already have spawned
    its own follow-up task.  Spawning a VU follow-up on top of that
    would (a) abort the queued task via ``abort_running_tasks_for_conv``
    and (b) clobber the user's actual question.  Detect this by looking
    at the latest-task registry.
    """
    if not conv_id:
        return False
    try:
        from lib.tasks_pkg.manager import (
            _conv_latest_task,
            _conv_latest_task_lock,
        )
        with _conv_latest_task_lock:
            latest = _conv_latest_task.get(conv_id)
        return bool(latest) and latest != task.get('id')
    except Exception as e:
        logger.debug('[Autopilot] latest-task probe failed (non-fatal): %s', e)
        return False


def _append_vu_message_to_conv(conv_id: str, vu_msg_id: str,
                                text: str,
                                rounds: list | None = None,
                                run_id: str = '',
                                segments: list | None = None) -> dict | None:
    """Append the VU's reply as a user message in the conversation DB.

    Called ONLY after the VU has successfully produced a reply (i.e.
    after ``run_virtual_user`` returned non-``None``).  This is a
    deliberate design choice:

      • We DO NOT pre-write an empty placeholder before the VU runs.
        Doing so used to leave orphan empty rows in the DB whenever
        the cleanup path was missed (server crash, abort race, etc.)
        — visible to the user as "an empty VU bubble at the bottom"
        even when autopilot never actually took over.

      • The frontend lazily creates the VU bubble in memory when it
        receives the first ``autopilot_vu_event`` carrying actual
        content (``delta`` with text or ``tool_start``).  No DB write
        happens until success — so a VU that bails out (``[VU:
        TASK_DONE]``, abort, real user msg) leaves NO trace on disk.

    ``_msgId`` is the caller-minted id that the frontend used to route
    streaming updates; persisting it here lets a page reload right
    AFTER autopilot completes find the same message id and reconcile.
    """
    try:
        from lib.database import (
            DOMAIN_CHAT,
            db_execute_with_retry,
            get_thread_db,
            json_dumps_pg,
        )
    except Exception as e:
        logger.warning('[Autopilot] DB import failed: %s', e)
        return None

    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            logger.warning('[Autopilot] conv=%s not found — cannot append VU msg',
                           conv_id[:8])
            return None
        try:
            messages = json.loads(row[0] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Autopilot] conv=%s messages parse failed: %s',
                           conv_id[:8], e)
            return None

        vu_msg = {
            'role': 'user',
            'content': text,
            'timestamp': int(time.time() * 1000),
            '_msgId': vu_msg_id,
            '_isVirtualUser': True,
        }
        if run_id:
            vu_msg['_autopilotRunId'] = run_id
        if rounds:
            vu_msg['toolRounds'] = rounds
        # Segments (epic pt_cb8f98b0cb9b47fb): the thin typed-timeline list so
        # the VU turn renders the IDENTICAL agent inline per-tool timeline. On
        # reload, the save_conv preserve-merge re-attaches this by `_msgId` (the
        # `_isVirtualUser` row carries `_msgId`, set above) on every stripped
        # client PUT — so the timeline survives refresh, not just live+settle.
        if segments:
            vu_msg['segments'] = segments
        messages.append(vu_msg)

        now_ms = int(time.time() * 1000)
        try:
            from lib.conversations import build_search_text
            search_text = build_search_text(messages)
        except Exception as e:
            logger.debug('[Autopilot] build_search_text failed: %s', e)
            search_text = ''

        db_execute_with_retry(
            db,
            '''UPDATE conversations
                  SET messages=?, updated_at=?, msg_count=?, search_text=?
                  WHERE id=? AND user_id=1''',
            (json_dumps_pg(messages), now_ms, len(messages), search_text,
             conv_id),
        )
        logger.info('[Autopilot] conv=%s ✅ Appended VU msg %s (%d chars, %d rounds)',
                    conv_id[:8], vu_msg_id[:12], len(text), len(rounds or []))
        return vu_msg
    except Exception as e:
        logger.error('[Autopilot] conv=%s append failed: %s',
                     conv_id[:8], e, exc_info=True)
        return None


def _maybe_auto_translate_vu(conv_id: str, vu_msg_id: str, content: str) -> None:
    """Server-side auto-translate safety net for an appended Autopilot VU turn.

    The virtual-user turn is persisted by ``_append_vu_message_to_conv`` on a
    code path SEPARATE from ``manager._sync_result_to_conversation`` (which
    owns the assistant/critic safety net), so without this call a VU turn is
    only ever translated if a viewer happens to fire a manual translate — the
    reported "autopilot conversation never triggers auto-translate" bug.

    A VU row is stored ``role='user'`` + ``_isVirtualUser=True`` and is
    DISPLAY-translated (``content`` = model-language original, the safety net
    writes the UI-language ``translatedContent`` outer bubble), so the
    role-agnostic ``_maybe_auto_translate_assistant`` is the correct engine.

    We resolve the row INDEX from the freshly-persisted messages by matching
    ``_msgId == vu_msg_id`` (authoritative — never a guessed positional), and
    deliberately pass NO ``task``: the parent task's ``_assistantMsgId`` and its
    incremental per-round accumulator belong to the assistant turn, not this VU
    content — handing them in would mis-anchor the translation and adopt the
    wrong accumulator. The whole-message thread is the right path here. The
    safety net's own gates (``resolve_auto_translate`` off, already-Chinese,
    existing ``translatedContent``, and the ``claim_inflight`` dedup keyed by
    ``_msgId``) make this idempotent against a concurrent frontend manual
    translate. Best-effort: never raises into the autopilot loop.
    """
    if not conv_id or not vu_msg_id or not content:
        return
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,),
        ).fetchone()
        if not row:
            return
        messages = json.loads(row[0] or '[]')
        vu_idx = next(
            (i for i, m in enumerate(messages)
             if isinstance(m, dict) and m.get('_msgId') == vu_msg_id),
            -1,
        )
        if vu_idx < 0:
            logger.debug('[Autopilot] conv=%s VU msg %s not found for '
                         'auto-translate — skipping', conv_id[:8], vu_msg_id[:12])
            return
        from lib.tasks_pkg.auto_translate import _maybe_auto_translate_assistant
        _maybe_auto_translate_assistant(conv_id, content, vu_idx, db=db)
    except Exception as e:
        logger.warning('[Autopilot] conv=%s VU auto-translate failed '
                       '(non-fatal): %s', conv_id[:8], e)


def _start_followup_task(task: dict, conv_id: str) -> str | None:
    """Build api_messages from the conversation and spawn a new task.

    Mirrors what ``_start_task_for_conv`` does, but inlined to avoid
    importing from ``routes`` (orchestrator must not pull route-layer
    code at module scope — circular).
    """
    from lib.tasks_pkg import create_task
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
    from lib.tasks_pkg.manager import abort_running_tasks_for_conv

    cfg = dict(task.get('config') or {})
    # Strip checkpoint / continue flags so the follow-up runs fresh.
    #
    # ★ assistantMsgId MUST be stripped: it is the CLIENT-minted stable id of
    #   the ORIGINAL turn's assistant bubble (shipped once in the send POST).
    #   If it survives the cfg copy, create_task stamps it as this follow-up's
    #   `_assistantMsgId`, and _new_assistant_slot then reuses it as the
    #   committed row's `_msgId` — so EVERY follow-up in the run commits with
    #   the SAME `_msgId`. The frontend keys/dedups DOM nodes by `_msgId`, so N
    #   colliding assistant rows collapse into ONE bubble and the Agent replies
    #   between VU turns become invisible (observed: 16 assistant rows sharing
    #   one id → transcript degenerates to a wall of VU/user turns). The
    #   follow-up has NO live client bubble carrying this id (the frontend mints
    #   a fresh one in _attachAutopilotFollowup), so dropping it lets
    #   _assign_message_ids mint a UNIQUE server UUID per follow-up.
    for stale_key in (
        'excludeLast', 'toolHistory', 'contentPrefix',
        'checkpointToolRounds', 'checkpointUsage', 'checkpointApiRounds',
        'checkpointModifiedFiles', 'checkpointModifiedFileList',
        'assistantMsgId', 'msgId',
    ):
        cfg.pop(stale_key, None)

    api_messages = build_api_messages_from_db(conv_id, cfg)
    if not api_messages:
        logger.warning('[Autopilot] conv=%s build_api_messages returned '
                       'empty — cannot start follow-up', conv_id[:8])
        return None

    # Belt-and-braces: any other still-running task for this conv is
    # superseded by this autopilot follow-up, same as a real user send.
    abort_running_tasks_for_conv(conv_id)

    new_task = create_task(conv_id, api_messages, cfg)
    new_task_id = new_task['id']
    new_task['_autopilotParent'] = task.get('id')

    logger.info('[Autopilot] Spawning follow-up task %s for conv=%s '
                '(parent=%s)', new_task_id[:8], conv_id[:8],
                task.get('id', '?')[:8])
    audit_log('autopilot_followup',
              parent_task_id=task.get('id', ''),
              new_task_id=new_task_id,
              conv_id=conv_id)

    try:
        from lib.tasks_pkg import spawn_task as _spawn_task
        _spawn_task(new_task)
    except Exception as e:
        logger.error('[Autopilot] Failed to start follow-up thread: %s',
                     e, exc_info=True)
        from lib.error_envelope import make_envelope as _make_env
        new_task['status'] = 'error'
        new_task['error'] = _make_env(
            'internal',
            detail='Autopilot failed to spawn follow-up thread.',
            model=cfg.get('model', ''),
            context='autopilot',
            source='autopilot',
            raw=str(e),
        )
        return None

    # Update conversation settings.activeTaskId so reload still finds the
    # live task.  Best-effort — failure here doesn't break the loop.
    # Serialized read-merge-write (settings_store) so this doesn't clobber a
    # concurrent tool-state / autopilot settings write on the same row.
    try:
        from lib.conversations import set_conversation_settings
        # notify=False: notify_conv_changed is emitted just below (no double
        # push); the gate still invalidates the sidebar cache.
        set_conversation_settings(conv_id, {'activeTaskId': new_task_id},
                                  notify=False)
    except Exception as e:
        logger.debug('[Autopilot] activeTaskId update skipped: %s', e)

    try:
        from lib.conversations import notify_conv_changed
        notify_conv_changed(conv_id, rev=None)
    except Exception as e:
        logger.debug('[Autopilot] conv-changed notify skipped: %s', e)

    return new_task_id


def _emit_run_concluded(conv_id: str, run_id: str, text: str,
                        config: dict | None) -> None:
    """Emit ONE project-brain 'run_concluded' Activity event for a finished run.

    Autopilot per-turn 'started'/'completed' events are SUPPRESSED at the task
    seams (config.autopilotRunId set), so a deep run surfaces in the feed as a
    single human-meaningful pulse here, at run close-out — keyed on the run's
    project (config.projectPath). Best-effort: never raises into the caller.
    """
    try:
        proj = ((config or {}).get('projectPath') or '').strip()
        if not proj or not conv_id:
            return
        from lib.conversations.project_feed import emit_project_event
        summary = (text or '').strip().splitlines()[0] if text else ''
        emit_project_event(
            proj, conv_id, 'run_concluded',
            summary or 'Autopilot run concluded',
            payload={'runId': run_id})
    except Exception as e:
        logger.debug('[Autopilot] run_concluded feed emit skipped: %s', e)


def conclude_run(conv_id: str, reason: str = 'stopped',
                 run_id: str = '') -> dict | None:
    """Record the BACKEND-AUTHORITATIVE 'this autopilot run is over' fact.

    The single close-out seam for the paths that end a run WITHOUT a clean
    ``[VU: TASK_DONE]`` — a manual Stop, the toggle-OFF / queue-cancel disarm,
    or a superseding real user message. Historically these were "dumb": they
    cleared the marker but emitted NO run-level signal, so the frontend was
    forced to INFER run-end from stream/task absence (the inter-turn-gap
    heuristic that caused premature folds). This makes the terminal fact
    explicit and durable instead.

    Writes ONE concluded record (no report ``content`` — a manual stop has
    none) to the sidecar via :func:`_store_run_record`, then clears the run pin
    (``autopilotRunId`` + objective) so the next run is fresh — exactly the
    clean-close-out ordering. Idempotent: concluding an already-concluded run
    just refreshes the record (and never downgrades a ``task_done`` verdict).

    ``run_id`` may be passed explicitly; when omitted it is resolved from the
    most recent VU turn's ``_autopilotRunId`` so an already-disarmed run still
    folds. Returns the stored record, or ``None`` when there is no run to
    conclude (no run id resolvable → nothing was ever an autopilot run).
    """
    if not conv_id:
        return None
    if not run_id:
        run_id = _resolve_recent_run_id(conv_id)
    if not run_id:
        logger.debug('[Autopilot] conclude_run: conv=%s no run id — nothing to '
                     'conclude', conv_id[:8])
        return None
    record = _store_run_record(conv_id, run_id, reason=reason)
    _clear_run_id(conv_id)
    return record


def _resolve_recent_run_id(conv_id: str) -> str:
    """Return the most recent VU turn's ``_autopilotRunId`` for a conversation.

    Prefers the still-pinned ``settings.autopilotRunId`` (the live run); falls
    back to scanning the message tail for the newest ``_autopilotRunId`` stamp
    (an already-disarmed run whose pin was cleared). Returns '' when the
    conversation has no autopilot run at all. Best-effort — failures return ''.
    """
    if not conv_id:
        return ''
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT settings, messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            return ''
        try:
            settings = json.loads(row[0] or '{}') if row[0] else {}
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Autopilot] settings JSON parse failed, using fallback: %s', e)
            settings = {}
        pinned = (settings.get('autopilotRunId') or '').strip()
        if pinned:
            return pinned
        try:
            msgs = json.loads(row[1] or '[]') if row[1] else []
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Autopilot] messages JSON parse failed, using fallback: %s', e)
            msgs = []
        for m in reversed(msgs):
            if isinstance(m, dict) and (m.get('_autopilotRunId') or '').strip():
                return m['_autopilotRunId'].strip()
    except Exception as e:
        logger.debug('[Autopilot] _resolve_recent_run_id failed conv=%s: %s',
                     conv_id[:8], e)
    return ''


def _resolve_run_anchor_msgid(conv_id: str, run_id: str) -> str:
    """Resolve the stable ``_msgId`` of a run's BOUNDARY turn, server-side.

    This is the backend authority for report PLACEMENT. The boundary is the
    last turn belonging to the run: the run's VU turn, EXTENDED forward over the
    trailing unstamped agent follow-up(s) it prompted, stopping at the next
    run's VU turn / a real (non-VU) human turn / end-of-list. Returns that
    turn's ``_msgId`` so the frontend can dock the run's close-out report there
    by a stable id — never a mutable array index (the
    stream-target-resolution-by-msgid convention).

    Returns '' when the run has no turn on disk, or its boundary turn carries no
    ``_msgId`` (cannot anchor without a stable id — the caller then omits the
    anchor and the frontend uses its ts-tail last resort). Best-effort — any
    failure returns ''.
    """
    if not conv_id or not run_id:
        return ''
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            return ''
        try:
            msgs = json.loads(row[0] or '[]') if row[0] else []
        except (json.JSONDecodeError, TypeError):
            msgs = []
        # Last turn STAMPED with this run id (only the VU turn carries it).
        stamped_idx = -1
        for i, m in enumerate(msgs):
            if isinstance(m, dict) and (m.get('_autopilotRunId') or '').strip() == run_id:
                stamped_idx = i
        if stamped_idx < 0:
            return ''
        # Extend past the VU turn over the unstamped agent follow-up(s) it
        # prompted: stop at the next run-stamped turn (a new VU turn), a real
        # (non-VU) human turn, or end-of-list.
        boundary = stamped_idx
        for j in range(stamped_idx + 1, len(msgs)):
            m = msgs[j]
            if not isinstance(m, dict):
                break
            if (m.get('_autopilotRunId') or '').strip():
                break
            if m.get('role') == 'user' and not m.get('_isVirtualUser'):
                break
            boundary = j
        anchor = msgs[boundary]
        return (anchor.get('_msgId') or '').strip() if isinstance(anchor, dict) else ''
    except Exception as e:
        logger.debug('[Autopilot] _resolve_run_anchor_msgid failed conv=%s run=%s: %s',
                     conv_id[:8], run_id, e)
        return ''


def _emit_run_concluded_event(task: dict, conv_id: str, run_id: str,
                              reason: str = 'task_done') -> dict | None:
    """Persist the terminal fold record + emit the run-concluded pulse/SSE.

    The report-free close-out seam. The A-layer close-out REPORT (the LLM
    reporter turn + its sidecar ``content``) was removed; what remains is the
    B-layer fold machinery both close-out paths still need:

      1. write the BACKEND-AUTHORITATIVE concluded record via
         :func:`_store_run_record` (``status='concluded'``, ``reason``,
         ``anchorMsgId`` — NO report ``content``);
      2. emit the project-brain ``run_concluded`` Activity pulse;
      3. emit the ``autopilot_run_concluded`` SSE so a connected client folds
         the run's (VU→assistant)×N transcript immediately.

    ``reason`` is ``'task_done'`` on a clean ``[VU: TASK_DONE]`` and the guard
    reason (``budget_exhausted`` / ``stuck`` / …) on an abnormal cutoff — the
    latter stamps the record ``incomplete`` (via ``_store_run_record``) so the
    fold renders "stopped early — needs review". Returns the stored record, or
    ``None`` on persist failure. Best-effort on the emit steps.
    """
    from lib.tasks_pkg.manager import append_event

    tid = task['id'][:8]
    record = _store_run_record(conv_id, run_id, reason=reason)
    if record is None:
        return None

    _emit_run_concluded(conv_id, run_id, '', task.get('config'))

    try:
        append_event(task, build_event(
            EventType.AUTOPILOT_RUN_CONCLUDED,
            runId=run_id,
            record=record,
        ))
    except Exception as e:
        logger.debug('[Autopilot %s] run-concluded emit failed: %s', tid, e)
    return record


def maybe_run_autopilot(task: dict) -> dict | None:
    """End-of-turn hook: run the VU and spawn a follow-up task if eligible.

    Called from ``_finalize_and_emit_done`` BEFORE ``append_event(done_evt)``
    so the returned info can be embedded in the same ``done`` SSE event
    that finishes the current turn.  This eliminates the polling race
    where the SSE stream closed before the VU had time to spawn the
    follow-up task — the synthetic user message is now delivered
    in-band on the same connection.

    Returns ``{'next_task_id': str, 'vu_msg': dict}`` when a follow-up
    was spawned, ``None`` otherwise (no autopilot, no eligible context,
    VU emitted ``[VU: TASK_DONE]``, real user message queued, or any
    failure path).  The orchestrator inlines the dict into ``done_evt``
    as ``autopilotNextTaskId`` + ``autopilotVuMessage``.
    """
    tid = task['id'][:8]

    if not is_autopilot_enabled(task):
        # Log at debug level so silencing is invisible in normal mode
        # but findable when someone wonders "why didn't it take over?".
        cfg = task.get('config') or {}
        logger.debug('[Autopilot %s] Skip — not enabled '
                     '(autopilot=%s, endpointMode=%s, _endpoint_managed=%s)',
                     tid, cfg.get('autopilot'), cfg.get('endpointMode'),
                     task.get('_endpoint_managed'))
        return None

    conv_id = task.get('convId') or ''
    if not conv_id or task.get('_inline_messages'):
        logger.debug('[Autopilot %s] Skip — no DB-backed conversation', tid)
        return None
    if task.get('aborted'):
        logger.info('[Autopilot %s] Skip — task aborted before VU could run', tid)
        return None
    if task.get('error'):
        logger.info('[Autopilot %s] Skip — task ended in error: %.120s',
                    tid, str(task.get('error')))
        return None
    if task.get('finishReason') == 'tool_rounds_exhausted':
        logger.info('[Autopilot %s] Skip — tool rounds exhausted', tid)
        return None

    if _has_pending_real_message(conv_id):
        logger.info('[Autopilot %s] Skip — real user message queued '
                    '(it takes priority)', tid)
        return None
    # ``_successor_already_running`` is largely redundant in the new
    # ordering (queue dispatch happens AFTER us via persist_task_result),
    # but keep it as defense-in-depth for endpoint-mode / branch flows
    # that may have already advanced the latest-task registry for this
    # conversation.
    if _successor_already_running(task, conv_id):
        logger.info('[Autopilot %s] Skip — another task already took over '
                    'for conv=%s', tid, conv_id[:8])
        return None

    from lib.tasks_pkg.manager import append_event

    # Mint the VU message id up front and EAGERLY emit `autopilot_vu_start`
    # so the frontend creates the simulated-user bubble in the USER lane
    # the moment the worker stops — showing "Autopilot · composing…" with
    # the Autopilot avatar, exactly like a real pending user turn.  The
    # VU's thinking / tool calls / reply then stream INTO that bubble via
    # the wrapped `autopilot_vu_event` frames (see _VUEventForwarder).
    #
    # IMPORTANT — the start event is IN-MEMORY ONLY: it does NOT write
    # anything to the conv DB.  Persistence happens exactly once, on
    # success, in `_append_vu_message_to_conv` (fired right before
    # `autopilot_vu_done`).  Failure paths (TASK_DONE / abort / queued
    # real user msg) emit `autopilot_vu_cancel`, which removes the
    # in-memory bubble and leaves NO trace on disk — preserving the
    # "no ghost empty VU at the bottom" guarantee.
    vu_msg_id = str(uuid.uuid4())
    # Resolve the explicit run boundary up front so it can be stamped on
    # BOTH the VU turn (below) and the summary report (TASK_DONE branch).
    run_id = _get_or_persist_run_id(conv_id)

    # ★ Carry the parent worker's SETTLED finish metadata on vu_start so the
    #   frontend can complete the parent bubble's finish bar (model / usage /
    #   cost / finishReason) at the MOMENT the VU takes over the streaming
    #   substrate — not tens of seconds later when the parent `done` event
    #   finally fires (that event is deliberately withheld until the whole VU
    #   stream completes, so the follow-up baton can ride on it). Without this
    #   the early-finalized worker bubble shows a bar with ONLY the model tag
    #   (no tokens / no cost / no ✓) for the entire VU turn. `_committedMsg`
    #   was stamped by the pre-emit `_sync_result_to_conversation` in the
    #   orchestrator finalize (the EXACT dict written to conversations.messages,
    #   carrying finishReason/usage/cost/apiRounds). It is display-only here —
    #   the authoritative record is still shipped verbatim on the parent `done`
    #   event's `committedMessage`; this is the identical dict, just delivered
    #   early so the parent bar never renders incomplete.
    #
    #   SKIP-PATH FALLBACK: `_committedMsg` is unset when the pre-emit conv
    #   sync was skipped (freshness guard / CAS-exhaustion / inline). Emitting
    #   vu_start with NO finish payload there would leave the parent bar
    #   incomplete for the WHOLE VU turn (filled only by the late `done`) —
    #   the exact "sometimes incomplete" state the objective forbids. But the
    #   fields that DRAW the bar are already settled on the task itself by the
    #   orchestrator finalize BEFORE this hook runs: `finishReason` (_finalize
    #   ~L621), `usage` (~L622/917), `model` (~L847), `apiRounds` (~L925), and
    #   `provider_id` (lets the frontend `calcCostCny` compute the cost-tag
    #   even without a committed `cost`). So when there is no committed dict we
    #   build a MINIMAL parentMessage from those task fields. We only omit
    #   `parentMessage` entirely when the task genuinely has NOTHING to show
    #   (no finishReason AND no usage — e.g. an errored turn with no metering),
    #   the sole circumstance where the bar legitimately waits for `done`.
    _parent_msg = task.get('_committedMsg')
    if not _parent_msg:
        _fr = task.get('finishReason')
        _usg = task.get('usage')
        if _fr or _usg:
            _parent_msg = {'role': 'assistant'}
            if _fr:
                _parent_msg['finishReason'] = _fr
            if _usg:
                _parent_msg['usage'] = _usg
            for _k in ('model', 'provider_id', 'apiRounds', 'preset',
                       'toolSummary', 'thinkingDepth'):
                _v = task.get(_k)
                if _v is not None:
                    _parent_msg[_k] = _v
            logger.debug('[Autopilot %s] vu_start using task-field fallback '
                         'parentMessage (no _committedMsg; finishReason=%s)',
                         tid, _fr)
    _start_evt = build_event(EventType.AUTOPILOT_VU_START, vuMsgId=vu_msg_id)
    if _parent_msg:
        _start_evt['parentMessage'] = _parent_msg
    try:
        append_event(task, _start_evt)
    except Exception as e:
        logger.debug('[Autopilot %s] vu_start emit failed: %s', tid, e)

    vu_result = run_virtual_user(task, vu_msg_id=vu_msg_id)
    if vu_result is None:
        # VU emitted [VU: TASK_DONE], errored, or was aborted. On a graceful
        # TASK_DONE, disarm the persistent marker so the loop ends and the
        # queue-bar sentinel disappears.  (Abort/error leave the marker intact
        # — a transient failure shouldn't silently disarm.)
        if task.get('_vu_emitted_done'):
            # The run reached its objective. CONCLUDE (fold) the run and settle
            # the turn synchronously — the A-layer close-out REPORT was removed,
            # so there is no longer a slow reporter LLM turn / EN→ZH translation
            # to keep off the hot path. All that remains is the cheap B-layer
            # fold fact: persist the concluded record (stamping `anchorMsgId`
            # while the run's boundary turn is stable — no new round has started
            # yet), emit the project-brain pulse, and emit the
            # `autopilot_run_concluded` SSE so a connected client folds the run
            # immediately. On this clean TASK_DONE path there is no follow-up
            # baton to strand — the loop is ENDING — and the terminal `done`
            # fires right after, closing the stream. Best-effort: a failure here
            # just leaves the run unfolded until the next settings round-trip.
            try:
                _emit_run_concluded_event(task, conv_id, run_id,
                                          reason='task_done')
            except Exception as e:
                logger.warning('[Autopilot %s] run conclude failed '
                               '(non-fatal): %s', tid, e, exc_info=True)
            # Disarm + clear the run pin so the turn settles now.
            try:
                from lib.message_queue import clear_autopilot_marker
                clear_autopilot_marker(conv_id)
            except Exception as e:
                logger.debug('[Autopilot %s] marker clear failed: %s', tid, e)
            _clear_run_id(conv_id)
        # Tell the frontend to discard any in-memory bubble it may
        # have lazily created from inner stream events; nothing was
        # ever persisted.
        try:
            append_event(task, build_event(
                EventType.AUTOPILOT_VU_CANCEL,
                vuMsgId=vu_msg_id,
            ))
        except Exception as e:
            logger.debug('[Autopilot %s] vu_cancel emit failed: %s', tid, e)
        return None
    vu_text = vu_result['text']
    vu_rounds = vu_result.get('rounds') or []
    vu_segments = vu_result.get('segments') or []

    # Race-close: a real user may have submitted a message while the VU
    # LLM call was running.  If so, defer to that real message instead
    # of clobbering it with a synthetic VU turn.
    if _has_pending_real_message(conv_id):
        logger.info('[Autopilot %s] Real user message arrived during VU '
                    'call — deferring to queue', tid)
        try:
            append_event(task, build_event(EventType.AUTOPILOT_VU_CANCEL,
                                 vuMsgId=vu_msg_id))
        except Exception as e:
            logger.debug('[Autopilot %s] vu_cancel emit failed: %s', tid, e)
        return None
    if task.get('aborted'):
        logger.info('[Autopilot %s] Aborted while VU was running — stopping', tid)
        try:
            append_event(task, build_event(EventType.AUTOPILOT_VU_CANCEL,
                                 vuMsgId=vu_msg_id))
        except Exception as e:
            logger.debug('[Autopilot %s] vu_cancel emit failed: %s', tid, e)
        return None

    # VU produced a reply — NOW commit it to the conv DB.  But FIRST make
    # sure the parent's final assistant reply is committed: on the
    # runtime-arm path (autopilot flipped on mid-stream) the orchestrator's
    # pre-hook sync may have been skipped (it gates on is_autopilot_enabled
    # evaluated a few lines earlier), so do it here too — idempotent.
    _presync_parent_reply(task)
    vu_msg = _append_vu_message_to_conv(
        conv_id, vu_msg_id, vu_text, rounds=vu_rounds, run_id=run_id,
        segments=vu_segments,
    )
    if vu_msg is None:
        return None

    # Server-side auto-translate safety net for the VU turn — the append path
    # above is SEPARATE from manager._sync_result_to_conversation (which owns
    # the assistant/critic safety net), so without this a VU turn is left
    # untranslated unless a viewer fires a manual translate. Row index resolved
    # from the persisted _msgId (not guessed); best-effort, never blocks.
    _maybe_auto_translate_vu(conv_id, vu_msg_id, vu_text)

    # Tell the frontend the VU bubble is fully baked.  Carries the
    # final content + rounds so a client that lazily built the bubble
    # from streaming deltas — or one that missed them entirely (cold
    # replay, late connect) — can reconcile in one shot.
    try:
        append_event(task, build_event(
            EventType.AUTOPILOT_VU_DONE,
            vuMsgId=vu_msg_id,
            vuMessage=vu_msg,
        ))
    except Exception as e:
        logger.debug('[Autopilot %s] vu_done emit failed: %s', tid, e)

    # ★ BUDGET / STUCK GUARD — the mechanical backstop the loop historically
    #   lacked ("No turn cap, no state-change watchdog").  The VU turn is now
    #   persisted (its reply is preserved in history), so we count it, then
    #   decide whether the run may spawn the NEXT follow-up.  The counters are
    #   pinned per-run in settings (durable across the recursive follow-up
    #   tasks AND a crash+kick-resume), so a run that hit its turn ceiling or
    #   emitted N near-identical nudges STOPS here instead of looping forever.
    #   The check is FAIL-OPEN (a settings glitch never wedges a healthy loop).
    # Worker's touched-file set THIS turn (the churn signal for the
    # no-progress guard). modifiedFileList is populated on the parent task by
    # the orchestrator before this hook runs (orchestrator.py ~L785).
    _turn_targets = [f.get('path') for f in (task.get('modifiedFileList') or [])
                     if isinstance(f, dict) and f.get('path')]
    budget = _record_vu_turn_and_check_budget(conv_id, vu_text,
                                              targets=_turn_targets)
    if budget.get('stop'):
        reason = budget.get('reason') or 'budget_exhausted'
        logger.warning('[Autopilot %s] run STOPPED by guard (reason=%s, turn=%d) '
                       '— escalating as unfinished/needs-review', tid, reason,
                       budget.get('turn', 0))
        # Same CONCLUDE machinery as a clean close-out — record stamped
        # incomplete, feed pulse + run_concluded SSE emitted so the run folds
        # and the loop disarms — but with NO report: an abnormal end is not a
        # normal end, and the objective is unverified, so we do NOT spend an
        # LLM turn writing a debrief. The fold renders "stopped early — needs
        # review" over the last VU turn (the visible tail). This mirrors the
        # report-less manual-stop arm (conclude_run).
        try:
            _emit_run_concluded_event(task, conv_id, run_id, reason=reason)
        except Exception as e:
            logger.warning('[Autopilot %s] incomplete-conclude failed '
                           '(non-fatal): %s', tid, e, exc_info=True)
        try:
            from lib.message_queue import clear_autopilot_marker
            clear_autopilot_marker(conv_id)
        except Exception as e:
            logger.debug('[Autopilot %s] marker clear failed: %s', tid, e)
        _clear_run_id(conv_id)
        return None

    # ★ FINAL supersede recheck — the last gate before we spawn a follow-up.
    #   Everything between the post-VU abort check above and here does real
    #   work that takes wall-clock time (parent pre-sync, VU-message DB commit,
    #   the _maybe_auto_translate_vu LLM call, the budget bookkeeping). A user
    #   action that supersedes this run — a concurrent regenerate / edit / send
    #   — lands in exactly that window: it calls abort_running_tasks_for_conv
    #   (stamping this parent task['aborted']=True) and create_task (registering
    #   its OWN task as _conv_latest_task). Without this recheck we would still
    #   call _start_followup_task, whose create_task supersede invariant would
    #   then ABORT the user's just-started task — "autopilot snipes the user's
    #   regen". Re-read BOTH signals (aborted flag + latest-task registry) at
    #   the last possible moment and stand down if either says we were
    #   superseded. The already-persisted VU turn stays in history (harmless);
    #   we simply don't chain another turn on top of the user's action.
    if task.get('aborted'):
        logger.info('[Autopilot %s] Superseded (task aborted) just before '
                    'follow-up spawn — standing down', tid)
        return None
    if _successor_already_running(task, conv_id):
        logger.info('[Autopilot %s] Superseded (a newer task owns conv=%s) just '
                    'before follow-up spawn — standing down', tid, conv_id[:8])
        return None

    next_task_id = _start_followup_task(task, conv_id)
    if next_task_id is None:
        return None

    # Tell ``_dispatch_queued_message`` (which runs slightly after us
    # inside ``persist_task_result``) that autopilot already spawned a
    # successor for this conversation.  Otherwise a real user message
    # that landed in the tiny window between our post-VU queue re-check
    # and now would race-spawn its own task and abort our follow-up.
    # The queued message will be picked up when the autopilot follow-up
    # itself completes.
    task['_autopilot_spawned_followup'] = next_task_id

    return {'next_task_id': next_task_id, 'vu_msg': vu_msg}


# ──────────────────────────────────────────────────────────────────
#  Kick from idle — start the VU loop on a FINISHED conversation
# ──────────────────────────────────────────────────────────────────

def _run_autopilot_kick(task: dict) -> None:
    """Carrier-task entry: run the VU hook directly, with NO worker turn.

    Used by the "push the conversation forward" gesture (empty-Enter on a
    finished conversation with autopilot ON).  Unlike a normal task, this
    carrier never calls the LLM as the assistant — the conversation already
    ended and the last message is the agent's reply, so the virtual user
    should answer it straight away.  We reuse the SAME end-of-turn hook the
    natural-stop path runs (``maybe_run_autopilot``): it emits the
    ``autopilot_vu_*`` stream, appends the synthetic user message, spawns the
    follow-up worker task, and returns the ``next_task_id`` / ``vu_msg``
    baton.  The baton rides out on this carrier's ``done`` event (and on
    ``task['_autopilot_followup']`` for the poll path) exactly as it does at
    a natural stop, so the frontend attaches to the follow-up with no extra
    plumbing.

    Invoked from ``orchestrator.run_task`` when ``task['_autopilot_kick']``
    is set.
    """
    from lib.tasks_pkg.manager import append_event, persist_task_result

    tid = task['id'][:8]
    # The carrier produces no assistant content of its own; flip to 'done'
    # immediately so the SSE generator / poll treat the (in-flight) autopilot
    # decision window correctly via the _autopilot_deciding latch below.
    task['status'] = 'done'

    done_evt = build_event(EventType.DONE)
    if task.get('model'):
        done_evt['model'] = task['model']

    task['_autopilot_deciding'] = True
    try:
        ap_result = maybe_run_autopilot(task)
        if ap_result:
            done_evt['autopilotNextTaskId'] = ap_result['next_task_id']
            done_evt['autopilotVuMessage'] = ap_result['vu_msg']
            # Same transport-agnostic stash as the natural-stop path so a
            # client that fell back to /api/chat/poll still gets the baton.
            task['_autopilot_followup'] = ap_result
            logger.info('[Autopilot kick %s] VU took over conv=%s → follow-up %s',
                        tid, task.get('convId', '')[:8],
                        ap_result['next_task_id'][:8])
        else:
            logger.info('[Autopilot kick %s] VU declined to take over conv=%s '
                        '(TASK_DONE / no eligible context)', tid,
                        task.get('convId', '')[:8])
    except Exception as e:
        # Failure → no baton will arrive; clear the latch so the stream can
        # finalize.  The success path keeps it set until AFTER append_event
        # (below) so the SSE generator never sees a terminal task before the
        # baton-carrying done event is buffered (same window as the
        # natural-stop path in orchestrator._finalize_and_emit_done).
        task['_autopilot_deciding'] = False
        logger.error('[Autopilot kick %s] hook raised: %s', tid, e, exc_info=True)

    append_event(task, done_evt)
    # Baton is now buffered — safe to let _task_terminal() report finished.
    task['_autopilot_deciding'] = False
    persist_task_result(task)


def kick_autopilot(conv_id: str, config: dict | None = None) -> dict:
    """Start the virtual-user loop on a conversation whose reply has finished.

    The "push it forward for me" gesture: the user chatted with autopilot ON,
    the turn ended, and they want the virtual user to keep the conversation
    going WITHOUT typing anything.  Because ``maybe_run_autopilot`` only runs
    as an end-of-turn hook (there is no live task to hang it on once the reply
    finished), we spawn a thin carrier task whose ``run_task`` short-circuits
    straight to :func:`_run_autopilot_kick`.

    Refuses (``taskId=None``) when a non-VU task is already ``running`` for the
    conversation — in that case the caller should ARM the live task instead
    (see :func:`arm_autopilot`), so we never double-drive the loop.

    Also persists ``settings.autopilotEnabled=true`` so subsequent manual
    sends keep looping, mirroring the arm route.

    Returns ``{'taskId': str}`` on success, or ``{'taskId': None, 'error':
    str}`` when there is nothing to kick (no conversation, empty history, or a
    task is already running).
    """
    if not conv_id:
        return {'taskId': None, 'error': 'conv_id is required'}

    # Refuse if a live (non-VU) task is already running — arm it instead.
    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        for t in tasks.values():
            if (t.get('convId') == conv_id
                    and t.get('status') == 'running'
                    and not t.get('_vu_subtask')):
                logger.info('[Autopilot kick] conv=%s already has a running '
                            'task %s — refusing kick (arm instead)',
                            conv_id[:8], t.get('id', '?')[:8])
                return {'taskId': None, 'error': 'task_already_running'}

    cfg = dict(config or {})
    cfg['autopilot'] = True
    cfg['endpointMode'] = False
    # Strip assistantMsgId here too: this kick config is the template every
    # follow-up copies (via task['config'] → _start_followup_task), so a stray
    # client-minted id must not seed the run. See the _start_followup_task
    # comment for the collision this prevents.
    for stale_key in (
        'excludeLast', 'toolHistory', 'contentPrefix',
        'checkpointToolRounds', 'checkpointUsage', 'checkpointApiRounds',
        'checkpointModifiedFiles', 'checkpointModifiedFileList',
        'assistantMsgId', 'msgId',
    ):
        cfg.pop(stale_key, None)

    from lib.tasks_pkg import create_task, spawn_task
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db

    api_messages = build_api_messages_from_db(conv_id, cfg)
    if api_messages is None:
        return {'taskId': None, 'error': 'conversation_not_found'}
    if not api_messages:
        return {'taskId': None, 'error': 'conversation_empty'}

    task = create_task(conv_id, api_messages, cfg)
    task['_autopilot_kick'] = True

    # Persist the setting so the loop keeps going on any later manual send.
    # Serialized read-merge-write (settings_store) so this doesn't clobber a
    # concurrent tool-state / autopilot settings write on the same row.
    try:
        from lib.conversations import set_conversation_settings
        set_conversation_settings(
            conv_id, {'autopilotEnabled': True, 'activeTaskId': task['id']})
    except Exception as e:
        logger.warning('[Autopilot kick] persist autopilotEnabled failed '
                       'conv=%s: %s', conv_id[:8], e)

    logger.info('[Autopilot kick] conv=%s spawning carrier task %s',
                conv_id[:8], task['id'][:8])
    audit_log('autopilot_kick', conv_id=conv_id, task_id=task['id'])
    spawn_task(task)
    return {'taskId': task['id']}


def resume_armed_autopilot_after_crash(
        extra_conv_ids: list[str] | None = None) -> list[str]:
    """Re-kick every autopilot run left armed when the server died.

    When the server dies while an autopilot follow-up is in flight, the
    end-of-turn hook (:func:`maybe_run_autopilot`) never finished: no VU reply
    was persisted, no follow-up spawned, and no ``done`` baton was emitted.
    Startup recovery (:func:`recover_stale_tasks_on_startup`) restores the
    interrupted assistant reply into the conversation, but it does NOT resume
    the loop — so the run is left settled-but-armed and only continues on the
    user's next manual send. This bridges that gap.

    SCOPE — the DURABLE armed-marker is the AUTHORITATIVE source, NOT the set of
    crash-recovered tasks. We enumerate :func:`list_armed_autopilot_convs` (every
    conv carrying a ``KIND_AUTOPILOT`` marker row, which survives restart) and
    re-kick each. This deliberately catches the armed-but-idle case that a
    recovered-tasks-only gate would MISS: a conversation armed from idle whose
    carrier never spawned, or whose reply already finished before the crash, has
    an armed marker but was never an interrupted task — so it is absent from
    ``recovered_conv_ids`` yet must still resume. ``extra_conv_ids`` (the
    recovery set) is unioned in for belt-and-braces, but the marker scan is what
    guarantees completeness.

    Only conversations with an armed marker are resumed — a run that concluded
    cleanly (``[VU: TASK_DONE]``) or was disarmed cleared its marker, so it is
    correctly left alone. ``kick_autopilot`` itself refuses (``taskId=None``) if
    a live non-VU task is already running for the conv, so calling it
    unconditionally is safe — no double-driving. Best-effort per conv: one
    failure never aborts the batch.

    Returns the list of conv_ids for which a resume carrier was spawned.
    """
    resumed: list[str] = []
    try:
        from lib.message_queue import (
            get_autopilot_marker_config,
            has_autopilot_marker,
            list_armed_autopilot_convs,
        )
    except Exception as e:
        logger.warning('[Autopilot] resume-after-crash: message_queue import '
                       'failed: %s', e)
        return resumed

    # Authoritative: every conv with a durable armed marker. Union the recovery
    # set only for logging symmetry — has_autopilot_marker re-gates each below,
    # so a recovered conv WITHOUT a marker (clean-closed / disarmed) is skipped.
    try:
        armed = set(list_armed_autopilot_convs())
    except Exception as e:
        logger.warning('[Autopilot] resume-after-crash: marker scan failed: %s', e)
        armed = set()
    candidates = armed | {c for c in (extra_conv_ids or []) if c}

    for conv_id in candidates:
        if not conv_id:
            continue
        try:
            if not has_autopilot_marker(conv_id):
                continue
            cfg = get_autopilot_marker_config(conv_id) or {}
            res = kick_autopilot(conv_id, cfg)
            new_tid = res.get('taskId')
            if new_tid:
                resumed.append(conv_id)
                logger.info('[Autopilot] Resumed armed run after crash for '
                            'conv=%s → carrier %s', conv_id[:8], new_tid[:8])
                audit_log('autopilot_resume_after_crash',
                          conv_id=conv_id, task_id=new_tid)
            else:
                logger.info('[Autopilot] resume-after-crash skipped conv=%s '
                            '(%s)', conv_id[:8], res.get('error', 'no task'))
        except Exception as e:
            logger.warning('[Autopilot] resume-after-crash failed for conv=%s: '
                           '%s', conv_id[:8], e, exc_info=True)
    return resumed


# ──────────────────────────────────────────────────────────────────
#  Runtime arming — turn autopilot on for an ALREADY-RUNNING task
# ──────────────────────────────────────────────────────────────────
#  Runtime arming — turn autopilot on for an ALREADY-RUNNING task
# ──────────────────────────────────────────────────────────────────

def arm_autopilot(conv_id: str) -> dict:
    """Arm autopilot for a conversation whose task is already in flight.

    Use case: the user chatted with autopilot OFF, then decides to step
    away mid-reply and wants the virtual user to take over at the next
    natural stop.  Toggling the frontend button only affects the NEXT
    task — the in-flight task's ``config['autopilot']`` was frozen at
    creation time, so its end-of-turn hook would never fire.

    This flips ``config['autopilot'] = True`` on every live (status=
    ``running``) task for the conversation.  Because ``_finalize_and_emit_done``
    re-reads ``is_autopilot_enabled(task)`` at finalize, the running task
    will now run the VU hook when it stops.  Mutating ``config`` (rather
    than a side flag) also means the value propagates to autopilot
    follow-ups via ``_start_followup_task``'s ``dict(task['config'])``,
    so the loop continues until the VU emits ``[VU: TASK_DONE]``.

    Endpoint-managed tasks are skipped — autopilot and endpoint mode are
    mutually exclusive (they share the same termination boundary).

    Returns ``{'armed': bool, 'taskIds': [...]}`` — ``armed`` is True iff
    at least one live task was flipped.  When no task is live (the reply
    already finished), ``armed`` is False and the caller should rely on
    the persisted ``autopilotEnabled`` setting to kick off the loop on the
    user's next send.
    """
    from lib.tasks_pkg.manager import tasks, tasks_lock

    armed_ids: list[str] = []
    marker_cfg: dict = {}
    endpoint_blocked = False
    with tasks_lock:
        # Pass 1 — mutual exclusion: if ANY live task for the conv is endpoint
        # mode, refuse to arm autopilot (they share the same termination
        # boundary; running both double-loops).
        for tid, t in tasks.items():
            if t.get('convId') != conv_id or t.get('status') != 'running':
                continue
            if t.get('_vu_subtask'):
                continue
            cfg = t.get('config')
            if t.get('_endpoint_managed') or (isinstance(cfg, dict) and cfg.get('endpointMode')):
                endpoint_blocked = True
                break
        # Pass 2 — flip config.autopilot on live non-endpoint tasks + capture
        # a config to seed the marker.
        if not endpoint_blocked:
            for tid, t in tasks.items():
                if t.get('convId') != conv_id or t.get('status') != 'running':
                    continue
                if t.get('_endpoint_managed') or t.get('_vu_subtask'):
                    continue
                cfg = t.get('config')
                if not isinstance(cfg, dict):
                    continue
                if not marker_cfg:
                    marker_cfg = dict(cfg)
                if not cfg.get('autopilot'):
                    cfg['autopilot'] = True
                    armed_ids.append(tid)

    if endpoint_blocked:
        logger.info('[Autopilot] Arm refused for conv=%s — endpoint mode is '
                    'live (mutually exclusive)', conv_id[:8])
        return {'armed': False, 'taskIds': [], 'markerAdded': False}

    # Persist the armed-marker sentinel in the queue so the arm survives a
    # page reload, shows in the queue bar (cancellable), and — critically —
    # keeps autopilot armed even when no task is live (the "I'll step away,
    # take over when the current reply finishes" gesture works whether or not
    # a reply is still streaming).  Idempotent: at most one marker per conv.
    marker_added = False
    try:
        from lib.message_queue import arm_autopilot_marker
        res = arm_autopilot_marker(conv_id, marker_cfg)
        marker_added = res.get('armed', False)
    except Exception as e:
        logger.warning('[Autopilot] failed to persist armed-marker for '
                       'conv=%s: %s', conv_id[:8], e)

    if armed_ids:
        logger.info('[Autopilot] Armed %d live task(s) for conv=%s: %s '
                    '(marker_added=%s)', len(armed_ids), conv_id[:8],
                    [t[:8] for t in armed_ids], marker_added)
    else:
        logger.info('[Autopilot] Arm requested for conv=%s — no live task to '
                    'flip; persistent marker now governs (marker_added=%s)',
                    conv_id[:8], marker_added)
    audit_log('autopilot_armed', conv_id=conv_id, task_ids=armed_ids,
              marker_added=marker_added)

    # ``armed`` reflects whether autopilot is now armed for the conv — True if
    # a live task was flipped OR a marker is in place.
    armed = bool(armed_ids) or marker_added or _marker_exists(conv_id)
    return {'armed': armed, 'taskIds': armed_ids, 'markerAdded': marker_added}


def _marker_exists(conv_id: str) -> bool:
    try:
        from lib.message_queue import has_autopilot_marker
        return has_autopilot_marker(conv_id)
    except Exception as e:
        logger.debug('[Autopilot] _marker_exists probe failed for conv=%s: %s',
                     conv_id[:8] if conv_id else '?', e)
        return False


def disarm_autopilot(conv_id: str) -> dict:
    """Cancel autopilot for a conversation: clear the marker + live config.

    The inverse of :func:`arm_autopilot`.  Removes the persistent armed-marker
    sentinel AND flips ``config['autopilot']=False`` on any live task so the
    loop stops at the current turn's natural end.  Used by the queue-bar
    cancel button and the toggle-OFF gesture.

    Returns ``{disarmed, markerCleared, taskIds}``.
    """
    from lib.tasks_pkg.manager import tasks, tasks_lock

    marker_cleared = False
    try:
        from lib.message_queue import clear_autopilot_marker
        marker_cleared = clear_autopilot_marker(conv_id)
    except Exception as e:
        logger.warning('[Autopilot] disarm: marker clear failed for conv=%s: %s',
                       conv_id[:8], e)

    cleared_ids: list[str] = []
    with tasks_lock:
        for tid, t in tasks.items():
            if t.get('convId') != conv_id or t.get('_vu_subtask'):
                continue
            cfg = t.get('config')
            if isinstance(cfg, dict) and cfg.get('autopilot'):
                cfg['autopilot'] = False
                cleared_ids.append(tid)

    # ★ Symmetric close-out — the manual-stop arm of the conclude contract.
    #   Historically disarm was "dumb": it cleared the marker/flag but emitted
    #   NO run-level fact, forcing the frontend to INFER run-end from stream
    #   absence (the inter-turn-gap heuristic behind premature folds). Now we
    #   write the BACKEND-AUTHORITATIVE concluded record (reason=stopped, no
    #   report) so the fold keys on a durable fact — and return it so the
    #   calling client (which may have NO live SSE stream, the idle-disarm
    #   case) can fold instantly without a reload. Self-guards: no run id →
    #   None (nothing was ever an autopilot run to conclude).
    concluded = None
    try:
        concluded = conclude_run(conv_id, reason='stopped')
    except Exception as e:
        logger.warning('[Autopilot] disarm: conclude_run failed for conv=%s: %s',
                       conv_id[:8], e, exc_info=True)

    logger.info('[Autopilot] Disarmed conv=%s (markerCleared=%s, tasks=%s, concluded=%s)',
                conv_id[:8], marker_cleared, [t[:8] for t in cleared_ids],
                bool(concluded))
    audit_log('autopilot_disarmed', conv_id=conv_id,
              marker_cleared=marker_cleared, task_ids=cleared_ids,
              concluded=bool(concluded))
    result = {'disarmed': marker_cleared or bool(cleared_ids),
              'markerCleared': marker_cleared, 'taskIds': cleared_ids}
    if concluded is not None:
        result['runConcluded'] = concluded
    return result
