"""lib/conversations/drift_repair.py — turn a detected stall into a correction.

WHY THIS EXISTS (pt_cadaa70ffa6b468d)
-------------------------------------
``drift_tracker`` can already tell a client that is merely sampling late (its
value is MOVING, just 60s behind) from one that is frozen while the server
advances — the latter being the "notify frame was dropped and this tab will
never converge on its own" case. But the whole subsystem stopped at
*observation*: the verdict only chose a log level. The server knew exactly
which socket was stuck and did nothing about it, so recovery still required
the user to press F5.

That is the second half of the reported symptom: not only was the frontend
lagging, it had no way to find out, and nothing was coming to tell it.

This module is the repair half. It is deliberately SEPARATE from the tracker:

  * the tracker answers "is this a fault?" — pure, in-memory, no I/O;
  * this answers "should I spend a frame fixing it, and have I already?" —
    policy plus a rate limit.

Keeping them apart is what lets the repair policy be unit-tested without a
push hub, and stops a future edit from making the *detector* mutate state.

WHY A COOLDOWN IS PART OF THE DESIGN, NOT AN OPTIMISATION
---------------------------------------------------------
A stalled client keeps POSTing its digest every 60s. If the snapshot does not
heal it (say the socket is half-open in a way the ping watchdog has not yet
caught), an uncooled repair would emit a frame per client per minute for as
long as the condition lasts — turning a repair into a slow flood exactly when
the transport is already unhealthy. The cooldown bounds that to one attempt
per socket per window, so a repair that is not working degrades to "rare and
visible in the log" rather than "constant and invisible".

A repair is also only attempted for a SUSTAINED stall (tracker's own
threshold, ~180s = at least three consecutive frozen reports). A single missed
beat is not worth a frame; the next notify almost always closes it.
"""

from __future__ import annotations

import os
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'should_repair',
    'note_repair_attempt',
    'note_repair_outcome',
    'repair_stats',
    'repair_cooldown_sec',
    'reset',
    'tracked_sockets',
]

#: Minimum gap between two repair frames aimed at the SAME socket.
#:
#: Default 300s: the client reports every ~60s, so this admits at most one
#: repair per five reports. Overridable for an investigation without a code
#: change, matching how the tracker exposes its own threshold.
_DEFAULT_COOLDOWN_SEC = 300.0

#: Hard cap on tracked sockets, so a client cycling its ``_rid`` (every
#: reconnect mints a fresh one — see push.js::_mintWsRid) cannot pin unbounded
#: memory. Oldest-touched entries are evicted first.
_MAX_TRACKED = 2000

#: Shared cooldown bucket for clients that report no socket id (WebSocket
#: blocked by a proxy, or push not yet connected). They still get repaired —
#: the correction rides HTTP — but share one rate-limit slot, since there is no
#: identity to separate them by.
_ANON_BUCKET = '<anon>'

_lock = threading.Lock()
_last_repair: dict[str, float] = {}

#: Outcome tally. A repair whose effect is never checked is indistinguishable
#: from the mechanism silently not firing at all — the same "measured or it does
#: not exist" rule this project applies to capabilities. ``ineffective`` is the
#: number that matters most: it says the self-heal is spinning without result,
#: which no amount of ``attempted`` can reveal on its own.
_stats: dict[str, int] = {'attempted': 0, 'converged': 0, 'ineffective': 0}

#: Sockets awaiting an effectiveness verdict: socket_id -> attempt timestamp.
#: Bounded by the same cap as the cooldown map.
_pending_outcome: dict[str, float] = {}


def repair_cooldown_sec() -> float:
    """Seconds a socket must wait between two repair frames."""
    raw = os.environ.get('TOFU_SYNC_REPAIR_COOLDOWN_SEC', '')
    if not raw:
        return _DEFAULT_COOLDOWN_SEC
    try:
        val = float(raw)
    except (TypeError, ValueError) as e:
        logger.debug('[DriftRepair] bad TOFU_SYNC_REPAIR_COOLDOWN_SEC=%r: %s',
                     raw, e)
        return _DEFAULT_COOLDOWN_SEC
    return val if val > 0 else _DEFAULT_COOLDOWN_SEC


def should_repair(socket_id: str, verdicts, now: float | None = None) -> bool:
    """Decide whether to send a corrective snapshot to this reporting client.

    Args:
        socket_id: The reporting client's push-socket correlation id, used
            ONLY as a rate-limiting identity — the correction travels on the
            HTTP response, so this socket does not need to be live, or to
            exist at all.

            ★ An EMPTY id used to return False here, back when the repair was
            pushed down the socket and an untargetable repair was undeliverable.
            That premise is gone, and keeping the early return would have
            locked out precisely the population that needs this most: a client
            whose WebSocket is blocked by a corporate proxy has no socket id,
            no push channel to self-heal through, and is therefore the LEAST
            able to recover on its own. An empty id now falls back to a shared
            cooldown bucket — slightly coarser rate-limiting for anonymous
            clients, which is the correct trade against never repairing them.
        verdicts: The per-divergence verdict dicts from
            ``drift_tracker.observe_divergence``. A repair is warranted iff at
            least one is ``sustained`` — i.e. the client value is frozen while
            the server advances, and has been for the tracker's threshold.
        now: Epoch seconds; injectable so tests need no sleeps.

    Returns:
        True iff a repair should be sent now (sustained stall AND out of
        cooldown). Does NOT record the attempt — call ``note_repair_attempt``
        once the correction is genuinely delivered (an HTTP 200 carrying it).
    """
    key = socket_id or _ANON_BUCKET
    try:
        sustained = any(bool(v.get('sustained')) for v in (verdicts or [])
                        if isinstance(v, dict))
    except Exception as e:
        logger.debug('[DriftRepair] malformed verdicts (%r): %s', verdicts, e)
        return False
    if not sustained:
        return False

    ts = time.time() if now is None else now
    with _lock:
        prev = _last_repair.get(key)
    if prev is not None and (ts - prev) < repair_cooldown_sec():
        logger.debug('[DriftRepair] socket=%s still in cooldown (%.0fs ago)',
                     key[:8], ts - prev)
        return False
    return True


def note_repair_attempt(socket_id: str, now: float | None = None) -> None:
    """Record that a repair was DELIVERED to ``socket_id``.

    Call this only on evidence that actually proves delivery. A synchronous
    HTTP 200 qualifies: the response body reached the client or the request
    failed outright.

    ``PushHub.deliver_to_socket`` does NOT qualify and must never gate this.
    It returns True once a frame is ENQUEUED, and on a half-open socket nothing
    drains that queue — measured: with the queue at its 1000-frame bound the
    enqueue still reports True while the peer receives nothing. Arming the
    cooldown on that reading tells the one client that genuinely needs repair
    that it was repaired, then silences it for the whole window.

    Also opens an effectiveness window: the next probe for this socket decides
    whether the repair actually made it converge (see note_repair_outcome).
    """
    ts = time.time() if now is None else now
    key = socket_id or _ANON_BUCKET
    with _lock:
        if key not in _last_repair and len(_last_repair) >= _MAX_TRACKED:
            oldest = min(_last_repair, key=_last_repair.get)
            _last_repair.pop(oldest, None)
            _pending_outcome.pop(oldest, None)
        _last_repair[key] = ts
        _pending_outcome[key] = ts
        _stats['attempted'] += 1


def note_repair_outcome(socket_id: str, converged: bool,
                        now: float | None = None) -> bool:
    """Record whether a repair sent to ``socket_id`` actually worked.

    Closes the loop that made the self-heal unfalsifiable: without it, a
    mechanism that fires constantly and fixes nothing looks exactly like one
    that is quietly healing every stall.

    Args:
        socket_id: the socket a repair was previously delivered to.
        converged: True iff that socket's digest has since AGREED with the
            server — i.e. the correction landed and was applied.
        now: epoch seconds; injectable for tests.

    Returns:
        True iff an outcome was actually recorded (a repair was pending for
        this socket). A socket with nothing pending returns False so a caller
        cannot inflate the tally by reporting agreement for sockets that were
        never repaired.
    """
    key = socket_id or _ANON_BUCKET
    with _lock:
        pending = _pending_outcome.pop(key, None)
        if pending is None:
            return False
        _stats['converged' if converged else 'ineffective'] += 1
    logger.info('[DriftRepair] socket=%s repair %s',
                key[:8],
                'CONVERGED' if converged else 'DID NOT converge')
    return True


def repair_stats() -> dict:
    """Attempted / converged / ineffective tallies. Diagnostics and tests."""
    with _lock:
        return dict(_stats)


def tracked_sockets() -> int:
    """Number of sockets currently holding a cooldown. Diagnostics/tests."""
    with _lock:
        return len(_last_repair)


def reset() -> None:
    """Drop all cooldown + outcome state. Test-support only."""
    with _lock:
        _last_repair.clear()
        _pending_outcome.clear()
        for k in _stats:
            _stats[k] = 0
