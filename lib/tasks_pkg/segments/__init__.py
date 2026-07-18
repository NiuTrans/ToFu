"""lib/tasks_pkg/segments/ — the ordered typed-segment model (SoT groundwork).

Board epic ``pt_cb8f98b0cb9b47fb`` (design: docs/EPIC_SEGMENT_TIMELINE_DESIGN.md).

An assistant turn is stored today as THREE parallel channels on the task dict:
``task['content']`` (deliverable string), ``task['thinking']`` (reasoning
string), and ``task['toolRounds']`` (ordered per-round dicts). They are not
interleaved, so the chronological order the model produced
"thinking → prose → tool → prose → answer" is lost — which is the root cause of
both the headless "narrator" leak (streaming compat forwards every delta, incl.
scaffolding prose) and the grouped-by-type UI layout.

This package introduces the replacement: ONE ordered, append-only list of typed
segments (the Anthropic content-blocks shape we already half-emit). Each text
segment carries a ``deliverable`` flag — the structural boundary between the
answer and inter-round narration that the three-channel model lacks.

**Step 1 discipline: SHIPS DARK.** ``assemble_segments`` is populated alongside
the three channels; the three channels are proved to be loss-less *projections*
of the segment list via ``derive_content`` / ``derive_thinking`` /
``derive_tool_rounds``. The golden test ``tests/test_segment_model.py`` pins
byte-identity so none of the ~40 measured backend readers can drift.

Package layout (facade-preserving — every public symbol is re-exported here so
``from lib.tasks_pkg.segments import X`` and ``from lib.tasks_pkg import
segments as seg_mod`` both keep working byte-identically):

  * ``_types``    — SEG_* type tags + RESUMABLE_FINISH_REASONS (the closed
                    vocabulary; defined ONCE, shared by every submodule).
  * ``_assemble`` — assemble_segments + _merged_rounds.
  * ``_derive``   — derive_content / derive_thinking / derive_tool_rounds /
                    deliverable_text + _rounds_view_from_segments.
  * ``_project``  — reconstruct_tool_messages_from_segments /
                    tool_history_from_segments / resume_prefill_from_segments.
  * ``_edit``     — apply_edited_deliverable.
  * ``_serde``    — segments_to_json / rehydrate_segments.

Pure functions; no Flask, no DB, no LLM.
"""

# No code lives in this file — it is a pure re-export facade.
# All implementations live in the sub-modules listed above.

from lib.tasks_pkg.segments._types import (  # noqa: E402,F401
    SEG_THINKING,
    SEG_TEXT,
    SEG_TOOL_USE,
    SEG_TOOL_RESULT,
    RESUMABLE_FINISH_REASONS,
    SYNTHETIC_INBOX_MARKERS,
    is_synthetic_inbox_round,
)

from lib.tasks_pkg.segments._assemble import (  # noqa: E402,F401
    assemble_segments,
    _merged_rounds,
)

from lib.tasks_pkg.segments._derive import (  # noqa: E402,F401
    derive_content,
    derive_thinking,
    derive_tool_rounds,
    deliverable_text,
    _rounds_view_from_segments,
)

from lib.tasks_pkg.segments._project import (  # noqa: E402,F401
    reconstruct_tool_messages_from_segments,
    tool_history_from_segments,
    resume_prefill_from_segments,
)

from lib.tasks_pkg.segments._edit import (  # noqa: E402,F401
    apply_edited_deliverable,
)

from lib.tasks_pkg.segments._serde import (  # noqa: E402,F401
    segments_to_json,
    rehydrate_segments,
)


__all__ = [
    'assemble_segments',
    'derive_content',
    'derive_thinking',
    'derive_tool_rounds',
    'deliverable_text',
    'apply_edited_deliverable',
    'resume_prefill_from_segments',
    'reconstruct_tool_messages_from_segments',
    'tool_history_from_segments',
    'segments_to_json',
    'rehydrate_segments',
    'RESUMABLE_FINISH_REASONS',
    'SEG_THINKING',
    'SEG_TEXT',
    'SEG_TOOL_USE',
    'SYNTHETIC_INBOX_MARKERS',
    'is_synthetic_inbox_round',
]
