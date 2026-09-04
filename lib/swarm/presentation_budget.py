"""Resource ceilings for reconstructible swarm-panel presentation data.

The agent transcript and final result remain authoritative elsewhere.  These
limits govern only the live/durable tool timeline rendered inside the parent
conversation, so a long-running swarm cannot make every conversation sync pay
for an unbounded copy of child tool output.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
from typing import Any

SWARM_TOOL_TIMELINE_ROW_LIMIT = 30
SWARM_TOOL_TIMELINE_DETAIL_CHARS = 2_000
SWARM_TOOL_TIMELINE_JSON_BYTES = 32 * 1024
SWARM_TOOL_NAME_CHARS = 128
SWARM_TOOL_ARGS_BRIEF_CHARS = 512
_SWARM_TOOL_STATUS_CHARS = 64


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _nonnegative_int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def _declared_full_chars(source: Mapping[str, Any], field: str, text: str) -> int:
    return max(len(text), _nonnegative_int(source.get(f"{field}FullChars")))


def _bounded_browser_tool_call(source: Any) -> dict[str, Any] | None:
    """Normalize one historical presentation row without retaining extras."""
    if not isinstance(source, Mapping):
        return None
    name = _text(source.get("toolName"))[:SWARM_TOOL_NAME_CHARS]
    if not name:
        return None
    raw_args = _text(source.get("argsBrief"))
    args_brief = raw_args[:SWARM_TOOL_ARGS_BRIEF_CHARS]
    call: dict[str, Any] = {
        "toolName": name,
        "argsBrief": args_brief,
        "status": _text(source.get("status"))[:_SWARM_TOOL_STATUS_CHARS]
        or "done",
        "preview": "",
        "error": "",
    }
    args_full_chars = _declared_full_chars(source, "argsBrief", raw_args)
    if args_full_chars > len(args_brief) or source.get("argsBriefTruncated"):
        call["argsBriefFullChars"] = args_full_chars
        call["argsBriefTruncated"] = True

    for field in ("preview", "error"):
        raw_text = _text(source.get(field))
        full_chars = _declared_full_chars(source, field, raw_text)
        visible = raw_text[:SWARM_TOOL_TIMELINE_DETAIL_CHARS]
        call[field] = visible
        if full_chars:
            call[f"{field}FullChars"] = full_chars
            call[f"{field}Truncated"] = bool(
                source.get(f"{field}Truncated")
                or len(visible) < full_chars
            )

    elapsed = source.get("elapsed")
    if (
        isinstance(elapsed, (int, float))
        and not isinstance(elapsed, bool)
        and math.isfinite(elapsed)
        and elapsed >= 0
    ):
        call["elapsed"] = elapsed
    return call


def _timeline_json_bytes(tool_calls: list[dict[str, Any]]) -> int:
    """Upper-bound compact UTF-8 bytes for every Unicode value."""
    return len(json.dumps(
        tool_calls,
        ensure_ascii=True,
        separators=(",", ":"),
    ))


def _fit_timeline_byte_budget(
    tool_calls: list[dict[str, Any]],
    omitted: int,
) -> tuple[list[dict[str, Any]], int]:
    calls = tool_calls
    while calls and _timeline_json_bytes(calls) > SWARM_TOOL_TIMELINE_JSON_BYTES:
        stripped = False
        for call in calls:
            for field in ("preview", "error", "argsBrief"):
                text = call.get(field)
                if not text:
                    continue
                full_key = f"{field}FullChars"
                truncated_key = f"{field}Truncated"
                call[full_key] = max(
                    _nonnegative_int(call.get(full_key)),
                    len(text),
                )
                call[truncated_key] = True
                call[field] = ""
                stripped = True
                break
            if stripped:
                break
        if not stripped:
            calls.pop(0)
            omitted += 1
    return calls, omitted


def bounded_browser_tool_timeline(
    value: Any,
    *,
    omitted: Any = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Project at most the newest bounded presentation rows.

    Only the retained tail is inspected, so a malformed historical list with
    millions of entries cannot turn one cold read into millions of Python
    normalizations. Every removed row and detail truncation remains explicit.
    """
    if not isinstance(value, list):
        return [], _nonnegative_int(omitted)
    omitted_count = _nonnegative_int(omitted) + max(
        0,
        len(value) - SWARM_TOOL_TIMELINE_ROW_LIMIT,
    )
    calls: list[dict[str, Any]] = []
    for source in value[-SWARM_TOOL_TIMELINE_ROW_LIMIT:]:
        call = _bounded_browser_tool_call(source)
        if call is None:
            omitted_count += 1
            continue
        calls.append(call)
    return _fit_timeline_byte_budget(calls, omitted_count)


def swarm_snapshot_for_browser(value: Any) -> Any:
    """Bound reconstructible tool timelines in one request-local snapshot.

    The durable snapshot, agent result, objective, final preview, and status
    remain untouched. Only the child tool-call presentation copy is projected,
    and already-canonical snapshots retain their original object identity.
    """
    if not isinstance(value, Mapping) or not isinstance(value.get("agents"), list):
        return value
    changed = False
    agents: list[Any] = []
    for source in value["agents"]:
        if not isinstance(source, Mapping) or "toolCalls" not in source:
            agents.append(source)
            continue
        calls, omitted = bounded_browser_tool_timeline(
            source.get("toolCalls"),
            omitted=source.get("toolCallsOmitted"),
        )
        agent = dict(source)
        agent["toolCalls"] = calls
        if omitted:
            agent["toolCallsOmitted"] = omitted
        else:
            agent.pop("toolCallsOmitted", None)
        if agent == source:
            agents.append(source)
            continue
        agents.append(agent)
        changed = True
    if not changed:
        return value
    return {**value, "agents": agents}


__all__ = [
    "SWARM_TOOL_ARGS_BRIEF_CHARS",
    "SWARM_TOOL_NAME_CHARS",
    "SWARM_TOOL_TIMELINE_DETAIL_CHARS",
    "SWARM_TOOL_TIMELINE_JSON_BYTES",
    "SWARM_TOOL_TIMELINE_ROW_LIMIT",
    "bounded_browser_tool_timeline",
    "swarm_snapshot_for_browser",
]
