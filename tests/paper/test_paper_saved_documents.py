"""Saved manuscript open/read contract with a real Sidecar and restart boundary."""
from __future__ import annotations

import asyncio

import pytest

from lib.paper.builds import document_repository, saved_documents, service
from lib.paper.builds.document_repository import PaperDocumentRepository
from tests.storage import test_storage_authority_sql_contract as storage_support
from tests.api import test_api_v1_paper_builds as api_support
from tests.paper.test_paper_build_retention import _repository
from tests.paper.test_paper_build_cache import _build
from tests.support.envelope import v1_get_json
from tests.paper.test_paper_document_repository import (
    test_identity_is_stable_without_source_access_and_is_owner_scoped,
    test_new_generation_and_first_completion_fence_old_publications,
    test_unverified_or_failed_attempt_cannot_displace_verified_pdf,
    test_abandoned_generation_cannot_publish_and_same_request_can_restart,
    test_reading_final_save_can_overtake_scroll_save_but_not_another_window,
)

storage = storage_support.storage
scheduler = api_support.scheduler
app = api_support.app
pytestmark = pytest.mark.unit


def retain(storage, tmp_path):
    papers = PaperDocumentRepository(7, client_factory=lambda **_: storage.client)
    source = {'source': 'local', 'project_root': str(tmp_path), 'path': 'main.tex',
              'main_file': 'main.tex', 'label': 'main.tex'}
    identity = papers.register(source)['document_id']
    generation = papers.request(identity, 'conversation', 'first')['generation']
    build = _build(tmp_path, document=identity)
    with _repository(storage).prepare(build) as proof:
        papers.finish(identity, generation, build.receipt, proof)
    return papers, source, build


def test_open_after_restart_and_source_loss_preserves_pdf_and_reading(storage, tmp_path, monkeypatch):
    papers, source, build = retain(storage, tmp_path)
    reading = {'build_id': build.receipt['build_id'], 'page': 19, 'fraction': .4,
               'horizontal': .5, 'scale': 1.25, 'fit': False, 'reading_mode': True}
    papers.save_reading(build.receipt['document_id'], reading, 0)
    storage.stop()
    storage.start()
    monkeypatch.setattr(document_repository, '_storage', lambda **_: storage.client)
    monkeypatch.setattr(saved_documents, 'owned_settings', lambda *_: {'paperDocument': source})
    monkeypatch.setattr(service, 'compile_snapshot', lambda *_a, **_k: pytest.fail('open must not compile'))
    result = saved_documents.open_document({'conv_id': 'conversation'}, owner_user_id=7)
    assert result['can_compile'] is False
    assert result['build']['receipt'] == build.receipt
    assert result['reading'] == reading and result['reading_revision'] == 1
    assert build.receipt['build_id'] in result['build']['pdf_url']


def test_http_saved_document_owner_auth_nullable_open_and_reading_conflict(app, storage, tmp_path, monkeypatch):
    _, source, build = retain(storage, tmp_path)
    monkeypatch.setattr(document_repository, '_storage', lambda **_: storage.client)
    # Conversation access still resolves before any document lookup.
    def settings(conv_id, owner):
        if conv_id != 'conversation':
            from lib.paper.builds.errors import BuildError
            raise BuildError('document_not_found', 'Unavailable conversation')
        return {'paperDocument': source}
    monkeypatch.setattr(saved_documents, 'owned_settings', settings)
    reading = {'build_id': build.receipt['build_id'], 'page': 7, 'fraction': .4,
               'horizontal': .5, 'scale': 1.25, 'fit': False, 'reading_mode': True}
    endpoint = f"/api/v4/paper/documents/{build.receipt['document_id']}/reading"

    async def run():
        client = app.test_client()
        body = {'conv_id': 'conversation'}
        assert (await client.post('/api/v4/paper/documents/open', json=body)).status_code == 401
        assert (await client.post('/api/v4/paper/documents/open', json=body,
                headers={'X-Test-Owner': '7', 'X-Test-Scopes': 'read'})).status_code == 403
        result = await client.post('/api/v4/paper/documents/open', json=body, headers={'X-Test-Owner': '7'})
        assert result.status_code == 200
        assert (await v1_get_json(result))['document']['build']['receipt'] == build.receipt
        other = await client.post('/api/v4/paper/documents/open', json=body, headers={'X-Test-Owner': '8'})
        assert (await v1_get_json(other))['document'] is None
        request = {'state': reading, 'expected_revision': 0, 'writer_id': 'f' * 32, 'sequence': 1}
        assert (await client.post(endpoint, json=request, headers={'X-Test-Owner': '8'})).status_code == 404
        assert (await client.post(endpoint, json=request, headers={'X-Test-Owner': '7'})).status_code == 200
        assert (await client.post(endpoint, json=request, headers={'X-Test-Owner': '7'})).status_code == 200
        stale = {**request, 'writer_id': 'e' * 32}
        assert (await client.post(endpoint, json=stale, headers={'X-Test-Owner': '7'})).status_code == 409
        denied = await client.post('/api/v4/paper/documents/open', json={'conv_id': 'unowned'},
                                   headers={'X-Test-Owner': '7'})
        assert denied.status_code == 404
    asyncio.run(run())
