"""lib/agent_inbox/_state.py — the SINGLE process-wide home of the inbox state.

This module owns the exactly-once delivery registry.  **Every** queue operation
imports these objects BY REFERENCE (``from lib.agent_inbox._state import
_inboxes`` etc.) so there is exactly one ``_inboxes`` / ``_tombstones`` in the
process.  A divergent copy would drop peer messages or double-deliver — breaking
the exactly-once guarantee.  Do NOT reassign these names anywhere; only mutate
the objects in place under ``_lock``.
"""

from __future__ import annotations

import threading
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════
#  Priority order (lower number = higher priority)
# ═══════════════════════════════════════════════════════════

_PRIORITY: dict[str, int] = {
    'now':   0,   # urgent — drained before user-typed input
    'next':  1,   # default — drained alongside user input
    'later': 2,   # background — system notifications, never starves user
}


# ═══════════════════════════════════════════════════════════
#  Per-task storage
# ═══════════════════════════════════════════════════════════

# task_id → list[InboxItem]; accepted items remain until drain/clear.
_inboxes: dict[str, list[dict[str, Any]]] = {}
_lock = threading.Lock()

#: Tombstone: keys whose owning task has ended.  Late-arriving producers
#: (e.g. a swarm worker that finished after ``clear()`` was called) will be
#: prevented from re-creating the inbox until explicit revival or process exit.
_tombstones: set[str] = set()


#: Consumed durable identities grouped by the task/conversation receiver slot.
#: Clear/revival retains these proofs because old producers can still publish.
#: No count eviction: forgetting an identity can re-deliver accepted work.
#: Process shutdown owns retirement; test reset may explicitly forget a slot.
#: No payloads or durable delivery history are held here.
_consumed_queue_ids: dict[str, set[str]] = {}
