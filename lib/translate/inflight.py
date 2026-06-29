"""Per-message in-flight guard for server-side auto-translation.

WHY
---
The server-side auto-translate safety net can be invoked for the SAME message
from several independent paths that race:

  * the single-turn safety net (``manager._sync_result_to_conversation``),
  * the endpoint per-turn trigger (``endpoint._trigger_per_turn_auto_translate``),
  * the endpoint end-of-task rescan (``endpoint._trigger_endpoint_auto_translate``),
  * (and the frontend, via its own ``/api/translate/start``).

Historically dedup leaned on TWO weak signals checked just before spawning:
``translatedContent`` already existing in the row, and a scan of the live
``_translate_tasks`` registry for a RUNNING task with a matching ``msgIdx``.
Both race: between "I checked, nothing is running" and "I spawned my thread",
another path can spawn too — and the msgIdx-keyed scan is defeated the moment a
concurrent insert shifts the index. The result is two (or more) translate
threads for one message, the slower clobbering the faster (the reported
"translation flickers / sometimes wrong" instability).

This module is the authoritative, atomic, identity-keyed guard: a caller
``claim``s ``(conv_id, msg_key)`` BEFORE scheduling any work; a second caller's
claim returns False so it stands down. The claim is released when the work
settles (committed / failed / handed off and finalized). Entries self-expire
after a TTL so a crashed worker can never wedge a message permanently.

The key is the stable ``_msgId`` whenever the message has one (robust against
concurrent inserts); callers without an id fall back to an index-derived key
(``#idx:<n>``) which is strictly better than the old "no guard at all".
"""

import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

# A claimed entry older than this is treated as stale and may be re-claimed —
# a translate worker that crashed without releasing must not wedge the message
# forever. Comfortably longer than the translate engine's own retry budget so
# we never re-claim a still-legitimately-running translation.
_INFLIGHT_TTL = 900.0  # 15 min

_lock = threading.Lock()
# (conv_id, msg_key) -> claimed_at epoch seconds
_inflight: dict[tuple[str, str], float] = {}


def msg_key(msg_id, msg_idx) -> str:
    """Derive the dedup key for a message.

    Prefers the stable ``_msgId`` (insert-drift-proof); falls back to an
    index-derived key only when no id exists. Returns ``''`` when neither is
    usable (caller should then skip the guard rather than claim a bogus key).
    """
    if msg_id:
        return str(msg_id)
    if msg_idx is not None:
        try:
            return f'#idx:{int(msg_idx)}'
        except (ValueError, TypeError) as e:
            logger.debug('[InFlight] non-numeric msg_idx %r: %s', msg_idx, e)
    return ''


def claim_inflight(conv_id, msg_id, msg_idx) -> bool:
    """Atomically claim a translation slot for ``(conv_id, message)``.

    Returns True when the caller now OWNS the slot (must eventually call
    :func:`release_inflight`), or False when another live claim already exists
    (the caller must stand down and NOT schedule a duplicate translation).

    A stale claim (older than ``_INFLIGHT_TTL``) is silently taken over.
    When the message has no usable key (no id and no index) the guard is a
    no-op and returns True (degrade to the pre-guard behaviour rather than
    block translation).
    """
    if not conv_id:
        return True
    key = msg_key(msg_id, msg_idx)
    if not key:
        return True
    now = time.time()
    full = (conv_id, key)
    with _lock:
        prev = _inflight.get(full)
        if prev is not None and (now - prev) < _INFLIGHT_TTL:
            logger.info('[InFlight] conv=%s key=%s already claimed %.0fs ago — '
                        'standing down (dedup)', conv_id[:8], key[:12], now - prev)
            return False
        if prev is not None:
            logger.info('[InFlight] conv=%s key=%s stale claim (%.0fs) — taking over',
                        conv_id[:8], key[:12], now - prev)
        _inflight[full] = now
        return True


def release_inflight(conv_id, msg_id, msg_idx) -> None:
    """Release a previously-claimed slot. Idempotent / best-effort."""
    if not conv_id:
        return
    key = msg_key(msg_id, msg_idx)
    if not key:
        return
    with _lock:
        _inflight.pop((conv_id, key), None)


def is_inflight(conv_id, msg_id, msg_idx) -> bool:
    """Read-only probe: True iff a live (non-stale) claim exists."""
    if not conv_id:
        return False
    key = msg_key(msg_id, msg_idx)
    if not key:
        return False
    with _lock:
        prev = _inflight.get((conv_id, key))
        return prev is not None and (time.time() - prev) < _INFLIGHT_TTL
