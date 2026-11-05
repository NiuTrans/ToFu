"""Durable MCP wire declarations, not execution grants.

One bounded slot in the configured conversation settings authority, deleted
with the conversation. Reads never consult replay bodies or process caches.
First-writer-wins uses the existing cross-backend settings snapshot CAS;
identity inside the slot prevents copied conversation settings sharing a wire.
No backend fallback, TTL, per-turn history, or additional database is involved.

The slot lives inside the conversation header, which every catalog page, list,
get and Turn settlement reads whole. Version 2 therefore stores the exact wire
JSON text zlib-compressed and base85-encoded (JSON-safe, nothing to escape) so
an ordinary wire keeps the header within the native inline value bound.
Version-1 inline text stays readable. ``compact`` re-encodes it losslessly
under the same snapshot CAS; ``start_legacy_slot_compaction`` runs one
fail-soft pass per process. Both encodings yield the identical wire text, so
the provider request prefix is unchanged.
"""
from __future__ import annotations

import base64
import copy
import json
import threading
import time
import uuid
import zlib

from lib.conversation_sync.settings_policy import MCP_WIRE_DECLARATIONS_SETTING_KEY as SETTING_KEY

from lib.mcp.declarations import (
    MAX_DECLARATION_BYTES,
    MAX_DECLARATIONS as MAX_DECLARATIONS,
    validate_definitions,
)
from lib.agent_core.tool_errors import MCPDeclarationError
from lib.log import get_logger

logger = get_logger(__name__)

_MAX_CAS_ATTEMPTS = 8
_SLOT_VERSION = 2
_LEGACY_SLOT_VERSION = 1
_WIRE_FIELD = 'wireZlibBase85'
_LEGACY_WIRE_FIELD = 'wireJson'
_CATALOG_PAGE_LIMIT = 1000


def _pack_wire(wire_text: str) -> str:
    return base64.b85encode(zlib.compress(wire_text.encode('utf-8'), 9)).decode('ascii')


def _unpack_wire(packed) -> str:
    if not isinstance(packed, str):
        raise MCPDeclarationError('Malformed MCP declaration wire')
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(base64.b85decode(packed), MAX_DECLARATION_BYTES + 1)
        if (len(raw) > MAX_DECLARATION_BYTES or inflater.unconsumed_tail
                or not inflater.eof or inflater.unused_data):
            raise MCPDeclarationError('Malformed or oversized MCP declaration wire')
        return raw.decode('utf-8')
    except (ValueError, zlib.error) as exc:
        raise MCPDeclarationError('Malformed MCP declaration wire') from exc


def _legacy_wire_text(slot) -> str:
    wire = slot.get(_LEGACY_WIRE_FIELD)
    if not isinstance(wire, str) or len(wire.encode('utf-8')) > MAX_DECLARATION_BYTES:
        raise MCPDeclarationError('Malformed or oversized MCP declaration wire')
    return wire


def _validated_slot(slot) -> dict:
    if not isinstance(slot, dict) or slot.get('version') not in (
            _LEGACY_SLOT_VERSION, _SLOT_VERSION):
        raise MCPDeclarationError('Unsupported MCP declaration slot')
    if (type(slot.get('ownerUserId')) is not int or slot['ownerUserId'] < 1
            or not isinstance(slot.get('conversationId'), str)
            or not slot['conversationId'].strip()):
        raise MCPDeclarationError('Malformed MCP declaration identity')
    return slot


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
        slot = _validated_slot(settings[SETTING_KEY])
        if any(slot.get(key) != value for key, value in identity.items()):
            # Fork/clone/import copies settings, but cannot copy ownership.
            return None
        if slot.get('reset') is True:
            return None
        if slot['version'] == _SLOT_VERSION:
            wire = _unpack_wire(slot.get(_WIRE_FIELD))
        else:
            wire = _legacy_wire_text(slot)
        try:
            return validate_definitions(json.loads(wire))
        except (TypeError, ValueError) as exc:
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

    def _replace_settings(self, identity, before, after) -> bool:
        # Fresh identity per transition avoids receipt reuse after an explicit
        # reset (A -> B -> A). Transport retries reuse this ID.
        result = self._storage().command('conversation.settings.update', {
            'conv_id': identity['conversationId'], 'user_id': identity['ownerUserId'],
            'updates': after, 'replace': True, 'expected_settings': before,
        }, f'mcp-declarations:{uuid.uuid4().hex}')
        if result.get('missing'):
            raise MCPDeclarationError('MCP declaration conversation disappeared')
        return bool(result.get('applied'))

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
                after[SETTING_KEY] = {'version': _SLOT_VERSION, **identity}
                if reset:
                    after[SETTING_KEY]['reset'] = True
                else:
                    # JSON text preserves nested object insertion order even
                    # through JSONB/native map canonicalization of settings.
                    after[SETTING_KEY][_WIRE_FIELD] = _pack_wire(json.dumps(
                        candidate, ensure_ascii=False, allow_nan=False,
                        separators=(',', ':')))
                if after == before:
                    return candidate
                if self._replace_settings(identity, before, after):
                    return copy.deepcopy(candidate)
            raise MCPDeclarationError('MCP declaration settings contention exhausted')
        except MCPDeclarationError:
            raise
        except Exception as exc:
            raise MCPDeclarationError('MCP declaration write failed') from exc

    def compact(self, *, user_id: int, conversation_id: str) -> bool:
        """Re-encode one version-1 wire slot losslessly; True when rewritten.

        Owned and copied (foreign) slots keep their identity and exact wire
        text. Absent, reset and version-2 slots are unchanged; malformed slots
        fail closed and stay for explicit reset. Settings-only updates keep the
        conversation's ``updated_at`` and ``rev``.
        """
        identity = self._identity(user_id, conversation_id)
        try:
            for _ in range(_MAX_CAS_ATTEMPTS):
                before = self._settings(identity)
                if SETTING_KEY not in before:
                    return False
                slot = _validated_slot(before[SETTING_KEY])
                if slot['version'] != _LEGACY_SLOT_VERSION or slot.get('reset') is True:
                    return False
                packed = _pack_wire(_legacy_wire_text(slot))
                after = copy.deepcopy(before)
                after[SETTING_KEY] = {key: value for key, value in slot.items()
                                      if key != _LEGACY_WIRE_FIELD}
                after[SETTING_KEY].update({'version': _SLOT_VERSION, _WIRE_FIELD: packed})
                if self._replace_settings(identity, before, after):
                    return True
            raise MCPDeclarationError('MCP declaration settings contention exhausted')
        except MCPDeclarationError:
            raise
        except Exception as exc:
            raise MCPDeclarationError('MCP declaration compaction failed') from exc

    def _live_conversation_ids(self, user_id, should_stop):
        payload = {
            'user_id': user_id, 'catalog_page': True, 'include_messages': False,
            'order_by': 'updated_at_desc', 'limit': _CATALOG_PAGE_LIMIT,
            'before_id': '', 'settings_keys': [],
        }
        while not should_stop():
            page = self._storage().query('conversation.list', payload) or {}
            headers = [item['metadata'] for item in page.get('items') or []]
            for header in headers:
                yield header['id']
            if not page.get('has_more') or not headers:
                return
            payload = dict(payload, before_updated_at=headers[-1]['updated_at'],
                           before_id=headers[-1]['id'])

    def compact_legacy_slots(self, *, user_id: int, should_stop=lambda: False) -> dict:
        """One sequential pass over the owner's live conversations.

        Work is bounded by the live catalog; trashed conversations are visited
        by a pass after restore. A failed conversation keeps its readable
        legacy slot and does not stop the pass.
        """
        stats = {'scanned': 0, 'compacted': 0, 'failed': 0}
        for conversation_id in self._live_conversation_ids(user_id, should_stop):
            if should_stop():
                break
            stats['scanned'] += 1
            try:
                stats['compacted'] += self.compact(
                    user_id=user_id, conversation_id=conversation_id)
            except MCPDeclarationError as exc:
                stats['failed'] += 1
                logger.warning('[MCPDeclarations] legacy slot kept for %s: %s',
                               conversation_id, exc)
        return stats


_compaction_lock = threading.Lock()
_compaction_stop: threading.Event | None = None
_compaction_thread: threading.Thread | None = None


def start_legacy_slot_compaction(
    *, user_id: int, store: MCPDeclarationStore | None = None,
) -> threading.Thread:
    """Start one fail-soft pass off the request path; a live pass is reused."""
    global _compaction_stop, _compaction_thread
    owner_store = store or MCPDeclarationStore()
    with _compaction_lock:
        if _compaction_thread is not None and _compaction_thread.is_alive():
            return _compaction_thread
        stop = threading.Event()

        def _run() -> None:
            started = time.monotonic()
            try:
                stats = owner_store.compact_legacy_slots(
                    user_id=user_id, should_stop=stop.is_set)
            except Exception as exc:  # catalog reads can fail while storage restarts
                logger.warning('[MCPDeclarations] legacy slot compaction aborted: %s', exc)
                return
            logger.info('[MCPDeclarations] legacy slot compaction %s in %.1fs',
                        stats, time.monotonic() - started)

        thread = threading.Thread(
            target=_run, name='tofu-mcp-declaration-compaction', daemon=True)
        thread.start()
        _compaction_stop, _compaction_thread = stop, thread
        return thread


def stop_legacy_slot_compaction(*, timeout: float = 2.0) -> None:
    """Stop between conversations; an in-flight CAS commits or fails atomically."""
    global _compaction_stop, _compaction_thread
    with _compaction_lock:
        stop, thread = _compaction_stop, _compaction_thread
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        # A timed-out join is not a stopped owner; keep the handle so another
        # start cannot run a second pass while this one drains.
        if thread is None or not thread.is_alive():
            _compaction_stop, _compaction_thread = None, None
