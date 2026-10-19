"""Directory publication precedes metadata; sync faults retain prior evidence."""
import asyncio
import os
import stat

import pytest

from lib.knowledge import file_durability
from lib.knowledge.repository import KnowledgeRepository
from tests.knowledge.test_knowledge_publication import prepare_operation

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    not hasattr(os, 'O_DIRECTORY'), reason='POSIX directory fsync required')]
pytest_plugins = ('tests._knowledge_sidecar',)


def file_snapshot(store):
    return {str(path): path.read_bytes()
            for root in (store._source_root(1), store._asset_root(1))
            for path in root.glob('*')}


@pytest.mark.parametrize('operation', ['create', 'reindex', 'media_create', 'media_replace'])
def test_directory_entries_are_synced_before_metadata_entry(
        isolated_knowledge, tmp_path, monkeypatch, operation):
    store = isolated_knowledge
    invoke, target = prepare_operation(store, tmp_path, monkeypatch, operation)
    synced, commands = [], []
    original_sync, original_client = file_durability._sync_directory, KnowledgeRepository._client
    roots = []
    if operation in ('create', 'media_create'):
        roots.append(store._source_root(1))
    if operation != 'media_create':
        roots.append(store._asset_root(1))
    anchor = tmp_path
    expected = {ancestor for root in roots for ancestor in [root, *root.parents]
                if ancestor == anchor or anchor in ancestor.parents}

    def sync(directory):
        original_sync(directory)
        synced.append(directory)

    class CheckPublication:
        def __init__(self, client):
            self.client = client

        def command(self, operation, *args, **kwargs):
            if operation == target:
                assert set(synced) == expected
                assert len(synced) == len(expected)
                for directory in expected - {anchor}:
                    assert synced.index(directory) < synced.index(directory.parent)
                commands.append(operation)
            return self.client.command(operation, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(file_durability, '_sync_directory', sync)
        patch.setattr(KnowledgeRepository, '_client', lambda owner, write=False:
                      CheckPublication(original_client(owner, write=True)) if write
                      else original_client(owner))
        invoke()
    assert commands == [target]


@pytest.mark.parametrize('operation', ['create', 'reindex', 'media_create', 'media_replace'])
@pytest.mark.parametrize('failure_type', [OSError, KeyboardInterrupt, asyncio.CancelledError])
def test_directory_sync_failure_cleans_candidates_before_storage_entry(
        isolated_knowledge, tmp_path, monkeypatch, operation, failure_type):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, operation)
    before, entries = file_snapshot(store), []
    failure = failure_type('injected directory sync failure')
    original_client = KnowledgeRepository._client

    def fail(_directory):
        raise failure

    def client(owner, write=False):
        if write:
            entries.append(True)
        return original_client(owner, write=write)

    with monkeypatch.context() as patch:
        patch.setattr(file_durability, '_sync_directory', fail)
        patch.setattr(KnowledgeRepository, '_client', client)
        with pytest.raises(failure_type) as caught:
            invoke()
    assert caught.value is failure and not entries
    assert file_snapshot(store) == before


def test_directory_sync_failure_closes_descriptor_and_preserves_original(tmp_path, monkeypatch):
    original_close = os.close
    failure = OSError('directory flush failed')
    descriptors = []

    def fail(descriptor):
        assert stat.S_ISDIR(os.fstat(descriptor).st_mode)
        descriptors.append(descriptor)
        raise failure

    def close(descriptor):
        original_close(descriptor)
        raise KeyboardInterrupt('close also failed')

    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', fail)
        patch.setattr(os, 'close', close)
        with pytest.raises(OSError) as caught:
            file_durability._sync_directory(tmp_path)
    assert caught.value is failure and len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert 'KeyboardInterrupt' in failure.__notes__[0]


def test_preparation_does_not_create_a_missing_or_escape_an_anchor(tmp_path):
    missing = tmp_path / 'missing'
    with pytest.raises(FileNotFoundError):
        file_durability.prepare_directory(missing / 'owner', anchor=missing)
    assert not missing.exists()
    with pytest.raises(ValueError):
        file_durability.prepare_directory(tmp_path / '..' / 'escape', anchor=tmp_path)
