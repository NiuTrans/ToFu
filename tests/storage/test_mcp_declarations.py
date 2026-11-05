"""Durable declaration authority on the configured disposable storage process."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
import uuid

import pytest

from lib.storage.mcp_declarations import (
    MAX_DECLARATION_BYTES, MCPDeclarationError, MCPDeclarationStore, SETTING_KEY,
    start_legacy_slot_compaction, stop_legacy_slot_compaction,
)
from tests.storage.test_storage_application_contract import application_storage as application_storage
from tests.storage.test_storage_application_contract import application_storage_owner_id as application_storage_owner_id

pytestmark = pytest.mark.api

NATIVE_INLINE_VALUE_BYTES = json.loads(
    (Path(__file__).resolve().parents[2] / 'contracts' / 'tofudb_ir_v1.json').read_text()
)['physicalBounds']['maximumEntityInlineValueBytes']
# Allowance for the versioned-document envelope and logical key around a header.
INLINE_HEADER_BUDGET = NATIVE_INLINE_VALUE_BYTES - 512


def definition(name='read'):
    # Deliberately non-alphabetic nested keys: JSONB/map ordering must not
    # change the provider wire, including schema properties/required arrays.
    return {'type': 'function', 'function': {'name': f'mcp__docs__{name}',
        'parameters': {'type': 'object', 'properties': {'z': {'type': 'string'},
                       'a': {'type': 'integer'}}, 'required': ['z', 'a']},
        'description': 'ordered wire'}}


def typical_wire(count=24):
    """Ordinary MCP tool JSON, larger than one native inline entity value."""
    return [{'type': 'function', 'function': {
        'name': f'mcp__docs__tool_{index:02d}',
        'description': (f'Search, read or update workspace documents (variant {index}). '
                        'Returns identifiers, titles and the most relevant excerpt.'),
        'parameters': {'type': 'object', 'properties': {
            'query': {'type': 'string', 'description': 'Full-text query; quoted phrases match.'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
            'cursor': {'type': 'string', 'description': 'Opaque cursor from a previous call.'}},
            'required': ['query'], 'additionalProperties': False}}} for index in range(count)]


def wire_text(definitions):
    return json.dumps(definitions, ensure_ascii=False, separators=(',', ':'))


def header(client, conv):
    return client.query('conversation.get', {'conv_id': conv, 'user_id': 1,
                                             'derive_messages': False})['metadata']


def encoded_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def create(client, conv='declarations', settings=None):
    client.command('conversation.create', {'conv_id': conv, 'user_id': 1,
        'title': 'Declaration test', 'created_at': 1, 'updated_at': 1,
        'settings': settings or {'unrelated': 'preserve'}}, uuid.uuid4().hex)
    return conv


def test_durable_empty_reset_owner_and_delete(application_storage):
    client = application_storage
    conv = create(client)
    store = MCPDeclarationStore(client)
    assert store.get(user_id=1, conversation_id=conv) is None
    assert store.freeze([], user_id=1, conversation_id=conv) == []
    assert MCPDeclarationStore(client).freeze([definition()], user_id=1, conversation_id=conv) == []
    with pytest.raises(MCPDeclarationError):
        store.get(user_id=2, conversation_id=conv)
    store.reset(user_id=1, conversation_id=conv)
    assert store.get(user_id=1, conversation_id=conv) is None
    wire = [definition('second'), definition('first')]
    store.freeze(wire, user_id=1, conversation_id=conv)
    assert json.dumps(store.get(user_id=1, conversation_id=conv)) == json.dumps(wire)
    # Reset/re-freeze the same candidate must not replay an old command receipt.
    store.reset(user_id=1, conversation_id=conv)
    store.freeze(wire, user_id=1, conversation_id=conv)
    assert store.get(user_id=1, conversation_id=conv) == wire
    settings = client.query('conversation.get', {'conv_id': conv, 'user_id': 1,
        'derive_messages': False})['metadata']['settings']
    assert settings['unrelated'] == 'preserve'
    clone = create(client, 'clone', settings)
    assert store.get(user_id=1, conversation_id=clone) is None
    client.command('conversation.delete', {'conv_id': conv, 'user_id': 1}, uuid.uuid4().hex)
    with pytest.raises(MCPDeclarationError):
        store.get(user_id=1, conversation_id=conv)


def test_atomic_first_writer_preserves_concurrent_settings(application_storage):
    client = application_storage
    conv = create(client)
    barrier = threading.Barrier(2)
    local = threading.local()

    class RacingClient:
        def query(self, operation, payload):
            result = client.query(operation, payload)
            if not getattr(local, 'read', False):
                local.read = True
                barrier.wait(timeout=10)
            return result

        def command(self, *args):
            return client.command(*args)

    store = MCPDeclarationStore(RacingClient())
    def freeze(index):
        return store.freeze([definition(str(index))], user_id=1, conversation_id=conv)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(freeze, range(2)))
    assert results[0] == results[1]
    assert MCPDeclarationStore(client).get(user_id=1, conversation_id=conv) == results[0]
    assert client.query('conversation.get', {'conv_id': conv, 'user_id': 1,
        'derive_messages': False})['metadata']['settings']['unrelated'] == 'preserve'


def test_corruption_bounds_and_missing_fail_closed(application_storage):
    client = application_storage
    conv = create(client)
    store = MCPDeclarationStore(client)
    oversized = definition()
    oversized['function']['description'] = 'x' * MAX_DECLARATION_BYTES
    with pytest.raises(MCPDeclarationError, match='bytes'):
        store.freeze([oversized], user_id=1, conversation_id=conv)
    assert store.get(user_id=1, conversation_id=conv) is None
    with pytest.raises(MCPDeclarationError, match='missing'):
        store.freeze([], user_id=1, conversation_id='absent')
    client.command('conversation.settings.update', {'conv_id': conv, 'user_id': 1,
        'updates': {SETTING_KEY: {'version': 999}}}, uuid.uuid4().hex)
    with pytest.raises(MCPDeclarationError, match='Unsupported'):
        store.get(user_id=1, conversation_id=conv)


def test_contention_exhaustion_and_storage_failure_are_visible():
    class Client:
        def query(self, *args):
            return {'metadata': {'settings': {}}}
        def command(self, *args):
            return {'applied': False, 'conflict': True}
    with pytest.raises(MCPDeclarationError, match='contention'):
        MCPDeclarationStore(Client()).freeze([], user_id=1, conversation_id='c')
    class Broken(Client):
        def command(self, *args):
            raise OSError('storage offline')
    with pytest.raises(MCPDeclarationError, match='write failed') as caught:
        MCPDeclarationStore(Broken()).freeze([], user_id=1, conversation_id='c')
    assert isinstance(caught.value.__cause__, OSError)


def test_unrelated_settings_race_retries_without_clobber(application_storage):
    client = application_storage
    conv = create(client)

    class RacingSettings:
        raced = False
        def query(self, *args):
            return client.query(*args)
        def command(self, *args):
            if not self.raced:
                self.raced = True
                client.command('conversation.settings.update', {
                    'conv_id': conv, 'user_id': 1,
                    'updates': {'parallel_worker': 'retained'},
                }, uuid.uuid4().hex)
            return client.command(*args)

    store = MCPDeclarationStore(RacingSettings())
    store.freeze([definition()], user_id=1, conversation_id=conv)
    settings = client.query('conversation.get', {'conv_id': conv, 'user_id': 1,
        'derive_messages': False})['metadata']['settings']
    assert settings['parallel_worker'] == 'retained'
    assert settings['unrelated'] == 'preserve'
    # No write/history growth on ordinary repeated freezes or detached reads.
    first = store.get(user_id=1, conversation_id=conv)
    first[0]['function']['description'] = 'mutated caller copy'
    assert store.freeze([], user_id=1, conversation_id=conv) == [definition()]


def test_corrupt_slot_requires_explicit_reset(application_storage):
    client = application_storage
    conv = create(client, settings={SETTING_KEY: {'version': 999}})
    store = MCPDeclarationStore(client)
    with pytest.raises(MCPDeclarationError):
        store.freeze([], user_id=1, conversation_id=conv)
    store.reset(user_id=1, conversation_id=conv)
    assert store.get(user_id=1, conversation_id=conv) is None
    assert store.freeze([], user_id=1, conversation_id=conv) == []


def test_frozen_wire_keeps_conversation_header_inline(application_storage):
    # Catalog pages, lists, gets and Turn settlement read the whole header;
    # wire text inside it pushed ordinary headers out of line (a blob read each).
    client = application_storage
    conv = create(client)
    wire = typical_wire()
    assert len(wire_text(wire).encode('utf-8')) > NATIVE_INLINE_VALUE_BYTES
    store = MCPDeclarationStore(client)
    store.freeze(wire, user_id=1, conversation_id=conv)
    assert wire_text(store.get(user_id=1, conversation_id=conv)) == wire_text(wire)
    assert encoded_bytes(header(client, conv)) <= INLINE_HEADER_BUDGET


def test_catalog_page_reads_no_header_blob_after_compaction(application_storage, request):
    if request.node.callspec.params['application_storage'] == 'sqlite':
        pytest.skip('blob materialization is a native-engine cost')
    client = application_storage
    text = wire_text(typical_wire())
    for index in range(6):
        conv = f'catalog-{index}'
        create(client, conv, {'folderId': 'folder', SETTING_KEY: {
            'version': 1, 'ownerUserId': 1, 'conversationId': conv, 'wireJson': text}})
    MCPDeclarationStore(client).freeze(typical_wire(), user_id=1,
                                       conversation_id=create(client, 'catalog-v2'))

    def blob_reads():
        cache = client.metrics()['decodedBlobChunkCache']
        return cache['hits'] + cache['misses'] + cache['wholeBlob']['lookups']

    def catalog(limit):
        before = blob_reads()
        page = client.query('conversation.list', {
            'user_id': 1, 'catalog_page': True, 'include_messages': False,
            'order_by': 'updated_at_desc', 'limit': limit, 'before_id': '',
            'settings_keys': ['folderId', SETTING_KEY]})
        assert len(page['items']) == 7
        return page['items'], blob_reads() - before

    _, legacy_reads = catalog(500)
    assert legacy_reads >= 6
    assert MCPDeclarationStore(client).compact_legacy_slots(user_id=1)['compacted'] == 6
    items, compact_reads = catalog(499)
    assert {item['metadata']['settings'][SETTING_KEY]['version'] for item in items} == {2}
    assert compact_reads == 0


def test_legacy_slots_compact_losslessly_without_reordering(application_storage):
    client = application_storage
    text = wire_text(typical_wire())
    legacy = {'version': 1, 'ownerUserId': 1, 'conversationId': 'legacy-own', 'wireJson': text}
    create(client, 'legacy-own', {SETTING_KEY: legacy, 'unrelated': 'preserve'})
    create(client, 'legacy-copy', {SETTING_KEY: legacy})
    create(client, 'legacy-corrupt', {SETTING_KEY: dict(legacy, ownerUserId='forged')})
    store = MCPDeclarationStore(client)
    assert wire_text(store.get(user_id=1, conversation_id='legacy-own')) == text
    before = {conv: header(client, conv) for conv in ('legacy-own', 'legacy-copy')}
    assert encoded_bytes(before['legacy-own']) > NATIVE_INLINE_VALUE_BYTES

    assert store.compact_legacy_slots(user_id=1) == {'scanned': 3, 'compacted': 2, 'failed': 1}
    assert wire_text(store.get(user_id=1, conversation_id='legacy-own')) == text
    assert store.get(user_id=1, conversation_id='legacy-copy') is None
    for conv, old in before.items():
        new = header(client, conv)
        assert (new['updated_at'], new['rev']) == (old['updated_at'], old['rev'])
        assert new['settings'][SETTING_KEY]['conversationId'] == 'legacy-own'
        assert encoded_bytes(new) <= INLINE_HEADER_BUDGET
    assert header(client, 'legacy-own')['settings']['unrelated'] == 'preserve'
    assert store.compact_legacy_slots(user_id=1) == {'scanned': 3, 'compacted': 0, 'failed': 1}


def test_compaction_pass_is_single_and_stoppable():
    entered = threading.Event()

    class Store:
        passes = 0

        def compact_legacy_slots(self, *, user_id, should_stop):
            Store.passes += 1
            entered.set()
            while not should_stop():
                time.sleep(0.01)
            return {}

    thread = start_legacy_slot_compaction(user_id=1, store=Store())
    assert entered.wait(5)
    assert start_legacy_slot_compaction(user_id=1, store=Store()) is thread
    stop_legacy_slot_compaction(timeout=5)
    assert not thread.is_alive() and Store.passes == 1
