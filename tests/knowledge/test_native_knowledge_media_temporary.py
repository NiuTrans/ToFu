"""Real native restart preserves streamed-media temporary publication outcomes."""
import asyncio
import hashlib

import pytest

from tests.knowledge.test_knowledge_local_publication import files
from tests.knowledge.test_knowledge_media_temporary import (
    test_committed_media_does_not_remove_an_already_transferred_temporary as check_create,
    test_duplicate_media_still_removes_its_untransferred_temporary as check_duplicate,
    test_media_metadata_failure_is_not_masked_by_consumed_temporary_cleanup as check_metadata,
    test_unknown_media_commit_retains_primary_and_original_source as check_unknown,
)
from tests.knowledge.test_knowledge_publication import RAW, assert_published_files
from tests.knowledge.test_native_knowledge_publication import native_publication_store

pytestmark = pytest.mark.unit


def assert_restarted_outcome(store, *, published):
    from lib.knowledge.repository import KnowledgeRepository
    from lib.storage import start_storage, stop_storage

    repository = KnowledgeRepository(1)
    digest = hashlib.sha256(RAW).hexdigest()
    row = repository.document_by_digest(digest)
    assert (row is not None) is published
    if published:
        assert_published_files(store, 'media_create')
    before = files(store)
    stop_storage()
    start_storage()
    assert repository.document_by_digest(digest) == row
    assert files(store) == before
    if published:
        assert_published_files(store, 'media_create')


@pytest.mark.parametrize('check', [check_create, check_duplicate], ids=['create', 'duplicate'])
def test_native_media_success_releases_only_its_own_temporary(
        native_publication_store, tmp_path, monkeypatch, check):
    check(native_publication_store, tmp_path, monkeypatch)
    assert_restarted_outcome(native_publication_store, published=True)


@pytest.mark.parametrize('kind', [TypeError, MemoryError, asyncio.CancelledError])
def test_native_media_local_metadata_failure_has_no_publication(
        native_publication_store, tmp_path, monkeypatch, kind):
    check_metadata(native_publication_store, tmp_path, monkeypatch, kind)
    assert_restarted_outcome(native_publication_store, published=False)


@pytest.mark.parametrize('kind', ['lost_reply', 'cancel'])
def test_native_media_unknown_commit_keeps_source_and_primary_failure(
        native_publication_store, tmp_path, monkeypatch, kind):
    check_unknown(native_publication_store, tmp_path, monkeypatch, kind)
    assert_restarted_outcome(native_publication_store, published=True)
