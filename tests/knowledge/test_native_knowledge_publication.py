"""Lost publication replies preserve files across actual native storage restart.

Run with configured native test storage and a source-matched TOFUDB_BIN. The
application fixture owns its disposable authority; mutations use normal ports.
"""
import pytest

from tests.knowledge.test_knowledge_publication import (
    assert_published_files, prepare_operation,
    test_postcommit_failure_preserves_original_and_visuals as check_failure,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def native_publication_store(request, tmp_path, monkeypatch):
    from runtime_guards import load_deployment_configuration

    if load_deployment_configuration().storage_backend != 'tofudb':
        pytest.skip('configured native test storage required')
    request.getfixturevalue('flask_app')
    from lib.knowledge import store
    from lib.knowledge.repository import KnowledgeRepository
    from lib.storage import storage_status

    assert storage_status()['ready'] and storage_status()['backend'] == 'tofudb'
    repository = KnowledgeRepository(1)
    # Distinct per-test roots and receipts; no live application data is opened.
    repository.clear_owner(command_id=f'publication.prepare:{tmp_path.name}')
    monkeypatch.setattr(store, '_SOURCE_ROOT_OVERRIDE', str(tmp_path / 'files'))
    monkeypatch.setattr(store, '_ASSET_ROOT_OVERRIDE', str(tmp_path / 'files'))
    try:
        yield store
    finally:
        repository.clear_owner(command_id=f'publication.cleanup:{tmp_path.name}')


@pytest.mark.parametrize('operation', ['create', 'reindex', 'media_create', 'media_replace'])
@pytest.mark.parametrize('kind', ['lost_reply', 'io_error', 'interrupt', 'cancel'])
def test_committed_files_survive_native_restart(
        native_publication_store, tmp_path, monkeypatch, operation, kind):
    from lib.knowledge.repository import KnowledgeRepository
    from lib.storage import start_storage, stop_storage

    store = native_publication_store
    check_failure(store, tmp_path, monkeypatch, operation, kind)
    row = assert_published_files(store, operation)
    content = KnowledgeRepository(1).document_content(row['id'], offset=0, limit=100)
    stop_storage()
    start_storage()
    assert assert_published_files(store, operation) == row
    assert KnowledgeRepository(1).document_content(row['id'], offset=0, limit=100) == content


def test_unconfirmed_replacements_retain_only_written_candidate_bytes(
        native_publication_store, tmp_path, monkeypatch):
    from lib.knowledge.repository import KnowledgeRepository
    from lib.storage.errors import StorageError
    from tests.knowledge.test_knowledge_publication import ASSET, RAW

    store = native_publication_store
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, 'media_replace')
    original_client = KnowledgeRepository._client

    class Refuse:
        def command(self, *args, **kwargs):
            raise StorageError('database_unavailable', 'unconfirmed transport outcome')

    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeRepository, '_client',
                      lambda owner, write=False: Refuse() if write else original_client(owner))
        for _ in range(3):
            with pytest.raises(StorageError):
                invoke()
    assets = list(store._asset_root(1).iterdir())
    sources = list(store._source_root(1).iterdir())
    assert len(assets) == 3 and sum(path.stat().st_size for path in assets) == 3 * len(ASSET)
    assert len(sources) == 1 and sources[0].read_bytes() == RAW
