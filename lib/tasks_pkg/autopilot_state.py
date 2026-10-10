"""Historical Autopilot objective/budget compatibility helpers.

Live Goal lifecycle and run-pin resolution are owned by lib.goal_runs.
Legacy imports below are identity-preserving aliases to those owners; only
retired objective/budget helpers remain local for historical extensions.
"""

from __future__ import annotations

import uuid

from lib.goal_runs.run_pins import (
    clear_run_id as _clear_run_id,
    _snapshot_pinned_run_id as _snapshot_pinned_run_id,
    _latest_stamped_run_id as _latest_stamped_run_id,
    _snapshot_has_unloaded_prefix as _snapshot_has_unloaded_prefix,
    _anchor_from_messages as _anchor_from_messages,
    resolve_recent_run_id as _resolve_recent_run_id,
    resolve_run_anchor_turn_id as _resolve_run_anchor_turn_id,
)

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


# ── Objective extraction ────────────────────────────────────────────


def _extract_objective(messages: list) -> str:
    """Return the original objective = the FIRST real user message text.

    Skips VU directive turns (``_isVuDirective``) and synthetic virtual-user
    turns (``_isVirtualUser``) so the anchor is always the human's opening
    ask, never an autopilot-generated reply.  Returns '' when none found.
    """
    from lib.tasks_pkg.compaction._history import history_entries, user_text
    for message in messages or []:
        for entry in history_entries(message):
            if entry['kind'] == 'opening_request':
                return user_text(entry).strip()
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


from lib.goal_runs.run_pins import update_objective_from_receipt as _update_objective_from_receipt


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
