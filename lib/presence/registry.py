"""Owner-scoped, process-local presence for active project workers.

Presence is ephemeral runtime state. Tasks announce themselves, refresh their
heartbeat while work is progressing, and become idle or disappear when work
ends. One batch-scoped TTL sweeper exists only while at least one peer may need
a transition or reap; empty process state owns no background thread.

The authoritative key is ``(user_id, normalized_project_path, peer_id)``.
Nothing is mirrored into a project directory: after a process restart no work
from the old process is live, so an empty registry is the only truthful state.
Every pushed frame is filtered by the authenticated owner in ``PushHub``.
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import TypeAlias

from lib.log import get_logger

logger = get_logger(__name__)

ACTIVE_TTL_SEC = 25.0
IDLE_TTL_SEC = 180.0
PUSH_CHANNEL = "presence"

ScopeKey: TypeAlias = tuple[int, str]
PeerMap: TypeAlias = dict[str, dict]

# (owner, absolute project path) -> composite peer id -> peer payload.
_state: dict[ScopeKey, PeerMap] = {}
_lock = threading.RLock()

_sweeper_started = False
_sweeper_thread: threading.Thread | None = None
_sweeper_stop = threading.Event()


def _owner_id(user_id: int) -> int:
    if isinstance(user_id, bool):
        raise ValueError("presence user_id must be a positive integer")
    owner = int(user_id)
    if owner < 1:
        raise ValueError("presence user_id must be a positive integer")
    return owner


def _scope_key(root: str, user_id: int) -> ScopeKey:
    return (_owner_id(user_id), os.path.abspath(root))


def _peer_key(conv_id: str, agent_id: str = "") -> str:
    """Return the stable identity of a conversation or nested sub-agent."""
    return f"{conv_id}#{agent_id}" if agent_id else conv_id


def _compute_status(peer: dict, now: float) -> str:
    if peer.get("_departing"):
        return "idle"
    age = now - (peer.get("lastBeatTs") or 0) / 1000.0
    return "active" if age <= ACTIVE_TTL_SEC else "idle"


def _status_label(peer: dict, status: str) -> str:
    if status != "active":
        return "idle"
    current_file = peer.get("currentFile")
    if current_file:
        return f"editing {current_file}"
    phase = peer.get("phase") or ""
    if phase and phase not in ("working", "generating"):
        return f"working ({phase})"
    return "generating" if phase == "generating" else "working"


def _decorate(peer: dict, now: float) -> dict:
    status = _compute_status(peer, now)
    decorated = dict(peer)
    decorated.pop("_departing", None)
    decorated.pop("_idleEmitted", None)
    decorated["status"] = status
    decorated["statusLabel"] = _status_label(peer, status)
    return decorated


def _active_peers(scope: ScopeKey, now: float) -> list[dict]:
    return [
        _decorate(peer, now)
        for peer in _state.get(scope, {}).values()
        if _compute_status(peer, now) == "active"
    ]


def _broadcast(payload: dict, *, user_id: int) -> None:
    """Publish one owner-filtered presence frame."""
    try:
        from lib.agent_core.events import EventType, build_event
        from lib.agent_core.push import push_event

        push_event(
            PUSH_CHANNEL,
            "*",
            build_event(EventType.PRESENCE, **payload),
            user_id=user_id,
        )
    except Exception as exc:
        logger.debug(
            "[presence] broadcast failed kind=%s owner=%s: %s",
            payload.get("kind"),
            user_id,
            exc,
        )


def _emit_peer_update(scope: ScopeKey, peer: dict, now: float) -> None:
    user_id, root = scope
    _broadcast(
        {"kind": "update", "root": root, "peer": _decorate(peer, now)},
        user_id=user_id,
    )


def _maybe_emit_conflicts(scope: ScopeKey, peer_key: str, now: float) -> None:
    with _lock:
        peers = _active_peers(scope, now)
    try:
        from lib.presence.conflict import detect_overlaps

        advisories = detect_overlaps(peers, exclude_key=peer_key)
    except Exception as exc:
        logger.debug("[presence] conflict detection failed scope=%r: %s", scope, exc)
        return
    user_id, root = scope
    for advisory in advisories:
        _broadcast(
            {"kind": "conflict", "root": root, "conflict": advisory},
            user_id=user_id,
        )


def announce(
    root: str,
    conv_id: str,
    *,
    user_id: int,
    agent_id: str = "",
    task_id: str = "",
    run_id: str = "",
    title: str = "",
    objective: str = "",
    phase: str = "",
    parent_title: str = "",
) -> None:
    """Register or refresh one worker within an explicit owner scope."""
    if not (root and conv_id):
        return
    scope = _scope_key(root, user_id)
    owner, normalized_root = scope
    key = _peer_key(conv_id, agent_id)
    now = time.time()
    timestamp_ms = int(now * 1000)
    with _lock:
        peers = _state.setdefault(scope, {})
        existing = peers.get(key) or {}
        peer = {
            "convId": conv_id,
            "agentId": agent_id or existing.get("agentId", ""),
            "parentTitle": parent_title or existing.get("parentTitle", ""),
            "taskId": task_id or existing.get("taskId", ""),
            "runId": run_id or existing.get("runId", ""),
            "title": title or existing.get("title", ""),
            "objective": objective or existing.get("objective", ""),
            "phase": phase or existing.get("phase", ""),
            "currentFile": existing.get("currentFile", ""),
            "files": list(existing.get("files", [])),
            "startedTs": existing.get("startedTs", timestamp_ms),
            "lastBeatTs": timestamp_ms,
        }
        peers[key] = peer
        decorated = _decorate(peer, now)
    _broadcast(
        {"kind": "update", "root": normalized_root, "peer": decorated},
        user_id=owner,
    )
    logger.info(
        "[presence] announce owner=%s root=%s conv=%s agent=%s task=%s run=%s",
        owner,
        os.path.basename(normalized_root) or normalized_root,
        conv_id[:8],
        agent_id or "-",
        (task_id or "-")[:8],
        (run_id or "-")[:8],
    )
    _start_sweeper_once()


def heartbeat(
    root: str,
    conv_id: str,
    *,
    user_id: int,
    agent_id: str = "",
    phase: str = "",
) -> None:
    """Refresh liveness, emitting only an observable status/phase change."""
    if not (root and conv_id):
        return
    scope = _scope_key(root, user_id)
    key = _peer_key(conv_id, agent_id)
    now = time.time()
    with _lock:
        peer = _state.get(scope, {}).get(key)
        if peer is None:
            return
        previous_status = _compute_status(peer, now)
        previous_phase = peer.get("phase", "")
        peer["lastBeatTs"] = int(now * 1000)
        if phase:
            peer["phase"] = phase
        peer.pop("_departing", None)
        peer.pop("_idleEmitted", None)
        changed = previous_status != "active" or bool(phase and phase != previous_phase)
        decorated = _decorate(peer, now) if changed else None
    if decorated is not None:
        owner, normalized_root = scope
        _broadcast(
            {"kind": "update", "root": normalized_root, "peer": decorated},
            user_id=owner,
        )


def record_files(
    root: str,
    conv_id: str,
    file_list: list[dict],
    *,
    user_id: int,
    agent_id: str = "",
    phase: str = "",
) -> None:
    """Merge touched files and emit overlap advisories within the same owner."""
    if not (root and conv_id):
        return
    scope = _scope_key(root, user_id)
    key = _peer_key(conv_id, agent_id)
    now = time.time()
    paths = [
        item.get("path")
        for item in (file_list or [])
        if isinstance(item, dict) and item.get("path")
    ]
    with _lock:
        peer = _state.get(scope, {}).get(key)
        if peer is None:
            return
        files = peer.setdefault("files", [])
        seen = set(files)
        for path in paths:
            if path not in seen:
                files.append(path)
                seen.add(path)
        if paths:
            peer["currentFile"] = paths[-1]
        if phase:
            peer["phase"] = phase
        peer["lastBeatTs"] = int(now * 1000)
        peer.pop("_departing", None)
        peer.pop("_idleEmitted", None)
        decorated = _decorate(peer, now)
    owner, normalized_root = scope
    _broadcast(
        {"kind": "update", "root": normalized_root, "peer": decorated},
        user_id=owner,
    )
    if paths:
        _maybe_emit_conflicts(scope, key, now)


def mark_idle(
    root: str,
    conv_id: str,
    *,
    user_id: int,
    agent_id: str = "",
) -> None:
    """Mark a worker idle while retaining it until the TTL reap."""
    if not (root and conv_id):
        return
    scope = _scope_key(root, user_id)
    key = _peer_key(conv_id, agent_id)
    now = time.time()
    with _lock:
        peer = _state.get(scope, {}).get(key)
        if peer is None:
            return
        peer["_departing"] = True
        peer["_idleEmitted"] = True
        peer["currentFile"] = ""
        decorated = _decorate(peer, now)
    owner, normalized_root = scope
    _broadcast(
        {"kind": "update", "root": normalized_root, "peer": decorated},
        user_id=owner,
    )


def depart(
    root: str,
    conv_id: str,
    *,
    user_id: int,
    agent_id: str = "",
) -> None:
    """Remove one exact worker identity from an owner scope."""
    if not (root and conv_id):
        return
    scope = _scope_key(root, user_id)
    key = _peer_key(conv_id, agent_id)
    with _lock:
        peers = _state.get(scope)
        if not peers or key not in peers:
            return
        peers.pop(key)
        if not peers:
            _state.pop(scope, None)
    owner, normalized_root = scope
    _broadcast(
        {
            "kind": "depart",
            "root": normalized_root,
            "peer": {"convId": conv_id, "agentId": agent_id},
        },
        user_id=owner,
    )
    logger.info(
        "[presence] depart owner=%s root=%s conv=%s agent=%s",
        owner,
        os.path.basename(normalized_root) or normalized_root,
        conv_id[:8],
        agent_id or "-",
    )


def snapshot(root: str, *, user_id: int) -> dict:
    """Return active peers for exactly one owner and project root."""
    scope = _scope_key(root, user_id)
    now = time.time()
    with _lock:
        return {"root": scope[1], "peers": _active_peers(scope, now)}


def sweep() -> int:
    """Transition or reap stale peers across all explicit owner scopes."""
    now = time.time()
    reaped = 0
    transitions: list[tuple[ScopeKey, dict]] = []
    departed: list[tuple[ScopeKey, dict]] = []
    with _lock:
        for scope, peers in list(_state.items()):
            for key, peer in list(peers.items()):
                age = now - (peer.get("lastBeatTs") or 0) / 1000.0
                if age > IDLE_TTL_SEC:
                    peers.pop(key)
                    reaped += 1
                    departed.append(
                        (
                            scope,
                            {
                                "convId": peer.get("convId", ""),
                                "agentId": peer.get("agentId", ""),
                            },
                        )
                    )
                    continue
                status = _compute_status(peer, now)
                if status == "idle" and not peer.get("_idleEmitted"):
                    peer["_idleEmitted"] = True
                    transitions.append((scope, dict(peer)))
                elif status == "active" and peer.get("_idleEmitted"):
                    peer.pop("_idleEmitted", None)
            if not peers:
                _state.pop(scope, None)
    for scope, peer in transitions:
        _emit_peer_update(scope, peer, now)
    for (owner, root), peer_identity in departed:
        _broadcast(
            {"kind": "depart", "root": root, "peer": peer_identity},
            user_id=owner,
        )
    if reaped:
        logger.info("[presence] sweep reaped %d stale peer(s)", reaped)
    return reaped


def _sweep_loop(interval: float) -> None:
    while not _sweeper_stop.wait(interval):
        try:
            sweep()
        except Exception as exc:
            logger.debug("[presence] sweep loop error: %s", exc)
        if _retire_sweeper_if_empty(threading.current_thread()):
            logger.debug("[presence] sweeper retired with empty registry")
            return


def _retire_sweeper_if_empty(thread: threading.Thread) -> bool:
    """Release only the exact worker that observed an empty registry."""
    global _sweeper_started, _sweeper_thread
    with _lock:
        if _state or _sweeper_thread is not thread:
            return False
        _sweeper_thread = None
        _sweeper_started = False
        return True


def _bounded_sweep_interval(interval: object) -> float:
    try:
        seconds = float(interval)
    except (TypeError, ValueError, OverflowError):
        return 10.0
    if not math.isfinite(seconds) or seconds <= 0:
        return 10.0
    return min(ACTIVE_TTL_SEC, max(0.1, seconds))


def start_sweeper(interval: float = 10.0) -> bool:
    """Start the process-wide TTL sweeper only for a non-empty batch."""
    global _sweeper_started, _sweeper_thread
    with _lock:
        if _sweeper_thread is not None and _sweeper_thread.is_alive():
            return False
        if not _state:
            _sweeper_started = False
            _sweeper_thread = None
            return False
        bounded_interval = _bounded_sweep_interval(interval)
        _sweeper_started = True
        _sweeper_stop.clear()
        thread = threading.Thread(
            target=_sweep_loop,
            args=(bounded_interval,),
            name="presence-sweeper",
            daemon=True,
        )
        _sweeper_thread = thread
        try:
            thread.start()
        except BaseException:
            if _sweeper_thread is thread:
                _sweeper_thread = None
                _sweeper_started = False
            raise
    logger.info(
        "[presence] sweeper started (interval=%.1fs)", bounded_interval)
    return True


def stop_sweeper(timeout: float = 2.0) -> bool:
    """Signal and bounded-join the presence sweeper."""
    global _sweeper_started, _sweeper_thread
    _sweeper_stop.set()
    with _lock:
        thread = _sweeper_thread
    if thread is None:
        with _lock:
            if _sweeper_thread is None:
                _sweeper_started = False
        return True
    try:
        wait_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug("[presence] invalid stop timeout; using 2.0: %s", exc)
        wait_seconds = 2.0
    if thread is not threading.current_thread():
        thread.join(timeout=wait_seconds)
    if thread.is_alive():
        return False
    with _lock:
        if _sweeper_thread is thread:
            _sweeper_thread = None
            _sweeper_started = False
    return True


def _start_sweeper_once() -> None:
    if not _sweeper_started:
        try:
            start_sweeper()
        except Exception as exc:
            logger.warning("[presence] sweeper start failed: %s", exc)


__all__ = [
    "announce",
    "depart",
    "heartbeat",
    "mark_idle",
    "record_files",
    "snapshot",
    "start_sweeper",
    "stop_sweeper",
    "sweep",
]
