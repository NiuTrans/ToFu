"""lib/scheduler/timer/_state.py — Shared in-memory timer registry state.

⚠️ CRITICAL: These four module-level objects are the SINGLE source of truth
for the timer subsystem's process-wide shared state. They MUST live in exactly
ONE module and be shared BY REFERENCE (imported, never re-created) by every
other submodule, and re-exported from the package ``__init__``.

  • ``_active_timers``   — {timer_id: threading.Thread} registry of live loops
  • ``_timers_lock``     — guards ``_active_timers``
  • ``_last_cmd_outputs``— {timer_id: str} last check_command output (early-exit)
  • ``_cmd_outputs_lock``— guards ``_last_cmd_outputs``

NB: ``lib/scheduler/executor.py`` imports ``_cmd_outputs_lock`` and
``_last_cmd_outputs`` DIRECTLY via ``from lib.scheduler.timer import ...`` — so
those two names MUST remain the SAME objects the poll loop reads/writes.
"""

from __future__ import annotations

import threading

from lib.log import get_logger

logger = get_logger(__name__)

# ── In-memory registry of active timer threads ──────────────────────────────

_active_timers: dict[str, threading.Thread] = {}
_timers_lock = threading.Lock()

# ── Per-timer cache of last check_command output for early-exit filtering ────
# If the command output hasn't changed since the last poll, we skip the LLM
# call entirely — saves tokens and reduces frontend noise.
_last_cmd_outputs: dict[str, str] = {}
_cmd_outputs_lock = threading.Lock()
