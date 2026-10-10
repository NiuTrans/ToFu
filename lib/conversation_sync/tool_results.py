"""Snapshot-only lazy read-tool output; exact owner/revision detail decoding.

Entry points: turns_with_lazy_tool_results derives a compact browser view;
tool_result_from_turn checks an owner-scoped repository Turn before returning
one exact output. No durable projection, replay base, or cache is changed.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

# These tools use the generic authoritative result viewer. Specialized command,
# write, approval, image and swarm cards retain all of their presentation data.
_LAZY_READ_TOOLS = frozenset({
    "read_files", "grep_search", "find_files", "list_dir", "get_conversation",
})
_MIN_LAZY_CHARS = 4096
_PREVIEW_CHARS = 240
_REFERENCE_FIELD = "_snapshotToolResult"


class ConversationToolResultStale(RuntimeError):
    def __init__(self, current_projection_revision: int) -> None:
        super().__init__("Conversation tool result revision changed")
        self.current_projection_revision = current_projection_revision


def turns_with_lazy_tool_results(
    turns: Any, *, conversation_id: str, owner_scope: str,
) -> Any:
    """Defer only immutable output with uniquely referenceable segment mirrors."""
    if not isinstance(turns, list) or not owner_scope:
        return turns
    replaced = []
    for turn in turns:
        if not isinstance(turn, Mapping) or turn.get("status") != "completed":
            replaced.append(turn)
            continue
        projection = turn.get("projection")
        revision = turn.get("projectionRevision")
        if not isinstance(projection, Mapping) or not isinstance(revision, int) or revision < 1:
            replaced.append(turn)
            continue
        rounds = projection.get("toolRounds")
        if not isinstance(rounds, list):
            replaced.append(turn)
            continue
        counts = Counter(str(r.get("toolCallId") or "") for r in rounds if isinstance(r, Mapping))
        nonreferenced_ids = {
            str(segment.get("id") or "")
            for segment in projection.get("segments", [])
            if isinstance(segment, Mapping) and segment.get("type") == "tool_use"
            and segment.get("roundRef") != segment.get("id")
        }
        next_rounds = []
        changed = False
        for index, round_value in enumerate(rounds):
            if not isinstance(round_value, Mapping):
                next_rounds.append(round_value)
                continue
            # Server-minted presentation references never come from storage.
            round_record = dict(round_value) if _REFERENCE_FIELD in round_value else round_value
            if _REFERENCE_FIELD in round_record:
                round_record.pop(_REFERENCE_FIELD)
                changed = True
            content = round_record.get("toolContent")
            call_id = str(round_record.get("toolCallId") or "")
            if (round_record.get("status") != "done"
                    or round_record.get("toolName") not in _LAZY_READ_TOOLS
                    or not isinstance(content, str) or len(content) <= _MIN_LAZY_CHARS
                    or not call_id or counts[call_id] != 1 or call_id in nonreferenced_ids):
                next_rounds.append(round_record)
                continue
            lazy = dict(round_record)
            del lazy["toolContent"]
            # Some producers also keep an exact result.content mirror.
            result = lazy.get("result")
            if isinstance(result, Mapping) and result.get("content") == content:
                lazy["result"] = {key: value for key, value in result.items() if key != "content"}
            lazy[_REFERENCE_FIELD] = {
                "conversationId": conversation_id, "turnId": turn["turnId"],
                "toolCallId": call_id, "roundIndex": index,
                "projectionRevision": revision, "ownerScope": owner_scope,
                "preview": content[:_PREVIEW_CHARS].encode("utf-16-le")[:_PREVIEW_CHARS * 2].decode("utf-16-le", errors="ignore"),
                "chars": len(content),
            }
            next_rounds.append(lazy)
            changed = True
        replaced.append({**turn, "projection": {**projection, "toolRounds": next_rounds}}
                        if changed else turn)
    return replaced


def tool_result_from_turn(
    turn: Mapping[str, Any], *, conversation_id: str, turn_id: str,
    round_index: int, projection_revision: int,
) -> dict[str, Any]:
    from .service import ConversationSyncNotFound
    current_revision = int(turn.get("projectionRevision") or 0)
    if current_revision != projection_revision:
        raise ConversationToolResultStale(current_revision)
    rounds = (turn.get("projection") or {}).get("toolRounds")
    if (turn.get("conversationId") != conversation_id or turn.get("turnId") != turn_id
            or turn.get("status") != "completed" or not isinstance(rounds, list)
            or not isinstance(round_index, int) or isinstance(round_index, bool)
            or not 0 <= round_index < len(rounds)):
        raise ConversationSyncNotFound("Conversation tool result not found")
    round_record = rounds[round_index]
    if (not isinstance(round_record, Mapping) or round_record.get("status") != "done"
            or round_record.get("toolName") not in _LAZY_READ_TOOLS
            or not isinstance(round_record.get("toolContent"), str)
            or not round_record.get("toolCallId")):
        raise ConversationSyncNotFound("Conversation tool result not found")
    return {
        "ok": True, "contract": "tofu.conversation-sync.tool-result/v1",
        "conversationId": conversation_id, "turnId": turn_id,
        "toolCallId": round_record["toolCallId"], "roundIndex": round_index,
        "projectionRevision": current_revision, "toolContent": round_record["toolContent"],
    }
