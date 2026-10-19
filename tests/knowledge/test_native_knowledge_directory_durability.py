"""Real native authority: directory barriers, faults, and batch-sized work."""
import asyncio
import os

import pytest

from lib.knowledge import file_durability
from tests.knowledge.test_knowledge_directory_durability import (
    test_directory_entries_are_synced_before_metadata_entry as check_order,
    test_directory_sync_failure_cleans_candidates_before_storage_entry as check_failure,
)
from tests.knowledge.test_native_knowledge_publication import native_publication_store as native_publication_store
from tests.knowledge.test_knowledge_publication import ASSET, RAW, prepare_operation

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    not hasattr(os, 'O_DIRECTORY'), reason='POSIX directory fsync required')]


@pytest.mark.parametrize('operation', ['create', 'reindex', 'media_create', 'media_replace'])
def test_native_metadata_follows_directory_barriers(
        native_publication_store, tmp_path, monkeypatch, operation):
    check_order(native_publication_store, tmp_path, monkeypatch, operation)


@pytest.mark.parametrize('operation', ['create', 'reindex', 'media_create', 'media_replace'])
@pytest.mark.parametrize('failure_type', [OSError, KeyboardInterrupt, asyncio.CancelledError])
def test_native_storage_is_not_entered_after_directory_sync_failure(
        native_publication_store, tmp_path, monkeypatch, operation, failure_type):
    check_failure(native_publication_store, tmp_path, monkeypatch, operation, failure_type)


def test_visual_batch_syncs_each_shared_directory_once(
        native_publication_store, tmp_path, monkeypatch):
    from lib.knowledge.repository import KnowledgeRepository
    from lib.storage import start_storage, stop_storage

    store = native_publication_store
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, 'create')
    original_extract = store.extract
    original_sync = file_durability._sync_directory
    synced = []

    def extract(*args, **kwargs):
        parsed = original_extract(*args, **kwargs)
        parsed['assets'] = [dict(parsed['assets'][0], caption=f'Visual {i}') for i in range(64)]
        return parsed

    def sync(directory):
        original_sync(directory)
        synced.append(directory)

    monkeypatch.setattr(store, 'extract', extract)
    monkeypatch.setattr(file_durability, '_sync_directory', sync)
    result = invoke()
    assert len(synced) == len(set(synced)) == 5
    stop_storage()
    start_storage()
    row = KnowledgeRepository(1).document(result['id'])
    assert (store._source_root(1) / row['stored_name']).read_bytes() == RAW
    assert len(row['assets']) == 64
    for asset in row['assets']:
        assert (store._asset_root(1) / asset['stored_name']).read_bytes() == ASSET
