"""Bound repeated tool occurrences in one provider response.

Entry points: ToolCallFloodGuard.observe and validate_tool_call_response.
Dependencies: caller identity and request-scoped LLM errors. No task/storage
state; each physical stream or final response owns a fresh guard.
"""

import hashlib
import json
import os

from lib.llm_errors import ProviderToolCallFloodError
from lib.tool_caller_identity import normalize_tool_caller, tool_caller_authority


def identical_tool_call_limit() -> int:
    """Safety bound for one repeated signature, independent of total fan-out."""
    try:
        return max(2, min(4096, int(os.environ.get(
            'TOFU_TOOL_CALL_FLOOD_HARD_CAP', '32'))))
    except (TypeError, ValueError):
        return 32


def _signature(call: dict, *, final: bool) -> bytes | None:
    if not isinstance(call, dict):
        return None
    function = call.get('function')
    if not isinstance(function, dict):
        return None
    name, arguments = function.get('name'), function.get('arguments')
    if not isinstance(name, str) or not name or not isinstance(arguments, str):
        return None
    if not final and not arguments.strip():
        # Interleaved streams may open every tool slot before sending args.
        # An empty placeholder is not yet a completed no-argument call.
        return None
    try:
        arguments = json.loads(arguments) if arguments.strip() else {}
    except (ValueError, TypeError):
        # Partial JSON is not evidence of an identical completed call.
        return None
    if not isinstance(arguments, dict):
        return None
    caller, error = normalize_tool_caller(
        call.get('caller'), require_program_identity=False)
    if error:
        return None
    authority = tool_caller_authority(caller) if caller else ('root', '')
    encoded = json.dumps((name, arguments, authority), sort_keys=True,
                         ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).digest()


class ToolCallFloodGuard:
    """Count response positions, never callbacks or provider correlation IDs."""

    def __init__(self):
        self.limit = identical_tool_call_limit()
        self._positions: dict[int, bytes] = {}
        self._counts: dict[bytes, int] = {}

    def observe(self, position: int, call: dict, *, final: bool = False) -> None:
        signature = _signature(call, final=final)
        previous = self._positions.get(position)
        if signature == previous:
            return
        # Interleaved argument deltas may revise a previously observed slot.
        # Its old provisional identity cannot count as another occurrence.
        if previous is not None:
            count = self._counts[previous] - 1
            if count:
                self._counts[previous] = count
            else:
                del self._counts[previous]
            del self._positions[position]
        if signature is None:
            return
        count = self._counts.get(signature, 0) + 1
        if count > self.limit:
            error = ProviderToolCallFloodError(
                tool_name=call['function']['name'], count=count, limit=self.limit)
            error.matching_positions = tuple(
                index for index, value in self._positions.items()
                if value == signature) + (position,)
            raise error
        self._counts[signature] = count
        self._positions[position] = signature


def validate_tool_call_response(calls) -> None:
    """Reject the whole response before parsing/dispatching any flooded batch."""
    guard = ToolCallFloodGuard()
    for position, call in enumerate(calls or ()):
        guard.observe(position, call, final=True)
