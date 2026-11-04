"""Internal exports carry every shareable capability and credential.

Owner decision 2026-11-01: ``export.py --mode internal`` (and ``--handoff``)
must arrive able to use the YourProvider middle platform with no manual MCP,
skill or API-key setup. These tests pin the new contract, including the
things that must still stay behind for FUNCTIONAL reasons (per-person SSO
identity, per-install keys, personal history) and the packaging defects that
previously broke carried skills (dropped vendored engines, shipped session
tickets).

Owner decision 2026-11-04 narrows the MCP part: only registrations served by
an internal MCP repo the export bundles travel. Third-party and personal
servers need their user's own tokens and stay behind.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

export = pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

import export_pkg._internal as internal  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = tmp_path / 'proj'
    (root / 'data' / 'config').mkdir(parents=True)
    monkeypatch.setattr(internal, 'ROOT', root)
    return root


# ── MCP registrations ────────────────────────────────────────────────
def _write_mcp(root: Path, config: dict, *, owner_store: bool) -> None:
    if owner_store:
        directory = root / 'data' / 'config' / 'mcp_servers'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / '1.json').write_text(json.dumps(config), encoding='utf-8')
    else:
        (root / 'data' / 'config' / 'mcp_servers.json').write_text(
            json.dumps(config), encoding='utf-8')


# The owner store's real registration shapes, with synthetic values.
_OWNER_REGISTRATIONS = {
    'github': {'command': '/mnt/personal/env/bin/python',
               'args': ['-c', 'import sys; sys.path.insert(0, "/mnt/personal/tofu")'],
               'enabled': True, 'env': {}},
    'github-batch': {'command': '/mnt/personal/env/bin/python', 'args': ['-c', '...'],
                     'enabled': True, 'env': {}},
    'hope': {'command': 'hope-mcp', 'args': [], 'enabled': True,
             'env': {'HOPE_USERNAME': 'operator'}},
    'overleaf': {'command': 'tofu-overleaf-mcp', 'args': [], 'enabled': True,
                 'env': {'OVERLEAF_SESSION': 'session-value',
                         'OVERLEAF_GIT_TOKEN': 'git-token-value'}},
    'xuecheng': {'command': 'xuecheng-mcp', 'args': [], 'enabled': True,
                 'env': {'XUECHENG_MIS': 'operator', 'XUECHENG_TOOLSET': 'all'}},
    'llm': {'command': 'llm-mcp', 'args': [], 'enabled': False,
            'env': {'LLM_MIS': 'operator'}},
    '12306-train': {'command': 'npx', 'args': ['-y', '12306-mcp'], 'enabled': True},
    'rollinggo-flight': {'enabled': False, 'url': 'https://example.invalid/mcp',
                         'env': {'ROLLINGGO_API_KEY': 'key-value'}},
    # A host-pinned path to a bundled launcher: only the bare launcher name
    # resolves to the bundled vendor/ source on another host.
    'hope-pinned': {'command': '/mnt/personal/env/bin/hope-mcp', 'args': [],
                    'enabled': True, 'env': {}},
}


def test_mcp_reads_the_live_owner_store_not_the_stale_legacy_file(project):
    _write_mcp(project, {'xuecheng': {'command': 'xuecheng-mcp', 'enabled': True}},
               owner_store=False)
    _write_mcp(project, {'hope': {'command': 'hope-mcp', 'enabled': True, 'env': {}}},
               owner_store=True)
    carried = internal._carried_mcp_config()
    assert set(carried) == {'hope'}, 'the runtime authority is the owner store'


def test_mcp_carries_only_registrations_served_by_a_bundled_repo(project):
    _write_mcp(project, _OWNER_REGISTRATIONS, owner_store=True)
    carried = internal._carried_mcp_config()
    assert set(carried) == {'hope', 'xuecheng', 'llm'}
    assert carried['hope'] == {'command': 'hope-mcp', 'args': [], 'enabled': True, 'env': {}}
    assert carried['xuecheng']['env'] == {'XUECHENG_TOOLSET': 'all'}
    assert carried['llm']['enabled'] is False, 'the operator on/off state travels as is'


def test_mcp_third_party_and_personal_servers_stay_behind(project):
    _write_mcp(project, _OWNER_REGISTRATIONS, owner_store=True)
    carried = internal._carried_mcp_config()
    for name in ('github', 'github-batch', 'overleaf', '12306-train',
                 'rollinggo-flight', 'hope-pinned'):
        assert name not in carried, f'{name} is not served by a bundled internal MCP repo'
    serialized = json.dumps(carried)
    for personal_secret in ('session-value', 'git-token-value', 'key-value'):
        assert personal_secret not in serialized


def test_mcp_carry_follows_the_bundled_repo_set(project, tmp_path):
    """Carried launchers are exactly the vendored ones, never a second list."""
    for sibling in ('hope-mcp', 'xuecheng-mcp', 'llm-mcp', 'unrelated-mcp'):
        (tmp_path / sibling).mkdir()
        (tmp_path / sibling / 'pyproject.toml').write_text(
            f'[project]\nname="{sibling}"\n', encoding='utf-8')
    dest = tmp_path / 'dest'
    internal._bundle_internal_mcp_repos(dest, 'internal')
    vendored = {path.name for path in (dest / 'vendor').iterdir() if path.is_dir()}
    assert vendored == {'hope-mcp', 'xuecheng-mcp', 'llm-mcp'}

    _write_mcp(project, {
        f'server-{launcher}': {'command': launcher, 'args': [], 'enabled': True}
        for launcher in ('hope-mcp', 'xuecheng-mcp', 'llm-mcp', 'unrelated-mcp')
    }, owner_store=True)
    carried = internal._carried_mcp_config()
    assert {entry['command'] for entry in carried.values()} == vendored


def test_mcp_strips_only_personal_login_identity(project):
    _write_mcp(project, {
        'hope': {'command': 'hope-mcp', 'enabled': True,
                 'env': {'HOPE_USERNAME': 'operator', 'HOPE_ENDPOINT': 'https://example.invalid'}},
        'xuecheng': {'command': 'xuecheng-mcp', 'enabled': True,
                     'env': {'XUECHENG_MIS': 'operator', 'XUECHENG_TOOLSET': 'all'}},
        'llm': {'command': 'llm-mcp', 'enabled': True, 'env': {'LLM_MIS': 'operator'}},
    }, owner_store=False)
    carried = internal._carried_mcp_config()
    assert carried['hope']['env'] == {'HOPE_ENDPOINT': 'https://example.invalid'}
    assert carried['xuecheng']['env'] == {'XUECHENG_TOOLSET': 'all'}
    assert carried['llm']['env'] == {}


def test_carried_mcp_config_is_written_owner_only(project, tmp_path):
    _write_mcp(project, {'hope': {'command': 'hope-mcp', 'enabled': True, 'env': {}}},
               owner_store=False)
    internal._stage_portable_mcp_config(tmp_path)
    path = tmp_path / 'data' / 'config' / 'mcp_servers.json'
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(json.loads(path.read_text())) == ['hope']


# ── Credential vault and shareable platform config ───────────────────
def test_vault_travels_with_its_key_and_private_modes(project, tmp_path):
    config = project / 'data' / 'config'
    (config / 'credentials_vault.json').write_text('{"entries": {}}', encoding='utf-8')
    (config / '.credentials_vault.key').write_text('key-material', encoding='utf-8')
    (config / 'features.json').write_text('{"trading": false}', encoding='utf-8')
    (config / 'private_hosts.json').write_text('{"hosts": []}', encoding='utf-8')
    (config / 'flask_secret_key').write_text('per-install', encoding='utf-8')
    (config / 'usage.json').write_text('{"personal": true}', encoding='utf-8')
    (config / '.first_run_token').write_text('per-install', encoding='utf-8')
    (config / 'site_packs').mkdir()
    (config / 'site_packs' / 'x.json').write_text('{"domain": "example.invalid"}',
                                                 encoding='utf-8')
    internal._copy_carried_config(tmp_path, 'internal')
    carried = tmp_path / 'data' / 'config'
    assert (carried / 'credentials_vault.json').stat().st_mode & 0o777 == 0o600
    assert (carried / '.credentials_vault.key').stat().st_mode & 0o777 == 0o600
    assert (carried / 'features.json').exists()
    assert (carried / 'private_hosts.json').exists()
    assert (carried / 'site_packs' / 'x.json').exists()
    assert not (carried / 'flask_secret_key').exists(), 'per-install key must not travel'
    assert not (carried / '.first_run_token').exists()
    assert not (carried / 'usage.json').exists(), 'personal history must not travel'


def test_non_internal_modes_carry_no_config(project, tmp_path):
    (project / 'data' / 'config' / 'credentials_vault.json').write_text('{}',
                                                                       encoding='utf-8')
    for mode in ('personal', 'opensource'):
        internal._copy_carried_config(tmp_path, mode)
    assert not (tmp_path / 'data').exists()


# ── Memories: lessons yes, personal profile no ───────────────────────
def test_memories_carry_lessons_but_not_the_user_profile(project, tmp_path):
    memories = project / 'data' / 'memories'
    (memories / 'global').mkdir(parents=True)
    (memories / 'global' / 'lesson.md').write_text(
        '---\ntags: [yourprovider, mcp]\n---\nlesson\n', encoding='utf-8')
    (memories / 'global' / 'tofu-debug.md').write_text(
        '---\ntags: [refactor, frontend]\n---\nproject debug note\n', encoding='utf-8')
    (memories / '.tofu_user_profile.md').write_text('personal profile', encoding='utf-8')
    (memories / 'profiles').mkdir()
    (memories / 'profiles' / '1.json').write_text('{}', encoding='utf-8')
    (project / '.tofu' / 'memories').mkdir(parents=True)
    (project / '.tofu' / 'memories' / 'project-lesson.md').write_text(
        '---\ntags: [sandbox, yourprovider]\n---\np\n', encoding='utf-8')
    internal._copy_carried_memories(tmp_path, 'internal')
    assert (tmp_path / 'data' / 'memories' / 'global' / 'lesson.md').exists()
    assert (tmp_path / '.tofu' / 'memories' / 'project-lesson.md').exists()
    assert not (tmp_path / 'data' / 'memories' / '.tofu_user_profile.md').exists()
    assert not (tmp_path / 'data' / 'memories' / 'profiles').exists()
    assert not (tmp_path / 'data' / 'memories' / 'global' / 'tofu-debug.md').exists(), \
        'Tofu-internal debugging lessons are not shared platform know-how'


# ── Skill packages: vendored engines yes, session state no ───────────
def _skill(root: Path) -> Path:
    package = root / 'data' / 'skills' / 'users' / '1' / 'dx-pura-api'
    (package / 'vendor' / 'node_modules' / 'dx-engine').mkdir(parents=True)
    (package / 'SKILL.md').write_text('# dx\n', encoding='utf-8')
    (package / 'vendor' / 'node_modules' / 'dx-engine' / 'main.js').write_text(
        'export {};\n', encoding='utf-8')
    (package / '.cache').mkdir()
    (package / '.cache' / 'dx-premint.json').write_text('{"tickets": true}',
                                                       encoding='utf-8')
    (package / '.dx-identity.json').write_text('{"mis": "operator"}', encoding='utf-8')
    (package / '.tofu' / 'file-history').mkdir(parents=True)
    (package / '.tofu' / 'file-history' / 'old.md').write_text('old', encoding='utf-8')
    (package / '.skillignore').write_text('.dx-identity.json\n.cache/\n', encoding='utf-8')
    return package


def test_skill_export_keeps_vendored_engine_and_drops_session_state(project, tmp_path):
    _skill(project)
    internal._copy_installed_skills(tmp_path, 'internal')
    carried = tmp_path / 'data' / 'skills' / 'users' / '1' / 'dx-pura-api'
    assert (carried / 'vendor' / 'node_modules' / 'dx-engine' / 'main.js').is_file(), \
        'dropping node_modules ships a skill whose CLI cannot start'
    assert not (carried / '.cache').exists(), 'live session tickets must not travel'
    assert not (carried / '.dx-identity.json').exists(), \
        'the package .skillignore marks the operator identity as never-publish'
    assert not (carried / '.tofu').exists()
    assert (carried / 'SKILL.md').is_file()


# ── Live provider authority ──────────────────────────────────────────
def test_carried_model_routing_is_staged_owner_only(project, tmp_path, monkeypatch):
    monkeypatch.setattr('export_pkg._live_state.read_carried_model_routing', lambda: {
        'document': {'contract_version': 'tofu.model-routing/v2', 'revision': 7,
                     'providers': [{'provider_id': 'example-corp'}],
                     'credentials': [{'credential_id': 'c1', 'kind': 'api_key',
                                      'secret_reference': 'mrs_1'}]},
        'secrets': {'c1': 'current-key'},
        'local_signin_credentials': [],
    })
    internal._stage_carried_model_routing(tmp_path, 'internal')
    path = tmp_path / internal._CARRIED_MODEL_ROUTING_REL
    payload = json.loads(path.read_text())
    assert payload['contract_version'] == internal._CARRIED_MODEL_ROUTING_CONTRACT
    assert payload['secrets'] == {'c1': 'current-key'}
    assert path.stat().st_mode & 0o777 == 0o600


def test_unreachable_live_authority_falls_back_loudly(project, tmp_path, monkeypatch, capsys):
    from export_pkg._live_state import LiveAuthorityError

    def _unavailable():
        raise LiveAuthorityError('application API unreachable')

    monkeypatch.setattr('export_pkg._live_state.read_carried_model_routing', _unavailable)
    internal._stage_carried_model_routing(tmp_path, 'internal')
    assert not (tmp_path / internal._CARRIED_MODEL_ROUTING_REL).exists()
    assert 'stale' in capsys.readouterr().out, \
        'a silent fallback would ship outdated keys without telling anyone'


# ── github-batch vendoring ───────────────────────────────────────────
def test_github_batch_source_is_vendored_when_registered(project, tmp_path):
    _write_mcp(project, {'github-batch': {'command': '/mnt/x/python', 'enabled': True}},
               owner_store=False)
    source = project / 'tools' / 'github-batch-mcp'
    (source / 'github_batch_mcp').mkdir(parents=True)
    (source / 'pyproject.toml').write_text('[project]\nname="github-batch-mcp"\n',
                                          encoding='utf-8')
    (source / 'github_batch_mcp' / 'server.py').write_text('# server\n', encoding='utf-8')
    internal._bundle_github_batch_mcp(tmp_path, 'internal')
    assert (tmp_path / 'vendor' / 'github-batch-mcp' / 'pyproject.toml').is_file()
    assert (tmp_path / 'vendor' / 'github-batch-mcp' /
            'github_batch_mcp' / 'server.py').is_file()
