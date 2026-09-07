"""Layer 2 — query-aware LLM summary with selective turn compression.

Force-injected by the orchestrator only — NOT in the model's tool list.
Triggered when estimated tokens exceed ``_SUMMARY_TRIGGER_RATIO`` of
usable context.

This package is a FACADE: the import path
``lib.tasks_pkg.compaction._layer2`` is unchanged and every symbol that used
to live in the old flat ``_layer2.py`` module is re-exported here so all
existing importers resolve byte-identically.  Implementations live in the
cohesive sub-modules:

  * ``_prompt``  — ``_SUMMARY_SYSTEM_PROMPT`` + input formatting/budget.
  * ``_anchor``  — objective anchor / boundary / query & file extraction.
  * ``_summary`` — ``_generate_query_aware_summary`` (cheap-model dispatch).
  * ``_compact`` — public entrypoints (``execute_compact_tool`` etc.).

Public surface:
  * ``execute_compact_tool``       — generates the summary, mutates messages
  * ``force_compact_if_needed``    — gates on threshold + injects synthetic pair
  * Boundary helpers:
      - ``_objective_anchor_index``
      - ``_extract_current_query``
      - ``_find_turn_boundary``
      - ``_format_messages_for_summary``
      - ``_generate_query_aware_summary``
      - ``_extract_recently_accessed_files``
  * ``_SUMMARY_SYSTEM_PROMPT`` — the cheap-model system prompt
"""

from lib.log import get_logger

# Re-exported so back-compat consumers / tests can reference (and monkeypatch)
# them on the ``_layer2`` namespace exactly as they could against the old flat
# module (which imported them at top level). ``_compact`` resolves these two
# through this package at call time, so a patch here takes effect.
from lib.tasks_pkg.compaction._archive import _archive_transcript  # noqa: F401
from lib.tasks_pkg.compaction._constants import _COMPACT_TOOL_NAME  # noqa: F401

from lib.tasks_pkg.compaction._layer2._prompt import (  # noqa: F401
    _SUMMARY_SYSTEM_PROMPT,
    _extract_summary_objective,
    _format_messages_for_summary,
    _summary_input_char_budget,
)
from lib.tasks_pkg.compaction._layer2._anchor import (  # noqa: F401
    _apiform_tool_rounds,
    _coerce_spec_list,
    _collect_user_verbatim,
    _extract_current_query,
    _extract_objective_anchor_text,
    _extract_recently_accessed_files,
    _find_turn_boundary,
    _fold_recent_intra_turn,
    _objective_anchor_index,
    _split_cold_rounds,
)
from lib.tasks_pkg.compaction._layer2._summary import (  # noqa: F401
    _generate_query_aware_summary,
)
from lib.tasks_pkg.compaction._layer2._compact import (  # noqa: F401
    execute_compact_tool,
    force_compact_if_needed,
)

logger = get_logger(__name__)

__all__ = [
    '_SUMMARY_SYSTEM_PROMPT',
    '_extract_summary_objective',
    '_format_messages_for_summary',
    '_summary_input_char_budget',
    '_coerce_spec_list',
    '_extract_current_query',
    '_extract_objective_anchor_text',
    '_collect_user_verbatim',
    '_extract_recently_accessed_files',
    '_find_turn_boundary',
    '_objective_anchor_index',
    '_split_cold_rounds',
    '_apiform_tool_rounds',
    '_fold_recent_intra_turn',
    '_generate_query_aware_summary',
    '_archive_transcript',
    '_COMPACT_TOOL_NAME',
    'execute_compact_tool',
    'force_compact_if_needed',
]
