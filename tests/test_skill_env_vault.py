#!/usr/bin/env python3
"""tests/test_skill_env_vault.py — skill env/key bindings (epic pt_b1c5ab25c79d4259).

Pins the long-term skill-credential design (owner directive 2026-08-05):

  * DECLARATION — OpenClaw nested-YAML ``metadata.openclaw.requires.env``
    actually parses (PyYAML seam in _frontmatter), so a skill's key needs
    are machine-readable, not prose.
  * STORAGE — values live ONLY in the credential vault (Fernet at rest),
    keyed ``skill.<id>.<env_lower>``; status APIs are redacted.
  * ELIGIBILITY — a declared env var is satisfied by the process env OR a
    vault binding (Settings → Skills unlocks the skill).
  * EXECUTION — run_command's child env carries every ENABLED skill's
    configured bindings (``exec_env_overlay``), so a skill's documented
    ``os.environ['SOME_KEY']`` lookup works with no server restart.
  * LIFECYCLE — uninstall clears the skill's vault bindings (no orphan
    secrets); ``set_skill_scope`` moves a package project↔global.
  * SURFACE — the /api/v1/skills/<id>/env + /scope routes envelope
    correctly and never echo a value.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

import lib.memory.storage._dirs as dirs

pytestmark = pytest.mark.unit


# ── fixtures ─────────────────────────────────────────────────────────

@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """Redirect the server data dir + the vault to tmp; clear latches."""
    import lib.credentials_vault as vault
    data_dir = tmp_path / 'data'
    monkeypatch.setattr(dirs, '_server_data_dir', lambda: str(data_dir))
    monkeypatch.setattr(vault, '_STORE_PATH', tmp_path / 'vault.json')
    monkeypatch.setattr(vault, '_KEY_PATH', tmp_path / '.vault.key')
    monkeypatch.setattr(vault, '_fernet', None)
    dirs._migrated_roots.clear()
    dirs._server_store_migrated = False
    yield tmp_path
    dirs._migrated_roots.clear()
    dirs._server_store_migrated = False


def _write_pkg(dirpath, pkg_id, *, enabled=None, metadata_yaml=None,
               body='guide body'):
    pkg_dir = os.path.join(dirpath, pkg_id)
    os.makedirs(pkg_dir, exist_ok=True)
    lines = ['---', f'name: {pkg_id}',
             f'description: guide for {pkg_id} tasks']
    if enabled is not None:
        lines.append(f'enabled: {str(enabled).lower()}')
    if metadata_yaml:
        lines.append(metadata_yaml)
    lines.append('---')
    lines.append('')
    lines.append(body)
    with open(os.path.join(pkg_dir, 'SKILL.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return pkg_dir


# OpenClaw nested-YAML metadata (the shape the hand-rolled parser used to
# collapse to ''). Indented block, no JSON.
_REQUIRES_ENV_YAML = (
    'metadata:\n'
    '  openclaw:\n'
    '    requires:\n'
    '      bins: [python3]\n'
    '      env: [SOYOUNG_API_KEY]'
)


def _proj(tmp_path, name='proj'):
    p = tmp_path / name
    (p / '.tofu' / 'skills').mkdir(parents=True, exist_ok=True)
    return str(p)


# ── 1. declaration parsing ───────────────────────────────────────────

def test_nested_yaml_metadata_parses_requires_env():
    from lib.memory.storage._frontmatter import (
        _extract_package_metadata, _parse_frontmatter)
    text = ('---\nname: demo\ndescription: demo skill\n'
            + _REQUIRES_ENV_YAML + '\n---\n\nbody\n')
    meta, _ = _parse_frontmatter(text)
    pkg = _extract_package_metadata(meta)
    assert pkg['requires_env'] == ['SOYOUNG_API_KEY']
    assert pkg['requires_bins'] == ['python3']


def test_inline_json_metadata_still_works():
    """Regression: the pre-existing JSON-metadata form must parse exactly
    as before (the nested-YAML seam is additive)."""
    from lib.memory.storage._frontmatter import (
        _extract_package_metadata, _parse_frontmatter)
    text = ('---\nname: demo\ndescription: demo skill\n'
            'metadata: {"openclaw": {"requires": {"env": ["A_KEY"]}}}\n'
            '---\n\nbody\n')
    meta, _ = _parse_frontmatter(text)
    assert _extract_package_metadata(meta)['requires_env'] == ['A_KEY']


def test_entry_name_roundtrip():
    from lib.skills.env import entry_name, env_name_from_entry
    entry = entry_name('soyoung-clinic-tools', 'SOYOUNG_API_KEY')
    assert entry == 'skill.soyoung-clinic-tools.soyoung_api_key'
    assert env_name_from_entry(entry, 'soyoung-clinic-tools') == \
        'SOYOUNG_API_KEY'
    # Another skill's entry must not reverse-resolve under this id.
    assert env_name_from_entry('skill.other.x_key', 'soyoung-clinic-tools') \
        is None


# ── 2. vault-backed CRUD + redaction ─────────────────────────────────

def test_set_status_delete_flow(isolated):
    from lib.skills.env import (
        delete_skill_env, set_skill_env, skill_env_status)
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg',
               metadata_yaml=_REQUIRES_ENV_YAML)
    from lib.skills import get_skill
    skill = get_skill('mypkg', project_path=proj)
    assert skill['requires_env'] == ['SOYOUNG_API_KEY']

    rows = skill_env_status(skill)
    assert rows == [{'name': 'SOYOUNG_API_KEY', 'declared': True,
                     'configured': False, 'hint': ''}]

    secret = 'sy-secret-0123456789abcdef'
    meta = set_skill_env('mypkg', 'SOYOUNG_API_KEY', secret)
    assert 'ct' not in meta and 'value' not in meta
    rows = skill_env_status(skill)
    assert rows[0]['configured'] is True
    assert rows[0]['hint'].startswith('sy-s')
    assert secret not in str(rows), 'status must never echo the value'

    assert delete_skill_env('mypkg', 'SOYOUNG_API_KEY') is True
    assert skill_env_status(skill)[0]['configured'] is False


def test_set_rejects_bad_env_names(isolated):
    from lib.skills.env import set_skill_env
    for bad in ('', '1STARTS_WITH_DIGIT', 'has space', 'has-dash'):
        with pytest.raises(ValueError):
            set_skill_env('mypkg', bad, 'x')


# ── 3. eligibility gate reads the vault ──────────────────────────────

def test_env_requirement_blocks_until_vault_configured(isolated):
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg',
               metadata_yaml=_REQUIRES_ENV_YAML)
    from lib.skills import get_skill
    from lib.skills.env import set_skill_env

    os.environ.pop('SOYOUNG_API_KEY', None)
    skill = get_skill('mypkg', project_path=proj)
    assert skill['eligible'] is False
    assert any('SOYOUNG_API_KEY' in r
               for r in skill['ineligible_reasons'])

    set_skill_env('mypkg', 'SOYOUNG_API_KEY', 'sy-0123456789abcdef')
    skill = get_skill('mypkg', project_path=proj)
    assert skill['eligible'] is True, skill['ineligible_reasons']


def test_process_env_still_satisfies_the_gate(isolated, monkeypatch):
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg',
               metadata_yaml=_REQUIRES_ENV_YAML)
    monkeypatch.setenv('SOYOUNG_API_KEY', 'from-process-env')
    from lib.skills import get_skill
    assert get_skill('mypkg', project_path=proj)['eligible'] is True


# ── 4. execution overlay ─────────────────────────────────────────────

def test_exec_env_overlay_enabled_only(isolated):
    proj = _proj(isolated)
    root = os.path.join(proj, '.tofu', 'skills')
    _write_pkg(root, 'onskill', metadata_yaml=_REQUIRES_ENV_YAML)
    _write_pkg(root, 'offskill', enabled=False,
               metadata_yaml=_REQUIRES_ENV_YAML)
    from lib.skills.env import exec_env_overlay, set_skill_env
    set_skill_env('onskill', 'SOYOUNG_API_KEY', 'sy-on-0123456789')
    set_skill_env('offskill', 'SOYOUNG_API_KEY', 'sy-off-0123456789')

    overlay = exec_env_overlay(project_path=proj)
    assert overlay.get('SOYOUNG_API_KEY') == 'sy-on-0123456789', (
        'the ENABLED skill binding must ride the child env')
    # Flip: enable offskill, disable onskill — the value must follow state.
    from lib.memory import toggle_memory
    toggle_memory('onskill', enabled=False, project_path=proj)
    toggle_memory('offskill', enabled=True, project_path=proj)
    overlay = exec_env_overlay(project_path=proj)
    assert overlay.get('SOYOUNG_API_KEY') == 'sy-off-0123456789'


def test_run_command_child_env_carries_the_overlay(isolated):
    """The run_command subprocess env seam (_get_cmd_env) must merge the
    overlay — this is what makes `os.environ['KEY']` inside a skill's
    documented curl/python snippet actually resolve."""
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg',
               metadata_yaml=_REQUIRES_ENV_YAML)
    from lib.skills.env import set_skill_env
    set_skill_env('mypkg', 'SOYOUNG_API_KEY', 'sy-child-env-012345')
    from lib.project_mod.run_command import _get_cmd_env
    env = _get_cmd_env(proj)
    assert env.get('SOYOUNG_API_KEY') == 'sy-child-env-012345'


# ── 5. lifecycle: uninstall cleanup + scope move ─────────────────────

def test_uninstall_clears_vault_bindings(isolated):
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg',
               metadata_yaml=_REQUIRES_ENV_YAML)
    from lib.skills import uninstall_skill
    from lib.skills.env import get_skill_env, set_skill_env
    set_skill_env('mypkg', 'SOYOUNG_API_KEY', 'sy-0123456789abcdef')
    assert get_skill_env('mypkg')
    assert uninstall_skill('mypkg', project_path=proj) is True
    assert get_skill_env('mypkg') == {}, 'no orphan secrets after uninstall'


def test_set_skill_scope_moves_package(isolated):
    proj = _proj(isolated)
    _write_pkg(os.path.join(proj, '.tofu', 'skills'), 'mypkg')
    from lib.skills import get_skill, set_skill_scope

    moved = set_skill_scope('mypkg', 'global', project_path=proj)
    assert moved['scope'] == 'global'
    assert not os.path.exists(
        os.path.join(proj, '.tofu', 'skills', 'mypkg'))
    assert os.path.isfile(os.path.join(
        str(isolated / 'data'), 'skills', 'global', 'mypkg', 'SKILL.md'))
    # Registry now resolves it from the global store — visible with NO
    # project attached (the chat-mode invisibility fix).
    assert get_skill('mypkg', project_path=None) is not None
    # Moving back is symmetric; a no-op move returns the same scope.
    back = set_skill_scope('mypkg', 'project', project_path=proj)
    assert back['scope'] == 'project'
    with pytest.raises(ValueError):
        set_skill_scope('mypkg', 'sideways', project_path=proj)


# ── 6. HTTP surface ──────────────────────────────────────────────────

def _install_shim():
    import quart
    sys.modules['flask'] = quart


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.mark.unit
class SkillEnvRoutesTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_shim()
        cls._prev_auth = os.environ.get('TOFU_AUTH_MODE')
        os.environ['TOFU_AUTH_MODE'] = 'open'
        from lib.auth_mode import reset_for_tests
        reset_for_tests()
        from quart import Quart
        cls.app = Quart(__name__)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)
        from routes.api_v1.skills import api_v1_skills_bp
        cls.app.register_blueprint(api_v1_skills_bp)

    @classmethod
    def tearDownClass(cls):
        if cls._prev_auth is None:
            os.environ.pop('TOFU_AUTH_MODE', None)
        else:
            os.environ['TOFU_AUTH_MODE'] = cls._prev_auth
        from lib.auth_mode import reset_for_tests
        reset_for_tests()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self._data_dir = tmp / 'data'
        self._orig_data_dir = dirs._server_data_dir
        dirs._server_data_dir = lambda: str(self._data_dir)
        dirs._migrated_roots.clear()
        dirs._server_store_migrated = False
        import lib.credentials_vault as vault
        self._vault_prev = (vault._STORE_PATH, vault._KEY_PATH, vault._fernet)
        vault._STORE_PATH = tmp / 'vault.json'
        vault._KEY_PATH = tmp / '.vault.key'
        vault._fernet = None
        self.addCleanup(self._restore)
        self.proj = os.path.join(self._tmp.name, 'proj')
        _write_pkg(os.path.join(self.proj, '.tofu', 'skills'), 'mypkg',
                   metadata_yaml=_REQUIRES_ENV_YAML)

    def _restore(self):
        dirs._server_data_dir = self._orig_data_dir
        dirs._migrated_roots.clear()
        dirs._server_store_migrated = False
        import lib.credentials_vault as vault
        (vault._STORE_PATH, vault._KEY_PATH,
         vault._fernet) = self._vault_prev

    def _q(self):
        from urllib.parse import quote
        return f'?project_path={quote(self.proj, safe="")}'

    def test_env_roundtrip_redacted(self):
        secret = 'sy-route-0123456789abcdef'

        async def go():
            r = await self.app.test_client().put(
                '/api/v1/skills/mypkg/env' + self._q(),
                json={'name': 'SOYOUNG_API_KEY', 'value': secret})
            self.assertEqual(r.status_code, 200, await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertNotIn(secret, str(body))

            r = await self.app.test_client().get(
                '/api/v1/skills/mypkg/env' + self._q())
            body = await r.get_json()
            rows = {e['name']: e for e in body['env']}
            self.assertTrue(rows['SOYOUNG_API_KEY']['configured'])
            self.assertNotIn(secret, str(body))

            r = await self.app.test_client().delete(
                '/api/v1/skills/mypkg/env/SOYOUNG_API_KEY' + self._q())
            self.assertEqual(r.status_code, 200)
            r = await self.app.test_client().get(
                '/api/v1/skills/mypkg/env' + self._q())
            body = await r.get_json()
            rows = {e['name']: e for e in body['env']}
            self.assertFalse(rows['SOYOUNG_API_KEY']['configured'])
        _run(go())

    def test_env_validation_400_and_unknown_skill_404(self):
        async def go():
            r = await self.app.test_client().put(
                '/api/v1/skills/mypkg/env' + self._q(),
                json={'name': '', 'value': 'x'})
            self.assertEqual(r.status_code, 400)
            r = await self.app.test_client().put(
                '/api/v1/skills/mypkg/env' + self._q(),
                json={'name': 'OK_NAME', 'value': ''})
            self.assertEqual(r.status_code, 400)
            r = await self.app.test_client().get(
                '/api/v1/skills/nope/env' + self._q())
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_scope_route_moves_package(self):
        async def go():
            r = await self.app.test_client().post(
                '/api/v1/skills/mypkg/scope' + self._q(),
                json={'scope': 'global'})
            self.assertEqual(r.status_code, 200, await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertEqual(body['skill']['scope'], 'global')
            r = await self.app.test_client().post(
                '/api/v1/skills/mypkg/scope' + self._q(),
                json={'scope': 'sideways'})
            self.assertEqual(r.status_code, 400)
        _run(go())
        self.assertTrue(os.path.isfile(os.path.join(
            str(self._data_dir), 'skills', 'global', 'mypkg', 'SKILL.md')))


if __name__ == '__main__':
    unittest.main()
