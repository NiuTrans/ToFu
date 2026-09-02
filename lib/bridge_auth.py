"""Resolve device-bridge credentials into one explicit repository owner.

Remote browser and desktop agents authenticate with a Sidecar credential that
literally carries ``agents:bridge``.  The packaged desktop app additionally
uses one process-memory capability for its in-process agent; that capability is
accepted only when the desktop poll boundary opts in.

There is no global shared secret, address-based trust, or credential-free
mode.  Successful resolution always returns a positive numeric owner string.
"""

from __future__ import annotations

import hmac
import secrets

from lib.api_keys import AuthContext
from lib.identity import PERSONAL_USER_ID
from lib.log import get_logger

logger = get_logger(__name__)

_PROCESS_AGENT_TOKEN = secrets.token_urlsafe(32)
_PROCESS_AGENT_KEY_ID = 'process:desktop-agent'


def process_agent_token() -> str:
    """Return the non-persisted capability shared with the in-process agent."""
    return _PROCESS_AGENT_TOKEN


def resolve_bridge_credential(
    provided: object,
    *,
    allow_process_agent: bool = False,
) -> AuthContext | None:
    """Resolve a bridge credential to its complete authentication context.

    Missing, revoked, suspended, wrong-scope, and unavailable credentials all
    fail closed. The returned context is cached at the HTTP boundary so one
    request authenticates and updates ``last_used_at`` exactly once.
    """
    token = str(provided or '').strip()
    if not token:
        return None
    if allow_process_agent and hmac.compare_digest(
        token, _PROCESS_AGENT_TOKEN
    ):
        return AuthContext(
            key_id=_PROCESS_AGENT_KEY_ID,
            name='in-process desktop agent',
            scopes=frozenset({'agents:bridge'}),
            owner_user_id=PERSONAL_USER_ID,
        )
    try:
        from lib.api_keys import validate_token

        context = validate_token(token)
    except Exception as exc:
        logger.warning('[BridgeAuth] credential authority unavailable: %s', exc)
        return None
    if (
        context is None
        or 'agents:bridge' not in context.scopes
        or context.owner_user_id is None
    ):
        return None
    return context


def identify_rejected_bridge_owner(provided: object) -> str:
    """Return the owner of a known inactive bridge token, without authority."""
    token = str(provided or '').strip()
    if not token:
        return ''
    try:
        from lib.api_keys import identify_known_token

        row = identify_known_token(token)
    except Exception as exc:
        logger.warning(
            '[BridgeAuth] credential recovery lookup unavailable: %s', exc)
        return ''
    if row is None or 'agents:bridge' not in set(row.get('scopes') or ()):
        return ''
    owner = str(row.get('owner_user_id') or '')
    return owner if owner.isdigit() and int(owner) > 0 else ''


__all__ = [
    'identify_rejected_bridge_owner', 'process_agent_token',
    'resolve_bridge_credential',
]
