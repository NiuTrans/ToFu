"""Autopilot state helpers — objective / run-id / budget / resolvers.

**Extraction context** (board epic ```, slice 1):
carved out of ``lib/tasks_pkg/autopilot.py`` per
``docs/modules/task_engine.md``. Chose a SIBLING module
(``autopilot_state.py``) rather than a full module→package conversion
(``autopilot/_state.py``) for slice 1: converting a heavily-imported module
into a package on a shared-HEAD cross-sibling worktree carries much bigger
merge risk than adding one new sibling file, and the wire-parity contract
(re-export identity through ``autopilot.py``) is byte-equivalent either way.

**Sequencing constraint ( gate)**: the sibling epic
``` (owner-parked, human-gated) plans to mutate
``_VUEventForwarder``, the ``_autopilot_deciding`` latch, and the VU
``convId=''`` opt-out. This module DELIBERATELY carries NONE of those
symbols — the extracted cluster is the "Objective + budget + resolvers"
group the audit identified as ZERO-overlap with the  cutover.
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
  * :func:`_update_objective_from_receipt` — re-pin from an L2 receipt.
  * :func:`_get_or_persist_run_id` — settings read-through mint.
  * :func:`_record_vu_turn_and_check_budget` — budget-guard RMW.
  * :func:`_clear_run_id` — run-end cleanup.
  * :func:`_resolve_recent_run_id` — DB reader.
  * :func:`_resolve_run_anchor_turn_id` — stable turn-identity reader.
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


# Run stamps and their boundary follow-ups are normally near the transcript
# tail. The repository preserves exact fallback when an older run lies outside
# this bounded first read.
_AUTOPILOT_RESOLVER_MESSAGE_WINDOW = 128


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


def _extract_objective_from_db(conv_id: str, *, user_id: int) -> str:
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
        from lib.tasks_pkg.conv_message_builder._load import _load_messages_from_db
        raw = _load_messages_from_db(conv_id, user_id=user_id)
    except Exception as e:
        logger.debug('[Autopilot] objective DB read failed conv=%s: %s',
                     conv_id[:8], e)
        return ''
    if not raw:
        return ''
    return _extract_objective(raw)


def _get_or_persist_objective(
    conv_id: str,
    messages: list,
    *,
    user_id: int,
) -> str:
    """Resolve the pinned autopilot objective for a conversation.

    The objective is the north star the virtual user measures the assistant
    against.  It is captured ONCE (the first real user message) and pinned to
    ``settings.autopilotObjective`` so every follow-up task's VU sees the SAME
    anchor even after compaction has trimmed the early conversation history.
    The pin is NOT frozen: :func:`_update_objective_from_receipt` re-pins it
    when an L2 compaction receipt records a newer binding human goal, so the
    VU tracks goal replacement instead of measuring against a stale opening
    ask.

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
            objective = (_extract_objective_from_db(conv_id, user_id=user_id)
                         or _extract_objective(messages))
            out['objective'] = objective
            if not objective:
                return False  # nothing worth pinning
            settings['autopilotObjective'] = objective
            logger.info('[Autopilot] conv=%s pinned objective (%d chars)',
                        conv_id[:8], len(objective))
            return None  # proceed with the write

        # notify=False: autopilotObjective is internal run-bookkeeping, never
        # rendered — invalidate the (now-stale) cache blob but don't push.
        res = update_conversation_settings(
            conv_id, _mut, user_id=user_id, notify=False)
        if res is None:
            # Conv row absent — derive without persisting (original behaviour).
            return _extract_objective(messages)
        return out['objective']
    except Exception as e:
        logger.warning('[Autopilot] objective resolve failed conv=%s: %s — '
                       'deriving from live messages', conv_id[:8], e)
        return _extract_objective(messages)


def _update_objective_from_receipt(
    conv_id: str,
    objective: str,
    *,
    user_id: int,
) -> bool:
    """Re-pin ``settings.autopilotObjective`` from an L2 compaction receipt.

    The receipt's ``### Objective`` is model-authored from the full verbatim
    user-message evidence, so it reflects the CURRENT effective goal —
    including a human explicitly replacing their opening ask. When it differs
    from the pin, overwrite the pin so the virtual user measures the
    assistant against the latest binding human goal rather than a stale
    opening request.

    No-ops (returns False): empty conv_id/objective, NO existing pin (never
    mint one here — pinning is :func:`_get_or_persist_objective`'s job, so
    non-autopilot conversations stay untouched), an identical pin, or a
    missing conversation row. All failures are non-fatal — compaction must
    never fail because run bookkeeping couldn't update.
    """
    objective = (objective or '').strip()
    if not conv_id or not objective:
        return False
    try:
        from lib.conversations import update_conversation_settings
        out = {'updated': False}

        def _mut(settings):
            existing = (settings.get('autopilotObjective') or '').strip()
            if not existing or existing == objective:
                return False  # no pin to refresh / already current — skip write
            settings['autopilotObjective'] = objective
            out['updated'] = True
            logger.info('[Autopilot] conv=%s objective re-pinned from L2 '
                        'receipt (%d → %d chars)',
                        conv_id[:8], len(existing), len(objective))
            return None

        # notify=False: same internal-bookkeeping convention as the mint path.
        res = update_conversation_settings(
            conv_id, _mut, user_id=user_id, notify=False)
        if res is None:
            return False
        return out['updated']
    except Exception as e:
        logger.warning('[Autopilot] objective re-pin failed conv=%s: %s',
                       conv_id[:8], e)
        return False


def _get_or_persist_run_id(conv_id: str, *, user_id: int) -> str:
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
        res = update_conversation_settings(
            conv_id, _mut, user_id=user_id, notify=False)
        if res is None:
            return new_id  # conv row absent → ephemeral id (original behaviour)
        return out['id']
    except Exception as e:
        logger.warning('[Autopilot] runId resolve failed conv=%s: %s — '
                       'using ephemeral id', conv_id[:8], e)
        return new_id


# ── Budget guard ────────────────────────────────────────────────────


def _record_vu_turn_and_check_budget(
    conv_id: str,
    vu_text: str,
    *,
    user_id: int,
    targets: list | None = None,
) -> dict:
    """Increment the run's VU turn count + append its request text, then verdict.

    Serialized read-merge-write through ``update_conversation_settings`` (never
    a bare RMW — see settings-column convention) so the increment doesn't
    clobber a concurrent ``activeTaskId`` / objective / summaries write on the
    same row.  The counters are pinned under ``autopilotTurnCount`` +
    ``autopilotVuHistory`` + ``autopilotProgress``, all cleared with the run
    pins in ``_clear_run_id``.

    ``targets`` is the set of files the WORKER touched this turn
    (``task['modifiedFileList']`` paths) — the churn signal for the
    advisory diminishing-returns ledger. The VU reply's ``[PROGRESS:
    resolved=X remaining=Y]`` line supplies its structured progress signal.

    Returns ``{'stop': bool, 'reason': str, 'turn': int}`` — ``reason`` is
    ``'budget_exhausted'`` (turn ceiling), else ''. Similar VU wording and the
    structured progress ledger are retained as bounded diagnostics but are
    never stop conditions: neither prose similarity nor several edits without
    a newly completed acceptance criterion can prove that work failed to
    advance. FAIL-OPEN: any error resolving/persisting returns no-stop so a
    settings glitch never wedges a healthy loop.
    """
    out = {'stop': False, 'reason': '', 'turn': 0}
    if not conv_id:
        return out
    try:
        from lib.agent_verdict import (
            autopilot_max_turns,
            parse_progress,
        )
        from lib.conversations import update_conversation_settings

        max_turns = autopilot_max_turns()
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
            return None  # always persist the incremented counters

        # notify=False: turn-count / VU-history / progress ledger are internal
        # budget bookkeeping, not rendered — invalidate cache but don't push.
        res = update_conversation_settings(
            conv_id, _mut, user_id=user_id, notify=False)
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


def _clear_run_id(conv_id: str, *, user_id: int) -> None:
    """Clear the pinned run id + budget counters when a run concludes.

    Called on TASK_DONE (after the summary is generated) so the NEXT autopilot
    run on the same conversation mints a fresh ``autopilotRunId`` AND resets its
    turn budget / VU history / progress ledger.  Clearing the budget counters
    ATOMICALLY with the run id (one serialized write) is what guarantees a fresh
    run always starts clean — and, conversely, that a run still in progress
    keeps its accumulated count.

    Hole A — ``autopilotObjective`` is DELIBERATELY NOT cleared here.  The
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
        update_conversation_settings(
            conv_id, _mut, user_id=user_id, notify=False)
    except Exception as e:
        logger.debug('[Autopilot] _clear_run_id failed conv=%s: %s', conv_id[:8], e)


# ── Run resolvers (DB reads) ────────────────────────────────────────


def _snapshot_pinned_run_id(snapshot) -> str:
    """Return a validated settings pin from one conversation snapshot."""
    raw_settings = snapshot.get('settings')
    try:
        settings = (
            dict(raw_settings)
            if isinstance(raw_settings, dict)
            else json.loads(raw_settings or '{}')
        ) if raw_settings else {}
    except (json.JSONDecodeError, TypeError) as error:
        logger.debug(
            '[Autopilot] settings JSON parse failed, using fallback: %s',
            error,
        )
        settings = {}
    return str(settings.get('autopilotRunId') or '').strip()


def _latest_stamped_run_id(messages: list) -> str:
    """Return the newest non-empty autopilot run stamp in ``messages``."""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        run_id = str(message.get('_autopilotRunId') or '').strip()
        if run_id:
            return run_id
    return ''


def _snapshot_has_unloaded_prefix(snapshot) -> bool:
    """Whether a bounded snapshot proves that older messages were omitted."""
    messages = snapshot.messages
    raw_count = snapshot.get('msg_count')
    if (
        isinstance(raw_count, int)
        and not isinstance(raw_count, bool)
        and raw_count >= 0
    ):
        return raw_count > len(messages)
    # Compatibility fakes/older authorities may omit msg_count. A completely
    # filled window is ambiguous and therefore takes the correctness fallback.
    return len(messages) >= _AUTOPILOT_RESOLVER_MESSAGE_WINDOW


def _anchor_from_messages(messages: list, run_id: str) -> tuple[bool, str]:
    """Return ``(stamp_found, boundary_turn_id)`` for one message projection."""
    stamped_idx = -1
    for index, message in enumerate(messages):
        if (
            isinstance(message, dict)
            and str(message.get('_autopilotRunId') or '').strip() == run_id
        ):
            stamped_idx = index
    if stamped_idx < 0:
        return False, ''
    boundary = stamped_idx
    for index in range(stamped_idx + 1, len(messages)):
        message = messages[index]
        if not isinstance(message, dict):
            break
        if str(message.get('_autopilotRunId') or '').strip():
            break
        if message.get('role') == 'user' and not message.get('_isVirtualUser'):
            break
        boundary = index
    anchor = messages[boundary]
    turn_id = (
        str(anchor.get('_turnId') or '').strip()
        if isinstance(anchor, dict)
        else ''
    )
    return True, turn_id


def _resolve_recent_run_id(conv_id: str, *, user_id: int) -> str:
    """Return the most recent VU turn's ``_autopilotRunId`` for a conversation.

    Prefers the still-pinned ``settings.autopilotRunId`` (the live run); falls
    back to scanning the message tail for the newest ``_autopilotRunId`` stamp
    (an already-disarmed run whose pin was cleared). Returns '' when the
    conversation has no autopilot run at all. Best-effort — failures return ''.
    """
    if not conv_id:
        return ''
    try:
        from lib.conversations.repository import get_conversation
        metadata = get_conversation(
            conv_id, user_id=user_id, include_messages=False)
        if metadata is None:
            return ''
        pinned = _snapshot_pinned_run_id(metadata)
        if pinned:
            return pinned

        tail = get_conversation(
            conv_id,
            user_id=user_id,
            message_window=_AUTOPILOT_RESOLVER_MESSAGE_WINDOW,
        )
        if tail is None:
            return ''
        # A run can be pinned between the metadata and tail reads.
        pinned = _snapshot_pinned_run_id(tail)
        if pinned:
            return pinned
        run_id = _latest_stamped_run_id(tail.messages)
        if run_id or not _snapshot_has_unloaded_prefix(tail):
            return run_id

        snapshot = get_conversation(conv_id, user_id=user_id)
        if snapshot is None:
            return ''
        return (
            _snapshot_pinned_run_id(snapshot)
            or _latest_stamped_run_id(snapshot.messages)
        )
    except Exception as e:
        logger.debug('[Autopilot] _resolve_recent_run_id failed conv=%s: %s',
                     conv_id[:8], e)
    return ''


def _resolve_run_anchor_turn_id(
    conv_id: str,
    run_id: str,
    *,
    user_id: int,
) -> str:
    """Resolve the stable ``_turnId`` of a run's boundary turn.

    This is the backend authority for report PLACEMENT. The boundary is the
    last turn belonging to the run: the run's VU turn, EXTENDED forward over the
    trailing unstamped agent follow-up(s) it prompted, stopping at the next
    run's VU turn / a real (non-VU) human turn / end-of-list. Returns that
    turn's ``_turnId`` so report placement never depends on an array index or
    on a second message-identity namespace.

    Returns '' when the run has no turn on disk, or its boundary turn carries no
    ``_turnId`` (cannot anchor without a stable id — the caller then omits the
    anchor and the frontend uses its ts-tail last resort). Best-effort — any
    failure returns ''.
    """
    if not conv_id or not run_id:
        return ''
    try:
        from lib.conversations.repository import get_conversation
        snapshot = get_conversation(
            conv_id,
            user_id=user_id,
            message_window=_AUTOPILOT_RESOLVER_MESSAGE_WINDOW,
        )
        if snapshot is None:
            return ''
        found, anchor = _anchor_from_messages(snapshot.messages, run_id)
        if found or not _snapshot_has_unloaded_prefix(snapshot):
            return anchor

        full_snapshot = get_conversation(conv_id, user_id=user_id)
        if full_snapshot is None:
            return ''
        _found, anchor = _anchor_from_messages(
            full_snapshot.messages, run_id)
        return anchor
    except Exception as e:
        logger.debug('[Autopilot] _resolve_run_anchor_turn_id failed conv=%s run=%s: %s',
                     conv_id[:8], run_id, e)
        return ''


__all__ = [
    '_VU_HISTORY_CAP',
    '_PROGRESS_LEDGER_CAP',
    '_extract_objective',
    '_extract_objective_from_db',
    '_get_or_persist_objective',
    '_update_objective_from_receipt',
    '_get_or_persist_run_id',
    '_record_vu_turn_and_check_budget',
    '_clear_run_id',
    '_resolve_recent_run_id',
    '_resolve_run_anchor_turn_id',
]
