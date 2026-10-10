"""Research manuscripts reuse the saved-document authority and reading CAS."""
import asyncio
import uuid

import pytest

from lib.paper.builds import document_repository, saved_documents
from lib.paper.builds.document_repository import PaperDocumentRepository
from lib.research.persistence import research_direction_hash
from lib.storage.errors import StorageError
from tests.paper import test_paper_build_retention as support
from tests.paper.test_paper_build_cache import _build
from tests.support.envelope import v1_get_json

pytestmark = pytest.mark.unit
storage = support.storage
app = support.app
scheduler = support.scheduler
DIRECTION = 'A retained Research manuscript'


def publish(storage, tmp_path, *, revision=0, document='d' * 64):
    build = _build(tmp_path, document=document)
    receipt = build.receipt
    reference = {key: receipt[key] for key in ('document_id', 'build_id')}
    reference.update(source_digest='c' * 64, manifest_digest=receipt['source_digest'],
                     pdf_sha256=receipt['artifacts'][0]['sha256'])
    with support._repository(storage).prepare(build) as proof:
        storage.client.command('research.workspace.put', {'user_id': 7,
            'paper_hash': research_direction_hash(DIRECTION), 'lang': 'en',
            'expected_revision': revision, 'updated_at': 1, 'artifact_proof': proof,
            'workspace': {'compilation': {'mode': 'paper_build', 'status': 'passing', 'build': reference}}},
            uuid.uuid4().hex)
    return build


def reading(build):
    return {'build_id': build.receipt['build_id'], 'page': 15, 'fraction': .35,
            'horizontal': .5, 'scale': 1.25, 'fit': False, 'reading_mode': True}


def repository(storage, owner=7):
    return PaperDocumentRepository(owner, client_factory=lambda **_: storage.client)


def source(build):
    return {'source': 'research', 'paper_hash': research_direction_hash(DIRECTION),
            'lang': 'en', 'document_id': build.receipt['document_id']}


def test_old_manuscript_adopts_its_identity_and_reading_survives_restart(storage, tmp_path, monkeypatch):
    build = publish(storage, tmp_path)
    papers = repository(storage)
    with pytest.raises(StorageError, match='unavailable'):
        papers.get(build.receipt['document_id'])
    record = papers.register(source(build))
    assert record['document_id'] == build.receipt['document_id']
    assert record['source']['label'] == 'main.tex'
    assert papers.register({**source(build), 'label': 'untrusted label'}) == record
    current = papers.save_reading(record['document_id'], reading(build), 0, writer_id='a' * 32, sequence=2)
    assert papers.save_reading(record['document_id'], {**reading(build), 'page': 1}, 0,
                              writer_id='a' * 32, sequence=1) == current
    with pytest.raises(StorageError) as conflict:
        papers.save_reading(record['document_id'], reading(build), 0, writer_id='b' * 32)
    assert conflict.value.code == 'database_conflict'
    storage.stop(); storage.start()
    monkeypatch.setattr(document_repository, '_storage', lambda **_: storage.client)
    request = {'direction': DIRECTION, 'lang': 'en', 'document_id': record['document_id']}
    restored = saved_documents.open_research_reading(request, owner_user_id=7)
    assert restored == {'document_id': record['document_id'], 'reading': reading(build), 'reading_revision': 1}
    # A newly published build keeps the same reading record and logical ID.
    new = publish(storage, tmp_path, revision=1)
    again = papers.register(source(new))
    assert again['reading'] == reading(build) and again['reading_revision'] == 1
    assert papers.get(record['document_id'])['receipt']['build_id'] == new.receipt['build_id']


def test_registration_requires_owned_workspace_and_publication_authority_stays_separate(storage, tmp_path):
    build = publish(storage, tmp_path)
    papers = repository(storage)
    for owner, supplied, code in [
        (8, source(build), 'database_not_found'),
        (7, {**source(build), 'lang': 'zh'}, 'database_not_found'),
        (7, {**source(build), 'document_id': 'e' * 64}, 'database_conflict'),
    ]:
        with pytest.raises(StorageError) as rejected:
            repository(storage, owner).register(supplied)
        assert rejected.value.code == code
    record = papers.register(source(build))
    for operation in [lambda: papers.request(record['document_id'], 'chat', 'attempt'),
                      lambda: papers.finish(record['document_id'], 1, build.receipt, None),
                      lambda: papers.abandon(record['document_id'], 1)]:
        with pytest.raises(StorageError) as rejected:
            operation()
        assert rejected.value.code == 'database_conflict'
    assert papers.current('chat') is None
    assert papers.get(record['document_id'])['receipt'] == build.receipt


def test_http_research_reading_requires_auth_scope_owner_and_exact_document(app, storage, tmp_path, monkeypatch):
    build = publish(storage, tmp_path)
    monkeypatch.setattr(document_repository, '_storage', lambda **_: storage.client)
    body = {'direction': DIRECTION, 'lang': 'en', 'document_id': build.receipt['document_id']}
    endpoint = '/api/v4/paper/documents/research/open'

    async def run():
        client = app.test_client()
        assert (await client.post(endpoint, json=body)).status_code == 401
        assert (await client.post(endpoint, json=body, headers={'X-Test-Owner': '7', 'X-Test-Scopes': 'read'})).status_code == 403
        assert (await client.post(endpoint, json=body, headers={'X-Test-Owner': '8'})).status_code == 404
        assert (await client.post(endpoint, json={**body, 'document_id': 'e' * 64}, headers={'X-Test-Owner': '7'})).status_code == 409
        response = await client.post(endpoint, json=body, headers={'X-Test-Owner': '7'})
        assert response.status_code == 200
        assert (await v1_get_json(response))['document'] == {
            'document_id': build.receipt['document_id'], 'reading': None, 'reading_revision': 0}
        saved = await client.post(f"/api/v4/paper/documents/{build.receipt['document_id']}/reading",
            json={'expected_revision': 0, 'writer_id': 'a' * 32, 'sequence': 1, 'state': reading(build)},
            headers={'X-Test-Owner': '7'})
        assert saved.status_code == 200
        response = await client.post(endpoint, json=body, headers={'X-Test-Owner': '7'})
        assert (await v1_get_json(response))['document']['reading'] == reading(build)
    asyncio.run(run())
