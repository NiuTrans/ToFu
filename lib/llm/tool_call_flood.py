"""Bound repeated tool occurrences in one provider response.

Entry points: ToolCallFloodGuard.observe and dedupe_tool_call_response.
Dependencies: caller identity and request-scoped LLM errors. No task/storage
state; each physical stream or final response owns a fresh guard.

Semantics: within ONE provider response, an exact signature (tool name +
canonical JSON arguments + caller authority) is retained at most
``TOFU_TOOL_CALL_FLOOD_HARD_CAP`` − 1 times (default 1). The next identical
occurrence decides the salvage: the adopted response keeps only the first
occurrence of each flooded signature, and the transcript never admits the
repetition at all. This replaces the historical reject-the-whole-response
behaviour: a degenerate repetition is collapsed, not surfaced as an error.
"""

import hashlib
import json
import os

from lib.llm.llm_errors import ProviderToolCallFloodError
from lib.tool_results.tool_caller_identity import normalize_tool_caller, tool_caller_authority


def identical_tool_call_limit() -> int:
    """Allowed occurrences of one signature before the response is salvaged.

    A value of N keeps up to N−1 identical occurrences and collapses from the
    Nth onward. The default 2 therefore collapses the FIRST repetition of any
    exact same-argument call inside one response.
    """
    try:
        return max(2, min(4096, int(os.environ.get(
            'TOFU_TOOL_CALL_FLOOD_HARD_CAP', '2'))))
    except (TypeError, ValueError):
        return 2


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
    """Count response positions, never callbacks or provider correlation IDs.

    ``observe`` returns the triggering :class:`ProviderToolCallFloodError` as
    soon as one signature exceeds the allowed occurrence count, so the caller
    can abort the upstream stream and salvage the already-accumulated prefix.
    It no longer raises: repetition is a collapse decision, not a rejection.
    """

    def __init__(self):
        self.limit = identical_tool_call_limit()
        self._positions: dict[int, bytes] = {}
        self._counts: dict[bytes, int] = {}
        self.triggered: ProviderToolCallFloodError | None = None

    def observe(self, position: int, call: dict, *, final: bool = False):
        """Watch one response position; return the salvage trigger, if any.

        Returns the :class:`ProviderToolCallFloodError` describing the first
        signature that exceeded the allowed occurrence count, or ``None``.
        Once triggered, the guard stays triggered: later positions are still
        recorded (so evidence stays honest) but no new error is produced.
        """
        signature = _signature(call, final=final)
        previous = self._positions.get(position)
        if signature == previous:
            return self.triggered
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
            return self.triggered
        count = self._counts.get(signature, 0) + 1
        if count >= self.limit and self.triggered is None:
            error = ProviderToolCallFloodError(
                tool_name=call['function']['name'], count=count, limit=self.limit)
            error.matching_positions = tuple(
                index for index, value in self._positions.items()
                if value == signature) + (position,)
            self.triggered = error
        self._counts[signature] = count
        self._positions[position] = signature
        return self.triggered


def dedupe_tool_call_response(calls):
    """Collapse repeated identical occurrences in one provider response.

    Returns ``(kept_calls, evidence)``. ``kept_calls`` preserves the original
    order and keeps only the first ``limit − 1`` occurrences of each exact
    signature. ``evidence`` is ``None`` when nothing was collapsed; otherwise
    it is a dict ``{signature_key: {"tool": name, "dropped": n}}`` suitable
    for stamping onto the assistant message as ``_flood_collapsed``.
    """
    guard = ToolCallFloodGuard()
    if not calls:
        return calls, None
    keep_per_signature = max(1, guard.limit - 1)
    counts: dict[bytes, int] = {}
    kept = []
    collapsed: dict[bytes, dict] = {}
    for position, call in enumerate(calls):
        signature = _signature(call, final=True)
        if signature is None:
            # Unparseable / placeholder occurrence: keep it untouched rather
            # than guess at its identity. The downstream malformed-shape
            # receipt path owns this call.
            kept.append(call)
            continue
        seen = counts.get(signature, 0)
        if seen < keep_per_signature:
            counts[signature] = seen + 1
            kept.append(call)
        else:
            counts[signature] = seen + 1
            entry = collapsed.setdefault(
                signature, {'tool': call['function']['name'], 'dropped': 0})
            entry['dropped'] += 1
    if not collapsed:
        return calls, None
    # Keyed by a short deterministic fingerprint so the stamp stays bounded
    # and free of raw argument payloads.
    evidence = {
        hashlib.sha256(key).hexdigest()[:16]: value
        for key, value in collapsed.items()
    }
    return kept, evidence


def validate_tool_call_response(calls) -> None:
    """Historical name for the flood admission hook.

    The guard no longer rejects the whole response. This shim is kept only
    for in-flight callers from older turns; it performs the same collapse as
    :func:`dedupe_tool_call_response` but mutates the list in place.
    """
    kept, evidence = dedupe_tool_call_response(calls)
    if evidence is None or calls is None:
        return
    calls[:] = kept
