"""Pure result/operation fingerprints shared by Python and native adapters.

Entry points accept already owned call/result facts. No world state, progress
ledger, authorization, execution, persistence or verification is inferred here.
"""
from __future__ import annotations

import hashlib
import json


def visible_result_evidence(result) -> str:
    try:
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), default=str)
    except (TypeError, ValueError):
        payload = repr(result)
    return hashlib.sha256(payload.encode('utf-8', 'replace')).hexdigest()


def nonretryable_failure_signature(call: dict, result) -> str | None:
    from lib.tools.result_envelope import nonretryable_tool_error_code
    code = nonretryable_tool_error_code(result)
    if not code:
        return None
    function = call.get('function') or {}
    arguments = function.get('arguments') or '{}'
    try:
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
    except ValueError:
        pass
    operation = json.dumps([function.get('name'), arguments], sort_keys=True, default=str)
    return f'{code}:{hashlib.sha256(operation.encode()).hexdigest()[:16]}'
