"""Conversation Reference — retrieve and format other conversations for cross-referencing.

Provides two tool implementations:
  - list_conversations: search/list conversations with optional keyword filter
  - get_conversation: retrieve full conversation content by ID

This package is a pure re-export facade — all implementations live in the
sub-modules (``_query``, ``_detail``, ``_tool``). The package is stateless and
reads only through the owner-scoped conversation repository.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ── Search/list surface (._query) ───────────────────────────────────────────
from lib.conv_ref._query import (  # noqa: E402,F401
    list_conversations,
)

# ── Single-conversation render surface (._detail) ────────────────────────────
from lib.conv_ref._detail import (  # noqa: E402,F401
    get_conversation,
    build_conversation_digest,
    _extract_text,
    _format_tool_rounds,
    _extract_result_text,
    _truncate,
)

# ── Tool-dispatch entrypoint (._tool) ────────────────────────────────────────
from lib.conv_ref._tool import execute_conv_ref_tool, raw_requested  # noqa: E402,F401


__all__ = [
    'list_conversations',
    'get_conversation',
    'build_conversation_digest',
    '_extract_text',
    '_format_tool_rounds',
    '_extract_result_text',
    '_truncate',
    'execute_conv_ref_tool',
    'raw_requested',
]
