"""lib/swarm/snapshot.py — durable swarm agent snapshot persistence.

The swarm "Parallel Execution" panel's per-agent state (``_swarmAgents``)
is synthesized live on the FRONTEND from ``swarm_*`` SSE events and is never
persisted. After a reload it is gone, so the panel could only rebuild
objective-only stubs from the spawn handle, and fire-and-forget swarms
(spawned but never ``await_agents``-ed) rendered every agent as ``unknown``.

This module is the ROOT-CAUSE fix: when the swarm settles (and incrementally
as each agent completes), the authoritative per-agent state from the
``MasterOrchestrator`` is written DURABLY onto the ``spawn_agents`` tool round
inside ``conversations.messages`` — the same source-of-truth store the rest of
the tool rounds already persist to. On reload the frontend prefers this
snapshot (``round._swarmSnapshot``) and renders a faithful, fully-expandable
panel with real status/preview/tokens/elapsed/modifiedFiles — even with no
``await_agents`` sibling round and no live ``_swarmAgents`` array.

The write is best-effort and CAS-guarded (mirrors
``manager._sync_partial_to_conversation``): it must NEVER raise into the swarm
driver thread, and must never clobber a concurrent frontend write.
"""

from __future__ import annotations

import json

from lib.log import get_logger

logger = get_logger(__name__)

#: Max chars of an agent's final answer to embed as the card preview. The
#: frontend slices to 1200 too (``_buildSwarmPanelHTML``), so this bounds the
#: persisted blob without losing anything the UI would show.
_PREVIEW_CHARS = 1200

#: How many optimistic-lock retries before giving up the durable write.
#: Paired with incremental backoff in persist_snapshot_to_conversation so a
#: busy row gets several real chances rather than a tight spin.
_MAX_CAS = 6


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
    agents = handle.get('agents') if isinstance(handle, dict) else None
    if not isinstance(agents, list):
        return set()
    return {a.get('id') for a in agents
            if isinstance(a, dict) and a.get('id')}


def find_spawn_round(messages: list, agent_ids) -> dict | None:
    """Find the spawn round whose handle overlaps *agent_ids*.

    Scans assistant messages newest-first; returns the first ``spawn_agents``
    tool round whose handle's agent ids intersect *agent_ids*. The intersection
    match disambiguates between multiple swarm waves / panels in one
    conversation. Returns ``None`` when no matching round is found (e.g. the
    spawning turn hasn't been persisted yet).
    """
    wanted = {str(x) for x in (agent_ids or [])}
    if not wanted or not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            continue
        rounds = msg.get('toolRounds')
        if not isinstance(rounds, list):
            continue
        for r in rounds:
            if _round_handle_ids(r) & wanted:
                return r
    return None


def _snapshot_version(snap) -> int:
    """Monotonic ordering key for a snapshot (higher = newer/more-complete).

    Prefers the explicit ``version`` field (settled*100000 + doneCount, set by
    ``master._build_agent_snapshot``); falls back to deriving it for legacy
    snapshots that predate the field. A non-dict / absent snapshot is -1 so any
    real snapshot outranks it.
    """
    if not isinstance(snap, dict):
        return -1
    v = snap.get('version')
    if isinstance(v, int):
        return v
    # Legacy fallback: derive from settled + terminal-agent count.
    settled = 1 if snap.get('settled') else 0
    agents = snap.get('agents') or []
    done = sum(1 for a in agents
               if isinstance(a, dict)
               and a.get('status') in ('done', 'failed', 'aborted'))
    return settled * 100000 + done


def filter_snapshot(snapshot: dict, keep_ids: set) -> dict:
    """Return a snapshot restricted to *keep_ids* (#4 multi-wave scoping).

    A follow-up ``spawn_agents`` in the same conversation merges both waves
    into ``master.specs``, so ``_build_agent_snapshot`` emits ONE snapshot
    spanning every wave. Stamping that combined snapshot onto a single round
    would make wave-1's panel show wave-2's agents (or never upgrade). We
    therefore stamp EACH spawn round with only the agents its own handle
    launched. Recomputes the derived counts/version over the kept subset so
    the monotonic guard stays correct per panel.
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


def persist_snapshot_to_conversation(conv_id: str, agent_ids,
                                     snapshot: dict) -> bool:
    """Durably write *snapshot* onto the spawn round in ``conversations.messages``.

    Best-effort, CAS-guarded, never raises. Returns ``True`` when the snapshot
    was written, ``False`` when there was nothing to do (no conv, spawn round
    not yet persisted, snapshot unchanged, or the row was concurrently rewritten
    on every retry).
    """
    if not conv_id:
        return False
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    except Exception as e:  # pragma: no cover - import guard
        logger.debug('[SwarmSnapshot] DB layer import failed: %s', e)
        return False

    import time as _time
    for attempt in range(_MAX_CAS):
        try:
            db = get_thread_db(DOMAIN_CHAT)
            row = db.execute(
                'SELECT messages, updated_at, rev FROM conversations WHERE id=? AND user_id=1',
                (conv_id,),
            ).fetchone()
            if not row or not row[0]:
                return False
            row[1]
            cur_rev = row[2]  # Phase 4 W5: CAS on rev (loop re-reads each attempt)
            try:
                messages = json.loads(row[0] or '[]')
            except (json.JSONDecodeError, TypeError):
                logger.warning('[SwarmSnapshot] conv=%s unparseable messages JSON',
                               conv_id[:8])
                return False

            # #4: stamp EVERY matching spawn round with only the agents its
            # own handle launched — a follow-up wave shares this conversation
            # and must not receive the other wave's combined snapshot.
            wanted = {str(x) for x in (agent_ids or [])}
            any_changed = False
            matched = False
            for msg in reversed(messages):
                if not isinstance(msg, dict) or msg.get('role') != 'assistant':
                    continue
                for r in (msg.get('toolRounds') or []):
                    hids = _round_handle_ids(r) & wanted
                    if not hids:
                        continue
                    matched = True
                    if stamp_round(r, filter_snapshot(snapshot, hids)):
                        any_changed = True
            if not matched:
                # Either the spawning turn hasn't been persisted yet (mid-turn
                # before the first checkpoint — the live-task stamp covers that,
                # a later settle call finds it once on disk), OR the handle's
                # agent ids drifted and we'll NEVER match. The caller passed a
                # non-empty agent set, so a persistent miss is a real durability
                # gap, not routine — surface it once at WARNING.
                logger.warning('[SwarmSnapshot] conv=%s no spawn round matched '
                               '%d agent id(s) — snapshot not persisted (handle '
                               'not yet on disk, or agent-id drift)',
                               conv_id[:8], len(list(agent_ids or [])))
                return False
            if not any_changed:
                # Identical, or the monotonic guard rejected an older snapshot
                # on every matched round — correct no-ops, not failures.
                return False

            messages_json = json_dumps_pg(messages)
            now_ms = int(_time.time() * 1000)
            cur = db.execute(
                'UPDATE conversations SET messages=?, updated_at=? '
                'WHERE id=? AND user_id=1 AND rev=?',
                (messages_json, now_ms, conv_id, cur_rev),
            )
            db.commit()
            if (getattr(cur, 'rowcount', 0) or 0) > 0:
                logger.info('[SwarmSnapshot] conv=%s persisted snapshot (%d agents, '
                            'v=%d) onto spawn round', conv_id[:8],
                            len(snapshot.get('agents') or []),
                            _snapshot_version(snapshot))
                # Event-driven cross-device sync: the persisted swarm panel is
                # conversation body state, so push the post-write rev → a
                # sibling tab with this conv open re-renders the panel without
                # a manual refresh.
                try:
                    from lib.conversations import notify_conv_changed
                    _ss_rev_row = db.execute(
                        'SELECT rev FROM conversations WHERE id=? AND user_id=1',
                        (conv_id,)).fetchone()
                    notify_conv_changed(conv_id, rev=(_ss_rev_row[0] if _ss_rev_row else None))
                except Exception as _ne:
                    logger.debug('[SwarmSnapshot] conv-changed notify skipped conv=%s: %s',
                                 conv_id[:8], _ne)
                return True
            # CAS miss — a concurrent writer (frontend sync / partial
            # checkpoint) landed first. Back off briefly then re-read + retry,
            # so a busy row doesn't burn all attempts in a tight spin.
            logger.debug('[SwarmSnapshot] conv=%s CAS miss attempt %d/%d — retrying',
                         conv_id[:8], attempt + 1, _MAX_CAS)
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
