"""Storage-neutral contract for durable conversation turn-source queues.

The application queue, Sidecar validation, and turn-creation supersession
rules import this module so kind names and priority meaning cannot drift into
independent state machines. It owns no storage or dispatch behavior.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final


KIND_REAL: Final = "real"
KIND_GOAL_CONTINUATION: Final = "goal_continuation"
KIND_PEER_MSG: Final = "peer_msg"
KIND_WORKFLOW: Final = "workflow_step"
KIND_AUTOPILOT: Final = "autopilot"

TURN_SOURCE_KIND_ORDER: Final = (
    KIND_REAL,
    KIND_GOAL_CONTINUATION,
    KIND_PEER_MSG,
    KIND_WORKFLOW,
    KIND_AUTOPILOT,
)
TURN_SOURCE_KINDS: Final = frozenset(TURN_SOURCE_KIND_ORDER)
TURN_SOURCE_PRIORITY: Final = MappingProxyType({
    KIND_REAL: 10,
    KIND_GOAL_CONTINUATION: 20,
    KIND_PEER_MSG: 40,
    KIND_WORKFLOW: 50,
    KIND_AUTOPILOT: 90,
})
UNKNOWN_TURN_SOURCE_PRIORITY: Final = 100

# Rolling-upgrade capability for the maintenance reaper's read-first path.
# A new application skips the writer command only after a new Sidecar echoes
# this exact contract alongside a boolean derived from the same bounded queue
# listing. Old Sidecars ignore the additive request members and return the
# legacy bare list, which keeps the command-first compatibility behavior.
QUEUE_REAP_PROBE_CONTRACT: Final = "tofu.queue.reap-probe/v1"
QUEUE_REAP_PROBE_REQUEST_FIELD: Final = "reap_probe_contract"
QUEUE_REAP_PROBE_RESPONSE_FIELD: Final = "reapProbeContract"
QUEUE_REAP_PROBE_CONVERSATIONS_FIELD: Final = "conversations"
QUEUE_REAP_PROBE_HAS_EXPIRED_FIELD: Final = "hasExpiredLeases"


def turn_source_priority(kind: str) -> int:
    """Return the canonical queue priority for a declared source kind."""
    return int(TURN_SOURCE_PRIORITY.get(kind, UNKNOWN_TURN_SOURCE_PRIORITY))


__all__ = [
    "KIND_AUTOPILOT",
    "KIND_GOAL_CONTINUATION",
    "KIND_PEER_MSG",
    "KIND_REAL",
    "KIND_WORKFLOW",
    "QUEUE_REAP_PROBE_CONTRACT",
    "QUEUE_REAP_PROBE_CONVERSATIONS_FIELD",
    "QUEUE_REAP_PROBE_HAS_EXPIRED_FIELD",
    "QUEUE_REAP_PROBE_REQUEST_FIELD",
    "QUEUE_REAP_PROBE_RESPONSE_FIELD",
    "TURN_SOURCE_KIND_ORDER",
    "TURN_SOURCE_KINDS",
    "TURN_SOURCE_PRIORITY",
    "UNKNOWN_TURN_SOURCE_PRIORITY",
    "turn_source_priority",
]
