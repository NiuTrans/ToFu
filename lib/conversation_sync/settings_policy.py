"""Public conversation-settings policy, shared by ingress and projections.

The durable declaration store owns its private slot and snapshot CAS. Public
callers may neither supply that slot nor receive it. Filter patches before a
storage merge; never read/replace settings to restore a private value, which
would race a concurrent reset/freeze. Trusted storage reads and CAS writes do
not pass through this boundary.

The public vocabulary that crosses the browser boundary is declared once in
contracts/conversation_settings_v1.json; server projections derive from it.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


from lib.conversation_sync.execution_identity import RESERVED_EXECUTION_FIELDS

CONVERSATION_SETTINGS_CONTRACT = json.loads(
    (Path(__file__).resolve().parents[2] / 'contracts'
     / 'conversation_settings_v1.json').read_text(encoding='utf-8'))
#: Settings keys carried by the sidebar catalog projection.
CATALOG_SETTING_KEYS = frozenset(
    key for key, spec in CONVERSATION_SETTINGS_CONTRACT['keys'].items()
    if spec.get('catalog'))
MCP_WIRE_DECLARATIONS_SETTING_KEY = '_mcpWireDeclarations'
PRIVATE_CONVERSATION_SETTING_KEYS = (
    frozenset({MCP_WIRE_DECLARATIONS_SETTING_KEY}) | RESERVED_EXECUTION_FIELDS
)


def public_conversation_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Copy public keys without inspecting (possibly malformed) private values."""
    return {
        key: value for key, value in settings.items()
        if key not in PRIVATE_CONVERSATION_SETTING_KEYS
    }


def public_conversation_defaults(defaults: Mapping[str, Any] | None) -> dict[str, Any]:
    """Sanitize settings carried by create/append/queued turn defaults.

    Preserve malformed non-mapping settings for the existing admission checks.
    Neither the request nor a trusted persisted settings snapshot is mutated.
    """
    result = dict(defaults or {})
    if isinstance(result.get('settings'), Mapping):
        result['settings'] = public_conversation_settings(result['settings'])
    return result
