"""_RoundState — the ONE flat carrier for run_task's cross-iteration locals.

pt_862771477a86 slice 1 (owner-scoped, rulings 2026-07-27): a PURE CONTAINER
SWAP — the 14 locals of the stream main loop that cross the iteration
boundary live here instead of as bare function locals, so the loop body can
later be extracted into chassis hooks without re-discovering what crosses.

Shape rulings (owner, recorded in docs/ROUND_STATE_LOCALS_INVENTORY.md §5):
  * FLAT — no control/llm/usage/tools sub-objects (grouping lives in the
    comments below, not in the type, so access stays ``rs.exit_reason``
    rather than ``rs.control.exit_reason``).
  * ``round_num`` and ``_premature_retry_count`` are NOT here — the chassis
    (run_agent_loop) owns the round index and the retry bonus natively;
    they stay plain locals until the cutover. (Inventory counted 16
    cross-iteration locals; −2 chassis-owned = 14 fields.)
  * task-dict channels (``_peer_inject_pending`` etc.) are NOT here either —
    their owner is the task (crash-recovery / sync layer consume them
    directly); absorbing them would create a second source of truth.

Field defaults reproduce the historical inline initializers byte-for-byte
(:451-456 and :505-511 of the pre-slice _run.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ['RoundState']


@dataclass
class RoundState:
    """Flat bag of the 14 cross-iteration locals (see module docstring)."""

    # ── constructor-required: resolved model config (fallback swaps these
    #    in-loop at the llm_result write-back) ──
    model: str
    preset: str
    thinking_enabled: bool

    # ── control ──
    exit_reason: str = 'running'                # set by natural/error exit
    abort_phase: str | None = None              # was _abort_detected_phase
    consecutive_tool_timeouts: int = 0          # breaker counter (≥3 → halt)
    last_checkpoint_ts: float = 0.0             # crash-checkpoint throttle

    # ── llm results (sticky "last round" values, read post-loop) ──
    assistant_msg: dict[str, Any] | None = None
    last_finish_reason: str | None = None
    last_usage: dict[str, Any] | None = None

    # ── usage accumulation ──
    accumulated_usage: dict[str, Any] = field(default_factory=dict)
    api_rounds: list = field(default_factory=list)

    # ── tools ──
    tool_call_happened: bool = False
    tool_round_num: int = 0                     # tool-round number allocator
