"""Candidate files survive unknown metadata outcomes and roll back before entry."""
import asyncio
import hashlib

import pytest

from lib.knowledge.repository import KnowledgeRepository
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._knowledge_sidecar',)

RAW = b'Original source evidence remains available after lost command replies.'
ASSET = b'Immutable visual evidence bytes'


def prepare_operation(store, tmp_path, monkeypatch, operation):
    original_extract = store.extract

    def extract(*arguments, **options):
        parsed = original_extract(*arguments, **options)
        store.close_extracted_assets(parsed)
        parsed['assets'] = [{'raw': ASSET, 'suffix': '.bin',
                             'caption': 'Visual evidence', 'kind': 'image'}]
        return parsed

    monkeypatch.setattr(store, 'extract', extract)
    monkeypatch.setattr(store, 'visual_enrichment_enabled', lambda **_: False)
    if operation in ('create', 'reindex'):
        create = lambda: store.add_document(RAW, 'source.txt', user_id=1)
        if operation == 'create':
            return create, 'knowledge.document.create'
        identity = create()['id']
        return lambda: store.reindex_document(identity, user_id=1), 'knowledge.document.replace'
    source = tmp_path / 'original.mp4'
    source.write_bytes(RAW)
    create = lambda: store.create_media_source(
        source, 'source.mp4', user_id=1, media_metadata={'status': 'pending'})
    if operation == 'media_create':
        return create, 'knowledge.document.create'
    identity = create()['id']
    return lambda: store.replace_media_evidence(
        identity, user_id=1, chunks=[{'content': 'Scene evidence', 'asset_ordinals': [0]}],
        assets=[{'raw': ASSET, 'suffix': '.bin', 'caption': 'Scene evidence'}],
        media_metadata={'status': 'ready'}), 'knowledge.document.replace'


def assert_published_files(store, operation):
    repository = KnowledgeRepository(1)
    row = repository.document_by_digest(hashlib.sha256(RAW).hexdigest())
    assert row is not None
    row = repository.document(row['id'])
    assert (store._source_root(1) / row['stored_name']).read_bytes() == RAW
    assets = row.get('assets') or []
    assert bool(assets) is (operation != 'media_create')
    for asset in assets:
        assert (store._asset_root(1) / asset['stored_name']).read_bytes() == ASSET
    return row


@pytest.mark.parametrize('operation', ['create', 'reindex', 'media_create', 'media_replace'])
@pytest.mark.parametrize('kind', ['lost_reply', 'io_error', 'interrupt', 'cancel'])
def test_postcommit_failure_preserves_original_and_visuals(
        isolated_knowledge, tmp_path, monkeypatch, operation, kind):
    store = isolated_knowledge
    invoke, target = prepare_operation(store, tmp_path, monkeypatch, operation)
    failure = {'lost_reply': lambda: StorageError('database_unavailable', 'reply lost', retryable=True),
               'io_error': lambda: OSError('reply decoding failed'),
               'interrupt': lambda: KeyboardInterrupt('interrupted after commit'),
               'cancel': lambda: asyncio.CancelledError('cancelled after commit')}[kind]()
    original_client = KnowledgeRepository._client
    receipts = []

    class DropReply:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            return getattr(self.client, name)

        def command(self, operation, payload, command_id, *arguments, **options):
            result = self.client.command(operation, payload, command_id, *arguments, **options)
            if operation == target:
                receipts.append((command_id, payload['document_id']))
                raise failure
            return result

    def client(owner, *, write=False):
        result = original_client(owner, write=write)
        return DropReply(result) if write else result

    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeRepository, '_client', client)
        with pytest.raises(type(failure)) as caught:
            invoke()
    assert caught.value is failure
    assert len(receipts) == (2 if kind == 'lost_reply' else 1)
    assert len(set(receipts)) == 1
    assert any('files retained' in note for note in failure.__notes__)
    assert_published_files(store, operation)
    # Ordinary digest retry can reach the committed document and its original.
    if operation == 'create':
        result = store.add_document(RAW, 'source.txt', user_id=1)
        assert result['duplicate'] and result['id'] == receipts[0][1]


@pytest.mark.parametrize('operation', ['create', 'reindex', 'media_create', 'media_replace'])
def test_failure_before_storage_client_entry_removes_only_new_files(
        isolated_knowledge, tmp_path, monkeypatch, operation):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, operation)
    original_client = KnowledgeRepository._client
    failure = OSError('storage client construction failed')
    roots = [store._source_root(1), store._asset_root(1)]
    before = {str(path): path.read_bytes() for root in roots for path in root.glob('*')}

    def refuse(owner, *, write=False):
        if write:
            raise failure
        return original_client(owner, write=False)

    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeRepository, '_client', refuse)
        with pytest.raises(OSError) as caught:
            invoke()
    assert caught.value is failure
    after = {str(path): path.read_bytes() for root in roots for path in root.glob('*')}
    assert after == before


def test_later_client_refusal_cannot_erase_an_earlier_unknown_commit(
        isolated_knowledge, tmp_path, monkeypatch):
    store = isolated_knowledge
    invoke, target = prepare_operation(store, tmp_path, monkeypatch, 'create')
    original_client = KnowledgeRepository._client
    first = StorageError('database_unavailable', 'first reply lost', retryable=True)
    second = StorageError('database_busy', 'retry not dispatched', retryable=True,
                          request_not_dispatched=True)
    attempts = []

    class DropReply:
        def __init__(self, client):
            self.client = client

        def command(self, operation, payload, command_id):
            assert operation == target
            self.client.command(operation, payload, command_id)
            raise first

    def client(owner, *, write=False):
        if not write:
            return original_client(owner, write=False)
        attempts.append(True)
        if len(attempts) == 2:
            raise second
        return DropReply(original_client(owner, write=True))

    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeRepository, '_client', client)
        with pytest.raises(StorageError) as caught:
            invoke()
    assert caught.value is second and second.request_not_dispatched
    assert len(attempts) == 2
    assert_published_files(store, 'create')


def test_cleanup_control_preserves_failure_and_attempts_every_candidate(
        isolated_knowledge, tmp_path, monkeypatch):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, 'create')
    original_client = KnowledgeRepository._client
    failure = OSError('storage client construction failed')
    attempts = []

    def refuse(owner, *, write=False):
        if write:
            raise failure
        return original_client(owner, write=False)

    def interrupt(paths, **options):
        attempts.extend(paths)
        raise KeyboardInterrupt('cleanup interrupted')

    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeRepository, '_client', refuse)
        patch.setattr(store, '_unlink_files', interrupt)
        with pytest.raises(OSError) as caught:
            invoke()
    assert caught.value is failure
    assert len(attempts) == 2 and len(set(attempts)) == 2
    assert len(failure.__notes__) == 2
