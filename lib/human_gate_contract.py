"""Shared wire and domain limits for owner-bound human gates.

Stdin, write approval, chat guidance, and orchestration human nodes all
resolve an in-process waiter by an opaque request identifier.  Keeping their
identifier and response limits here prevents one HTTP surface from accepting
payloads that another surface (or the shared registry) would reject.
"""

from __future__ import annotations


MAX_HUMAN_GATE_REQUEST_ID_LENGTH = 256
MAX_HUMAN_GATE_RESPONSE_LENGTH = 8000


def require_human_gate_request_id(value: object, *, field: str) -> str:
    """Return a normalized bounded request ID or fail closed."""
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a string')
    request_id = value.strip()
    if not request_id:
        raise ValueError(f'{field} is required')
    if len(request_id) > MAX_HUMAN_GATE_REQUEST_ID_LENGTH:
        raise ValueError(
            f'{field} exceeds {MAX_HUMAN_GATE_REQUEST_ID_LENGTH} characters')
    return request_id


__all__ = [
    'MAX_HUMAN_GATE_REQUEST_ID_LENGTH',
    'MAX_HUMAN_GATE_RESPONSE_LENGTH',
    'require_human_gate_request_id',
]
