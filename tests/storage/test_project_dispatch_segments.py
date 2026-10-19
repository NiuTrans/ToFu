"""Real SQLite/PostgreSQL/native transaction, ownership and page contracts.

Evidence is durable across more than the old 128-assignment ceiling. Faults
after earlier writes prove rollback; a retried command proves receipt replay.
The fixture never opens or switches the user's selected live authority.
"""
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

import pytest

from lib.storage.errors import StorageError
from lib.storage.generated_project_dispatch_contract import SPEC

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.fixture
def store(semantic_authority):
    factory, owner, engine = semantic_authority
    if engine is not None:
        assert engine._build_identity['source_sha256'] == engine._checkout_source_sha256
    return factory, {'user_id': owner, 'project_path': '/project/中文'}, engine


def change(identity='assignment', *, collection='assignments', version=0, **value):
    return {'collection': collection, 'id': identity, 'expected_version': version, 'value': value or {'brief': 'retained'}}


def commit(store, revision=0, changes=None, *, state=None, command_id=None):
    factory, scope, _ = store
    return factory(write=True).command('project.dispatch.commit', {**scope,
        'expected_revision': revision, 'head': state or {'nextSequence': revision + 1},
        'changes': changes if changes is not None else [change()]}, command_id or uuid.uuid4().hex)


def get(store, identity='assignment', collection='assignments'):
    factory, scope, _ = store
    return factory(write=False).query('project.dispatch.get', {**scope, 'collection': collection, 'id': identity})


def page(store, collection='indexes', **options):
    factory, scope, _ = store
    return factory(write=False).query('project.dispatch.list', {**scope, 'collection': collection, **options})


def test_atomic_receipt_replay_and_late_conflict_rollback(store):
    original = commit(store, changes=[change('a'), change('b')], command_id='first')
    assert original['version'] == 1
    # Simulate an unobserved ACK: exact replay returns the original receipt,
    # not a second project revision or a second child version.
    assert commit(store, changes=[change('a'), change('b')], command_id='first') == original
    assert get(store, 'a')['version'] == 1
    with pytest.raises(StorageError) as error:
        commit(store, 1, [change('new'), change('b', version=99)], command_id='late-conflict')
    assert error.value.code == 'database_conflict'
    assert get(store, 'new') is None
    assert get(store, 'root', 'head')['version'] == 1
    assert get(store, 'b')['version'] == 1
    # No poisoned receipt survived the rollback.
    assert commit(store, 1, [change('new')], command_id='late-conflict')['version'] == 2
    with pytest.raises(StorageError):
        commit(store, 2, [change('different')], command_id='first')
    assert get(store, 'different') is None


def test_every_owned_evidence_collection_is_retained_and_point_readable(store):
    changes = [change('same-id', collection=collection, kind=collection) for collection in SPEC['collections']]
    commit(store, changes=changes)
    for collection in SPEC['collections']:
        assert get(store, 'same-id', collection)['value'] == {'kind': collection}
        if collection == 'indexes':
            continue
        with pytest.raises(StorageError):
            commit(store, 1, [{'collection': collection, 'id': 'same-id', 'expected_version': 1, 'delete': True}])
        assert get(store, 'root', 'head')['version'] == 1


def test_project_lifetime_pages_keep_large_evidence_out_of_index_reads(store):
    for batch in range(3):
        rows = []
        for number in range(batch * 32, (batch + 1) * 32):
            identity = f'{number:020d}'
            rows += [change(identity, brief='evidence-' + identity + 'x' * 8192),
                     change(identity, collection='indexes', assignmentId=identity)]
        commit(store, batch, rows)
    for batch in range(3, 5):
        commit(store, batch, [change(f'{number:020d}') for number in range(batch * 32, (batch + 1) * 32)])
    first = page(store, limit=63)
    second = page(store, after_key=first[-1]['key'], limit=63)
    assert len(first) == 63 and len(second) == 33
    assert not {row['key'] for row in first} & {row['key'] for row in second}
    assert len(str(first)) < 20000
    keys, cursor = [], ''
    while rows := page(store, 'assignments', limit=64, after_key=cursor):
        assert all('value' not in row for row in rows)
        keys.extend(row['key'] for row in rows); cursor = rows[-1]['key']
    assert len(keys) == len(set(keys)) == 160
    assert get(store, '00000000000000000000')['value']['brief'].startswith('evidence-')
    _, _, engine = store
    if engine is not None:
        engine.restart_cycle()
        assert get(store, '00000000000000000000')['version'] == 1
        assert get(store, 'root', 'head')['version'] == 5


def test_owned_scope_cursor_literal_prefix_and_binary_order(store):
    identities = ['_:b', '_:Z', '_:a', '_:0', 'x:a']
    commit(store, changes=[change(identity, collection='indexes', id=identity) for identity in identities])
    assert [row['value']['id'] for row in page(store, prefix='_:')] == sorted(identities[:4])
    assert [row['value']['id'] for row in page(store, prefix='_:a')] == ['_:a']
    assert page(store, prefix='_:A') == []
    factory, scope, engine = store
    other = factory, {**scope, 'project_path': '/different-project'}, engine
    assert get(other) is None and page(other) == []
    foreign_cursor = page(store)[0]['key']
    with pytest.raises(StorageError):
        page(other, after_key=foreign_cursor)


def test_reverse_pages_are_owned_exclusive_and_complete(store):
    identities = [f'order:{number:020d}' for number in range(80)]
    commit(store, changes=[change(identity, collection='indexes', id=identity) for identity in identities])
    first = page(store, prefix='order:', reverse=True, limit=32)
    second = page(store, prefix='order:', reverse=True, after_key=first[-1]['key'], limit=32)
    last = page(store, prefix='order:', reverse=True, after_key=second[-1]['key'], limit=32)
    assert [row['value']['id'] for row in first + second + last] == identities[::-1]
    newer = page(store, prefix='order:', after_key=second[0]['key'], limit=32)
    assert [row['key'] for row in newer] == [row['key'] for row in first[::-1]]
    assert page(store, prefix='order:', reverse=True, after_key=last[-1]['key']) == []
    for invalid in (1, None, 'true'):
        with pytest.raises(StorageError): page(store, reverse=invalid)
    factory, scope, engine = store
    with pytest.raises(StorageError):
        page((factory, {**scope, 'project_path': '/another'}, engine), reverse=True, after_key=first[-1]['key'])
    heads = factory().query('project.dispatch.projects', {'user_id': scope['user_id'], 'limit': 1})
    assert heads[0]['value']['projectPath'] == scope['project_path']
    with pytest.raises(StorageError):
        factory().query('project.dispatch.projects', {'user_id': scope['user_id'], 'after_key': '999:elsewhere'})
    other_owner = factory, {**scope, 'user_id': scope['user_id'] + 1}, engine
    if engine is None:
        assert get(other_owner) is None and page(other_owner) == []
        assert factory().query('project.dispatch.projects', {'user_id': scope['user_id'] + 1}) == []
    else:
        with pytest.raises(StorageError) as error:
            get(other_owner)
        assert error.value.code == 'database_forbidden'


def test_project_head_serializes_racing_grants_without_partial_loser(store):
    commit(store, changes=[])
    barrier = threading.Barrier(2)
    def run(identity):
        barrier.wait(timeout=5)
        try:
            return identity, commit(store, 1, [change(identity)])
        except StorageError as error:
            return identity, error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ['left', 'right']))
    winner = [identity for identity, result in results if isinstance(result, dict)]
    loser = [identity for identity, result in results if result == 'database_conflict']
    assert len(winner) == len(loser) == 1
    assert get(store, winner[0]) and get(store, loser[0]) is None
    assert get(store, 'root', 'head')['version'] == 2


def test_only_indexes_can_be_deleted_and_absence_is_exact_cas(store):
    commit(store, changes=[change(), change('active', collection='indexes')])
    deletion = {'collection': 'assignments', 'id': 'assignment', 'expected_version': 1, 'delete': True}
    with pytest.raises(StorageError):
        commit(store, 1, [deletion])
    assert get(store)
    deletion.update(collection='indexes', id='active')
    commit(store, 1, [deletion])
    assert not page(store)
    with pytest.raises(StorageError) as error:
        commit(store, 2, [change('must-rollback'), deletion])
    assert error.value.code == 'database_conflict'
    assert get(store, 'must-rollback') is None
    deletion['expected_version'] = 0
    assert commit(store, 2, [deletion])['version'] == 3
    # Exercise the entire transaction width. Each native put/delete step must
    # own a distinct result slot even though only the head receipt is returned.
    commit(store, 3, [change(str(i), collection='indexes') for i in range(64)])
    commit(store, 4, [{'collection': 'indexes', 'id': str(i), 'expected_version': 1, 'delete': True} for i in range(64)])
    assert page(store) == [] and get(store, 'root', 'head')['version'] == 5


def test_generic_record_ports_cannot_bypass_dispatch_transaction(store):
    factory, scope, _ = store
    for namespace in SPEC['namespaces'].values():
        for operation in ['get', 'list', 'put', 'delete']:
            payload = {'namespace': namespace, 'key': 'guess', 'value': {}, 'user_id': scope['user_id']}
            with pytest.raises(StorageError) as error:
                if operation in ['get', 'list']:
                    factory().query('record.' + operation, payload)
                else:
                    factory(write=True).command('record.' + operation, payload, uuid.uuid4().hex)
            assert error.value.code == 'database_protocol_error'


def test_invalid_payloads_never_advance_head_or_leave_children(store):
    factory, scope, _ = store
    base = {**scope, 'expected_revision': 0, 'head': {}, 'changes': [change()]}
    cases = [
        {'changes': [change(), change()]}, {'expected_revision': True},
        {'changes': [change(collection='arbitrary')]}, {'changes': [change('bad/id')]},
        {'changes': [change(value='x', collection='indexes', oversized='x' * 2048)]},
        {'head': {'oversized': 'x' * 16384}},
        {'changes': [change(str(i)) for i in range(SPEC['limits']['commitChanges'] + 1)]},
        {'changes': [{**change(), 'expected_version': -1}]},
        {'changes': [{**change(), 'delete': False}]},
        {'changes': [{**change(), 'value': None}]},
        {'project_path': '  '}, {'project_path': '/a\x00b'}, {'user_id': 0},
    ]
    for invalid in cases:
        with pytest.raises(StorageError) as error:
            factory(write=True).command('project.dispatch.commit', {**base, **invalid}, uuid.uuid4().hex)
        assert error.value.code in {'database_protocol_error', 'database_forbidden'}
        assert get(store, 'root', 'head') is None and get(store) is None
    for options in [{'limit': 65}, {'limit': True}, {'prefix': '/'}, {'after_key': 'wrong'}]:
        with pytest.raises(StorageError):
            page(store, **options)


def test_encoded_byte_bounds_are_per_call_and_do_not_evict_durable_evidence(store):
    # Exactly 2048 UTF-8 bytes: two-byte characters demonstrate this is not a
    # character-count gate. A later excessive transaction retains this index.
    maximum = SPEC['limits']['indexBytes']
    commit(store, changes=[change('exact', collection='indexes', x='é' * ((maximum - 8) // 2))])
    assert len(page(store)) == 1
    with pytest.raises(StorageError):
        commit(store, 1, [change('too-large', collection='indexes', x='é' * ((maximum - 8) // 2) + 'x')])
    with pytest.raises(StorageError):
        commit(store, 1, [change('too-large', x='x' * SPEC['limits']['documentBytes'])])
    # Each child fits, but their combined document bytes exceed one command.
    with pytest.raises(StorageError):
        commit(store, 1, [change(str(i), x='x' * (3 * 1024 * 1024)) for i in range(3)])
    assert get(store, 'root', 'head')['version'] == 1
    assert page(store, 'assignments') == []
    assert len(page(store)) == 1
    exact = SPEC['limits']['documentBytes'] - 8  # {"x":""}
    commit(store, 1, [change('maximum-document', x='x' * exact)])
    assert len(get(store, 'maximum-document')['value']['x']) == exact
    assert page(store, 'assignments')[0].keys() == {'key', 'version', 'updated_at_ms'}
