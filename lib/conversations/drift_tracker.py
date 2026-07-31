"""lib/conversations/drift_tracker.py — convergence tracking for the P5 sync-drift probe.

WHY THIS EXISTS (pt_conv_state_ssot P6 precondition)
----------------------------------------------------
The P5 probe (``routes/api_v1/conversations.py::sync_digest``) compares a
client's 60-second digest against the server SSOTs and WARN-logs every
inequality. That is enough to prove drift EXISTS, but not enough to prove
drift is a FAULT — and P6 (sweeping the three redundant fallback branches)
requires the stronger statement. A ``client=1 server=181`` line on a
conversation that is actively streaming is the probe's own sampling latency:
the client's snapshot is up to 60s old while the server value is read live,
so on a conversation being written every second the two CANNOT agree. Logging
that at WARNING makes the real signal unfindable.

The discriminator is NOT elapsed time — it is whether the CLIENT VALUE IS
MOVING:

  * server changed, client changed  → the client is tracking, just sampled
    late. Healthy. This is the overwhelming majority of observations while
    any conversation is generating.
  * server changed, client FROZEN   → the client is not receiving (or not
    applying) frames. It will never catch up on its own. THIS is the
    "notify frame dropped, _serverRev never converges" hole that owner
    constraint #4 names, and the only shape that justifies keeping a
    fallback branch alive.

So a divergence is escalated to WARNING only when it is BOTH stalled (client
frozen while server moved) AND sustained past a threshold. Everything else is
DEBUG. The endpoint's HTTP response is deliberately unchanged — this module
only decides log severity and records convergence, so the probe stays a
read-only observer of both sides.

DIRECTION IS ALSO CLASSIFIED. ``client > server`` on a server-authoritative
rev should be impossible; if it appears it means the server row's rev went
BACKWARDS (row restored/rewritten) or a frame from another conversation was
applied. That is a distinct defect from ordinary lag and is surfaced as
``direction='client_ahead'`` rather than being folded into the same bucket.

Pure in-memory and dependency-free (no DB, no HTTP, no Quart) so the policy is
unit-testable without a live stack. State is per-process and advisory: losing
it on restart only restarts the observation window.
"""

from __future__ import annotations

import os
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'observe_divergence',
    'observe_agreement',
    'sustained_threshold_sec',
    'tracked_count',
    'reset',
]

# Escalate to WARNING only after a STALLED divergence has persisted this long.
# The client reports every ~60s, so the default spans at least three reports —
# enough that a single missed beat cannot trip it.
_DEFAULT_SUSTAINED_SEC = 180.0

# Hard cap on tracked (conv_id, kind) pairs. A pathological client could
# otherwise pin unbounded memory by cycling conv ids.
_MAX_TRACKED = 2000

_lock = threading.Lock()
_state: dict[tuple[str, str], dict] = {}


def sustained_threshold_sec() -> float:
    """Seconds a stalled divergence must persist before it warrants WARNING.

    Overridable via ``TOFU_SYNC_DRIFT_SUSTAINED_SEC`` so the window can be
    tightened during an investigation without a code change.
    """
    raw = os.environ.get('TOFU_SYNC_DRIFT_SUSTAINED_SEC', '')
    if not raw:
        return _DEFAULT_SUSTAINED_SEC
    try:
        val = float(raw)
    except (TypeError, ValueError) as e:
        logger.debug('[DriftTracker] bad TOFU_SYNC_DRIFT_SUSTAINED_SEC=%r: %s', raw, e)
        return _DEFAULT_SUSTAINED_SEC
    return val if val > 0 else _DEFAULT_SUSTAINED_SEC


def _fingerprint(value) -> str:
    """Stable change-detection key for a client/server value.

    Values are either a sorted list of task ids or a numeric rev, so ``repr``
    is a sufficient and total equality proxy. Only CHANGE is detected, never
    ordering — "did this side move at all" is the whole question.
    """
    return repr(value)


def _classify_direction(client, server) -> str:
    """Label the divergence direction when both sides are comparable numbers.

    Returns ``'client_behind'`` (ordinary lag), ``'client_ahead'`` (the
    impossible-for-server-authoritative-state case worth flagging on its own),
    or ``'incomparable'`` for non-numeric payloads such as task-id sets.
    """
    numeric = (int, float)
    if (isinstance(client, numeric) and not isinstance(client, bool)
            and isinstance(server, numeric) and not isinstance(server, bool)):
        if client < server:
            return 'client_behind'
        if client > server:
            return 'client_ahead'
        return 'equal'
    return 'incomparable'


def _prune_locked(now: float) -> None:
    """Drop the oldest-touched entries once over the cap. Caller holds the lock."""
    if len(_state) <= _MAX_TRACKED:
        return
    overflow = len(_state) - _MAX_TRACKED
    victims = sorted(_state.items(), key=lambda kv: kv[1]['last_seen'])[:overflow]
    for key, _ in victims:
        _state.pop(key, None)
    logger.debug('[DriftTracker] pruned %d stale entries (cap=%d)',
                 len(victims), _MAX_TRACKED)


def observe_divergence(conv_id: str, kind: str, client, server,
                       now: float | None = None) -> dict:
    """Record that ``conv_id``/``kind`` diverged, and judge how serious it is.

    Args:
        conv_id: Conversation id the digest entry referred to.
        kind: Divergence class — ``'rev'``, ``'task_ids'``, ``'unknown_conv'``.
        client: The value the client reported.
        server: The value the server holds.
        now: Epoch seconds; injectable so tests need no sleeps.

    Returns:
        A verdict dict::

            {
              'age':            float,  # seconds since first seen
              'observations':   int,    # times seen consecutively diverged
              'client_moved':   bool,   # client value changed since first seen
              'server_moved':   bool,   # server value changed since first seen
              'stalled':        bool,   # server moving while client frozen
              'sustained':      bool,   # stalled AND older than the threshold
              'direction':      str,    # client_behind / client_ahead / …
              'severity':       str,    # 'warning' when sustained, else 'debug'
            }
    """
    ts = time.time() if now is None else now
    key = (conv_id, kind)
    c_fp, s_fp = _fingerprint(client), _fingerprint(server)

    with _lock:
        entry = _state.get(key)
        if entry is None:
            entry = {
                'first_seen': ts,
                'last_seen': ts,
                'observations': 1,
                'first_client_fp': c_fp,
                'first_server_fp': s_fp,
                'client_moved': False,
                'server_moved': False,
            }
            _state[key] = entry
            _prune_locked(ts)
        else:
            entry['last_seen'] = ts
            entry['observations'] += 1
            if c_fp != entry['first_client_fp']:
                entry['client_moved'] = True
            if s_fp != entry['first_server_fp']:
                entry['server_moved'] = True

        age = max(0.0, ts - entry['first_seen'])
        observations = entry['observations']
        client_moved = entry['client_moved']
        server_moved = entry['server_moved']

    # A single observation can never establish "frozen" — we have nothing to
    # compare against yet, so the first sighting is always benign.
    stalled = bool(observations >= 2 and server_moved and not client_moved)
    sustained = bool(stalled and age >= sustained_threshold_sec())

    return {
        'age': age,
        'observations': observations,
        'client_moved': client_moved,
        'server_moved': server_moved,
        'stalled': stalled,
        'sustained': sustained,
        'direction': _classify_direction(client, server),
        'severity': 'warning' if sustained else 'debug',
    }


def observe_agreement(conv_id: str, kind: str,
                      now: float | None = None) -> dict | None:
    """Record that ``conv_id``/``kind`` now agrees; clear any tracked drift.

    Returns:
        ``None`` when nothing was being tracked (the steady-state case), else a
        resolution record ``{'age', 'observations', 'was_stalled'}`` describing
        the divergence that just closed — the positive evidence that the
        channel self-heals, which is exactly what P6 needs before deleting a
        fallback branch.
    """
    ts = time.time() if now is None else now
    key = (conv_id, kind)
    with _lock:
        entry = _state.pop(key, None)
    if entry is None:
        return None
    age = max(0.0, ts - entry['first_seen'])
    was_stalled = bool(entry['observations'] >= 2
                       and entry['server_moved']
                       and not entry['client_moved'])
    return {
        'age': age,
        'observations': entry['observations'],
        'was_stalled': was_stalled,
    }


def tracked_count() -> int:
    """Number of (conv_id, kind) pairs currently diverged. For tests/diagnostics."""
    with _lock:
        return len(_state)


def reset() -> None:
    """Drop all tracked state. Test-support only."""
    with _lock:
        _state.clear()
