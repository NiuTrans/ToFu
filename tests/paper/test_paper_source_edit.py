"""Paper source edit route: journaled writes, optimistic concurrency, hunks."""
from __future__ import annotations

import asyncio
import hashlib

import pytest
from quart import Quart, g, request

from lib.api_keys import AuthContext

pytestmark = pytest.mark.unit

OWNER = {'X-Test-Owner': '7'}
MAIN = '\\documentclass{article}\n\\begin{document}\nHello world.\n\\end{document}\n'


@pytest.fixture
def paper_env(tmp_path, monkeypatch):
    """Real tmp paper tree + isolated document storage + a journaled root."""
    from tests.support.paper_storage import isolated_paper_storage
    from lib.project_mod import config as pm_config
    root = tmp_path / 'paper'
    root.mkdir()
    (root / 'main.tex').write_text(MAIN)
    (root / 'refs.bib').write_text('@article{doe, title={T}}\n')
    (root / 'figure.pdf').write_bytes(b'%PDF-1.4 fixture\n')
    with isolated_paper_storage(tmp_path, monkeypatch):
        from lib.paper.builds.document_repository import PaperDocumentRepository
        row = PaperDocumentRepository(7).register({
            'source': 'local', 'path': 'paper/main.tex',
            'project_root': str(tmp_path), 'main_file': 'paper/main.tex',
            'label': 'paper/main.tex'})
        with pm_config._lock:
            pm_config._roots.clear()
            pm_config._roots['paper'] = pm_config._make_root_state(str(tmp_path))
            pm_config._state['path'] = str(tmp_path)
        try:
            yield {'root': root, 'document_id': row['document_id']}
        finally:
            with pm_config._lock:
                pm_config._roots.clear()
                pm_config._state['path'] = ''


@pytest.fixture
def app(paper_env):
    from routes.api_v1 import paper_builds
    application = Quart(__name__, static_folder=None)
    application.config['TESTING'] = True

    @application.before_request
    def principal_fixture():
        owner = request.headers.get('X-Test-Owner')
        if owner:
            g.auth_ctx = AuthContext(key_id='fixture', owner_user_id=int(owner),
                                     scopes=frozenset(request.headers.get(
                                         'X-Test-Scopes', 'tasks').split(',')))

    application.register_blueprint(paper_builds.api_v1_paper_builds_bp)
    return application


def _post(client, url, payload, headers=OWNER):
    return client.post(url, json=payload, headers=headers)


def test_full_write_roundtrip_changes_sha256_and_journals(app, paper_env):
    payload = {'document_id': paper_env['document_id'], 'path': 'main.tex',
               'content': MAIN.replace('Hello world.', 'Hello tofu.')}
    expected = hashlib.sha256(payload['content'].encode()).hexdigest()

    async def run():
        client = app.test_client()
        assert (await _post(client, '/api/v1/paper/documents/source/edit', payload,
                            headers={})).status_code == 401
        response = await _post(client, '/api/v1/paper/documents/source/edit', payload)
        assert response.status_code == 200
        body = await response.get_json()
        assert body['path'] == 'main.tex'
        assert body['size_bytes'] == len(payload['content'].encode())
        assert body['sha256'] == expected
        assert 'results' not in body
    asyncio.run(run())
    assert (paper_env['root'] / 'main.tex').read_text() == payload['content']
    from lib.project_mod.modifications import get_modifications
    journal = get_modifications(str(paper_env['root']))
    assert any(row['path'] == 'main.tex' for row in journal), journal


def test_base_sha256_conflict_is_typed(app, paper_env):
    current = hashlib.sha256(MAIN.encode()).hexdigest()

    async def run():
        client = app.test_client()
        stale = await _post(client, '/api/v1/paper/documents/source/edit', {
            'document_id': paper_env['document_id'], 'path': 'main.tex',
            'content': 'stale write', 'base_sha256': '0' * 64})
        assert stale.status_code == 409
        assert 'changed' in (await stale.get_json())['error']
        fresh = await _post(client, '/api/v1/paper/documents/source/edit', {
            'document_id': paper_env['document_id'], 'path': 'main.tex',
            'content': 'fresh write', 'base_sha256': current})
        assert fresh.status_code == 200
    asyncio.run(run())
    assert (paper_env['root'] / 'main.tex').read_text() == 'fresh write'


def test_edits_mode_reports_per_hunk_results(app, paper_env):
    payload = {'document_id': paper_env['document_id'], 'path': 'main.tex',
               'edits': [{'search': 'Hello world.', 'replace': 'Hello tofu.',
                          'description': 'greeting'},
                         {'search': 'no such anchor anywhere', 'replace': 'x'}]}

    async def run():
        client = app.test_client()
        response = await _post(client, '/api/v1/paper/documents/source/edit', payload)
        assert response.status_code == 200
        body = await response.get_json()
        assert body['results'][0] == {'index': 0, 'ok': True}
        assert body['results'][1]['index'] == 1
        assert body['results'][1]['ok'] is False
        assert 'not found' in body['results'][1]['error']
    asyncio.run(run())
    text = (paper_env['root'] / 'main.tex').read_text()
    assert 'Hello tofu.' in text and 'no such anchor' not in text


def test_exactly_one_mode_is_enforced(app, paper_env):
    async def run():
        client = app.test_client()
        both = await _post(client, '/api/v1/paper/documents/source/edit', {
            'document_id': paper_env['document_id'], 'path': 'main.tex',
            'content': 'x', 'edits': [{'search': 'a', 'replace': 'b'}]})
        assert both.status_code == 400
        neither = await _post(client, '/api/v1/paper/documents/source/edit', {
            'document_id': paper_env['document_id'], 'path': 'main.tex'})
        assert neither.status_code == 400
    asyncio.run(run())
    assert (paper_env['root'] / 'main.tex').read_text() == MAIN


def test_path_traversal_and_binary_suffix_are_refused(app, paper_env):
    async def run():
        client = app.test_client()
        traversal = await _post(client, '/api/v1/paper/documents/source/edit', {
            'document_id': paper_env['document_id'], 'path': '../escape.tex',
            'content': 'x'})
        assert traversal.status_code == 400
        assert 'relative path' in (await traversal.get_json())['error']
        binary = await _post(client, '/api/v1/paper/documents/source/edit', {
            'document_id': paper_env['document_id'], 'path': 'figure.pdf',
            'content': 'x'})
        assert binary.status_code == 400
        assert 'Binary' in (await binary.get_json())['error']
    asyncio.run(run())


def test_foreign_owner_and_scope_guards(app, paper_env):
    payload = {'document_id': paper_env['document_id'], 'path': 'main.tex',
               'content': 'x'}

    async def run():
        client = app.test_client()
        assert (await _post(client, '/api/v1/paper/documents/source/edit', payload,
                            headers={'X-Test-Owner': '7', 'X-Test-Scopes': 'read'})).status_code == 403
        assert (await _post(client, '/api/v1/paper/documents/source/edit', payload,
                            headers={'X-Test-Owner': '8'})).status_code == 404
    asyncio.run(run())
    assert (paper_env['root'] / 'main.tex').read_text() == MAIN
