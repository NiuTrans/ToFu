"""A connector's workflow skill arrives with the connector, never over the user's.

Installing Tencent Docs or Kingsoft Docs from the MCP catalog installs the
vendor-neutral ``online-spreadsheets`` guide into that owner's skills through
the validated installer. A later revision updates a copy Tofu installed and
nobody edited; an owner's own or edited same-id skill is kept. A guide
failure is reported, never allowed to fail the connector install.

Run:  pytest tests/skills/test_skill_companion.py -m unit
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.mcp import registry
from lib.skills.companion import COMPANION_SOURCE_REGISTRY, ensure_companion_skill
from tests.skills.test_skill_installation_contract import isolated as _isolated_fixture

pytestmark = pytest.mark.unit

OWNER = 7


@pytest.fixture(name='isolated')
def skill_workspace(tmp_path, monkeypatch):
    yield from _isolated_fixture.__wrapped__(tmp_path, monkeypatch)


def _package(root: Path, body: str) -> Path:
    path = root / 'online-spreadsheets'
    path.mkdir(parents=True, exist_ok=True)
    (path / 'SKILL.md').write_text(
        '---\nname: online-spreadsheets\ndescription: sheets workflow\n---\n\n'
        f'{body}\n', encoding='utf-8')
    return path


def _global_skill_md(owner: int = OWNER) -> Path:
    from lib.skills.paths import owner_global_skills_dir
    return Path(owner_global_skills_dir(owner)) / 'online-spreadsheets' / 'SKILL.md'


def test_install_then_unchanged_then_update_an_unedited_copy(isolated):
    source = _package(isolated / 'v1', 'revision one')
    assert ensure_companion_skill(str(source), owner_user_id=OWNER) == {
        'id': 'online-spreadsheets', 'action': 'installed'}
    assert ensure_companion_skill(str(source), owner_user_id=OWNER)['action'] == 'unchanged'

    newer = _package(isolated / 'v2', 'revision two')
    assert ensure_companion_skill(str(newer), owner_user_id=OWNER)['action'] == 'updated'
    assert 'revision two' in _global_skill_md().read_text(encoding='utf-8')

    from lib.skills.promote import recorded_origin
    origin = recorded_origin(str(_global_skill_md().parent))
    assert origin['source_registry'] == COMPANION_SOURCE_REGISTRY


def test_an_edited_or_foreign_same_id_skill_is_kept(isolated):
    source = _package(isolated / 'v1', 'revision one')
    ensure_companion_skill(str(source), owner_user_id=OWNER)
    installed = _global_skill_md()
    installed.write_text(installed.read_text(encoding='utf-8') + '\nmy note\n',
                         encoding='utf-8')
    newer = _package(isolated / 'v2', 'revision two')
    assert ensure_companion_skill(str(newer), owner_user_id=OWNER)['action'] == 'kept'
    assert 'my note' in installed.read_text(encoding='utf-8')

    other_owner_copy = _global_skill_md(8)
    _package(other_owner_copy.parent.parent, 'my own sheets guide')
    assert ensure_companion_skill(str(newer), owner_user_id=8)['action'] == 'kept'
    assert 'my own sheets guide' in other_owner_copy.read_text(encoding='utf-8')


def test_unreadable_existing_copy_reports_failure_instead_of_raising(isolated, monkeypatch):
    from lib.skills import companion
    ensure_companion_skill(str(_package(isolated / 'v1', 'one')), owner_user_id=OWNER)

    def unreadable(_path):
        raise PermissionError('denied')
    monkeypatch.setattr(companion, 'canonical_skill_content_sha256', unreadable)
    result = ensure_companion_skill(
        str(_package(isolated / 'v2', 'two')), owner_user_id=OWNER)
    assert result == {'id': 'online-spreadsheets', 'action': 'failed'}


def test_shipped_companion_package_is_valid_for_both_connectors(isolated):
    from lib.skills.promote import inspect_package
    dirs = {registry.companion_skill_dir(registry.get_catalog_entry(server_id))
            for server_id in ('tencent-docs', 'kdocs')}
    [package] = dirs
    row = inspect_package(package, owner_user_id=OWNER)
    assert row['status'] == 'missing' and row['id'] == 'online-spreadsheets'
    assert registry.companion_skill_dir({'companion_skill': '../outside'}) is None
    assert registry.companion_skill_dir({'companion_skill': 'absent-guide'}) is None
    assert registry.companion_skill_dir({}) is None


def test_catalog_install_reports_the_guide_and_survives_its_failure(isolated, monkeypatch):
    from quart import g, request
    import lib.mcp
    from lib.api_keys import AuthContext
    from lib.app.app_factory import create_base_app
    from lib.mcp import config
    from lib.skills import companion
    from routes.api_v1.mcp import api_v1_mcp_bp

    monkeypatch.setattr(config, '_CONFIG_DIR', str(isolated / 'mcp-config'))
    connected = []
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner: SimpleNamespace(
        connect_server=lambda server_id, cfg: connected.append(server_id) or [
            SimpleNamespace(name='read_file')]))
    app = create_base_app(__name__, {'TESTING': True})

    @app.before_request
    async def authenticated_fixture():
        g.auth_ctx = AuthContext(key_id='test', scopes=frozenset({'admin'}),
                                 owner_user_id=int(request.headers['X-Test-Owner']))
        g.rate_decision = None

    app.register_blueprint(api_v1_mcp_bp)

    async def install(owner, server_id, key):
        response = await app.test_client().post(
            '/api/v1/mcp/catalog/install', headers={'X-Test-Owner': str(owner)},
            json={'id': server_id, 'env': {key: 'not-a-real-token-0000'}})
        return response.status_code, await response.get_json()

    status, body = asyncio.run(install(OWNER, 'kdocs', 'KDOCS_TOKEN'))
    assert status == 200 and connected == ['kdocs']
    assert body['companion_skill'] == {
        'id': 'online-spreadsheets', 'action': 'installed'}
    assert _global_skill_md().is_file()
    assert not _global_skill_md(9).exists(), 'only the requesting owner gets it'

    def broken(*_args, **_kwargs):
        raise companion.InstallerError('disk full')
    monkeypatch.setattr(companion, 'install_skill_package', broken)
    status, body = asyncio.run(install(9, 'tencent-docs', 'TENCENT_DOCS_TOKEN'))
    assert status == 200 and connected == ['kdocs', 'tencent-docs']
    assert body['companion_skill']['action'] == 'failed'
    assert config.load_mcp_config(user_id=9)['tencent-docs']['enabled'] is True
