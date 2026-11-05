"""Resource ceilings for reconstructible swarm-panel presentation data.

The agent transcript and final result remain authoritative elsewhere.  These
limits govern only the live/durable tool timeline rendered inside the parent
conversation, so a long-running swarm cannot make every conversation sync pay
for an unbounded copy of child tool output.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from json.encoder import encode_basestring_ascii
import math
from typing import Any

SWARM_TOOL_TIMELINE_ROW_LIMIT = 30
SWARM_TOOL_TIMELINE_DETAIL_CHARS = 2_000
SWARM_TOOL_TIMELINE_JSON_BYTES = 32 * 1024
# A settled swarm's timelines are frozen history shown only inside collapsed
# agent rows. One shared budget bounds the whole snapshot, so a wide swarm no
# longer multiplies the per-agent ceiling into every conversation read.
SWARM_SETTLED_SNAPSHOT_TIMELINE_JSON_BYTES = 128 * 1024
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


def _declared_full_chars(
    source: Mapping[str, Any], full_chars_key: str, text: str,
) -> int:
    declared = source.get(full_chars_key)
    # Ints are the stored shape; anything else keeps the coercing parse.
    if type(declared) is not int:
        declared = _nonnegative_int(declared)
    return max(len(text), declared)


_STRIPPABLE_DETAIL_FIELDS = ("preview", "error", "argsBrief")
_FULL_CHARS_KEYS = {field: f"{field}FullChars" for field in _STRIPPABLE_DETAIL_FIELDS}
_TRUNCATED_KEYS = {field: f"{field}Truncated" for field in _STRIPPABLE_DETAIL_FIELDS}
_DETAIL_TEXT_FIELDS = tuple(
    (field, _FULL_CHARS_KEYS[field], _TRUNCATED_KEYS[field])
    for field in ("preview", "error")
)


def _bounded_browser_tool_call(source: Any) -> dict[str, Any] | None:
    """Normalize one historical presentation row without retaining extras."""
    if type(source) is not dict and not isinstance(source, Mapping):
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
    # This is the worker-minted occurrence key, shared with live tool frames.
    # Dropping it at the browser boundary makes recovery duplicate the same
    # call. Legacy id-less rows stay id-less; never derive it from tool names.
    call_id = _text(source.get("callId"))
    if call_id:
        call["callId"] = call_id
    ref = source.get('inspectorRef')
    if ((type(ref) is dict or isinstance(ref, Mapping))
            and ref.get('taskId') and ref.get('turn')
            and _nonnegative_int(ref.get('roundNum'))):
        call['inspectorRef'] = {
            'taskId': _text(ref['taskId'])[:256],
            'turn': _text(ref['turn'])[:128],
            'roundNum': _nonnegative_int(ref['roundNum']),
        }
    args_full_chars = _declared_full_chars(source, "argsBriefFullChars", raw_args)
    if args_full_chars > len(args_brief) or source.get("argsBriefTruncated"):
        call["argsBriefFullChars"] = args_full_chars
        call["argsBriefTruncated"] = True

    for field, full_chars_key, truncated_key in _DETAIL_TEXT_FIELDS:
        raw_text = _text(source.get(field))
        full_chars = _declared_full_chars(source, full_chars_key, raw_text)
        visible = raw_text[:SWARM_TOOL_TIMELINE_DETAIL_CHARS]
        call[field] = visible
        if full_chars:
            call[full_chars_key] = full_chars
            call[truncated_key] = bool(
                source.get(truncated_key)
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


# Comma + encoded key + colon for a marker appended to a non-empty row.
_MEMBER_BYTES = {
    key: 1 + len(json.dumps(key)) + 1
    for key in (*_FULL_CHARS_KEYS.values(), *_TRUNCATED_KEYS.values())
}
# ASCII-escaped compact JSON: an upper bound on UTF-8 bytes for every value.
# Normalized rows are fresh acyclic dicts, so the cycle bookkeeping is skipped.
_ASCII_JSON = json.JSONEncoder(
    ensure_ascii=True, separators=(",", ":"), check_circular=False,
)


def _json_bytes(value: Any) -> int:
    if type(value) is str:
        return len(encode_basestring_ascii(value))
    if type(value) is bool:
        return 4 if value else 5
    if type(value) is int:
        return len(repr(value))
    return len(_ASCII_JSON.encode(value))


def _elide_detail(call: dict[str, Any], field: str, text: str) -> int:
    """Empty one detail, set its explicit markers, return the markers' size change."""
    full_key = _FULL_CHARS_KEYS[field]
    truncated_key = _TRUNCATED_KEYS[field]
    full_chars = max(_nonnegative_int(call.get(full_key)), len(text))
    # A missing marker is appended as a comma, its encoded key, a colon and
    # its value.
    delta = (
        len(repr(full_chars)) - _json_bytes(call[full_key])
        if full_key in call
        else _MEMBER_BYTES[full_key] + len(repr(full_chars))
    )
    delta += (
        4 - _json_bytes(call[truncated_key])
        if truncated_key in call
        else _MEMBER_BYTES[truncated_key] + 4
    )
    call[full_key] = full_chars
    call[truncated_key] = True
    call[field] = ""
    return delta


def _strip_detail(call: dict[str, Any], field: str, text: str) -> int:
    """Elide one detail explicitly; return the exact encoded size change."""
    # The text becomes "" (two quote bytes).
    return 2 - _json_bytes(text) + _elide_detail(call, field, text)


def _fully_stripped(
    tool_calls: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Copy every row with all details elided, plus the strips' total growth bound.

    A non-empty text encodes to at least three bytes, so one strip grows the
    list by at most its marker change minus one.
    """
    rows: list[dict[str, Any]] = []
    growth = 0
    for call in tool_calls:
        row = dict(call)
        for field in _STRIPPABLE_DETAIL_FIELDS:
            text = row.get(field)
            if text:
                growth += max(0, _elide_detail(row, field, text) - 1)
        rows.append(row)
    return rows, growth


def _fit_timeline_byte_budget(
    tool_calls: list[dict[str, Any]],
    omitted: int,
    budget_bytes: int = SWARM_TOOL_TIMELINE_JSON_BYTES,
) -> tuple[list[dict[str, Any]], int]:
    """Strip details oldest-first, then drop the oldest rows, until it fits.

    The list is encoded once; each strip then adjusts that compact size by
    its exact delta, and a dropped row by its own size plus one comma.

    A timeline far over budget (a wide settled swarm's share) skips the
    per-detail walk with the same result: each intermediate size equals the
    fully stripped size minus the deltas of the strips still ahead, so it is
    at least that size minus their total growth. When even that bound
    exceeds the budget, no intermediate state could have fit and the
    sequential walk would strip everything before dropping rows. Detail
    characters beyond the budget are the cheap trigger: each encodes to at
    least one byte, so the unstripped list cannot fit.
    """
    calls = tool_calls
    total: int | None = None
    if sum(
        len(call.get(field) or "")
        for call in calls
        for field in _STRIPPABLE_DETAIL_FIELDS
    ) > budget_bytes:
        stripped, growth = _fully_stripped(calls)
        stripped_total = _json_bytes(stripped)
        if stripped_total - growth > budget_bytes:
            calls, total = stripped, stripped_total
    if total is None:
        total = _json_bytes(calls)
        for call in calls:
            for field in _STRIPPABLE_DETAIL_FIELDS:
                if total <= budget_bytes:
                    return calls, omitted
                text = call.get(field)
                if text:
                    total += _strip_detail(call, field, text)
    while calls and total > budget_bytes:
        removed = calls.pop(0)
        total -= _json_bytes(removed) + (1 if calls else 0)
        omitted += 1
    return calls, omitted


def bounded_browser_tool_timeline(
    value: Any,
    *,
    omitted: Any = 0,
    budget_bytes: int = SWARM_TOOL_TIMELINE_JSON_BYTES,
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
    return _fit_timeline_byte_budget(calls, omitted_count, budget_bytes)


def swarm_snapshot_for_browser(value: Any) -> Any:
    """Bound reconstructible tool timelines in one request-local snapshot.

    The durable snapshot, agent result, objective, final preview, and status
    remain untouched. Only the child tool-call presentation copy is projected,
    and already-canonical snapshots retain their original object identity.
    A settled snapshot divides one shared budget evenly among its timelines,
    never exceeding the per-agent ceiling; an unsettled one may still receive
    durable progress, so it keeps the per-agent ceiling.
    """
    if not isinstance(value, Mapping) or not isinstance(value.get("agents"), list):
        return value
    budget_bytes = SWARM_TOOL_TIMELINE_JSON_BYTES
    if value.get("settled") is True:
        timelines = sum(
            1 for source in value["agents"]
            if isinstance(source, Mapping) and "toolCalls" in source
        )
        budget_bytes = min(
            budget_bytes,
            SWARM_SETTLED_SNAPSHOT_TIMELINE_JSON_BYTES // max(1, timelines),
        )
    changed = False
    agents: list[Any] = []
    for source in value["agents"]:
        if not isinstance(source, Mapping) or "toolCalls" not in source:
            agents.append(source)
            continue
        calls, omitted = bounded_browser_tool_timeline(
            source.get("toolCalls"),
            omitted=source.get("toolCallsOmitted"),
            budget_bytes=budget_bytes,
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
    "SWARM_SETTLED_SNAPSHOT_TIMELINE_JSON_BYTES",
    "SWARM_TOOL_ARGS_BRIEF_CHARS",
    "SWARM_TOOL_NAME_CHARS",
    "SWARM_TOOL_TIMELINE_DETAIL_CHARS",
    "SWARM_TOOL_TIMELINE_JSON_BYTES",
    "SWARM_TOOL_TIMELINE_ROW_LIMIT",
    "bounded_browser_tool_timeline",
    "swarm_snapshot_for_browser",
]
