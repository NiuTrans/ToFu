"""tests/test_api_v1_integration.py — End-to-end v1 API integration.

Boots a minimal Quart app with the same blueprint layout as
``server.py`` and exercises the auth, capabilities, openapi, and key
management endpoints through a real test client.

Does NOT exercise the chat/completions path because that requires the
LLM dispatcher; covered separately by smoke tests with mocked
``stream_chat``.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

import pytest


class _AppFixture:
    """Build a Quart app with the headless API blueprints registered.

    Uses a tmp api_keys.json so the production file is never touched.
    """

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Patch the API-key store path BEFORE the auth module loads.
        self._patch_api_keys_path()
        # Install the flask→quart shim like server.py does.
        import quart  # noqa
        sys.modules['flask'] = quart
        for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
            qs = f'quart.{attr}'
            if qs in sys.modules:
                sys.modules[f'flask.{attr}'] = sys.modules[qs]

        # Patch Quart's Request.get_json to be sync-callable so
        # request_parser.parse_body() works from sync route handlers.
        # (Same logic as server.py:_install_flask_shim — but only when
        # the original async coroutine function is still in place. If
        # server.py was imported by an earlier test, the sync wrapper
        # is already installed and we leave it alone.)
        from quart.wrappers import Request as _QR
        import inspect as _inspect
        if _inspect.iscoroutinefunction(_QR.get_json):
            _orig_get_json = _QR.get_json

            def _sync_get_json(self, *a, **kw):
                import asyncio as _a
                coro = _orig_get_json(self, *a, **kw)
                try:
                    loop = _a.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    fut = _a.ensure_future(coro)
                    while not fut.done():
                        loop._run_once()
                    return fut.result()
                return _a.run(coro)

            _QR.get_json = _sync_get_json

        # Force test mode: TUNNEL_TOKEN must be set so the auth
        # middleware actually rejects unauthenticated /api/v1/* calls.
        os.environ['TUNNEL_TOKEN'] = 'test-tunnel-token-not-real'

        from quart import Quart
        self.app = Quart(__name__)
        self.app.config['TESTING'] = True

        # Register the bearer auth hook + a couple of blueprints.
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        self.app.before_request(bearer_auth_before_request)
        self.app.after_request(attach_rate_headers)

        from routes.api_v1.capabilities import api_v1_capabilities_bp
        from routes.api_v1.keys import api_v1_keys_bp
        from routes.api_v1.folders import api_v1_folders_bp
        from routes.api_v1.optimizer import api_v1_optimizer_bp
        from routes.api_v1.scheduler import api_v1_scheduler_bp
        from routes.api_v1.endpoint import api_v1_endpoint_bp
        from routes.api_v1.swarm import api_v1_swarm_bp
        from routes.api_v1.desktop import api_v1_desktop_bp
        from routes.api_v1.browser import api_v1_browser_bp
        from routes.api_v1.memory import api_v1_memory_bp
        from routes.api_v1.skills import api_v1_skills_bp
        from routes.api_v1.mcp import api_v1_mcp_bp
        from routes.api_v1.daily_report import api_v1_daily_report_bp
        from routes.api_v1.oauth import api_v1_oauth_bp
        from routes.api_v1.project import api_v1_project_bp
        from routes.api_v1.translate import api_v1_translate_bp
        from routes.api_v1.artifacts import api_v1_artifacts_bp
        from routes.api_v1.uploads import api_v1_uploads_bp
        from routes.api_v1.paper import api_v1_paper_bp
        from routes.api_v1.common import api_v1_common_bp
        from routes.api_v1.config import api_v1_config_bp
        # Trigger the side-effect import that registers the routes on api_v1_config_bp.
        from routes import config as _legacy_config  # noqa: F401
        from routes.api_docs import api_docs_bp
        from routes.legacy_redirects import legacy_redirects_bp
        self.app.register_blueprint(api_v1_capabilities_bp)
        self.app.register_blueprint(api_v1_keys_bp)
        self.app.register_blueprint(api_v1_folders_bp)
        self.app.register_blueprint(api_v1_optimizer_bp)
        self.app.register_blueprint(api_v1_scheduler_bp)
        self.app.register_blueprint(api_v1_endpoint_bp)
        self.app.register_blueprint(api_v1_swarm_bp)
        self.app.register_blueprint(api_v1_desktop_bp)
        self.app.register_blueprint(api_v1_browser_bp)
        self.app.register_blueprint(api_v1_memory_bp)
        self.app.register_blueprint(api_v1_skills_bp)
        self.app.register_blueprint(api_v1_mcp_bp)
        self.app.register_blueprint(api_v1_daily_report_bp)
        self.app.register_blueprint(api_v1_oauth_bp)
        self.app.register_blueprint(api_v1_project_bp)
        self.app.register_blueprint(api_v1_translate_bp)
        self.app.register_blueprint(api_v1_artifacts_bp)
        self.app.register_blueprint(api_v1_uploads_bp)
        self.app.register_blueprint(api_v1_paper_bp)
        self.app.register_blueprint(api_v1_common_bp)
        self.app.register_blueprint(api_v1_config_bp)
        self.app.register_blueprint(api_docs_bp)
        self.app.register_blueprint(legacy_redirects_bp)

    def _patch_api_keys_path(self):
        from lib import api_keys
        self._orig_path = api_keys._STORE_PATH
        api_keys._STORE_PATH = os.path.join(self._tmp.name, 'api_keys.json')
        api_keys._cache.clear()
        api_keys._cache_loaded = False

    def cleanup(self):
        from lib import api_keys
        api_keys._STORE_PATH = self._orig_path
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        self._tmp.cleanup()


_FIXTURE = None


def _fixture():
    global _FIXTURE
    if _FIXTURE is None:
        _FIXTURE = _AppFixture()
    return _FIXTURE


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class IntegrationTest(unittest.TestCase):

    # The credential gate only rejects in private/multi-user mode; in 'open'
    # mode (the conftest default) unauthenticated /api/v1/* calls get a
    # synthetic principal and return 200. This file asserts the auth
    # contract, so the per-test conftest fixture forces private mode.
    pytestmark = pytest.mark.auth_mode('private')

    @classmethod
    def setUpClass(cls):
        cls.fix = _fixture()

    @classmethod
    def tearDownClass(cls):
        global _FIXTURE
        if _FIXTURE is not None:
            _FIXTURE.cleanup()
            _FIXTURE = None

    def setUp(self):
        # Fresh state on every test.
        from lib import api_keys
        api_keys._cache.clear()
        api_keys._cache_loaded = False

    def _client(self):
        return self.fix.app.test_client()

    def test_capabilities_is_public(self):
        async def go():
            r = await self._client().get('/api/v1/capabilities')
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertIn('models', body)
            self.assertIn('scopes', body)
            self.assertIn('config_schema', body)
            self.assertIn('admin', body['scopes'])
        _run(go())

    def test_openapi_json_lists_routes(self):
        async def go():
            r = await self._client().get('/api/openapi.json')
            self.assertEqual(r.status_code, 200)
            spec = json.loads(await r.get_data(as_text=True))
            self.assertEqual(spec['openapi'], '3.1.0')
            self.assertIn('/api/v1/capabilities', spec['paths'])
            self.assertIn('/api/v1/keys', spec['paths'])
            self.assertIn('bearerAuth',
                          spec['components']['securitySchemes'])
        _run(go())

    def test_openapi_yaml_returned(self):
        async def go():
            r = await self._client().get('/api/openapi.yaml')
            self.assertEqual(r.status_code, 200)
            text = await r.get_data(as_text=True)
            self.assertTrue(text.startswith('openapi:') or
                             text.startswith('# pip install pyyaml'))
        _run(go())

    def test_swagger_ui_renders(self):
        async def go():
            r = await self._client().get('/api/docs')
            self.assertEqual(r.status_code, 200)
            html = await r.get_data(as_text=True)
            self.assertIn('SwaggerUIBundle', html)
        _run(go())

    def test_keys_list_requires_admin(self):
        async def go():
            # No auth at all
            r = await self._client().get('/api/v1/keys')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_keys_create_with_admin_then_list(self):
        from lib.api_keys import create_key
        # Bootstrap an admin key directly (no HTTP) then use it.
        _row, admin_token = create_key(name='bootstrap',
                                       scopes=[], admin=True)

        async def go():
            cli = self._client()
            # Create another (regular) key via the admin token.
            r = await cli.post('/api/v1/keys',
                               headers={'Authorization': f'Bearer {admin_token}'},
                               json={'name': 'bot',
                                     'scopes': ['chat', 'tasks'],
                                     'rate_limit_rpm': 30})
            self.assertEqual(r.status_code, 201)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertTrue(body['token'].startswith('tofu_live_'))

            # List keys with the admin token.
            r = await cli.get('/api/v1/keys',
                              headers={'Authorization': f'Bearer {admin_token}'})
            self.assertEqual(r.status_code, 200)
            ldata = await r.get_json()
            names = [k['name'] for k in ldata['keys']]
            self.assertIn('bot', names)
            self.assertIn('bootstrap', names)

            # The new key (chat-scoped only) cannot list keys.
            child_token = body['token']
            r = await cli.get('/api/v1/keys',
                              headers={'Authorization': f'Bearer {child_token}'})
            self.assertEqual(r.status_code, 403)
        _run(go())

    def test_unknown_token_rejected(self):
        async def go():
            r = await self._client().get(
                '/api/v1/capabilities',
                headers={'Authorization': 'Bearer tofu_live_zzzz'},
            )
            # Wrong token is rejected at the auth layer regardless of
            # whether the route is public.
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_folders_crud_full_lifecycle(self):
        # Authenticate as admin so we can hit the auth-required folder routes,
        # but redirect the folders.json store at the v1 module to a tmp file
        # so we don't pollute production state.
        from lib.api_keys import create_key
        _row, token = create_key(name='folder-tester', scopes=[], admin=True)

        import importlib
        import os
        import tempfile
        folders_mod = importlib.import_module('routes.api_v1.folders')
        orig_path = folders_mod._FOLDERS_PATH
        tmp_dir = tempfile.mkdtemp()
        folders_mod._FOLDERS_PATH = os.path.join(tmp_dir, 'folders.json')

        try:
            async def go():
                cli = self._client()
                hdrs = {'Authorization': f'Bearer {token}'}

                # 0. Empty list at start.
                r = await cli.get('/api/v1/folders', headers=hdrs)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(await r.get_json(), [])

                # 1. Create.
                r = await cli.post('/api/v1/folders', headers=hdrs,
                                   json={'name': 'Work', 'color': '#f00'})
                self.assertEqual(r.status_code, 201)
                folder = await r.get_json()
                self.assertEqual(folder['name'], 'Work')
                self.assertEqual(folder['color'], '#f00')
                fid = folder['id']
                self.assertTrue(fid.startswith('f_'))

                # 2. Empty name → 400.
                r = await cli.post('/api/v1/folders', headers=hdrs,
                                   json={'name': '   '})
                self.assertEqual(r.status_code, 400)

                # 3. Update.
                r = await cli.put(f'/api/v1/folders/{fid}', headers=hdrs,
                                  json={'name': 'Personal', 'collapsed': True})
                self.assertEqual(r.status_code, 200)
                updated = await r.get_json()
                self.assertEqual(updated['name'], 'Personal')
                self.assertTrue(updated['collapsed'])

                # 4. Update unknown id → 404.
                r = await cli.put('/api/v1/folders/f_nope', headers=hdrs,
                                  json={'name': 'X'})
                self.assertEqual(r.status_code, 404)

                # 5. Reorder.
                r = await cli.post('/api/v1/folders', headers=hdrs,
                                   json={'name': 'Inbox'})
                second = (await r.get_json())['id']
                r = await cli.post('/api/v1/folders/reorder', headers=hdrs,
                                   json={'order': [second, fid]})
                self.assertEqual(r.status_code, 200)
                r = await cli.get('/api/v1/folders', headers=hdrs)
                lst = await r.get_json()
                order_map = {f['id']: f['order'] for f in lst}
                self.assertEqual(order_map[second], 0)
                self.assertEqual(order_map[fid], 1)

                # 6. Delete.
                r = await cli.delete(f'/api/v1/folders/{fid}', headers=hdrs)
                self.assertEqual(r.status_code, 200)
                r = await cli.delete(f'/api/v1/folders/{fid}', headers=hdrs)
                self.assertEqual(r.status_code, 404)
            _run(go())
        finally:
            folders_mod._FOLDERS_PATH = orig_path
            import shutil; shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_folders_requires_auth(self):
        async def go():
            r = await self._client().get('/api/v1/folders')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_legacy_folders_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/folders',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_optimizer_proposals_requires_auth(self):
        async def go():
            r = await self._client().get('/api/v1/optimizer/proposals')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_optimizer_proposals_list_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='opt-reader', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/optimizer/proposals',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertIn('proposals', body)
        _run(go())

    def test_optimizer_mutation_requires_admin(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='opt-non-admin', scopes=['chat'])
        async def go():
            r = await self._client().post(
                '/api/v1/optimizer/run-now',
                headers={'Authorization': f'Bearer {token}'},
                json={'dry_run': True})
            self.assertEqual(r.status_code, 403)
        _run(go())

    def test_scheduler_proactive_status_requires_auth(self):
        async def go():
            r = await self._client().get('/api/v1/scheduler/proactive/status')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_scheduler_proactive_status_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='sched-reader', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/scheduler/proactive/status',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertIn('proactive', body)
        _run(go())

    def test_scheduler_pause_requires_admin(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='sched-non-admin', scopes=['chat'])
        async def go():
            r = await self._client().post(
                '/api/v1/scheduler/tasks/abc123/pause',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 403)
        _run(go())

    def test_timer_list_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='timer-reader', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/timer/list',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertIn('timers', body)
            self.assertIn('active_count', body)
        _run(go())

    def test_endpoint_start_requires_auth(self):
        async def go():
            r = await self._client().post(
                '/api/v1/endpoint/start', json={'messages': []})
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_endpoint_start_rejects_missing_user_message(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='endpoint-tester', scopes=['chat'])
        async def go():
            r = await self._client().post(
                '/api/v1/endpoint/start',
                headers={'Authorization': f'Bearer {token}'},
                json={'messages': [
                    {'role': 'system', 'content': 'You are helpful'}
                ], 'config': {}})
            self.assertEqual(r.status_code, 400)
        _run(go())

    def test_endpoint_status_unknown_task_404(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='endpoint-status', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/endpoint/status/no-such-task',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_swarm_status_unknown_task(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='swarm-tester', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/swarm/status/no-such-task',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertFalse(body['active'])
        _run(go())

    def test_swarm_config_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='swarm-conf', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/swarm/config',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['available'])
            self.assertIn('roles', body)
        _run(go())

    def test_swarm_abort_requires_swarm_scope(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='swarm-no-scope', scopes=['chat'])
        async def go():
            r = await self._client().post(
                '/api/v1/swarm/abort/some-task',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 403)
        _run(go())

    def test_desktop_status_requires_auth(self):
        async def go():
            r = await self._client().get('/api/v1/desktop/status')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_desktop_status_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='desktop-tester', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/desktop/status',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertIn('connected', body)
            self.assertIn('last_poll', body)
            self.assertIn('pending_commands', body)
        _run(go())

    def test_browser_status_requires_auth(self):
        async def go():
            r = await self._client().get('/api/v1/browser/status')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_browser_status_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='browser-tester', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/browser/status',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertIn('connected', body)
            self.assertIn('clients', body)
            self.assertIn('pendingCommands', body)
        _run(go())

    def test_browser_clients_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='browser-clients', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/browser/clients',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertIsInstance(body['clients'], list)
        _run(go())

    def test_memory_list_requires_auth(self):
        async def go():
            r = await self._client().get('/api/v1/memory')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_memory_list_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='memory-tester', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/memory',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertIn('memories', body)
            # Post-split (pt_229606ca): the memory surface is memory-only —
            # the transient ``skills`` alias is gone; skills live at
            # /api/v1/skills.
            self.assertNotIn('skills', body)
        _run(go())

    def test_memory_get_unknown_404(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='memory-404', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/memory/no-such-id',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_memory_catalog_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='memory-catalog', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/skills/catalog',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertIn('catalog', body)
            self.assertIsInstance(body['catalog'], list)
        _run(go())

    def test_mcp_servers_requires_auth(self):
        async def go():
            r = await self._client().get('/api/v1/mcp/servers')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_mcp_servers_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='mcp-tester', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/mcp/servers',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertIn('servers', body)
            self.assertIsInstance(body['servers'], list)
        _run(go())

    def test_mcp_catalog_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='mcp-catalog', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/mcp/catalog',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertTrue(body['ok'])
            self.assertIn('catalog', body)
            self.assertIsInstance(body['catalog'], list)
        _run(go())

    def test_mcp_upsert_rejects_missing_name(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='mcp-upsert', scopes=['chat'])
        async def go():
            r = await self._client().post(
                '/api/v1/mcp/servers',
                headers={'Authorization': f'Bearer {token}'},
                json={'command': 'echo'})
            self.assertEqual(r.status_code, 400)
        _run(go())

    def test_daily_report_status_requires_auth(self):
        async def go():
            r = await self._client().get('/api/v1/daily-report/status/2026-05-29')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_daily_report_conv_count_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='daily-reader', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/daily-report/conv-count/2026-05-29',
                headers={'Authorization': f'Bearer {token}'})
            self.assertIn(r.status_code, (200, 404))
        _run(go())

    def test_oauth_status_requires_auth(self):
        async def go():
            r = await self._client().get('/api/v1/oauth/status')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_oauth_status_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='oauth-reader', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/oauth/status',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
        _run(go())

    def test_oauth_test_requires_admin(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='oauth-nonadmin', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/oauth/test',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 403)
        _run(go())

    def test_project_status_requires_auth(self):
        async def go():
            r = await self._client().get('/api/v1/project/status')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_project_status_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='project-reader', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/project/status',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
        _run(go())

    def test_project_set_rejects_empty_path(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='project-set', scopes=['chat'])
        async def go():
            r = await self._client().post(
                '/api/v1/project/set',
                headers={'Authorization': f'Bearer {token}'},
                json={'path': ''})
            self.assertEqual(r.status_code, 400)
        _run(go())

    def test_project_undo_rejects_no_active_project(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='project-undo', scopes=['chat'])
        async def go():
            r = await self._client().post(
                '/api/v1/project/undo',
                headers={'Authorization': f'Bearer {token}'},
                json={'taskId': 'abc'})
            # Either no active project (400) or unknown task — both are
            # acceptable; we only care it didn't 500.
            self.assertNotEqual(r.status_code, 500)
        _run(go())

    def test_translate_sync_requires_auth(self):
        async def go():
            r = await self._client().post('/api/v1/translate', json={'text': 'hi'})
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_translate_sync_empty_text(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='translate-tester', scopes=['chat'])
        async def go():
            r = await self._client().post(
                '/api/v1/translate',
                headers={'Authorization': f'Bearer {token}'},
                json={'text': ''})
            self.assertEqual(r.status_code, 400)
        _run(go())

    def test_translate_poll_not_found(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='translate-poll', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/translate/poll/no-such-task',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_artifacts_list_requires_auth(self):
        async def go():
            r = await self._client().get('/api/v1/artifacts')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_artifacts_list_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='artifact-tester', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/artifacts?limit=5',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            body = await r.get_json()
            self.assertIn('count', body)
            self.assertIn('artifacts', body)
        _run(go())

    def test_artifacts_get_unknown_404(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='artifact-404', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/artifacts/no-such-id',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_artifacts_scan_rejects_missing_conv_id(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='artifact-scan', scopes=['chat'])
        async def go():
            r = await self._client().post(
                '/api/v1/artifacts/scan',
                headers={'Authorization': f'Bearer {token}'},
                json={})
            self.assertEqual(r.status_code, 400)
        _run(go())

    def test_images_models_requires_auth(self):
        async def go():
            r = await self._client().get('/api/v1/images/models')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_images_models_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='img-models', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/images/models',
                headers={'Authorization': f'Bearer {token}'})
            # 200 OK or 503 if no provider configured — both are non-401.
            self.assertNotEqual(r.status_code, 401)
        _run(go())

    def test_legacy_images_generate_is_404(self):
        # /api/images/upload + /api/images/<file> stay as carve-outs;
        # /api/images/generate + /api/images/models migrated.
        async def go():
            r = await self._client().post(
                '/api/images/generate',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'},
                json={'prompt': 'x'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_pricing_v1_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='pricing-v1', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/pricing',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
        _run(go())

    def test_features_v1_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='features-v1', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/features',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
        _run(go())

    def test_provider_templates_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='templates-v1', scopes=['chat'])
        async def go():
            r = await self._client().get(
                '/api/v1/providers/templates',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
        _run(go())

    def test_legacy_server_config_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/server-config',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_provider_templates_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/provider-templates',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_chat_active_v1_requires_auth(self):
        async def go():
            r = await self._client().get('/api/v1/chat/active')
            self.assertEqual(r.status_code, 401)
        _run(go())

    def test_legacy_chat_active_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/chat/active',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_chat_queue_post_is_404(self):
        # Bare POST /api/chat/queue (manual enqueue) was deleted outright.
        async def go():
            r = await self._client().post(
                '/api/chat/queue',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'},
                json={'convId': 'x'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_pricing_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/pricing',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_me_stub_is_404(self):
        # /api/me deleted outright — use /api/v1/users/me instead.
        async def go():
            r = await self._client().get(
                '/api/me',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_conversations_search_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/conversations/search?q=test',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_artifacts_meta_is_404(self):
        # raw/view/export carve-outs stay; only meta/list/pin/delete/scan/versions migrated
        async def go():
            r = await self._client().get(
                '/api/artifacts/some-id',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_translate_is_404(self):
        async def go():
            r = await self._client().post(
                '/api/translate/start',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'},
                json={'text': 'hi'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_project_status_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/project/status',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_oauth_status_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/oauth/status',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_daily_report_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/daily-report/status/2026-05-29',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_mcp_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/mcp/servers',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_memory_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/memory',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_browser_status_is_404(self):
        # /api/browser/{poll,commands,result,download} stay as carve-outs;
        # /api/browser/{status,clients,test} were migrated.
        async def go():
            r = await self._client().get(
                '/api/browser/status',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_desktop_status_is_404(self):
        # /api/desktop/poll stays as a carve-out; only /api/desktop/status
        # was migrated.
        async def go():
            r = await self._client().get(
                '/api/desktop/status',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_swarm_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/swarm/config',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_endpoint_is_404(self):
        async def go():
            r = await self._client().post(
                '/api/endpoint/start',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'},
                json={'messages': []})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_scheduler_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/scheduler/proactive/status',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_timer_list_is_404(self):
        async def go():
            r = await self._client().get(
                '/api/timer/list',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 404)
        _run(go())

    def test_legacy_optimizer_redirects_to_v1(self):
        # The legacy /api/optimizer/* surface was removed in favour of
        # /api/v1/optimizer/*, but stale browser tabs still poll the
        # old URL. We serve a 308 (permanent, method-preserving) so
        # those tabs keep working AND we stop spamming the error log.
        async def go():
            r = await self._client().get(
                '/api/optimizer/proposals?limit=60',
                headers={'X-Tunnel-Token': 'test-tunnel-token-not-real'})
            self.assertEqual(r.status_code, 308)
            self.assertIn('/api/v1/optimizer/proposals',
                          r.headers.get('Location', ''))
            self.assertIn('limit=60', r.headers.get('Location', ''))
        _run(go())

    def test_whoami_with_token(self):
        from lib.api_keys import create_key
        _row, token = create_key(name='who', scopes=['chat'])

        async def go():
            r = await self._client().get(
                '/api/v1/keys/whoami',
                headers={'Authorization': f'Bearer {token}'})
            self.assertEqual(r.status_code, 200)
            data = await r.get_json()
            self.assertTrue(data['authenticated'])
            self.assertEqual(data['name'], 'who')
            self.assertIn('chat', data['scopes'])
        _run(go())


if __name__ == '__main__':
    unittest.main()
