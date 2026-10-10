"""Goal-run pin state helpers, relocated from the retired standalone autopilot.

These resolvers back the LIVE Flow close-out path: the pinned run id settings
blob, the boundary-turn anchor used for fold-report placement, and the pin
cleanup on run conclusion. Wire names (``settings.autopilotRunId``,
``_autopilotRunId`` message stamps) are unchanged on purpose — the durable
records and the frontend fold read them. Log strings stay byte-identical to
the pre-relocation implementation for ops continuity.
"""
from __future__ import annotations

import json

from lib.log import get_logger

logger = get_logger(__name__)

# Bounded tail window for run-id / anchor resolution before falling back to a
# full transcript read (see docs/TASK_EXECUTION_MODES.md close-out resolvers).
_RESOLVER_MESSAGE_WINDOW = 128


def clear_run_id(conv_id: str, *, user_id: int) -> None:
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
        logger.debug('[Autopilot] clear_run_id failed conv=%s: %s', conv_id[:8], e)


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
    return len(messages) >= _RESOLVER_MESSAGE_WINDOW


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


def resolve_recent_run_id(conv_id: str, *, user_id: int) -> str:
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
            message_window=_RESOLVER_MESSAGE_WINDOW,
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
        logger.debug('[Autopilot] resolve_recent_run_id failed conv=%s: %s',
                     conv_id[:8], e)
    return ''


def resolve_run_anchor_turn_id(
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
            message_window=_RESOLVER_MESSAGE_WINDOW,
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
        logger.debug('[Autopilot] resolve_run_anchor_turn_id failed conv=%s run=%s: %s',
                     conv_id[:8], run_id, e)
        return ''


__all__ = [
    'clear_run_id',
    'resolve_recent_run_id',
    'resolve_run_anchor_turn_id',
]


def update_objective_from_receipt(
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
