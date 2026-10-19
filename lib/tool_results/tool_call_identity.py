"""Execution-safe tool-call correlation-id repair.

Responsibility
--------------
This module owns source-level call-id repair for agent runners. Repair is
performed before assistant/tool messages are appended so every logical result
has one unambiguous protocol owner. Provider response positions are execution
occurrences: two distinct items remain distinct even when their tool name and
arguments are byte-for-byte equal. Content equality is never proof of a
transport replay.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, MutableSet, Sequence
from typing import Any


def ensure_unique_tool_call_ids(
    tool_calls: Sequence[Any] | Iterable[Any],
    claimed_ids: MutableSet[str],
    *,
    id_prefix: str,
) -> int:
    """Mutate calls so every retained executable occurrence has a unique id.

    ``claimed_ids`` is caller-owned and may span rounds.  IDs are repaired on
    the assistant call object itself, before result creation, so source history
    never relies on a later wire-only sanitizer to guess occurrence pairing.
    Non-dict entries remain untouched for independent malformed-call handling.
    """
    repaired = 0
    safe_prefix = "".join(
        character if character.isalnum() or character in "_-" else "_"
        for character in str(id_prefix or "call")
    )[:48] or "call"
    for position, tool_call in enumerate(tool_calls or ()):
        if not isinstance(tool_call, dict):
            continue
        raw_id = tool_call.get("id")
        call_id = str(raw_id or "").strip()
        if not call_id or call_id in claimed_ids:
            while True:
                candidate = (
                    f"{safe_prefix}_{position}_{uuid.uuid4().hex[:12]}"
                )
                if candidate not in claimed_ids:
                    call_id = candidate
                    break
            tool_call["id"] = call_id
            repaired += 1
        elif raw_id != call_id:
            tool_call["id"] = call_id
            repaired += 1
        claimed_ids.add(call_id)
    return repaired


__all__ = [
    "ensure_unique_tool_call_ids",
]
