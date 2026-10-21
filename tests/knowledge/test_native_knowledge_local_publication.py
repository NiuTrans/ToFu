"""Local metadata failure leaves real native authority and old files unchanged."""
import hashlib

import pytest

from tests.knowledge.test_knowledge_local_publication import (
    files,
    test_metadata_failure_rolls_back_only_unpublished_candidates as check_failure,
)
from tests.knowledge.test_knowledge_publication import RAW
from tests.knowledge.test_native_knowledge_publication import native_publication_store

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('operation', ['create', 'reindex', 'media_create', 'media_replace'])
@pytest.mark.parametrize('kind', ['serialization', 'allocation', 'interrupt', 'cancel'])
def test_local_failure_preserves_native_authority_through_restart(
        native_publication_store, tmp_path, monkeypatch, operation, kind):
    from lib.knowledge.repository import KnowledgeRepository
    from lib.storage import start_storage, stop_storage

    store = native_publication_store
    check_failure(store, tmp_path, monkeypatch, operation, kind)
    repository = KnowledgeRepository(1)
    digest = hashlib.sha256(RAW).hexdigest()
    row = repository.document_by_digest(digest)
    assert (row is not None) is (operation in ('reindex', 'media_replace'))
    document = repository.document(row['id']) if row else None
    before = files(store)
    stop_storage()
    start_storage()
    assert repository.document_by_digest(digest) == row
    assert (repository.document(row['id']) if row else None) == document
    assert files(store) == before
