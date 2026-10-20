"""Pure result/operation fingerprints shared by Python and native adapters.

Entry points accept already owned call/result facts. No world state, progress
ledger, authorization, execution, persistence or verification is inferred here.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping


def _normalized_call(call: dict) -> dict:
    function = call.get('function')
    function = function if isinstance(function, dict) else call
    arguments = function.get('arguments', call.get('arguments', {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            pass
    return {'name': str(function.get('name') or call.get('name') or ''),
            'arguments': arguments}


def tool_calls_signature(tool_calls: Iterable[Any]) -> str:
    """Ordered operation identity; provider correlation IDs carry no progress."""
    normalized = [_normalized_call(call) for call in tool_calls or ()
                  if isinstance(call, dict)]
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def visible_result_evidence(result) -> str:
    try:
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), default=str)
    except (TypeError, ValueError):
        payload = repr(result)
    return hashlib.sha256(payload.encode('utf-8', 'replace')).hexdigest()


def tool_result_evidence(call: dict, result) -> str:
    """Bind visible content to its operation, independent of callback order."""
    return visible_result_evidence([_normalized_call(call), result])


def counted_result_evidence(counts: Mapping[str, int]) -> tuple[str, ...]:
    """Keep repeated outcomes distinct in a ledger that accepts a set of IDs."""
    return tuple(f'{count}:{digest}' for digest, count in sorted(counts.items()))


def nonretryable_failure_signature(call: dict, result) -> str | None:
    from lib.tools.result_envelope import ToolResultEnvelopeV2, nonretryable_tool_error_code
    code = nonretryable_tool_error_code(result)
    if not code:
        return None
    function = call.get('function') or {}
    arguments = function.get('arguments') or '{}'
    try:
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
    except ValueError:
        pass
    failure_codes = None
    if code == 'tool_batch_failed' and function.get('name') == 'execute_tools':
        if not isinstance(arguments, dict) or not isinstance(arguments.get('calls'), list):
            return None
        try:
            payload = (result.to_dict() if isinstance(result, ToolResultEnvelopeV2)
                       else json.loads(result) if isinstance(result, str) else result)
        except (TypeError, ValueError):
            return None
        items = payload.get('items') if isinstance(payload, dict) else None
        if (not isinstance(items, list) or not items or not isinstance(items[0], dict)
                or set(items[0]) != {'failureCodes'}):
            return None
        failure_codes = items[0]['failureCodes']
        if (not isinstance(failure_codes, list) or not failure_codes
                or len(failure_codes) != len(arguments['calls'])
                or not all(isinstance(value, str) and value.strip() == value
                           and 0 < len(value) <= 96 for value in failure_codes)):
            return None
        # Only this declared receipt-correlation field is transport metadata.
        # Keep child order and every business argument, including call_id
        # inside arguments. Never mutate the provider's original call.
        arguments = {**arguments, 'calls': [
            {key: value for key, value in child.items() if key != 'call_id'}
            if isinstance(child, dict) else child for child in arguments['calls']]}
    identity = [function.get('name'), arguments]
    if failure_codes is not None:
        identity.append(failure_codes)
    operation = json.dumps(identity, sort_keys=True, default=str)
    return f'{code}:{hashlib.sha256(operation.encode()).hexdigest()[:16]}'
