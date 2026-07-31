"""Autopilot state helpers — objective / run-id / budget / resolvers.

**Extraction context** (board epic ``pt_00459503f23b4c0e``, slice 1):
carved out of ``lib/tasks_pkg/autopilot.py`` per
``docs/AUTOPILOT_DECOMPOSITION_AUDIT.md``. Chose a SIBLING module
(``autopilot_state.py``) rather than a full module→package conversion
(``autopilot/_state.py``) for slice 1: converting a heavily-imported module
into a package on a shared-HEAD cross-sibling worktree carries much bigger
merge risk than adding one new sibling file, and the wire-parity contract
(re-export identity through ``autopilot.py``) is byte-equivalent either way.

**Sequencing constraint (pt_8dc03017 gate)**: the sibling epic
``pt_8dc030176bad450b`` (owner-parked, human-gated) plans to mutate
``_VUEventForwarder``, the ``_autopilot_deciding`` latch, and the VU
``convId=''`` opt-out. This module DELIBERATELY carries NONE of those
symbols — the extracted cluster is the "Objective + budget + resolvers"
group the audit identified as ZERO-overlap with the pt_8dc03017 cutover.
A future dispatch (post-cutover) can consolidate ``autopilot_state.py`` +
the remaining unmoved clusters (baton, VU, markers) into an
``autopilot/`` package.

**What's in here**: all pure-ish state read/mint/reset helpers whose
side effects are limited to ``conversations.settings`` writes via
``update_conversation_settings``:

  * :func:`_extract_objective` — pure list scan, no I/O.
  * :func:`_extract_objective_from_db` — DB read via
    ``conv_message_builder._load_messages_from_db``.
  * :func:`_get_or_persist_objective` — settings read-through mint.
  * :func:`_get_or_persist_run_id` — settings read-through mint.
  * :func:`_record_vu_turn_and_check_budget` — budget-guard RMW.
  * :func:`_clear_run_id` — run-end cleanup.
  * :func:`_resolve_recent_run_id` — DB reader.
  * :func:`_resolve_run_anchor_msgid` — DB reader.
  * Module constants ``_VU_HISTORY_CAP`` / ``_PROGRESS_LEDGER_CAP``.

All private ("_"-prefixed) — internal to the autopilot package; the
facade module ``lib.tasks_pkg.autopilot`` re-exports every symbol so
existing ``from lib.tasks_pkg.autopilot import _X`` call sites and
``monkeypatch.setattr(ap, '_X', ...)`` patch points keep working
byte-identically.
"""

from __future__ import annotations

import json
import uuid

from lib.log import audit_log, get_logger

logger = get_logger(__name__)


# ── Per-run budget caps ─────────────────────────────────────────────
#
# Per-run budget state lives in settings alongside the run pins so it is
# DURABLE across the recursive follow-up tasks (the loop spans separate tasks,
# not one function) AND across a server crash + kick-resume: the counters are
# keyed to ``autopilotRunId`` and cleared together with it in ``_clear_run_id``,
# so a resumed run CONTINUES its count rather than restarting at 0 (a
# crash-looping run must not evade the cap).  Bounded history keeps the settings
# blob small.
_VU_HISTORY_CAP = 6


_PROGRESS_LEDGER_CAP = 8


# ── Objective extraction ────────────────────────────────────────────


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


# ── Budget guard ────────────────────────────────────────────────────


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


# ── Run resolvers (DB reads) ────────────────────────────────────────


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
        except (json.JSONDecodeError, TypeError) as _e:
            logger.debug('resolve run anchor msgid: malformed JSON/unexpected type (%s)', _e)
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


__all__ = [
    '_VU_HISTORY_CAP',
    '_PROGRESS_LEDGER_CAP',
    '_extract_objective',
    '_extract_objective_from_db',
    '_get_or_persist_objective',
    '_get_or_persist_run_id',
    '_record_vu_turn_and_check_budget',
    '_clear_run_id',
    '_resolve_recent_run_id',
    '_resolve_run_anchor_msgid',
]
