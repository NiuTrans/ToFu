"""Per-round open — ROUND_START event + phase emit + accumulator build.

Extracted 2026-08-01 ( slice 32) from ``run_task``'s stream
loop. Two helpers at two different points of each iteration:

* ``emit_round_open`` runs at the TOP of the iteration (right after the
  abort gate): RENDER_CONTRACT Phase 3's explicit ROUND boundary plus
  the per-round phase event.
* ``build_stream_accumulator`` runs later (after round-request prep):
  the StreamingToolAccumulator construction.

The accumulator's project path deliberately comes from
``cfg.get('projectPath')`` — NOT the resolved ``project_path`` local —
byte-exact preservation of the inline original.
"""

from __future__ import annotations

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event
from lib.tasks_pkg.orchestrator._finalize import _emit_tool_round_phase

logger = get_logger(__name__)


def emit_round_open(task, rs, round_num):
    """Emit the ROUND boundary + per-round phase event.

    RENDER_CONTRACT Phase 3: explicit ROUND_START at the TOP of every
    round the model actually runs — INCLUDING a prose-only round (no
    tool calls), which previously had NO signal the client could key
    round attribution off. The reducer opens the round here off the
    canonical roundNum instead of inferring it from the first
    tool_start / llmRound grouping. The phase event anchors on {} for
    round 0 (no assistant message yet) and rs.assistant_msg after.
    """
    append_event(task, build_event(EventType.ROUND_START, roundNum=round_num))
    _emit_tool_round_phase(
        task, rs.assistant_msg if round_num > 0 else {}, round_num)


def build_stream_accumulator(task, rs, cfg, round_num, project_enabled):
    """Construct the per-round StreamingToolAccumulator.

    Streaming tool execution: pre-executes read-only tools while the
    model is still generating subsequent tool calls, and emits
    tool_start events immediately during streaming so the frontend
    shows "Searching…" / "Running…" without delay.
    """
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    return StreamingToolAccumulator(
        task, project_path=cfg.get('projectPath'),
        tool_round_num=rs.tool_round_num,
        round_num=round_num,
        project_enabled=project_enabled,
    )
