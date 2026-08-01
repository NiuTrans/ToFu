"""lib/llm_sanitize — Message-list sanitization helpers for the LLM API.

Split from the former monolithic ``lib/llm_sanitize.py`` module into a
facade-preserving package.  Every symbol below is re-exported so that
``from lib.llm_sanitize import X`` keeps working byte-identically for all
consumers (lib/llm/body.py, lib/llm/__init__.py, wire_messages.py, tests).

Public surface
==============
- :data:`_API_MESSAGE_FIELDS` — frozenset of valid OpenAI-compatible message keys
- :func:`_strip_non_api_fields` — drop frontend metadata before sending
- :func:`_strip_tool_calls` — remove tool_calls but keep reasoning fields
- :func:`_sanitize_messages` — apply gateway keyword sanitization in-place
- :func:`_sanitize_gateway_content` — single-string keyword replacement
- :func:`_fix_orphaned_tool_calls` — defensive Anthropic tool_use/tool_result fixer
- :func:`_fix_tool_call_adjacency` — Anthropic adjacency requirement enforcer
- :func:`_fix_empty_user_messages` — replace empty user content with placeholder
- :func:`_drop_empty_assistant_messages` — drop pure-ghost assistant messages
- :func:`_merge_consecutive_same_role` — merge consecutive user/assistant pairs

These functions are pure data transformations with no I/O side effects
beyond logging. They are called from ``build_body`` in lib/llm/body.py
during every API request.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Gateway keyword sanitization (._gateway)
# ══════════════════════════════════════════════════════════

from lib.llm_sanitize._gateway import (  # noqa: E402,F401
    _GATEWAY_BLOCKED_TERMS,
    _sanitize_gateway_content,
    _sanitize_messages,
)


# ══════════════════════════════════════════════════════════
#  API field filtering (._fields)
# ══════════════════════════════════════════════════════════

from lib.llm_sanitize._fields import (  # noqa: E402,F401
    _API_MESSAGE_FIELDS,
    _strip_non_api_fields,
    _strip_tool_calls,
)


# ══════════════════════════════════════════════════════════
#  Tool-call/result repair (._toolcalls)
# ══════════════════════════════════════════════════════════

from lib.llm_sanitize._toolcalls import (  # noqa: E402,F401
    _fix_orphaned_tool_calls,
    _fix_tool_call_adjacency,
)


# ══════════════════════════════════════════════════════════
#  Structural message fixes (._messages)
# ══════════════════════════════════════════════════════════

from lib.llm_sanitize._messages import (  # noqa: E402,F401
    _drop_empty_assistant_messages,
    _fix_empty_user_messages,
    _merge_consecutive_same_role,
    _strip_empty_text_blocks,
)


__all__ = [
    '_GATEWAY_BLOCKED_TERMS',
    '_API_MESSAGE_FIELDS',
    '_sanitize_gateway_content',
    '_sanitize_messages',
    '_strip_non_api_fields',
    '_strip_tool_calls',
    '_fix_orphaned_tool_calls',
    '_fix_tool_call_adjacency',
    '_fix_empty_user_messages',
    '_drop_empty_assistant_messages',
    '_merge_consecutive_same_role',
    '_strip_empty_text_blocks',
]
