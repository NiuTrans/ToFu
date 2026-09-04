"""Identify executor tasks that belong to the conversation turn authority.

The public conversation model is addressed by a stable ``turnId`` and
``attemptId``.  Executor task ids are private transport details.  This module
is the single predicate used at the task/conversation boundary; callers must
not introduce protocol-version flags or infer authority from settings.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_NON_AUTHORITY_TASK_FLAGS = ("_inline_messages", "_vu_subtask")


def is_conversation_attempt(context: Mapping[str, Any] | None) -> bool:
    """Return whether *context* owns a complete turn/attempt identity.

    Inline and virtual-user carrier tasks are transport holders, not turn
    executors.  They must fail closed even if a shallow-copied parent config
    left stale identity fields behind; otherwise their private projection can
    overwrite the parent's authoritative turn while both streams are live.
    """
    if not isinstance(context, Mapping):
        return False
    if any(context.get(flag) for flag in _NON_AUTHORITY_TASK_FLAGS):
        return False
    return bool(
        str(context.get("_turnId") or "").strip()
        and str(context.get("_attemptId") or "").strip()
    )


def persisted_result_is_conversation_attempt(
    metadata: Mapping[str, Any] | None,
) -> bool:
    """Recognize persisted executor rows without a redundant mode marker."""
    if not isinstance(metadata, Mapping):
        return False
    return bool(
        str(metadata.get("turnId") or "").strip()
        and str(metadata.get("attemptId") or "").strip()
    )


__all__ = [
    "is_conversation_attempt",
    "persisted_result_is_conversation_attempt",
]
