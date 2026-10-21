"""Visual spool teardown failure retains prior native metadata through restart."""
import hashlib

import pytest

from tests.knowledge.test_knowledge_file_ownership import (
    test_visual_close_failure_rolls_back_candidates_and_is_not_repeated as check_close,
)
from tests.knowledge.test_knowledge_local_publication import files
from tests.knowledge.test_knowledge_publication import RAW
from tests.knowledge.test_native_knowledge_publication import native_publication_store

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('operation', ['create', 'reindex'])
@pytest.mark.parametrize('kind', [OSError, KeyboardInterrupt])
def test_native_spool_close_failure_never_publishes_candidates(
        native_publication_store, tmp_path, monkeypatch, operation, kind):
    from lib.knowledge.repository import KnowledgeRepository
    from lib.storage import start_storage, stop_storage

    store = native_publication_store
    check_close(store, tmp_path, monkeypatch, operation, kind)
    repository = KnowledgeRepository(1)
    digest = hashlib.sha256(RAW).hexdigest()
    row = repository.document_by_digest(digest)
    assert (row is not None) is (operation == 'reindex')
    before = files(store)
    stop_storage()
    start_storage()
    assert repository.document_by_digest(digest) == row
    assert files(store) == before
