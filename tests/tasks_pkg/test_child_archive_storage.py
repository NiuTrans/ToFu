"""Child archive ownership through the real SQLite/PostgreSQL Sidecar."""
import os
import uuid
from pathlib import Path
from contextlib import ExitStack
import pytest
from lib.storage import StorageSupervisor, StorageError

pytestmark = pytest.mark.unit

@pytest.fixture(params=['sqlite', 'postgres'])
def client(request, tmp_path, monkeypatch):
    with ExitStack() as stack:
        if request.param == 'postgres':
            source = os.environ.get('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE')
            if not source or not Path(source).is_file():
                pytest.skip('isolated PostgreSQL DSN fixture unavailable')
            from tests.support.postgres_isolation import isolated_postgres_dsn
            dsn = stack.enter_context(isolated_postgres_dsn(Path(source), tmp_path))
            monkeypatch.setenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', str(dsn))
        supervisor = StorageSupervisor(project_root=tmp_path, backend=request.param, startup_timeout=60)
        supervisor.start()
        try:
            yield supervisor.client
        finally:
            supervisor.stop()


def command(client, op, payload):
    return client.command(op, payload, uuid.uuid4().hex)


def birth(client, conv='real', parent='parent', child='child', user=71):
    command(client, 'conversation.create', {'conv_id': conv, 'user_id': user})
    command(client, 'record.put', {'namespace': 'task_results', 'key': parent,
        'value': {'task_id': parent, 'user_id': user, 'conv_id': conv}})
    payload = {'conversation_id': conv, 'user_id': user, 'parent_task_id': parent, 'child_task_id': child}
    return payload, command(client, 'invocation.register_child', payload)


def test_full_lifecycle_root_neighbor_and_idempotency(client, monkeypatch):
    from lib.tasks_pkg import persistence_store
    monkeypatch.setattr(persistence_store, '_storage_client', lambda **kw: client)
    store = persistence_store.DefaultConversationStore()
    payload, identity = birth(client)
    scope = identity['archive_scope']
    assert command(client, 'invocation.register_child', payload) == identity
    root = store.archive_transcript('real', [{'role': 'user', 'content': 'root'}], user_id=71)
    first = store.archive_transcript(scope, [{'role': 'user', 'content': 'first'}], user_id=71)
    second = store.archive_transcript(scope, [{'role': 'user', 'content': 'second'}], user_id=71)
    assert root and first and second
    assert len(store.list_compaction_archives('real', user_id=71)) == 1
    assert len(store.list_compaction_archives(scope, user_id=71)) == 2
    assert store.get_compaction_archive('real', first, user_id=71) is None
    assert not store.update_archive_summary(first, 'wrong', 1, 1, user_id=71)['updated']
    assert store.update_archive_summary(first, 'summary', 2, 1, user_id=71, archive_scope=scope,
        post_messages=[{'role': 'assistant', 'content': 'summary'}])['updated']
    got = store.get_compaction_archive(scope, first, user_id=71, include_post_messages=True)
    assert got['archive']['convId'] == 'real'
    assert got['postMessages'][0]['content'] == 'summary'
    assert store.prune_archives(scope, 1, user_id=71) == 1
    assert store.delete_archives(scope, user_id=71)['deleted'] == 1
    assert store.get_compaction_archive('real', root, user_id=71)

    third = store.archive_transcript(scope, [{'role': 'user', 'content': 'third'}], user_id=71)
    command(client, 'conversation.delete', {'conv_id': 'real', 'user_id': 71})
    with pytest.raises(StorageError):
        store.list_compaction_archives(scope, user_id=71)
    command(client, 'conversation.restore', {'conv_id': 'real', 'user_id': 71})
    assert store.get_compaction_archive(scope, third, user_id=71)
    command(client, 'conversation.purge', {'conv_id': 'real', 'user_id': 71})
    command(client, 'conversation.create', {'conv_id': 'real', 'user_id': 71})
    assert store.list_compaction_archives(scope, user_id=71) == []
    assert store.list_compaction_archives('real', user_id=71) == []

def test_owner_parent_rebinding_and_record_guard(client):
    payload, identity = birth(client)
    for changes in ({'user_id': 72}, {'parent_task_id': 'missing'}, {'conversation_id': 'missing'}):
        with pytest.raises(StorageError):
            command(client, 'invocation.register_child', {**payload, **changes})
    birth(client, conv='other', parent='other-parent', child='other-child')
    with pytest.raises(StorageError):
        command(client, 'invocation.register_child', {**payload, 'conversation_id': 'other', 'parent_task_id': 'other-parent'})
    with pytest.raises(StorageError):
        command(client, 'record.put', {'namespace': 'task_results', 'key': 'child', 'value': {'user_id': 72}})
    with pytest.raises(StorageError):
        command(client, 'record.delete', {'namespace': 'task_results', 'key': 'child'})
    with pytest.raises(StorageError):
        client.query('compaction_archive.list', {'conversation_id': 'real', 'user_id': 71, 'archive_scope': 'invocation:71:unregistered'})


def test_registration_headless_and_failure_disposal(monkeypatch):
    import lib.storage
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.invocation_registration import register_child_invocation
    disposed = []
    monkeypatch.setattr(manager, 'discard_task', disposed.append)
    def fail(**kwargs):
        raise RuntimeError('registration unavailable')
    monkeypatch.setattr(lib.storage, 'get_storage_client', fail)
    register_child_invocation({'id': 'headless'}, {'_transientRuntime': True})
    assert disposed == []
    with pytest.raises(RuntimeError, match='registration unavailable'):
        register_child_invocation({'id': 'child'}, {})
    assert disposed == ['child']


def test_real_birth_archive_and_cost_settlement_preserve_parent(client, monkeypatch):
    from copy import deepcopy
    import lib.storage
    import lib.agent_core.store as stores
    from lib.tasks_pkg import persistence_store
    from lib.tasks_pkg.child_invocation import create_child_invocation
    from lib.tasks_pkg.manager import discard_task, task_user_id
    from lib.tasks_pkg.compaction._archive import _archive_transcript
    from lib.tasks_pkg.compaction._ownership import compaction_owner_key
    from lib.tasks_pkg.compaction._settlement import settle_compaction_usage
    from lib.tasks_pkg.compaction._compaction_usage import (
        record_compaction_usage, get_compaction_usage, reset_compaction_usage,
    )
    namespace = uuid.uuid4().hex
    payload, _ = birth(client, conv=namespace, parent=namespace + '-parent',
                       child=namespace + '-sibling')
    parent = {'id': payload['parent_task_id'], 'convId': namespace,
              '_userId': 71, 'config': {'memoryEnabled': False},
              'usage': {'prompt_tokens': 101}, 'messages': [{'role': 'user', 'content': 'root'}]}
    task_user_id(parent)
    before = deepcopy(parent)
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda **kw: client)
    monkeypatch.setattr(persistence_store, '_storage_client', lambda **kw: client)
    store = persistence_store.DefaultConversationStore()
    monkeypatch.setattr(stores, 'get_conversation_store', lambda: store)
    child = create_child_invocation(parent, model='offline-test', tools=[],
        objective='child goal', event_sink=lambda event: None, abort_check=lambda: False)
    key = compaction_owner_key(child)
    try:
        assert child['_archiveScope'] == key
        assert client.query('invocation.resolve_child', {
            'archive_scope': key, 'user_id': 71})['parent_task_id'] == parent['id']
        root_archive = store.archive_transcript(namespace, parent['messages'], user_id=71)
        child_archive = _archive_transcript(namespace, child['messages'], task=child,
                                            user_id=71, emit_event=False)
        assert child_archive
        assert store.get_compaction_archive(key, child_archive, user_id=71)
        assert store.get_compaction_archive(namespace, child_archive, user_id=71) is None
        assert store.get_compaction_archive(namespace, root_archive, user_id=71)
        record_compaction_usage(namespace, {'prompt_tokens': 11})
        record_compaction_usage(key, {'prompt_tokens': 23, 'completion_tokens': 3})
        root_ledger = deepcopy(get_compaction_usage(namespace))
        child['usage'] = {'prompt_tokens': 7}
        settle_compaction_usage(child)
        settle_compaction_usage(child)
        assert child['usage']['prompt_tokens'] == 30
        assert child['usage']['completion_tokens'] == 3
        assert len(child['apiRounds']) == 1
        assert get_compaction_usage(namespace) == root_ledger
        assert parent == before
        settle_compaction_usage(parent)
        settle_compaction_usage(parent)
        assert parent['usage']['prompt_tokens'] == 112
        assert len(parent['apiRounds']) == 1
        assert child['usage']['prompt_tokens'] == 30
    finally:
        reset_compaction_usage(namespace)
        reset_compaction_usage(key)
        discard_task(child['id'])


def test_sqlite_registration_and_archive_write_rollback(tmp_path, monkeypatch):
    import sqlite3
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.schema import initialize_schema
    from lib.storage_sidecar.operations_pkg._invocations import register_child, resolve_child
    from lib.storage_sidecar.operations_pkg._archives import _archive_create, _archive_list
    from lib.storage_sidecar.operations_pkg._records import _record_put
    from lib.storage_sidecar.operations_pkg import _turns_core
    connection = sqlite3.connect(tmp_path / 'rollback.sqlite')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    try:
        initialize_schema(session)
        connection.execute("INSERT INTO storage_conversations(id,user_id) VALUES ('rollback',71)")
        _record_put(session, {'namespace': 'task_results', 'key': 'parent',
            'value': {'task_id': 'parent', 'user_id': 71, 'conv_id': 'rollback'}})
        connection.commit()
        payload = {'conversation_id': 'rollback', 'user_id': 71,
                   'parent_task_id': 'parent', 'child_task_id': 'child'}
        with pytest.raises(RuntimeError, match='after birth'):
            with connection:
                register_child(session, payload)
                raise RuntimeError('after birth')
        scope = 'invocation:71:child'
        with pytest.raises(StorageError):
            resolve_child(session, {'archive_scope': scope, 'user_id': 71})
        with connection:
            identity = register_child(session, payload)
        archive = {**payload, 'archive_scope': identity['archive_scope'],
                   'archive_id': 'rollback-archive', 'messages': []}
        def fail_after_insert(*args):
            raise RuntimeError('after archive insert')
        with monkeypatch.context() as patch:
            patch.setattr(_turns_core, '_mark_conversation_search_projection_dirty', fail_after_insert)
            with pytest.raises(RuntimeError, match='after archive insert'):
                with connection:
                    _archive_create(session, archive)
        assert _archive_list(session, archive)['archives'] == []
        with connection:
            assert _archive_create(session, archive)['created']
        assert len(_archive_list(session, archive)['archives']) == 1
    finally:
        connection.close()
