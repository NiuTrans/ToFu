"""Identify executor tasks that belong to the conversation turn authority.

The public conversation model is addressed by a stable ``turnId`` and
``attemptId``.  Executor task ids are private transport details.  This module
is the single predicate used at the task/conversation boundary; callers must
not introduce protocol-version flags or infer authority from settings.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def is_conversation_attempt(context: Mapping[str, Any] | None) -> bool:
    """Return whether *context* carries a complete turn/attempt identity."""
    if not isinstance(context, Mapping):
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
