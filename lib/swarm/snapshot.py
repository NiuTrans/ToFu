"""lib/swarm/snapshot.py — durable swarm agent snapshot persistence.

The swarm "Parallel Execution" panel's per-agent state (``_swarmAgents``)
is synthesized live on the FRONTEND from ``swarm_*`` SSE events and is never
persisted. After a reload it is gone, so the panel could only rebuild
objective-only stubs from the spawn handle, and fire-and-forget swarms
(spawned but never ``await_agents``-ed) rendered every agent as ``unknown``.

When the swarm settles (and incrementally as each agent completes), the
authoritative per-agent state from the ``MasterOrchestrator`` is written onto
the matching ``spawn_agents`` tool round in the assistant turn projection.
On reload the frontend prefers this snapshot (``round._swarmSnapshot``)
and renders a faithful, fully-expandable
panel with real status/preview/tokens/elapsed/modifiedFiles — even with no
``await_agents`` sibling round and no live ``_swarmAgents`` array.

The write is best-effort and CAS-guarded: it must never raise into the swarm
driver thread or clobber the running attempt that owns a live projection.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from lib.log import get_logger
from lib.tools.result_envelope import sparse_result_items

logger = get_logger(__name__)

#: How many optimistic-lock retries before giving up the durable write.
#: Paired with incremental backoff in persist_snapshot_to_conversation so a
#: busy row gets several real chances rather than a tight spin.
_MAX_CAS = 6


def _unwrap_result_payload(payload: Any) -> Any:
    """Accept the spawn handle in every persisted recording shape.

    Rounds persist the sparse ``summary_items`` model projection
    (``{"summary": ..., "items": [handle]}`` — ``_model_projection`` in
    lib/tools/result_envelope.py intentionally drops ``contractVersion``
    from that projection), a full ``tofu.tool-result/v2`` envelope, or the
    bare handle directly. All three shapes match; gating on the marker
    alone recovered zero agents and left the reloaded panel empty.
    """
    items = sparse_result_items(payload)
    if items is None:
        return payload
    for item in items:
        if isinstance(item, dict) and (
                item.get('agent_id')
                or any(isinstance(item.get(key), list)
                       for key in ('agents', 'completed', 'results'))):
            return item
    if items and isinstance(items[0], dict):
        return items[0]
    return payload


def _round_handle_ids(round_entry: dict) -> set[str]:
    """Return the set of agent ids referenced by a spawn round's handle.

    The persisted ``spawn_agents`` round stores the launch handle JSON in
    ``toolContent`` (``{agents:[{id, ...}]}``). Returns an empty set when the
    round isn't a parseable spawn handle.
    """
    if not isinstance(round_entry, dict):
        return set()
    if round_entry.get('toolName') != 'spawn_agents':
        return set()
    raw = round_entry.get('toolContent')
    if not isinstance(raw, str) or not raw:
        return set()
    try:
        handle = json.loads(raw)
    except (ValueError, TypeError) as e:
        logger.debug('[SwarmSnapshot] spawn handle JSON parse failed: %s', e)
        return set()
    handle = _unwrap_result_payload(handle)
    agents = handle.get('agents') if isinstance(handle, dict) else None
    if not isinstance(agents, list):
        return set()
    return {a.get('id') for a in agents
            if isinstance(a, dict) and a.get('id')}


def _snapshot_version(snap) -> int:
    """Monotonic ordering key for a snapshot (higher = newer/more-complete).

    The producer must provide the explicit ``version`` field
    (settled*100000 + doneCount). Missing or malformed snapshots rank below
    every valid snapshot.
    """
    if not isinstance(snap, dict):
        return -1
    v = snap.get('version')
    if isinstance(v, int):
        return v
    return -1


def filter_snapshot(snapshot: dict, keep_ids: set) -> dict:
    """Return a snapshot restricted to *keep_ids* (#4 multi-wave scoping).

    A follow-up ``spawn_agents`` in the same conversation merges both waves
    into ``master.specs``, so ``_build_agent_snapshot`` emits ONE snapshot
    spanning every wave. Stamping that combined snapshot onto a single round
    would make wave-1's panel show wave-2's agents (or never upgrade). We
    therefore stamp EACH spawn round with only the agents its own handle
    launched. Recomputes the derived counts/version over the kept subset so
    the monotonic guard stays correct per panel.

    NOTE: agent dicts are carried through BY REFERENCE, so every per-agent
    field (including ``startedAt``, the running stopwatch's anchor) survives
    this rewrite automatically. Do not switch to rebuilding agent dicts field
    by field here — that is exactly how a per-agent field silently goes
    missing on the reload path.
    """
    if not isinstance(snapshot, dict):
        return snapshot
    agents = [a for a in (snapshot.get('agents') or [])
              if isinstance(a, dict) and a.get('id') in keep_ids]
    done_count = sum(1 for a in agents
                     if a.get('status') in ('done', 'failed', 'aborted'))
    total_tokens = sum((a.get('tokens') or 0) for a in agents
                       if isinstance(a.get('tokens'), int))
    settled = bool(snapshot.get('settled'))
    return {
        'agents':      agents,
        'settled':     settled,
        'totalTokens': total_tokens,
        'agentCount':  len(agents),
        'doneCount':   done_count,
        'version':     (1 if settled else 0) * 100000 + done_count,
    }


def stamp_round(round_entry: dict, snapshot: dict) -> bool:
    """Stamp *snapshot* onto a spawn round in place — MONOTONICALLY (#2).

    Refuses to overwrite an existing snapshot with a STRICTLY OLDER one (a
    late-retrying partial that lost a CAS race must never clobber a landed
    settled/more-complete snapshot — that would regress the exact reload bug
    this mechanism fixes). An equal-or-newer version wins.

    Returns ``True`` when the round actually changed (so callers can avoid a
    needless DB write / re-render when nothing was updated).
    """
    if not isinstance(round_entry, dict):
        return False
    changed = False
    existing = round_entry.get('_swarmSnapshot')
    if existing != snapshot:
        if _snapshot_version(snapshot) < _snapshot_version(existing):
            # Older/partial trying to overwrite newer/settled — reject the
            # snapshot body, but still allow the _swarm flag fixup below.
            logger.debug('[SwarmSnapshot] refusing to stamp older snapshot '
                         '(incoming v=%d < persisted v=%d)',
                         _snapshot_version(snapshot), _snapshot_version(existing))
        else:
            round_entry['_swarmSnapshot'] = snapshot
            changed = True
    # The frontend gate (_isRoundSwarm) needs _swarm truthy to render the
    # panel; a persisted spawn round already has it, but assert it so a
    # snapshot can never land on a round the UI then refuses to upgrade.
    if not round_entry.get('_swarm'):
        round_entry['_swarm'] = True
        changed = True
    return changed


def reconcile_spawn_round_from_active_session(
    task: dict,
    round_entry: dict,
) -> bool:
    """Stamp the active session's latest snapshot once its handle is readable.

    A fast sub-agent may start and finish before ``spawn_agents`` returns its
    handle.  Both completion-time persistence paths then see no match: the
    live round has no ``toolContent`` yet, and the turn projection has not been
    checkpointed yet.  There is no later agent transition to retry the write,
    so history falls back to an empty/non-expandable panel.

    Tool settlement is the first boundary at which the handle is guaranteed to
    be present on the authoritative round.  Re-read the live session here and
    stamp its current snapshot directly onto that exact round.  ``stamp_round``
    is monotonic and equality-aware, making this compensation safe when an
    ordinary agent callback already won the race or when settlement is replayed.
    """
    if not isinstance(task, dict) or not isinstance(round_entry, dict):
        return False
    handle_ids = _round_handle_ids(round_entry)
    if not handle_ids:
        return False
    try:
        from lib.swarm.integration._config import swarm_key_for
        from lib.swarm.integration._state import _get_session

        session = _get_session(swarm_key_for(task))
        if session is None:
            # The spawning task id remains a supported alias for standalone
            # tasks and for the narrow interval before a conversation alias is
            # visible to every caller.
            session = _get_session(str(task.get('id') or ''))
        if session is None:
            return False
        snapshot = session._build_agent_snapshot()
        scoped_ids = handle_ids & {
            str(agent.get('id'))
            for agent in (snapshot.get('agents') or [])
            if isinstance(agent, dict) and agent.get('id')
        }
        if not scoped_ids:
            logger.warning(
                '[SwarmSnapshot] active session has no agents matching the '
                'settled spawn handle (task=%s)',
                str(task.get('id') or '')[:8],
            )
            return False
        return stamp_round(round_entry, filter_snapshot(snapshot, scoped_ids))
    except Exception as e:
        # Snapshot persistence is diagnostic projection enrichment; it must
        # never turn a successfully launched swarm tool into a failed tool.
        logger.warning(
            '[SwarmSnapshot] spawn-settlement compensation failed task=%s: %s',
            str(task.get('id') or '')[:8], e, exc_info=True,
        )
        return False


def _persist_snapshot_to_turns(conv_id: str, agent_ids, snapshot: dict, *,
                               user_id) -> bool | str:
    """Persist onto the owner-scoped turn projections.

    Returns ``'retry'`` for a projection CAS miss and otherwise the usual
    best-effort boolean. A live turn is owned by its running attempt: the
    in-memory live-task stamp plus the attempt's own checkpoint carries that
    case, so this detached writer never edits it.
    """
    wanted = {str(x) for x in (agent_ids or [])}
    if not wanted:
        return False
    try:
        from lib.turn_lifecycle import (
            LIVE_ATTEMPT_STATUSES, LifecycleConflict, LifecycleNotFound,
            list_turns, update_turn_projection,
        )
        page = list_turns(conv_id, user_id=user_id, limit=2000)
    except LifecycleNotFound:
        return False
    except Exception as e:
        # Fail closed: a sidecar read error must never reroute a possibly
        # turn-native transcript into the v1 archive writer.
        logger.warning('[SwarmSnapshot] conv=%s turn read failed: %s',
                       conv_id[:8], e)
        return False

    turns = page.get('turns') or []
    if not turns:
        return False

    matched = False
    live_matched = False
    applied_turns = 0
    latest_rev = None
    for turn in reversed(turns):
        projection = turn.get('projection')
        if not isinstance(projection, dict):
            continue
        candidate = copy.deepcopy(projection)
        turn_changed = False
        for round_entry in (candidate.get('toolRounds') or []):
            hids = _round_handle_ids(round_entry) & wanted
            if not hids:
                continue
            matched = True
            if stamp_round(round_entry, filter_snapshot(snapshot, hids)):
                turn_changed = True
        if not turn_changed:
            continue
        if turn.get('status') in LIVE_ATTEMPT_STATUSES:
            # The running attempt's projection writer owns this row.  The live
            # task stamp above already carries the snapshot, and forcing a
            # detached edit here would violate the single-writer turn protocol.
            live_matched = True
            logger.debug('[SwarmSnapshot] conv=%s turn=%s is live — durable '
                         'projection left to the owning attempt',
                         conv_id[:8], str(turn.get('turnId') or '')[:8])
            continue
        try:
            result = update_turn_projection(
                conv_id, str(turn.get('turnId') or ''),
                projection=candidate,
                expected_projection_revision=int(
                    turn.get('projectionRevision') or 0),
                user_id=user_id)
        except LifecycleConflict as e:
            logger.debug('[SwarmSnapshot] conv=%s turn=%s projection CAS miss: %s',
                         conv_id[:8], str(turn.get('turnId') or '')[:8], e)
            return 'retry'
        applied_turns += 1
        latest_rev = result.get('conversationRevision')

    if not matched:
        logger.warning('[SwarmSnapshot] conv=%s no spawn round matched '
                       '%d agent id(s) in turn projections — snapshot not '
                       'persisted (handle not yet on disk, or agent-id drift)',
                       conv_id[:8], len(list(agent_ids or [])))
        return False
    if not applied_turns:
        if not live_matched:
            logger.debug('[SwarmSnapshot] conv=%s turn projections already current',
                         conv_id[:8])
        return False

    logger.info('[SwarmSnapshot] conv=%s persisted snapshot (%d agents, v=%d) '
                'onto %d turn projection(s)', conv_id[:8],
                len(snapshot.get('agents') or []), _snapshot_version(snapshot),
                applied_turns)
    try:
        from lib.conversations import notify_conv_changed
        notify_conv_changed(conv_id, rev=latest_rev, user_id=user_id)
    except Exception as e:
        logger.debug('[SwarmSnapshot] conv-changed notify skipped conv=%s: %s',
                     conv_id[:8], e)
    return True


def persist_snapshot_to_conversation(conv_id: str, agent_ids, snapshot: dict,
                                     *, user_id) -> bool:
    """Durably write *snapshot* onto a turn-native spawn round.

    Best-effort and never raises. Returns ``True`` when the snapshot was
    written, or ``False`` when there was nothing to do (no conversation or
    matching spawn round, unchanged snapshot, a live attempt owns the turn,
    or every projection CAS lost).
    """
    if not conv_id:
        return False
    import time as _time
    for attempt in range(_MAX_CAS):
        try:
            turn_result = _persist_snapshot_to_turns(
                conv_id, agent_ids, snapshot, user_id=user_id)
            if turn_result != 'retry':
                return bool(turn_result)
            logger.debug('[SwarmSnapshot] conv=%s projection CAS miss attempt '
                         '%d/%d — retrying', conv_id[:8], attempt + 1,
                         _MAX_CAS)
            _time.sleep(0.05 * (attempt + 1))
        except Exception as e:
            logger.warning('[SwarmSnapshot] conv=%s persist attempt %d failed: %s',
                           conv_id[:8], attempt + 1, e, exc_info=True)
            return False
    # Durability loss — the snapshot for this round did NOT land. NOT routine:
    # on reload the panel falls back to the (less complete) handle recovery.
    logger.warning('[SwarmSnapshot] conv=%s gave up after %d CAS misses (frontend '
                   'kept winning the row) — snapshot v=%d not persisted this round',
                   conv_id[:8], _MAX_CAS, _snapshot_version(snapshot))
    return False
