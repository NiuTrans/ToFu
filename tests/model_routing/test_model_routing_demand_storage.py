"""Large owner authorities preserve CAS, migration originals and backend parity."""
import json

import pytest

from lib.model_routing import ModelRoutingError, OwnerBoundary, empty_document
from lib.model_routing.repository import InMemoryModelRoutingRepository, ModelRoutingRepository
from tests.research.test_research_workspace_evidence_guard import storage  # noqa: F401

pytestmark = pytest.mark.unit


def large_authority():
    document = empty_document()
    document['creators'] = [{'creator_id': f'creator-{i:05}', 'name': '完整'*128}
                            for i in range(11000)]
    assert len(json.dumps(document, ensure_ascii=False).encode()) > 8 * 1024 * 1024
    return document


def verify_authority(repository):
    owner = OwnerBoundary.create(1, 'demand')
    other = OwnerBoundary.create(1, 'other-tenant')
    document = large_authority()
    first = repository.compare_and_swap(owner, document, expected_revision=0)
    assert first.revision == 1 and first.document['creators'] == document['creators']
    assert repository.get(other).revision == 0
    with pytest.raises(ModelRoutingError) as conflict:
        repository.compare_and_swap(owner, document, expected_revision=0)
    assert conflict.value.kind == 'model_routing_revision_conflict'
    document['creators'][0]['name'] = '下一版'
    receipt = {'reason': 'owner-reviewed migration', 'complete_original': '证'*3000000}
    assert len(json.dumps(receipt, ensure_ascii=False).encode()) > 8 * 1024 * 1024
    second = repository.compare_and_swap(owner, document, expected_revision=1,
                                         migration_receipt=receipt)
    assert second.revision == 2 and second.document['creators'] == document['creators']
    archived = repository.migration_receipt(owner)
    assert archived['backup'] == first.document
    assert archived['receipt'] == receipt
    assert repository.get(other).revision == 0
    return owner, second


def test_in_memory_repository_preserves_complete_large_authority_and_migration():
    verify_authority(InMemoryModelRoutingRepository())


def test_selected_backend_preserves_complete_large_authority_and_migration(storage):  # noqa: F811
    supervisor, backend = storage
    repository = ModelRoutingRepository()
    repository._client = lambda **kwargs: supervisor.client
    owner, written = verify_authority(repository)
    supervisor.stop()
    supervisor.start()
    assert repository.get(owner).document == written.document
