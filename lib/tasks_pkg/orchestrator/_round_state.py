"""_RoundState — the ONE flat carrier for run_task's cross-iteration locals.

 slice 1 (owner-scoped, rulings 2026-07-27): a PURE CONTAINER
SWAP — the 15 values of the stream main loop that cross the iteration
boundary live here instead of as bare function locals, so the loop body can
later be extracted into chassis hooks without re-discovering what crosses.

Shape rulings (owner, recorded in docs/modules/task_engine.md §5):
  * FLAT — no control/llm/usage/tools sub-objects (grouping lives in the
    comments below, not in the type, so access stays ``rs.exit_reason``
    rather than ``rs.control.exit_reason``).
  * ``round_num`` and ``premature_retry_count`` are NOT here. After the
    shared-runner cutover, ``run_agent_loop`` owns the round index and the
    root policy adapter owns the stream-analyser retry projection; neither is
    a bare ``run_task`` cross-iteration local. The original inventory counted
    16 locals and excluded those two; the versioned ``ProviderStreamResult``
    later added one explicit carrier so terminal stream state is not degraded
    back to the legacy tuple (15 fields total).
  * task-dict channels (``_peer_inject_pending`` etc.) are NOT here either —
    their owner is the task (crash-recovery / sync layer consume them
    directly); absorbing them would create a second source of truth.

Field defaults reproduce the historical inline initializers byte-for-byte
(:451-456 and :505-511 of the pre-slice _run.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lib.llm.stream_result import ProviderStreamResult

__all__ = ['RoundState']


@dataclass
class RoundState:
    """Flat bag of the 15 cross-iteration values (see module docstring)."""

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
    last_stream_result: ProviderStreamResult | None = None

    # ── usage accumulation ──
    accumulated_usage: dict[str, Any] = field(default_factory=dict)
    api_rounds: list = field(default_factory=list)

    # ── tools ──
    tool_call_happened: bool = False
    tool_round_num: int = 0                     # tool-round number allocator
