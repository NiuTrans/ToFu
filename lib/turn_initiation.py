"""Canonical vocabulary for the actor that initiated a conversation turn.

Writers stamp one validated ``_initiator`` value. Readers never infer identity
from feature-specific flags, which keeps attribution independent of autopilot,
scheduler, peer-message and project-brain implementation details.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

INITIATOR_HUMAN = "human"
INITIATOR_AUTOPILOT = "autopilot"
INITIATOR_PROACTIVE = "proactive"
INITIATOR_TIMER = "timer"
INITIATOR_BRAIN = "brain"
INITIATOR_PEER = "peer"
INITIATOR_OPERATOR = "operator"
INITIATOR_SWARM = "swarm"

NON_HUMAN_INITIATORS = frozenset(
    {
        INITIATOR_AUTOPILOT,
        INITIATOR_PROACTIVE,
        INITIATOR_TIMER,
        INITIATOR_BRAIN,
        INITIATOR_PEER,
        INITIATOR_OPERATOR,
        INITIATOR_SWARM,
    }
)
VALID_INITIATORS = frozenset({INITIATOR_HUMAN}) | NON_HUMAN_INITIATORS


def stamp_initiator(message: dict[str, Any], initiator: str) -> dict[str, Any]:
    """Stamp a controlled initiator in place and return ``message``.

    Human input remains unstamped because absence is the canonical human
    representation. Unknown initiators are rejected loudly at the write seam.
    """
    if not isinstance(message, dict):
        raise TypeError("turn message must be a dict")
    if initiator == INITIATOR_HUMAN:
        message.pop("_initiator", None)
        return message
    if initiator not in NON_HUMAN_INITIATORS:
        raise ValueError(f"unknown turn initiator: {initiator!r}")
    message["_initiator"] = initiator
    return message


def resolve_initiator(message: dict[str, Any]) -> str:
    """Return the canonical initiator; malformed explicit values fail closed."""
    if not isinstance(message, dict):
        return INITIATOR_HUMAN
    initiator = message.get("_initiator")
    if initiator is None:
        return INITIATOR_HUMAN
    if initiator not in VALID_INITIATORS:
        logger.warning("[Initiator] unknown persisted value=%r", initiator)
        return INITIATOR_HUMAN
    return initiator


def is_auto_initiated(message: dict[str, Any]) -> bool:
    """Return whether the turn was initiated outside the human input box."""
    return resolve_initiator(message) in NON_HUMAN_INITIATORS


__all__ = [
    "INITIATOR_HUMAN",
    "INITIATOR_AUTOPILOT",
    "INITIATOR_PROACTIVE",
    "INITIATOR_TIMER",
    "INITIATOR_BRAIN",
    "INITIATOR_PEER",
    "INITIATOR_OPERATOR",
    "INITIATOR_SWARM",
    "NON_HUMAN_INITIATORS",
    "VALID_INITIATORS",
    "stamp_initiator",
    "resolve_initiator",
    "is_auto_initiated",
]
