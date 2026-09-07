"""Canonical validation for tool-call authority envelopes.

``caller`` decides whether a call belongs to the root model, a hosted program,
or a delegated agent.  Any projection that silently drops malformed caller
metadata changes authority, so live ingress and every replay path share these
pure helpers instead of relying on ``isinstance(value, dict)`` alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAX_TOOL_CALLER_ID_CHARS = 512


def normalize_tool_caller(
    value: Any,
    *,
    require_program_identity: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return a copied, normalized authority envelope or a bounded error.

    ``None`` means caller attribution is genuinely absent. Program callers may
    temporarily lack a parent identity only at the live ingress seam, before
    program-item reconciliation; persisted/replayed callers always require it.
    """
    if value is None:
        return None, None
    if not isinstance(value, Mapping):
        return None, 'caller must be an object'

    caller_type = value.get('type')
    if caller_type == 'program':
        identity_field = 'caller_id'
        identity_required = require_program_identity
    elif caller_type == 'multi_agent':
        identity_field = 'agent_name'
        identity_required = True
    else:
        return None, 'caller.type must be program or multi_agent'

    identity = value.get(identity_field)
    if not identity_required and identity is None:
        # Hosted Responses reconciliation may receive a provisional program
        # envelope before the parent item is known. Retain only the authority
        # discriminator; arbitrary provider metadata is not authorization.
        return {'type': caller_type}, None
    if not isinstance(identity, str) or not identity.strip():
        if not identity_required and identity in ('', None):
            return {'type': caller_type}, None
        return None, f'caller.{identity_field} must be non-empty text'
    identity = identity.strip()
    if len(identity) > MAX_TOOL_CALLER_ID_CHARS:
        return None, (
            f'caller.{identity_field} exceeds '
            f'{MAX_TOOL_CALLER_ID_CHARS} characters')

    # Caller envelopes are authority tokens, not extensible metadata bags.
    # Copying unknown fields into replay/provider messages lets inert payload
    # bytes become a second, unbounded notion of identity.
    normalized = {'type': caller_type, identity_field: identity}
    return normalized, None


def tool_caller_authority(caller: Mapping[str, Any]) -> tuple[str, str]:
    """Return the fields that confer authority, excluding inert metadata."""
    if caller.get('type') == 'program':
        return 'program', str(caller.get('caller_id') or '')
    return 'multi_agent', str(caller.get('agent_name') or '')


__all__ = [
    'MAX_TOOL_CALLER_ID_CHARS',
    'normalize_tool_caller',
    'tool_caller_authority',
]
