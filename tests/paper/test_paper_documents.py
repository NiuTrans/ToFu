"""Synthetic document discovery and owner-scoped settings persistence boundaries."""
import copy

import pytest

from lib.paper.builds import documents
from lib.paper.builds.errors import BuildError

pytestmark = pytest.mark.unit


@pytest.fixture
def owned(tmp_path, monkeypatch):
    from lib.conversations import repository, settings_store
    state = {'projectPath': str(tmp_path), 'unrelated': 'preserve'}
    monkeypatch.setattr(repository, 'get_conversation', lambda conv_id, *, user_id, **kw:
                        {'settings': copy.deepcopy(state)} if (conv_id, user_id) == ('c', 7) else None)
    # Exercise the real settings_store CAS against an owner-addressed storage seam.
    class Client:
        def query(self, operation, payload):
            assert payload['user_id'] == 7
            return {'metadata': {'settings': copy.deepcopy(state)}}

        def command(self, operation, payload, command_id):
            assert payload['expected_settings'] == state
            assert payload['user_id'] == 7
            state.clear()
            state.update(copy.deepcopy(payload['updates']))
            return {'applied': True}
    import lib.storage
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda **kw: Client())
    monkeypatch.setattr(settings_store, '_publish_after_settings_write', lambda *a: None)
    monkeypatch.setattr('lib.paper.builds.service.compile_snapshot',
                        lambda *a, **kw: pytest.fail('discovery must not compile'))
    from tests.support.paper_storage import isolated_paper_storage
    with isolated_paper_storage(tmp_path, monkeypatch):
        yield tmp_path, state


def tex(root, name):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\\documentclass{article}\n\\begin{document}Synthetic\\end{document}')
    return path


def discover(**kw):
    return documents.discover_documents({'conv_id': 'c', **kw}, owner_user_id=7)


def test_nested_multiple_selection_and_reopen_persistence(owned):
    root, state = owned
    tex(root, 'CARD_project/main.tex')
    result = discover()
    assert result['selected']['path'] == 'CARD_project/main.tex'
    assert 'paperDocument' not in state
    tex(root, 'CARD_iclr2027/main.tex')
    assert discover()['selected'] is None
    with pytest.raises(BuildError, match='Choose one'):
        documents.select_document({'conv_id': 'c', 'path': '.', 'project_root': str(root)}, owner_user_id=7)
    selected = documents.select_document({'conv_id': 'c', 'path': 'CARD_iclr2027',
                                         'main_file': 'CARD_iclr2027/main.tex',
                                         'project_root': str(root)}, owner_user_id=7)['selected']
    assert state['unrelated'] == 'preserve'
    assert state['paperDocument'] == selected
    assert discover()['selected'] == selected
    (root / selected['path']).unlink()
    result = discover()
    assert result['selected'] is None
    assert 'selection_stale' in {d['code'] for d in result['diagnostics']}
    assert state['paperDocument'] == selected


def test_owner_and_root_isolation(owned, tmp_path):
    root, state = owned
    tex(root, 'main.tex')
    with pytest.raises(BuildError) as exc:
        documents.discover_documents({'conv_id': 'c'}, owner_user_id=8)
    assert exc.value.code == 'document_not_found'
    with pytest.raises(BuildError):
        discover(project_root=str(root.parent))
    assert discover(path='../')['documents'] == []
    with pytest.raises(BuildError):
        documents.select_document({'conv_id': 'c', 'path': str(root / 'main.tex'),
                                   'project_root': str(root)}, owner_user_id=7)


def test_symlinks_skips_and_remote_no_fallback(owned):
    root, state = owned
    tex(root, '.hidden/main.tex')
    tex(root, 'vendor/main.tex')
    tex(root, 'output/main.tex')
    assert discover()['documents'] == []
    tex(root, 'real/main.tex')
    (root / 'linked').symlink_to(root / 'real', target_is_directory=True)
    result = discover()
    assert result['selected'] is None
    assert result['diagnostics']
    assert discover(path='linked/main.tex')['documents'] == []
    state['projectPath'] = 'remote:device:paper'
    state['projectPaths'] = [str(root)]
    result = discover(project_root='remote:device:paper')
    assert result['documents'] == []
    assert result['diagnostics'][0]['code'] == 'desktop_unavailable'


@pytest.mark.parametrize('budget,value', [('discovery_entries', 1), ('discovery_bytes', 1),
                                         ('discovery_file_bytes', 1), ('discovery_candidates', 1),
                                         ('discovery_depth', 0), ('discovery_roots', 0)])
def test_scans_fail_explicitly_at_each_budget(owned, monkeypatch, budget, value):
    root, _ = owned
    tex(root, 'a/main.tex')
    tex(root, 'b/main.tex')
    original = documents.limit
    monkeypatch.setattr(documents, 'limit', lambda name: value if name == budget else original(name))
    if budget == 'discovery_roots':
        with pytest.raises(BuildError) as exc:
            discover()
        assert exc.value.code == 'discovery_limit'
    else:
        result = discover()
        assert result['diagnostics']
        assert result['selected'] is None


def test_missing_directory_scan_is_not_empty_success(owned):
    result = discover(path='missing')
    assert result['documents'] == []
    assert result['diagnostics'][0]['code'] == 'discovery_scan'


def test_document_http_auth_selection_and_reopen(owned):
    import asyncio
    from quart import Quart, g, request
    from lib.api_keys import AuthContext
    from routes.api_v1.paper_builds import api_v1_paper_builds_bp
    root, _ = owned
    tex(root, 'CARD_project/main.tex')
    app = Quart(__name__, static_folder=None)
    app.config['TESTING'] = True

    @app.before_request
    def principal():
        if request.headers.get('X-Owner'):
            g.auth_ctx = AuthContext(key_id='fixture', owner_user_id=int(request.headers['X-Owner']),
                                     scopes=frozenset(request.headers.get('X-Scope', 'tasks').split(',')))

    app.register_blueprint(api_v1_paper_builds_bp)

    async def run():
        client = app.test_client()
        discover_url = '/api/v1/paper/documents/discover'
        select_url = '/api/v1/paper/documents/select'
        for url in (discover_url, select_url):
            assert (await client.post(url, json={'conv_id': 'c'})).status_code == 401
            assert (await client.post(url, json={'conv_id': 'c'}, headers={
                'X-Owner': '7', 'X-Scope': 'read'})).status_code == 403
            assert (await client.post(url, json={'conv_id': 'c', 'path': '.',
                'project_root': str(root)} if url == select_url else {'conv_id': 'c'},
                headers={'X-Owner': '8'})).status_code == 404
        result = await client.post(discover_url, json={'conv_id': 'c'}, headers={'X-Owner': '7'})
        assert result.status_code == 200
        body = await result.get_json()
        assert body['selected']['path'] == 'CARD_project/main.tex'
        result = await client.post(select_url, json={'conv_id': 'c', 'path': 'CARD_project/main.tex',
                                  'project_root': str(root)}, headers={'X-Owner': '7'})
        assert result.status_code == 200
        saved = (await result.get_json())['selected']
        reopened = await app.test_client().post(discover_url, json={'conv_id': 'c'},
                                                headers={'X-Owner': '7'})
        assert (await reopened.get_json())['selected'] == saved
    asyncio.run(run())
