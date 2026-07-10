"""lib/conversations/turn_initiation.py — who STARTED a conversation turn.

The app has many code paths that inject a turn WITHOUT a human typing into the
input box: the Project-Brain autonomous dispatch, the proactive-agent
scheduler, timer continuations, swarm auto-continuation, peer / operator
nudges, and the autopilot virtual user. Historically each path stamped its OWN
ad-hoc boolean on the message dict (``_brainDispatch``, ``_proactive``,
``_timer``, ``_swarmAutoContinue``, ``_peerMessage``/``_peerHuman``,
``_isVirtualUser``/``_autopilotRunId``) and every reader had to know the whole
zoo — so three of them (proactive / timer / brain) fell through the frontend
avatar chain and rendered as the human "You".

This module makes ``_initiator`` the SINGLE authoritative field naming who
started the turn, and exposes ONE resolver every reader goes through:

  * :func:`stamp_initiator` — the one write seam (wired into the three backend
    injection choke points).
  * :func:`resolve_initiator` — the one read seam. Reads ``_initiator`` first,
    then falls back to the legacy booleans (one-directional migration: we never
    write the legacy booleans FROM ``_initiator``, we only read them when
    ``_initiator`` is absent on a pre-migration / persisted message).

Keeping the derivation in exactly one function is the point: we are migrating
AWAY from scattered inference, so we must not spawn a second scattered
inference layer while doing it. Both the backend ``reconcile._is_special_turn``
and the frontend registry lookup resolve through this same vocabulary.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

# ── Initiator vocabulary ──────────────────────────────────────────────────
#: A human typed into the input box (the default; NOT stamped explicitly).
INITIATOR_HUMAN = 'human'
#: Autopilot virtual user kept the conversation going.
INITIATOR_AUTOPILOT = 'autopilot'
#: The proactive-agent scheduler polled and decided to act.
INITIATOR_PROACTIVE = 'proactive'
#: A timer / watcher continuation fired.
INITIATOR_TIMER = 'timer'
#: The Project Brain autonomously dispatched an open board epic.
INITIATOR_BRAIN = 'brain'
#: A sibling conversation (agent) sent a peer message.
INITIATOR_PEER = 'peer'
#: A human operator nudged the conversation from the Team panel.
INITIATOR_OPERATOR = 'operator'
#: The swarm inbox auto-continued the turn to drain sub-agent updates.
INITIATOR_SWARM = 'swarm'

#: Every non-human initiator we stamp. ``human`` is the implicit default and is
#: intentionally NOT in this set — a human turn carries no ``_initiator``.
NON_HUMAN_INITIATORS = frozenset({
    INITIATOR_AUTOPILOT, INITIATOR_PROACTIVE, INITIATOR_TIMER,
    INITIATOR_BRAIN, INITIATOR_PEER, INITIATOR_OPERATOR, INITIATOR_SWARM,
})

#: All valid values ``_initiator`` may carry.
VALID_INITIATORS = frozenset({INITIATOR_HUMAN}) | NON_HUMAN_INITIATORS


def stamp_initiator(msg: dict[str, Any], initiator: str) -> dict[str, Any]:
    """Stamp ``msg['_initiator']`` with a validated initiator value in place.

    This is the ONE write seam. Callers pass a controlled-vocabulary value
    (one of the ``INITIATOR_*`` constants). A ``human`` initiator is a no-op
    (a human turn stays unstamped — the absence of ``_initiator`` IS "human"),
    so callers can pass it unconditionally. An unknown value is logged and
    dropped rather than silently persisted (fail-loud on a typo'd source).

    Args:
        msg: The message dict to stamp (mutated in place).
        initiator: One of the ``INITIATOR_*`` constants.

    Returns:
        The same ``msg`` dict (for chaining).
    """
    if not isinstance(msg, dict):
        return msg
    if initiator == INITIATOR_HUMAN:
        return msg
    if initiator not in NON_HUMAN_INITIATORS:
        logger.warning('[Initiator] stamp_initiator called with unknown '
                       'initiator=%r — ignored', initiator)
        return msg
    msg['_initiator'] = initiator
    return msg


def resolve_initiator(msg: dict[str, Any]) -> str:
    """Return WHO initiated ``msg`` — the ONE read seam.

    Resolution order (one-directional migration):
      1. The authoritative ``_initiator`` field, if it carries a known value.
      2. Otherwise fall back to the legacy per-path booleans (for messages
         persisted before ``_initiator`` existed, or written by a path not yet
         migrated). This fallback is the ONLY place that reads the legacy
         markers — every other reader calls this function.

    Never raises; returns :data:`INITIATOR_HUMAN` for a plain human turn or any
    unrecognised shape.
    """
    if not isinstance(msg, dict):
        return INITIATOR_HUMAN

    v = msg.get('_initiator')
    if v in VALID_INITIATORS:
        return v

    # ── Legacy fallback (ordered; peer/operator first as it is the most
    #    specific pair, then autopilot, then the single-marker paths). ──
    if msg.get('_peerMessage'):
        return INITIATOR_OPERATOR if msg.get('_peerHuman') else INITIATOR_PEER
    if msg.get('_isVirtualUser') or msg.get('_autopilotRunId'):
        return INITIATOR_AUTOPILOT
    if msg.get('_proactive'):
        return INITIATOR_PROACTIVE
    if msg.get('_timer'):
        return INITIATOR_TIMER
    if msg.get('_brainDispatch'):
        return INITIATOR_BRAIN
    if msg.get('_swarmAutoContinue'):
        return INITIATOR_SWARM
    return INITIATOR_HUMAN


def is_auto_initiated(msg: dict[str, Any]) -> bool:
    """True iff the turn was started by anything other than a human input-box
    message (resolved through :func:`resolve_initiator`)."""
    return resolve_initiator(msg) in NON_HUMAN_INITIATORS


__all__ = [
    'INITIATOR_HUMAN', 'INITIATOR_AUTOPILOT', 'INITIATOR_PROACTIVE',
    'INITIATOR_TIMER', 'INITIATOR_BRAIN', 'INITIATOR_PEER',
    'INITIATOR_OPERATOR', 'INITIATOR_SWARM',
    'NON_HUMAN_INITIATORS', 'VALID_INITIATORS',
    'stamp_initiator', 'resolve_initiator', 'is_auto_initiated',
]
