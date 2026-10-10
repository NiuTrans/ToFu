"""Paper edit proposals: bounded store lifecycle, journaled accept, routes."""
from __future__ import annotations

import asyncio

import pytest
from quart import Quart, g, request

from lib.api_keys import AuthContext
from lib.paper.builds.errors import BuildError

pytestmark = pytest.mark.unit

OWNER = {'X-Test-Owner': '7'}
MAIN = '\\documentclass{article}\n\\begin{document}\nHello world.\n\\end{document}\n'


@pytest.fixture
def paper_env(tmp_path, monkeypatch):
    """Real tmp paper tree + isolated document storage + journaled roots."""
    from tests.support.paper_storage import isolated_paper_storage
    from lib.project_mod import config as pm_config
    root = tmp_path / 'paper'
    root.mkdir()
    (root / 'main.tex').write_text(MAIN)
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
        pm_config._conv_roots.clear()
        pm_config.set_conv_roots('conv-test', str(tmp_path))
        try:
            yield {'root': root, 'document_id': row['document_id'],
                   'base': str(root)}
        finally:
            with pm_config._lock:
                pm_config._roots.clear()
                pm_config._state['path'] = ''
            pm_config._conv_roots.clear()


def _create(paper_env, edits, conv_id='conv-test', owner=7):
    from lib.paper.builds import proposals
    return proposals.create_proposal(owner_user_id=owner,
                                     document_id=paper_env['document_id'],
                                     conv_id=conv_id, edits=edits)


def test_create_prevalidates_without_writing(paper_env):
    proposal = _create(paper_env, [
        {'path': 'main.tex', 'search': 'Hello world.', 'replace': 'Hello tofu.',
         'description': 'greeting'},
        {'path': 'main.tex', 'search': 'missing anchor', 'replace': 'x'}])
    assert proposal['status'] == 'pending'
    assert proposal['conv_id'] == 'conv-test'
    assert len(proposal['proposal_id']) == 32
    first, second = proposal['edits']
    assert first['status'] == 'pending'
    assert second['status'] == 'failed' and second['error']
    assert (paper_env['root'] / 'main.tex').read_text() == MAIN


def test_create_refuses_traversal(paper_env):
    from lib.paper.builds import proposals
    with pytest.raises(BuildError) as caught:
        _create(paper_env, [{'path': '../escape.tex', 'search': 'a', 'replace': 'b'}])
    assert caught.value.code == 'source_path'


def test_get_roundtrip_and_foreign_owner(paper_env):
    from lib.paper.builds import proposals
    proposal = _create(paper_env, [
        {'path': 'main.tex', 'search': 'Hello world.', 'replace': 'Hi.'}])
    fetched = proposals.get_proposal(proposal['proposal_id'], owner_user_id=7)
    assert fetched['proposal_id'] == proposal['proposal_id']
    assert proposals.get_proposal(proposal['proposal_id'], owner_user_id=8) is None
    assert proposals.get_proposal('f' * 32, owner_user_id=7) is None


def test_expired_proposal_is_not_found(paper_env, monkeypatch):
    from lib.paper.builds import proposals
    monkeypatch.setattr(proposals, '_TTL_SECONDS', 0)
    proposal = _create(paper_env, [
        {'path': 'main.tex', 'search': 'Hello world.', 'replace': 'Hi.'}])
    assert proposals.get_proposal(proposal['proposal_id'], owner_user_id=7) is None
    with pytest.raises(BuildError) as caught:
        proposals.decide_proposal(proposal['proposal_id'], None, 'accept', owner_user_id=7)
    assert caught.value.code == 'proposal_not_found'
    with pytest.raises(BuildError):
        proposals.decide_proposal(proposal['proposal_id'], None, 'accept', owner_user_id=8)


def test_accept_applies_selected_hunks_and_journals(paper_env):
    from lib.paper.builds import proposals
    proposal = _create(paper_env, [
        {'path': 'main.tex', 'search': 'Hello world.', 'replace': 'Hello tofu.'},
        {'path': 'main.tex', 'search': '\\end{document}', 'replace': '% tail\n\\end{document}'}])
    outcome = proposals.decide_proposal(proposal['proposal_id'], [0], 'accept',
                                        owner_user_id=7)
    assert outcome['results'] == [{'index': 0, 'ok': True}]
    updated = outcome['proposal']
    assert updated['status'] == 'partial'  # hunk 1 is still pending
    assert updated['edits'][0]['status'] == 'applied'
    assert updated['edits'][1]['status'] == 'pending'
    text = (paper_env['root'] / 'main.tex').read_text()
    assert 'Hello tofu.' in text and '% tail' not in text
    from lib.project_mod.modifications import get_modifications
    journal = get_modifications(paper_env['base'], conv_id='conv-test')
    assert any(row['path'] == 'main.tex' and row.get('convId') == 'conv-test'
               for row in journal), journal
    final = proposals.decide_proposal(proposal['proposal_id'], None, 'accept',
                                      owner_user_id=7)['proposal']
    assert final['status'] == 'applied'
    assert '% tail' in (paper_env['root'] / 'main.tex').read_text()


def test_accept_revalidates_drifted_files(paper_env):
    from lib.paper.builds import proposals
    proposal = _create(paper_env, [
        {'path': 'main.tex', 'search': 'Hello world.', 'replace': 'Hi.'}])
    (paper_env['root'] / 'main.tex').write_text(MAIN.replace('Hello world.', 'Changed.'))
    outcome = proposals.decide_proposal(proposal['proposal_id'], None, 'accept',
                                        owner_user_id=7)
    assert outcome['results'][0]['ok'] is False
    assert outcome['proposal']['edits'][0]['status'] == 'failed'
    assert 'Hello world.' not in (paper_env['root'] / 'main.tex').read_text()


def test_reject_marks_without_writing(paper_env):
    from lib.paper.builds import proposals
    proposal = _create(paper_env, [
        {'path': 'main.tex', 'search': 'Hello world.', 'replace': 'Hi.'},
        {'path': 'main.tex', 'search': 'missing anchor', 'replace': 'x'}])
    outcome = proposals.decide_proposal(proposal['proposal_id'], None, 'reject',
                                        owner_user_id=7)
    assert outcome['results'] == [{'index': 0, 'ok': True}]
    assert outcome['proposal']['status'] == 'rejected'
    assert outcome['proposal']['edits'][0]['status'] == 'rejected'
    assert outcome['proposal']['edits'][1]['status'] == 'failed'
    assert (paper_env['root'] / 'main.tex').read_text() == MAIN


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


def test_proposal_routes_get_accept_reject(app, paper_env):
    accepted = _create(paper_env, [
        {'path': 'main.tex', 'search': 'Hello world.', 'replace': 'Hello tofu.'}])
    rejected = _create(paper_env, [
        {'path': 'main.tex', 'search': '\\end{document}', 'replace': '% no\n\\end{document}'}])

    async def run():
        client = app.test_client()
        url = '/api/v1/paper/proposals/get'
        assert (await client.post(url, json={'proposal_id': accepted['proposal_id']})).status_code == 401
        response = await client.post(url, json={'proposal_id': accepted['proposal_id']},
                                     headers=OWNER)
        assert response.status_code == 200
        assert (await response.get_json())['proposal_id'] == accepted['proposal_id']
        missing = await client.post(url, json={'proposal_id': 'f' * 32}, headers=OWNER)
        assert missing.status_code == 404
        foreign = await client.post(url, json={'proposal_id': accepted['proposal_id']},
                                    headers={'X-Test-Owner': '8'})
        assert foreign.status_code == 404
        accepted_response = await client.post('/api/v1/paper/proposals/accept',
                                              json={'proposal_id': accepted['proposal_id']},
                                              headers=OWNER)
        assert accepted_response.status_code == 200
        body = await accepted_response.get_json()
        assert body['status'] == 'applied'
        assert body['results'] == [{'index': 0, 'ok': True}]
        rejected_response = await client.post('/api/v1/paper/proposals/reject',
                                              json={'proposal_id': rejected['proposal_id'],
                                                    'indexes': [0]}, headers=OWNER)
        assert rejected_response.status_code == 200
        assert (await rejected_response.get_json())['status'] == 'rejected'
        gone = await client.post(url, json={'proposal_id': 'e' * 32}, headers=OWNER)
        assert gone.status_code == 404
    asyncio.run(run())
    text = (paper_env['root'] / 'main.tex').read_text()
    assert 'Hello tofu.' in text and '% no' not in text
