# HOT_PATH
"""Shared per-conversation state for the faithful compaction methods.

This module is the SINGLE owner of the process-wide mutable state used by
the OpenCode / Hermes / OpenClaw summarizers: the iterative running-summary
map, the cooldown map, and their guarding lock.  Every other faithful
submodule imports these BY REFERENCE from here, so there is exactly one
``_running_summaries`` dict in the process and ``reset_running_summary`` +
``_cooldown_ok`` read/write the SAME dict.
"""

from __future__ import annotations

import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)


def _log_id(conv_id: str) -> str:
    return conv_id[:8] if conv_id else '?'


# ── Per-conversation iterative-summary + cooldown state ─────────────────
_running_summaries: dict[str, str] = {}
_summary_state_lock = threading.Lock()
_last_summary_at: dict[str, float] = {}
_FAITHFUL_SUMMARY_COOLDOWN = 15.0


def reset_running_summary(conv_id: str) -> None:
    """Drop a conversation's running-summary + cooldown (call on reset)."""
    with _summary_state_lock:
        _running_summaries.pop(conv_id, None)
        _last_summary_at.pop(conv_id, None)


def _cooldown_ok(conv_id: str) -> bool:
    with _summary_state_lock:
        last = _last_summary_at.get(conv_id, 0)
        if time.time() - last < _FAITHFUL_SUMMARY_COOLDOWN:
            return False
        _last_summary_at[conv_id] = time.time()
        return True
