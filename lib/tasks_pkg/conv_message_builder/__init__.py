"""Server-side conversation message builder — replaces frontend buildApiMessages().

Loads raw conversation messages from PostgreSQL and transforms them into
the API-ready format that the LLM orchestrator expects.  This eliminates
the need for the frontend to construct messages — the POST body only needs
``{convId, config}``.

The transformations mirror what the old frontend ``buildApiMessages()`` did:
  1. Inject user system prompt (from config)
  2. Skip endpoint-mode display-only messages (_isEndpointPlanner, _isEndpointReview, _epIteration)
  3. Strip <notranslate>/<nt> tags from user text
  4. Prepend reply quotes
  5. Prepend conversation references
  6. Inline PDF text into user content
  7. Build multimodal image blocks (resolve /api/images/ URLs from disk)
  8. Expand stored ``toolRounds`` back into proper OpenAI-style
     ``assistant(tool_calls=[...])`` + ``tool(tool_call_id=..., content=...)``
     message sequences when the rounds have complete info (toolCallId +
     toolContent + status==done).  Falls back to a lossy ``toolSummary``
     JSON placeholder when rounds are legacy/incomplete.  This mirrors
     what ``lib.tasks_pkg.message_builder.inject_tool_history`` produces
     for Continue requests, so the debug preview and the real request
     see the same structure.
  9. Merge consecutive same-role messages (but never across structured
     tool-call sequences).

This module is a pure re-export facade — all implementations live in the
sub-modules below.  The import path ``lib.tasks_pkg.conv_message_builder``
is unchanged, so every ``from lib.tasks_pkg.conv_message_builder import X``
keeps working byte-identically.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Public entrypoints + DB load (._load)
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.conv_message_builder._load import (  # noqa: E402,F401
    build_api_messages_from_db,
    build_branch_api_messages,
    _load_messages_from_db,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Core transformation pipeline (._transform)
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.conv_message_builder._transform import (  # noqa: E402,F401
    _NT_RE,
    _transform_messages,
    _build_user_message,
    _build_assistant_messages,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Structured tool-call reconstruction (._toolcalls)
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.conv_message_builder._toolcalls import (  # noqa: E402,F401
    _reconstruct_tool_call_messages,
    build_assistant_tool_call_message,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Pre-/post-processing passes (._dedup)
# ═══════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.conv_message_builder._dedup import (  # noqa: E402,F401
    _dedup_duplicate_user_messages,
    _collapse_historical_endpoint_sessions,
    _merge_consecutive_same_role,
)


__all__ = [
    'build_api_messages_from_db',
    'build_branch_api_messages',
    '_load_messages_from_db',
    '_transform_messages',
    '_build_user_message',
    '_build_assistant_messages',
    '_reconstruct_tool_call_messages',
    'build_assistant_tool_call_message',
    '_dedup_duplicate_user_messages',
    '_collapse_historical_endpoint_sessions',
    '_merge_consecutive_same_role',
    '_NT_RE',
]
