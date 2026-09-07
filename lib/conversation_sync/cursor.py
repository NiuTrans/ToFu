"""Versioned opaque cursors for conversation-scoped ordered replay."""

from __future__ import annotations

import hashlib
import re
from typing import Any


_CURSOR_PATTERN = re.compile(r"^c1\.([0-9a-f]{16})\.([0-9a-z]+)$")


class ConversationCursorError(ValueError):
    """The caller supplied a malformed or wrong-conversation cursor."""


def _scope_fingerprint(conversation_id: str, user_id: Any) -> str:
    scope = f"{user_id}:{conversation_id}".encode("utf-8")
    return hashlib.sha256(scope).hexdigest()[:16]


def _base36(value: int) -> str:
    if value < 0:
        raise ValueError("cursor sequence cannot be negative")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    encoded = ""
    while value:
        value, remainder = divmod(value, 36)
        encoded = alphabet[remainder] + encoded
    return encoded


def encode_cursor(conversation_id: str, user_id: Any, sequence: int) -> str:
    """Encode a replay sequence without exposing it as an API contract."""
    return (
        f"c1.{_scope_fingerprint(conversation_id, user_id)}."
        f"{_base36(int(sequence))}"
    )


def decode_cursor(conversation_id: str, user_id: Any, cursor: str | None) -> int:
    """Decode and scope-check a cursor; an omitted cursor means stream start."""
    if cursor in (None, ""):
        return 0
    if not isinstance(cursor, str) or len(cursor) > 128:
        raise ConversationCursorError("cursor_invalid")
    match = _CURSOR_PATTERN.fullmatch(cursor)
    if match is None or match.group(1) != _scope_fingerprint(
        conversation_id, user_id
    ):
        raise ConversationCursorError("cursor_invalid")
    try:
        return int(match.group(2), 36)
    except ValueError as exc:
        raise ConversationCursorError("cursor_invalid") from exc


__all__ = ["ConversationCursorError", "decode_cursor", "encode_cursor"]
