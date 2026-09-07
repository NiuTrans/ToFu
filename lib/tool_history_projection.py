"""Pure projection from one provider tool batch to Continue ``toolHistory``.

Batch *identity* belongs to :mod:`lib.tool_round_identity`; this module owns
only the wire-compatible message shape.  Keeping the shape here prevents the
checkpoint scanner and the segment compatibility projection from drifting.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from lib.tool_round_replay import scan_replayable_tool_round_prefix


def build_tool_history_round(batch: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Project one provider response's validated causal prefix.

    Callers normally pass rounds already returned by the shared replay
    scanner.  Scanning again is intentional defense in depth: this small pure
    projection must never manufacture ``{}`` arguments or jump across a
    malformed identity-bearing occurrence when called directly.
    """
    round_out: dict[str, Any] = {
        "assistantContent": "",
        "toolCalls": [],
        "toolResults": [],
    }
    replay_prefix = scan_replayable_tool_round_prefix(list(batch))
    for round_entry in replay_prefix.rounds:
        if (not round_out["assistantContent"]
                and isinstance(round_entry.get("assistantContent"), str)
                and round_entry.get("assistantContent")):
            round_out["assistantContent"] = round_entry["assistantContent"]
        if (not round_out.get("thinking")
                and isinstance(round_entry.get("thinking"), str)
                and round_entry.get("thinking")):
            round_out["thinking"] = round_entry["thinking"]
        if (not round_out.get("thinkingSignature")
                and isinstance(round_entry.get("thinkingSignature"), str)
                and round_entry.get("thinkingSignature")):
            round_out["thinkingSignature"] = round_entry["thinkingSignature"]

        tool_call: dict[str, Any] = {
            "id": round_entry.get("toolCallId"),
            "name": round_entry.get("toolName"),
            "arguments": round_entry["toolArgs"],
        }
        if isinstance(round_entry.get("extraContent"), Mapping):
            tool_call["extraContent"] = dict(round_entry["extraContent"])
        if "caller" in round_entry and round_entry.get("caller") is not None:
            raw_caller = round_entry.get("caller")
            tool_call["caller"] = (dict(raw_caller)
                                     if isinstance(raw_caller, Mapping)
                                     else raw_caller)
        round_out["toolCalls"].append(tool_call)

        tool_result: dict[str, Any] = {
            "tool_call_id": round_entry.get("toolCallId"),
            "content": round_entry.get("toolContent") or "",
        }
        if "caller" in round_entry and round_entry.get("caller") is not None:
            raw_caller = round_entry.get("caller")
            tool_result["caller"] = (dict(raw_caller)
                                      if isinstance(raw_caller, Mapping)
                                      else raw_caller)
        round_out["toolResults"].append(tool_result)
    return round_out


__all__ = ["build_tool_history_round"]
