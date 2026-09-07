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

# task_id → list[InboxItem]; never grows beyond MAX_PER_TASK
_inboxes: dict[str, list[dict[str, Any]]] = {}
_lock = threading.Lock()

#: Hard cap per task to prevent runaway memory if orchestrator stops draining.
#: Items beyond this are dropped with a warning — the main agent will see fewer
#: notifications, but at least the process won't OOM. Calibrated to ~500 KB
#: assuming 2KB per swarm-update.
MAX_PER_TASK = 256

#: Tombstone: task_ids whose owning task has ended.  Late-arriving sub-agents
#: (a swarm worker that finished after ``clear()`` was called) will be
#: prevented from re-creating the inbox.  Bounded to avoid unbounded growth.
_tombstones: set[str] = set()
_TOMBSTONE_MAX = 1024
