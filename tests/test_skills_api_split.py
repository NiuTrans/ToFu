#!/usr/bin/env python3
"""tests/test_skills_api_split.py — /api/v1/skills ↔ /api/v1/memory split (P4).

Pins the API contract of the memory/skill decoupling (board epic pt_229606ca):

  * GET  /api/v1/skills            — packages ONLY (no flat memories)
  * GET  /api/v1/memory            — memories ONLY, no transient 'skills' key
  * POST /api/v1/skills/<id>/toggle, GET /api/v1/skills/<id>/files,
    DELETE /api/v1/skills/<id>     — package lifecycle on the skills surface
  * DELETE /api/v1/memory/<pkg>    — 400 (package guard surfaces; uninstall
    lives on the skills surface)
  * POST /api/v1/skills/install    — JSON-path install end to end
  * lib.skills.uninstall_skill     — refuses unknown / non-package ids

Runs in OPEN auth mode (synthetic principal) with the server data dir
redirected to a tmp dir — the real store is never touched.
"""

import asyncio
import io
import os
import tempfile
import unittest
import zipfile

import pytest

import lib.memory.storage._dirs as dirs


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _write_flat(dirpath, name, body='flat body'):
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, f'{name}.md'), 'w', encoding='utf-8') as f:
        f.write(f'---\nname: {name}\n'
                f'description: flat memory {name} description text\n'
                f'---\n\n{body}\n')


def _write_pkg(dirpath, pkg_id, body='pkg guide'):
    pkg_dir = os.path.join(dirpath, pkg_id)
    os.makedirs(os.path.join(pkg_dir, 'references'), exist_ok=True)
    with open(os.path.join(pkg_dir, 'SKILL.md'), 'w', encoding='utf-8') as f:
        f.write(f'---\nname: {pkg_id}\n'
                f'description: skill package {pkg_id} description\n'
                f'---\n\n{body}\n')
    with open(os.path.join(pkg_dir, 'references', 'r.md'), 'w',
              encoding='utf-8') as f:
        f.write('reference\n')


def _zip_bytes(pkg_folder='zipskill'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr(f'{pkg_folder}/SKILL.md',
                    '---\nname: Zip Skill\n'
                    'description: a zip-installed test skill package\n'
                    '---\n\nbody\n')
    return buf.getvalue()


@pytest.mark.unit
class SkillsApiSplitTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._prev_auth = os.environ.get('TOFU_AUTH_MODE')
        os.environ['TOFU_AUTH_MODE'] = 'open'
        from lib.auth_mode import reset_for_tests
        reset_for_tests()
        from quart import Quart
        cls.app = Quart(__name__, static_folder=None)
        cls.app.config.setdefault('PROVIDE_AUTOMATIC_OPTIONS', True)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)
        from routes.api_v1.memory import api_v1_memory_bp
        from routes.api_v1.skills import api_v1_skills_bp
        cls.app.register_blueprint(api_v1_memory_bp)
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
        self._data_dir = os.path.join(self._tmp.name, 'data')
        self._orig_data_dir = dirs._server_data_dir
        dirs._server_data_dir = lambda: self._data_dir
        dirs._migrated_roots.clear()
        dirs._server_store_migrated = False
        self.addCleanup(self._restore_dirs)

        # Seed: one project package + one project flat memory.
        self.proj = os.path.join(self._tmp.name, 'proj')
        _write_pkg(os.path.join(self.proj, '.tofu', 'skills'), 'mypkg')
        _write_flat(os.path.join(self.proj, '.tofu', 'memories'), 'mem1')

    def _restore_dirs(self):
        dirs._server_data_dir = self._orig_data_dir
        dirs._migrated_roots.clear()
        dirs._server_store_migrated = False

    def _q(self):
        from urllib.parse import quote
        return f'?project_path={quote(self.proj, safe="")}'

    # ── list surfaces ────────────────────────────────────────────────

    def test_skills_list_returns_only_packages(self):
        async def go():
            r = await self.app.test_client().get('/api/v1/skills' + self._q())
            self.assertEqual(r.status_code, 200, await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertIn('skills', body)
            ids = {s['id'] for s in body['skills']}
            self.assertEqual(ids, {'mypkg'})
            self.assertTrue(all(s['is_package'] for s in body['skills']))
            # Server-side absolute paths must not leak.
            self.assertNotIn('package_dir', body['skills'][0])
            self.assertNotIn('filepath', body['skills'][0])
        _run(go())

    def test_memory_list_excludes_packages_and_skills_key(self):
        async def go():
            r = await self.app.test_client().get('/api/v1/memory' + self._q())
            self.assertEqual(r.status_code, 200, await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertIn('memories', body)
            ids = {m['id'] for m in body['memories']}
            self.assertEqual(ids, {'mem1'})
            # The transient back-compat alias is gone post-split.
            self.assertNotIn('skills', body)
        _run(go())

    # ── package lifecycle on the skills surface ─────────────────────

    def test_toggle_skill(self):
        async def go():
            r = await self.app.test_client().post(
                '/api/v1/skills/mypkg/toggle' + self._q(), json={})
            self.assertEqual(r.status_code, 200, await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertFalse(body['enabled'])
        _run(go())
        from lib.skills import get_skill
        self.assertFalse(get_skill('mypkg', project_path=self.proj)['enabled'])

    def test_skill_files_listing(self):
        async def go():
            r = await self.app.test_client().get(
                '/api/v1/skills/mypkg/files' + self._q())
            self.assertEqual(r.status_code, 200, await r.get_data(as_text=True))
            body = await r.get_json()
            paths = {f['path']: f['kind'] for f in body['files']}
            self.assertEqual(paths.get('SKILL.md'), 'skill')
            self.assertEqual(paths.get('references/r.md'), 'doc')
            self.assertEqual(body['count'], 2)
        _run(go())

    def test_uninstall_via_skills_surface(self):
        async def go():
            r = await self.app.test_client().delete(
                '/api/v1/skills/mypkg' + self._q())
            self.assertEqual(r.status_code, 200, await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertTrue(body['deleted'])
        _run(go())
        self.assertFalse(os.path.exists(
            os.path.join(self.proj, '.tofu', 'skills', 'mypkg')))

    def test_uninstall_unknown_404(self):
        async def go():
            r = await self.app.test_client().delete(
                '/api/v1/skills/nope' + self._q())
            self.assertEqual(r.status_code, 404)
        _run(go())

    # ── memory surface refuses packages ──────────────────────────────

    def test_memory_delete_package_surfaces_guard(self):
        async def go():
            r = await self.app.test_client().delete(
                '/api/v1/memory/mypkg' + self._q())
            self.assertEqual(r.status_code, 400, await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertIn('skill package', str(body))
        _run(go())
        # The package survived.
        self.assertTrue(os.path.isfile(
            os.path.join(self.proj, '.tofu', 'skills', 'mypkg', 'SKILL.md')))

    # ── install through the skills surface ───────────────────────────

    def test_install_via_json_path(self):
        zip_path = os.path.join(self._tmp.name, 'pkg.zip')
        with open(zip_path, 'wb') as f:
            f.write(_zip_bytes())

        async def go():
            r = await self.app.test_client().post(
                '/api/v1/skills/install',
                json={'path': zip_path, 'scope': 'project',
                      'project_path': self.proj})
            self.assertEqual(r.status_code, 201, await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertEqual(body['memory']['scope'], 'project')
        _run(go())
        # Id derives from the SKILL.md name ('Zip Skill' → zip_skill).
        self.assertTrue(os.path.isfile(os.path.join(
            self.proj, '.tofu', 'skills', 'zip_skill', 'SKILL.md')))

    def test_catalog_served_on_skills_surface(self):
        async def go():
            r = await self.app.test_client().get(
                '/api/v1/skills/catalog' + self._q())
            self.assertEqual(r.status_code, 200, await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertIsInstance(body['catalog'], list)
            self.assertGreater(len(body['catalog']), 0)
            # The seeded package is not a catalog entry; nothing installed
            # flags should be set for it.
            self.assertIn('installed_ids', body)
        _run(go())


@pytest.mark.unit
class UninstallSkillLibTest(unittest.TestCase):
    """lib-level uninstall_skill edge cases (no HTTP)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_data_dir = dirs._server_data_dir
        dirs._server_data_dir = lambda: os.path.join(self._tmp.name, 'data')
        dirs._migrated_roots.clear()
        dirs._server_store_migrated = False
        self.addCleanup(self._restore)
        self.proj = os.path.join(self._tmp.name, 'proj')
        _write_pkg(os.path.join(self.proj, '.tofu', 'skills'), 'mypkg')
        _write_flat(os.path.join(self.proj, '.tofu', 'memories'), 'mem1')

    def _restore(self):
        dirs._server_data_dir = self._orig_data_dir
        dirs._migrated_roots.clear()
        dirs._server_store_migrated = False

    def test_uninstall_removes_package_dir(self):
        from lib.skills import uninstall_skill
        self.assertTrue(uninstall_skill('mypkg', project_path=self.proj))
        self.assertFalse(os.path.exists(
            os.path.join(self.proj, '.tofu', 'skills', 'mypkg')))

    def test_uninstall_refuses_unknown_and_flat_memory_ids(self):
        from lib.skills import uninstall_skill
        # Unknown id → False, nothing touched.
        self.assertFalse(uninstall_skill('nope', project_path=self.proj))
        # A FLAT MEMORY id is not a skill — uninstall_skill must not delete
        # it (that's delete_memory's job, and this cross-noun guard is the
        # mirror of the memory-side package guard).
        self.assertFalse(uninstall_skill('mem1', project_path=self.proj))
        self.assertTrue(os.path.isfile(
            os.path.join(self.proj, '.tofu', 'memories', 'mem1.md')))


if __name__ == '__main__':
    unittest.main()
