"""lib.presence.registry — authoritative cross-conversation presence state.

Single-server contract: the live registry lives in this module's process
memory (one Hypercorn process), guarded by one lock, and is **written through**
to ``<root>/.tofu/presence/registry.json`` atomically (via
:mod:`lib.json_store`) on every mutation so it survives a crash and can be
inspected by a human or a CLI. The disk copy is a mirror — on startup it is
RECONCILED against the live task registry, never trusted blindly (a server that
died mid-run left ghost peers marked "active" forever; see
:func:`reconcile_on_startup`).

The backend owns every judgment. A peer's ``status`` is computed HERE from its
last heartbeat vs :data:`ACTIVE_TTL_SEC` / :data:`IDLE_TTL_SEC` — the frontend
never derives liveness from mere presence. Each mutation broadcasts a
``presence`` event (fully formed, including the human ``statusLabel`` and any
conflict advisory) so a decision-free renderer can paint it.

Peer key: ``convId`` (one live peer per conversation; a follow-up autopilot
turn updates the same peer rather than spawning a new one). The peer also
carries the current ``taskId`` / autopilot ``runId`` for correlation.
"""

from __future__ import annotations

import os
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

# ── Liveness thresholds (backend-computed status) ──
# A peer is ACTIVE while its last heartbeat is within ACTIVE_TTL_SEC. The
# streaming heartbeat rides the existing ~5 s checkpoint throttle, so a long
# single-LLM generation (no tool rounds) still beats well inside this window —
# it must never flap to idle during the most intense work.
ACTIVE_TTL_SEC = 25.0
# After ACTIVE_TTL_SEC of silence a peer is IDLE (shown, dimmed). After
# IDLE_TTL_SEC total silence it is reaped (removed) by the sweep — this is also
# the ghost-peer cleanup for a crashed run that never fired a depart.
IDLE_TTL_SEC = 180.0

PUSH_CHANNEL = 'presence'

# ── In-memory authoritative state ──
#   root_path (abs) -> { peer_key -> peer_dict }
# A peer is a conversation OR a sub-agent of a conversation. The peer_key is
# ``convId`` for a conversation-level peer and ``f'{convId}#{agentId}'`` for a
# sub-agent, so N concurrent sub-agents of ONE conversation are N DISTINCT
# peers (not one collapsing entry). Every peer carries ``convId`` (the grouping
# key — sub-agents group under their parent conversation) and ``agentId`` ('' for
# a conversation peer). See docs: the composite key is what lets presence cover
# "different sub-agents within one conversation".
_state: dict[str, dict[str, dict]] = {}
_lock = threading.RLock()


def _peer_key(conv_id: str, agent_id: str = '') -> str:
    """Composite peer identity: conversation peer = convId; sub-agent = conv#agent."""
    return f'{conv_id}#{agent_id}' if agent_id else conv_id

_sweeper_started = False


# ═══════════════════════════════════════════════════════════════════
#  Disk mirror (write-through, atomic, per-path locked)
# ═══════════════════════════════════════════════════════════════════

def _registry_path(root: str) -> str:
    from lib.agent_artifacts import (
        FILE_HISTORY_ROOT_DIR,
        PRESENCE_REGISTRY_FILE,
        PRESENCE_SUBDIR,
    )
    return os.path.join(os.path.abspath(root), FILE_HISTORY_ROOT_DIR,
                        PRESENCE_SUBDIR, PRESENCE_REGISTRY_FILE)


def _persist_root(root: str) -> None:
    """Write-through the in-memory peers for ``root`` to disk atomically.

    Best-effort: a disk failure must never break the live in-memory feed.
    Caller holds ``_lock``; the on-disk write itself is serialised by
    json_store's per-path lock.
    """
    try:
        from lib.json_store import write_json_atomic
        peers = list(_state.get(root, {}).values())
        payload = {'root': root, 'updated_at': int(time.time() * 1000),
                   'peers': peers}
        write_json_atomic(_registry_path(root), payload, fsync=False)
    except Exception as e:
        logger.debug('[presence] persist failed for root=%s: %s', root, e)


# ═══════════════════════════════════════════════════════════════════
#  Status computation (backend-owned)
# ═══════════════════════════════════════════════════════════════════

def _compute_status(peer: dict, now: float) -> str:
    """Return 'active' | 'idle' from the heartbeat age. Backend-owned."""
    if peer.get('_departing'):
        return 'idle'
    age = now - (peer.get('lastBeatTs') or 0) / 1000.0
    if age <= ACTIVE_TTL_SEC:
        return 'active'
    return 'idle'


def _status_label(peer: dict, status: str) -> str:
    """Human-readable status string (the frontend renders this verbatim)."""
    if status == 'active':
        cf = peer.get('currentFile')
        if cf:
            return f'editing {cf}'
        phase = peer.get('phase') or ''
        # 'working' / 'generating' are the generic default phases — render
        # them as the plain word, not a redundant "working (working)".
        if phase and phase not in ('working', 'generating'):
            return f'working ({phase})'
        if phase == 'generating':
            return 'generating'
        return 'working'
    return 'idle'


def _decorate(peer: dict, now: float) -> dict:
    """Return a copy of ``peer`` with backend-computed status fields set."""
    status = _compute_status(peer, now)
    out = dict(peer)
    out['status'] = status
    out['statusLabel'] = _status_label(peer, status)
    return out


def _active_peers(root: str, now: float) -> list[dict]:
    return [_decorate(p, now) for p in _state.get(root, {}).values()
            if _compute_status(p, now) == 'active']


# ═══════════════════════════════════════════════════════════════════
#  Broadcast
# ═══════════════════════════════════════════════════════════════════

def _broadcast(payload: dict) -> None:
    """Push a presence frame to ALL clients (taskId='*'); frontend filters."""
    try:
        from lib.agent_core.events import EventType, build_event
        from lib.push import push_event
        push_event(PUSH_CHANNEL, '*', build_event(EventType.PRESENCE, **payload))
    except Exception as e:
        logger.debug('[presence] broadcast failed kind=%s: %s',
                     payload.get('kind'), e)


def _emit_peer_update(root: str, peer: dict, now: float) -> None:
    _broadcast({'kind': 'update', 'root': root, 'peer': _decorate(peer, now)})


def _maybe_emit_conflicts(root: str, peer_key: str, now: float) -> None:
    """Detect + broadcast notify-only overlap advisories involving ``peer_key``.

    ``peer_key`` is the composite identity of the peer that just wrote (a
    conversation = convId, a sub-agent = conv#agent). Detection is by PEER
    identity, so two sub-agents of ONE conversation touching the same file ARE
    flagged (the within-conversation worst case) exactly like two sibling
    conversations. Snapshots the active-peer list UNDER the lock (so the read
    can't race a concurrent mutation), then runs detection + broadcast OUTSIDE
    the lock.
    """
    with _lock:
        peers = _active_peers(root, now)
    try:
        from lib.presence.conflict import detect_overlaps
        advisories = detect_overlaps(peers, exclude_key=peer_key)
    except Exception as e:
        logger.debug('[presence] conflict detect failed root=%s: %s', root, e)
        return
    for adv in advisories:
        _broadcast({'kind': 'conflict', 'root': root, 'conflict': adv})


# ═══════════════════════════════════════════════════════════════════
#  Public API — mutations (wired at the existing task seams)
# ═══════════════════════════════════════════════════════════════════

def announce(root: str, conv_id: str, *, agent_id: str = '', task_id: str = '',
             run_id: str = '', title: str = '', objective: str = '',
             phase: str = '', parent_title: str = '') -> None:
    """Register / refresh a peer at the start of a task / autopilot turn / sub-agent.

    Idempotent per peer identity: a conversation peer keyed by ``conv_id`` (a
    follow-up turn updates the SAME peer, so an autopilot conversation never
    flickers gone→active), and a SUB-AGENT peer keyed by ``conv_id#agent_id``
    (so N concurrent sub-agents of one conversation are N distinct peers that
    group under the parent conversation, not one collapsing entry).

    Args:
        agent_id: when set, this peer is a sub-agent of ``conv_id``; the peer
            carries ``agentId`` and groups under the conversation. '' = the
            conversation-level peer.
        parent_title: (sub-agents) the parent conversation's title, for the
            backend-formed nested-row / conflict label.
    """
    if not (root and conv_id):
        return
    root = os.path.abspath(root)
    key = _peer_key(conv_id, agent_id)
    now = time.time()
    ts = int(now * 1000)
    with _lock:
        peers = _state.setdefault(root, {})
        existing = peers.get(key) or {}
        peer = {
            'convId': conv_id,
            'agentId': agent_id or existing.get('agentId', ''),
            'parentTitle': parent_title or existing.get('parentTitle', ''),
            'taskId': task_id or existing.get('taskId', ''),
            'runId': run_id or existing.get('runId', ''),
            'title': title or existing.get('title', ''),
            'objective': objective or existing.get('objective', ''),
            'phase': phase or existing.get('phase', ''),
            'currentFile': existing.get('currentFile', ''),
            # Preserve the touched-file set across turns of the same run.
            'files': list(existing.get('files', [])),
            'startedTs': existing.get('startedTs', ts),
            'lastBeatTs': ts,
        }
        peer.pop('_departing', None)
        peers[key] = peer
        _persist_root(root)
        _decorated = _decorate(peer, now)
    # Broadcast OUTSIDE the lock — never hold the global presence lock across
    # push-hub I/O (lock-ordering / latency hazard under real concurrency).
    _broadcast({'kind': 'update', 'root': root, 'peer': _decorated})
    logger.info('[presence] announce root=%s conv=%s agent=%s task=%s run=%s',
                os.path.basename(root) or root, conv_id[:8],
                (agent_id or '-'), (task_id or '-')[:8], (run_id or '-')[:8])
    _start_sweeper_once()


def heartbeat(root: str, conv_id: str, *, agent_id: str = '', phase: str = '') -> None:
    """Bump a peer's heartbeat (keeps it ACTIVE). Cheap; called frequently.

    Rides the existing ~5 s streaming checkpoint throttle (conversation path)
    or the sub-agent's token stream, so a long single-LLM generation never
    flaps to idle. Re-broadcasts only when the status WORD or phase actually
    changed (or it had gone idle) — a steady-state beat just refreshes the
    timestamp in memory + disk, no event spam.
    """
    if not (root and conv_id):
        return
    root = os.path.abspath(root)
    key = _peer_key(conv_id, agent_id)
    now = time.time()
    with _lock:
        peers = _state.get(root)
        peer = peers.get(key) if peers else None
        if peer is None:
            return
        prev_status = _compute_status(peer, now)
        prev_phase = peer.get('phase', '')
        peer['lastBeatTs'] = int(now * 1000)
        if phase:
            peer['phase'] = phase
        peer.pop('_departing', None)
        _persist_root(root)
        changed = (prev_status != 'active') or (phase and phase != prev_phase)
        _decorated = _decorate(peer, now) if changed else None
    # Broadcast OUTSIDE the lock (see announce).
    if _decorated is not None:
        _broadcast({'kind': 'update', 'root': root, 'peer': _decorated})


def record_files(root: str, conv_id: str, file_list: list[dict],
                 *, agent_id: str = '', phase: str = '') -> None:
    """Merge this round's touched files into the peer + check for overlaps.

    ``file_list`` is ``[{path, action, root?}, …]`` — the conversation path
    passes the orchestrator's authoritative per-round ``modifiedFileList``; the
    sub-agent path passes the file(s) it just edited (extracted at the swarm
    tool seam). We union the relative paths into the peer's running ``files``
    set, set ``currentFile``, bump the heartbeat, broadcast the update, then run
    notify-only overlap detection — which now flags BOTH sibling conversations
    AND sibling sub-agents touching the same file.
    """
    if not (root and conv_id):
        return
    root = os.path.abspath(root)
    key = _peer_key(conv_id, agent_id)
    now = time.time()
    paths = [f.get('path') for f in (file_list or [])
             if isinstance(f, dict) and f.get('path')]
    with _lock:
        peers = _state.get(root)
        peer = peers.get(key) if peers else None
        if peer is None:
            return
        files = peer.setdefault('files', [])
        seen = set(files)
        for p in paths:
            if p not in seen:
                files.append(p)
                seen.add(p)
        if paths:
            peer['currentFile'] = paths[-1]
        if phase:
            peer['phase'] = phase
        peer['lastBeatTs'] = int(now * 1000)
        peer.pop('_departing', None)
        _persist_root(root)
        _decorated = _decorate(peer, now)
    # Broadcast OUTSIDE the lock (see announce). Conflict detection already
    # ran outside the lock below.
    _broadcast({'kind': 'update', 'root': root, 'peer': _decorated})
    if paths:
        _maybe_emit_conflicts(root, key, now)


def mark_idle(root: str, conv_id: str, *, agent_id: str = '') -> None:
    """Transition a peer to IDLE at task done — but KEEP it (do not depart).

    A finished chat lingers (then fades via the sweep), and an autopilot
    conversation's next turn re-announces the SAME peer to ACTIVE — so we never
    flicker gone→active between back-to-back turns. The peer is reaped only by
    the sweep after IDLE_TTL_SEC, or explicitly via :func:`depart`. A sub-agent
    peer (``agent_id`` set) is marked idle on its own run end.
    """
    if not (root and conv_id):
        return
    root = os.path.abspath(root)
    key = _peer_key(conv_id, agent_id)
    now = time.time()
    with _lock:
        peers = _state.get(root)
        peer = peers.get(key) if peers else None
        if peer is None:
            return
        peer['_departing'] = True
        peer['currentFile'] = ''
        _persist_root(root)
        _decorated = _decorate(peer, now)
    # Broadcast OUTSIDE the lock (see announce).
    _broadcast({'kind': 'update', 'root': root, 'peer': _decorated})


def depart(root: str, conv_id: str, *, agent_id: str = '') -> None:
    """Remove a peer entirely (explicit abort / sweep reap / startup reconcile).

    Departs the specific peer identity (``conv_id`` for a conversation peer,
    ``conv_id#agent_id`` for a sub-agent). The depart frame carries both ids so
    the frontend can drop the exact nested row.
    """
    if not (root and conv_id):
        return
    root = os.path.abspath(root)
    key = _peer_key(conv_id, agent_id)
    with _lock:
        peers = _state.get(root)
        if not peers or key not in peers:
            return
        peers.pop(key, None)
        if not peers:
            _state.pop(root, None)
        # Persist either way: a now-empty root writes an empty peer list to
        # disk (so the mirror doesn't retain the departed peer). _persist_root
        # reads _state.get(root, {}), which is {} after the pop above.
        _persist_root(root)
    _broadcast({'kind': 'depart', 'root': root,
                'peer': {'convId': conv_id, 'agentId': agent_id}})
    logger.info('[presence] depart root=%s conv=%s agent=%s',
                os.path.basename(root) or root, conv_id[:8], agent_id or '-')


# ═══════════════════════════════════════════════════════════════════
#  Read API
# ═══════════════════════════════════════════════════════════════════

def snapshot(root: str | None = None) -> dict:
    """Return the current decorated active-peer view.

    With ``root`` → ``{root, peers:[…]}`` for that root. Without → a map of
    ``{root: [peers…]}`` across all roots. Decorated with backend-computed
    status; only ACTIVE peers are returned (idle/stale are filtered, mirroring
    what the frontend should display).
    """
    now = time.time()
    with _lock:
        if root is not None:
            root = os.path.abspath(root)
            return {'root': root, 'peers': _active_peers(root, now)}
        return {r: _active_peers(r, now) for r in list(_state.keys())}


# ═══════════════════════════════════════════════════════════════════
#  Sweep (active→idle→reaped) — the only clock-driven piece
# ═══════════════════════════════════════════════════════════════════

def sweep() -> int:
    """Re-evaluate every peer; broadcast status transitions; reap dead peers.

    A peer silent past ACTIVE_TTL_SEC flips to IDLE (broadcast once). A peer
    silent past IDLE_TTL_SEC is removed (ghost cleanup for a crashed run that
    never departed). Returns the number of peers reaped. No client polling —
    this is a single backend timer.
    """
    now = time.time()
    reaped = 0
    transitions: list[tuple[str, dict]] = []
    departed: list[tuple[str, dict]] = []
    with _lock:
        for root in list(_state.keys()):
            peers = _state.get(root, {})
            for key in list(peers.keys()):
                peer = peers[key]
                age = now - (peer.get('lastBeatTs') or 0) / 1000.0
                if age > IDLE_TTL_SEC:
                    peers.pop(key, None)
                    reaped += 1
                    departed.append((root, {'convId': peer.get('convId', ''),
                                            'agentId': peer.get('agentId', '')}))
                    continue
                # active→idle transition: mark so we broadcast it once.
                status = _compute_status(peer, now)
                if status == 'idle' and not peer.get('_idleEmitted'):
                    peer['_idleEmitted'] = True
                    transitions.append((root, dict(peer)))
                elif status == 'active' and peer.get('_idleEmitted'):
                    peer.pop('_idleEmitted', None)
            if not peers:
                _state.pop(root, None)
            else:
                _persist_root(root)
    for root, peer in transitions:
        _emit_peer_update(root, peer, now)
    for root, peer_ident in departed:
        _broadcast({'kind': 'depart', 'root': root, 'peer': peer_ident})
    if reaped:
        logger.info('[presence] sweep reaped %d stale peer(s)', reaped)
    return reaped


def _sweep_loop(interval: float) -> None:
    while True:
        time.sleep(interval)
        try:
            sweep()
        except Exception as e:
            logger.debug('[presence] sweep loop error: %s', e)


def start_sweeper(interval: float = 10.0) -> None:
    """Start the background sweep timer (idempotent)."""
    global _sweeper_started
    with _lock:
        if _sweeper_started:
            return
        _sweeper_started = True
    threading.Thread(target=_sweep_loop, args=(interval,),
                     name='presence-sweeper', daemon=True).start()
    logger.info('[presence] sweeper started (interval=%.0fs)', interval)


def _start_sweeper_once() -> None:
    if not _sweeper_started:
        start_sweeper()


# ═══════════════════════════════════════════════════════════════════
#  Startup reconciliation (ghost-peer cleanup)
# ═══════════════════════════════════════════════════════════════════

def reconcile_on_startup(known_roots: list[str] | None = None) -> int:
    """Reap ghost peers left by a crashed server; rebuild in-memory state.

    A server that died mid-run never fired ``depart``/``mark_idle``, so its
    ``.tofu/presence/registry.json`` shows dead peers as "active" forever — the
    "who's working" strip would lie after every restart. On startup we:

      1. Discover candidate roots: ``known_roots`` (e.g. recently-active
         project paths) plus any root already present in memory.
      2. For each, read the on-disk registry and DROP every peer whose:
         • task is not in the live task registry (the process is fresh, so the
           live registry is empty → effectively every disk peer is a ghost), OR
         • heartbeat is older than ACTIVE_TTL_SEC.
      3. Rewrite the cleaned registry to disk (an emptied root's file becomes
         an empty peer list, not a stale one).

    Because a just-booted server has NO live tasks, this effectively clears
    every persisted peer — which is correct: nothing is actually running yet.
    Live peers re-announce themselves as real tasks start. Returns the number
    of ghost peers reaped.

    Args:
        known_roots: optional list of project roots to reconcile. When omitted,
            only roots that already have an on-disk registry under a discovered
            path are handled (callers typically pass recent project paths).
    """
    from lib.json_store import read_json

    # Which task ids are actually live in THIS process right now?
    live_task_ids: set[str] = set()
    try:
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            live_task_ids = {tid for tid, t in tasks.items()
                             if isinstance(t, dict) and t.get('status') == 'running'}
    except Exception as e:
        logger.debug('[presence] reconcile: live-task probe failed: %s', e)

    roots: set[str] = set()
    for r in (known_roots or []):
        if r:
            roots.add(os.path.abspath(r))
    with _lock:
        roots.update(_state.keys())

    now = time.time()
    reaped = 0
    for root in roots:
        path = _registry_path(root)
        disk = read_json(path, default=None)
        if not isinstance(disk, dict):
            continue
        survivors: dict[str, dict] = {}
        for peer in (disk.get('peers') or []):
            if not isinstance(peer, dict):
                continue
            conv_id = peer.get('convId')
            if not conv_id:
                continue
            task_id = peer.get('taskId') or ''
            age = now - (peer.get('lastBeatTs') or 0) / 1000.0
            is_ghost = (task_id not in live_task_ids) or (age > ACTIVE_TTL_SEC)
            if is_ghost:
                reaped += 1
                continue
            survivors[_peer_key(conv_id, peer.get('agentId', ''))] = peer
        with _lock:
            if survivors:
                _state[root] = survivors
            else:
                _state.pop(root, None)
            _persist_root(root)
    if reaped:
        logger.info('[presence] startup reconciliation reaped %d ghost peer(s) '
                    'across %d root(s)', reaped, len(roots))
    return reaped
