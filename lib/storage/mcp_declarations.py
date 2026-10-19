"""Durable MCP wire declarations, not execution grants.

One bounded slot in the configured conversation settings authority, deleted
with the conversation. Reads never consult replay bodies or process caches.
First-writer-wins uses the existing cross-backend settings snapshot CAS;
identity inside the slot prevents copied conversation settings sharing a wire.
No backend fallback, TTL, per-turn history, or additional database is involved.
"""
from __future__ import annotations

import copy
import json
import uuid

from lib.conversation_sync.settings_policy import MCP_WIRE_DECLARATIONS_SETTING_KEY as SETTING_KEY

from lib.mcp.declarations import (
    MAX_DECLARATION_BYTES,
    MAX_DECLARATIONS as MAX_DECLARATIONS,
    validate_definitions,
)
from lib.agent_core.tool_errors import MCPDeclarationError

_MAX_CAS_ATTEMPTS = 8


class MCPDeclarationStore:
    """Explicit-owner repository over conversation.get/settings.update."""

    def __init__(self, client=None):
        self._client = client

    def _storage(self):
        if self._client is not None:
            return self._client
        from lib.storage import get_storage_client
        return get_storage_client(write=True)

    @staticmethod
    def _identity(user_id, conversation_id):
        if (isinstance(user_id, bool) or not isinstance(user_id, int)
                or user_id < 1 or not isinstance(conversation_id, str)
                or not conversation_id.strip()):
            raise MCPDeclarationError('MCP declarations require owner and conversation')
        return {'ownerUserId': user_id, 'conversationId': conversation_id.strip()}

    def _settings(self, identity):
        document = self._storage().query('conversation.get', {
            'conv_id': identity['conversationId'], 'user_id': identity['ownerUserId'],
            'derive_messages': False,
        })
        if not document:
            raise MCPDeclarationError('MCP declaration conversation is missing or not owned')
        settings = (document.get('metadata') or {}).get('settings') or {}
        if not isinstance(settings, dict):
            raise MCPDeclarationError('Malformed conversation settings')
        return settings

    @staticmethod
    def _wire(settings, identity):
        if SETTING_KEY not in settings:
            return None
        slot = settings[SETTING_KEY]
        if not isinstance(slot, dict) or slot.get('version') != 1:
            raise MCPDeclarationError('Unsupported MCP declaration slot')

        if (type(slot.get('ownerUserId')) is not int or slot['ownerUserId'] < 1
                or not isinstance(slot.get('conversationId'), str)
                or not slot['conversationId'].strip()):
            raise MCPDeclarationError('Malformed MCP declaration identity')
        if any(slot.get(key) != value for key, value in identity.items()):
            # Fork/clone/import copies settings, but cannot copy ownership.
            return None
        if slot.get('reset') is True:
            return None
        try:
            wire = slot['wireJson']
            if not isinstance(wire, str) or len(wire.encode('utf-8')) > MAX_DECLARATION_BYTES:
                raise MCPDeclarationError('Malformed or oversized MCP declaration wire')
            return validate_definitions(json.loads(wire))
        except (KeyError, TypeError, ValueError) as exc:
            raise MCPDeclarationError('Malformed MCP declaration wire') from exc

    def get(self, *, user_id: int, conversation_id: str) -> list[dict] | None:
        identity = self._identity(user_id, conversation_id)
        try:
            return self._wire(self._settings(identity), identity)
        except MCPDeclarationError:
            raise
        except Exception as exc:
            raise MCPDeclarationError('MCP declaration read failed') from exc

    def freeze(self, definitions, *, user_id: int, conversation_id: str) -> list[dict]:
        return self._update(definitions, user_id=user_id, conversation_id=conversation_id)

    def reset(self, *, user_id: int, conversation_id: str) -> None:
        """Explicit lifecycle reset; ordinary cache pressure must never call this."""
        self._update(None, user_id=user_id, conversation_id=conversation_id, reset=True)

    def _update(self, definitions, *, user_id, conversation_id, reset=False):
        identity = self._identity(user_id, conversation_id)
        candidate = None if reset else validate_definitions(definitions)
        try:
            for _ in range(_MAX_CAS_ATTEMPTS):
                before = self._settings(identity)
                current = None if reset else self._wire(before, identity)
                if not reset and current is not None:
                    return current
                after = copy.deepcopy(before)
                after[SETTING_KEY] = {'version': 1, **identity}
                if reset:
                    after[SETTING_KEY]['reset'] = True
                else:
                    # JSON text preserves nested object insertion order even
                    # through JSONB/native map canonicalization of settings.
                    after[SETTING_KEY]['wireJson'] = json.dumps(
                        candidate, ensure_ascii=False, allow_nan=False,
                        separators=(',', ':'))
                if after == before:
                    return candidate
                # Fresh identity per transition avoids receipt reuse after an
                # explicit reset (A -> B -> A). Transport retries reuse this ID.
                result = self._storage().command('conversation.settings.update', {
                    'conv_id': identity['conversationId'], 'user_id': user_id,
                    'updates': after, 'replace': True, 'expected_settings': before,
                }, f'mcp-declarations:{uuid.uuid4().hex}')
                if result.get('missing'):
                    raise MCPDeclarationError('MCP declaration conversation disappeared')
                if result.get('applied'):
                    return copy.deepcopy(candidate)
            raise MCPDeclarationError('MCP declaration settings contention exhausted')
        except MCPDeclarationError:
            raise
        except Exception as exc:
            raise MCPDeclarationError('MCP declaration write failed') from exc
