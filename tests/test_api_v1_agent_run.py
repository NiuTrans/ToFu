"""tests/test_api_v1_agent_run.py — End-to-end tests for the BYO surface.

Covers:
* POST /api/v1/providers — register, list, delete
* POST /api/v1/agent/run with inline ``model={base_url, api_key, id}``
  mints + disposes an ephemeral slot
* POST /api/v1/agent/run with ``model="foo@prov_xxx"`` resolves against
  the BYO store
* trajectory='sharegpt' produces a flattened result
* api_key is never echoed back in any response
"""

import asyncio
import os
import sys
import tempfile
import unittest


def _install_shim():
    import quart
    sys.modules['flask'] = quart
    for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
        qs = f'quart.{attr}'
        if qs in sys.modules:
            sys.modules[f'flask.{attr}'] = sys.modules[qs]
    # Newer Flask sansio (3.1+) reads config['PROVIDE_AUTOMATIC_OPTIONS']
    # in add_url_rule, but the installed Quart dropped it from
    # default_config → bare Quart(__name__) raises KeyError on
    # construction. server.py patches this at import time; replicate it
    # here so this module builds its own app standalone.
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    from quart.wrappers import Request as _QR
    import inspect
    if inspect.iscoroutinefunction(_QR.get_json):
        _orig = _QR.get_json

        def _sync_get_json(self, *a, **kw):
            import asyncio as _a
            coro = _orig(self, *a, **kw)
            return _a.run(coro)
        # Mirror server.py: stash the genuine async original so async
        # handlers (which call async_parse_body) recover + await it instead
        # of hitting this sync asyncio.run() shim from inside a running loop.
        _sync_get_json._genuine_async_get_json = _orig
        _QR.get_json = _sync_get_json


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class AgentRunRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_shim()
        cls._tmp = tempfile.TemporaryDirectory()

        # Isolate api_keys store
        from lib import api_keys, byo_providers
        cls._orig_keys = api_keys._STORE_PATH
        cls._orig_byo = byo_providers._STORE_PATH
        api_keys._STORE_PATH = os.path.join(cls._tmp.name, 'api_keys.json')
        byo_providers._STORE_PATH = os.path.join(cls._tmp.name, 'byo.json')
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        byo_providers._cache.clear()
        byo_providers._cache_loaded = False
        os.environ['TUNNEL_TOKEN'] = 'test-no-real'
        # These tests stub spawn_task and exercise the BYO surface /
        # mint-dispose mechanics — NOT endpoint reachability. The mint-time
        # TCP probe (added 2026-06) would otherwise make a real network call
        # to the sample sglang IP and time out in sandboxed CI. Disable it
        # here; reachability has its own dedicated test in test_ephemeral_slot.
        cls._orig_preflight = os.environ.get('TOFU_EPHEMERAL_PREFLIGHT')
        os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = '0'

        from quart import Quart
        cls.app = Quart(__name__)
        cls.app.config['TESTING'] = True

        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)

        from routes.api_v1.agent_run import api_v1_agent_run_bp
        from routes.api_v1.providers import api_v1_providers_bp
        cls.app.register_blueprint(api_v1_providers_bp)
        cls.app.register_blueprint(api_v1_agent_run_bp)

        # Mint a key with both scopes.
        from lib.api_keys import create_key
        _row, cls.token = create_key(
            name='byo-bot', scopes=['providers', 'agents:run'])

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys, byo_providers
        api_keys._STORE_PATH = cls._orig_keys
        byo_providers._STORE_PATH = cls._orig_byo
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        byo_providers._cache.clear()
        byo_providers._cache_loaded = False
        if cls._orig_preflight is None:
            os.environ.pop('TOFU_EPHEMERAL_PREFLIGHT', None)
        else:
            os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = cls._orig_preflight
        cls._tmp.cleanup()

    def setUp(self):
        from lib import byo_providers
        from lib.idempotency import _cache as _id_cache
        byo_providers._cache.clear()
        byo_providers._cache_loaded = False
        try:
            os.remove(byo_providers._STORE_PATH)
        except FileNotFoundError:
            pass
        _id_cache.clear()

        # The production `controller` counts in-flight slots in the SHARED
        # runtime_state_store (Build Order step 2). Reset it before AND after
        # each test so these route tests are insulated from any prior suite's
        # leaked count (e.g. test_admission's unbounded 1000) regardless of
        # run order — otherwise the cap-64 controller reads a polluted
        # in_flight and 503s every request.
        import lib.runtime_state_store as rss
        rss.reset_for_test()

        # Stub spawn_task so the orchestrator doesn't try to call out.
        import lib.tasks_pkg as pkg

        def _fake_spawn(task):
            task['content'] = 'hello from byo'
            task['status'] = 'done'
            task['finishReason'] = 'stop'
            task['usage'] = {'input_tokens': 5, 'output_tokens': 3,
                             'total_tokens': 8}
            from lib.tasks_pkg.manager import append_event
            append_event(task, {'type': 'delta', 'content': 'hello from byo'})
            append_event(task, {'type': 'done', 'finishReason': 'stop',
                                 'usage': task['usage']})

        self._orig_spawn = pkg.spawn_task
        pkg.spawn_task = _fake_spawn

    def tearDown(self):
        import lib.tasks_pkg as pkg
        pkg.spawn_task = self._orig_spawn
        # Leave the shared runtime_state_store clean so this suite never
        # leaks an in-flight count forward to whatever runs next.
        import lib.runtime_state_store as rss
        rss.reset_for_test()

    # ── Providers CRUD ──────────────────────────────────────────────

    def test_register_list_delete_provider(self):
        async def go():
            cli = self.app.test_client()
            # Create
            r = await cli.post(
                '/api/v1/providers',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'name': 'cluster-A',
                    'base_url': 'http://10.0.0.5:8080/v1',
                    'api_key': 'sk-internal-secret',
                    'models': [{'model_id': 'deepseek-v4-pro'}],
                    'auto_discover': False,
                })
            self.assertEqual(r.status_code, 201,
                              await r.get_data(as_text=True))
            body = await r.get_json()
            prov = body['provider']
            self.assertTrue(prov['id'].startswith('prov_'))
            # api_key is NOT echoed back in any form
            self.assertNotIn('api_key', prov)
            self.assertIn('key_hint', prov)
            self.assertNotIn('sk-internal-secret', str(body))

            prov_id = prov['id']

            # List
            r2 = await cli.get('/api/v1/providers',
                                headers={'Authorization': f'Bearer {self.token}'})
            self.assertEqual(r2.status_code, 200)
            d2 = await r2.get_json()
            self.assertEqual(len(d2['providers']), 1)
            self.assertEqual(d2['providers'][0]['id'], prov_id)

            # Delete
            r3 = await cli.delete(f'/api/v1/providers/{prov_id}',
                                    headers={'Authorization': f'Bearer {self.token}'})
            self.assertEqual(r3.status_code, 200)
            # Now list is empty
            r4 = await cli.get('/api/v1/providers',
                                headers={'Authorization': f'Bearer {self.token}'})
            self.assertEqual(len((await r4.get_json())['providers']), 0)
        _new_loop_run(go())

    # ── Agent/run with inline model ─────────────────────────────────

    def test_inline_provider_mints_and_disposes_ephemeral(self):
        async def go():
            from lib.llm_dispatch.ephemeral import count_ephemeral_slots
            n_before = count_ephemeral_slots()

            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': 'deepseek-v4-pro',
                    'provider': {
                        'base_url': 'http://33.236.230.114:8080/v1',
                        'api_key': 'sk-cluster-secret',
                    },
                    'messages': [{'role': 'user', 'content': 'hi'}],
                    'config': {'thinking': 'high', 'memory': True},
                    'timeout_s': 5,
                })
            self.assertEqual(r.status_code, 200,
                              await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertEqual(body['object'], 'agent.run')
            self.assertEqual(body['model'], 'deepseek-v4-pro')
            self.assertEqual(body['content'], 'hello from byo')
            # Secret never round-tripped
            text = await r.get_data(as_text=True)
            self.assertNotIn('sk-cluster-secret', text)

            # Ephemeral slot disposal happens in a daemon thread —
            # give it a moment then verify the count is back to baseline.
            import time as _time
            for _ in range(40):  # up to 4s
                if count_ephemeral_slots() == n_before:
                    break
                _time.sleep(0.1)
            self.assertEqual(count_ephemeral_slots(), n_before)

        _new_loop_run(go())

    def test_inline_provider_rejects_missing_url(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': 'deepseek-v4-pro',
                    'provider': {'api_key': 'x'},  # no base_url
                    'messages': [{'role': 'user', 'content': 'hi'}],
                })
            self.assertEqual(r.status_code, 400)
        _new_loop_run(go())

    def test_provider_block_with_suffix_is_ambiguous(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': 'foo@prov_xxx',
                    'provider': {'base_url': 'http://h:8/v1'},
                    'messages': [{'role': 'user', 'content': 'hi'}],
                })
            self.assertEqual(r.status_code, 400)
            body = await r.get_json()
            self.assertIn('cannot combine', str(body))
        _new_loop_run(go())

    def test_extra_headers_authorization_rejected(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': 'm',
                    'provider': {
                        'base_url': 'http://h:8/v1',
                        'api_key': '',
                        'extra_headers': {'Authorization': 'Bearer evil'},
                    },
                    'messages': [{'role': 'user', 'content': 'hi'}],
                })
            self.assertEqual(r.status_code, 400)
            body = await r.get_json()
            self.assertIn('reserved', str(body))
        _new_loop_run(go())

    def test_byo_suffix_resolves(self):
        async def go():
            cli = self.app.test_client()
            # First create a provider
            r1 = await cli.post(
                '/api/v1/providers',
                headers={'Authorization': f'Bearer {self.token}'},
                json={'name': 'C', 'base_url': 'http://10.0.0.6:8080/v1',
                      'api_key': '', 'auto_discover': False,
                      'models': [{'model_id': 'qwen3.5-FP8'}]})
            self.assertEqual(r1.status_code, 201)
            prov_id = (await r1.get_json())['provider']['id']

            # Use string suffix
            r2 = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': f'qwen3.5-FP8@{prov_id}',
                    'messages': [{'role': 'user', 'content': 'hi'}],
                    'timeout_s': 5,
                })
            self.assertEqual(r2.status_code, 200,
                              await r2.get_data(as_text=True))
            body = await r2.get_json()
            # Provider ID is exposed in the response (it's not secret)
            self.assertEqual(body.get('provider_id'), prov_id)
            self.assertEqual(body['model'], 'qwen3.5-FP8')
        _new_loop_run(go())

    def test_byo_suffix_unknown_provider_404(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': 'foo@prov_doesnotexist',
                    'messages': [{'role': 'user', 'content': 'hi'}],
                })
            self.assertEqual(r.status_code, 404)
        _new_loop_run(go())

    def test_trajectory_sharegpt_returned(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': 'global-only-model',  # plain string, no provider
                    'messages': [{'role': 'user', 'content': 'hi'}],
                    'trajectory': 'sharegpt',
                    'timeout_s': 5,
                })
            self.assertEqual(r.status_code, 200,
                              await r.get_data(as_text=True))
            body = await r.get_json()
            # Flat envelope: top-level trajectory_format + trajectory
            self.assertEqual(body['trajectory_format'], 'sharegpt')
            traj = body['trajectory']
            self.assertIsInstance(traj, list)
            roles = [r['from'] for r in traj]
            self.assertIn('human', roles)
            self.assertIn('gpt', roles)
            # Old nested envelope is gone
            self.assertNotIsInstance(body['trajectory'], dict)
        _new_loop_run(go())

    def test_unauthorized_without_scope(self):
        async def go():
            from lib.api_keys import create_key
            _row, no_scope_token = create_key(
                name='no-scope', scopes=['chat'])  # missing agents:run
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {no_scope_token}'},
                json={
                    'model': 'm',
                    'messages': [{'role': 'user', 'content': 'hi'}],
                })
            self.assertEqual(r.status_code, 403)
            body = await r.get_json()
            # 403 carries the structured fields a client can branch on
            # (top-level alongside the human-readable `error` string).
            self.assertEqual(body['missing_scope'], 'agents:run')
            self.assertIn('chat', body['granted_scopes'])
            self.assertEqual(body['required_scopes'], ['agents:run'])
        _new_loop_run(go())

    def test_config_aliases_and_raw_keys_coexist(self):
        async def go():
            cli = self.app.test_client()
            seen_cfg = {}

            import lib.tasks_pkg as pkg
            orig = pkg.spawn_task

            def _cap(task):
                # Capture the cfg the orchestrator would see.
                seen_cfg.update(task.get('config') or {})
                # And finish the task synthetically.
                return orig(task)

            pkg.spawn_task = _cap
            try:
                r = await cli.post(
                    '/api/v1/agent/run',
                    headers={'Authorization': f'Bearer {self.token}'},
                    json={
                        'model': 'm',
                        'messages': [{'role': 'user', 'content': 'hi'}],
                        'config': {
                            # Alias
                            'thinking': 'high',
                            'memory': True,
                            # Raw orchestrator key (unchanged passthrough)
                            'thinkingDepth': 'max',  # raw wins on conflict
                            'mySpecialKnob': 42,     # forward-compat
                        },
                        'timeout_s': 5,
                    })
                self.assertEqual(r.status_code, 200,
                                  await r.get_data(as_text=True))
            finally:
                pkg.spawn_task = orig
            # Alias expanded
            self.assertTrue(seen_cfg.get('memoryEnabled'))
            self.assertTrue(seen_cfg.get('thinkingEnabled'))
            # Raw key flowed through unchanged and overrode the alias
            self.assertEqual(seen_cfg.get('thinkingDepth'), 'max')
            # Unknown key passes through
            self.assertEqual(seen_cfg.get('mySpecialKnob'), 42)
        _new_loop_run(go())

    def test_legacy_capabilities_field_still_accepted(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': 'm',
                    'messages': [{'role': 'user', 'content': 'hi'}],
                    # Old `capabilities` shape still works.
                    'capabilities': {'thinking': 'medium'},
                    'timeout_s': 5,
                })
            self.assertEqual(r.status_code, 200,
                              await r.get_data(as_text=True))
        _new_loop_run(go())

    def test_deferred_finish_wakes_via_event(self):
        """The handler must await an event-driven wakeup, not poll: a task
        that finishes on a background thread AFTER the handler starts
        waiting still returns 200 (and promptly)."""
        async def go():
            import threading
            import time as _time
            import lib.tasks_pkg as pkg
            from lib.tasks_pkg.manager import append_event

            def _deferred_spawn(task):
                def _worker():
                    _time.sleep(0.3)
                    task['content'] = 'deferred hello'
                    task['status'] = 'done'
                    task['finishReason'] = 'stop'
                    task['usage'] = {'input_tokens': 1, 'output_tokens': 1,
                                     'total_tokens': 2}
                    append_event(task, {'type': 'done',
                                        'finishReason': 'stop',
                                        'usage': task['usage']})
                threading.Thread(target=_worker, daemon=True).start()

            pkg.spawn_task = _deferred_spawn
            try:
                cli = self.app.test_client()
                t0 = _time.time()
                r = await cli.post(
                    '/api/v1/agent/run',
                    headers={'Authorization': f'Bearer {self.token}'},
                    json={'model': 'm',
                          'messages': [{'role': 'user', 'content': 'hi'}],
                          'timeout_s': 5})
                elapsed = _time.time() - t0
                self.assertEqual(r.status_code, 200,
                                 await r.get_data(as_text=True))
                body = await r.get_json()
                self.assertEqual(body['content'], 'deferred hello')
                # Finished ~0.3s in; must not have spun the full 5s timeout.
                self.assertLess(elapsed, 3.0)
            finally:
                pkg.spawn_task = self._orig_spawn
        _new_loop_run(go())

    def test_stream_mode_emits_done(self):
        """Stream mode returns an SSE body that ends in [DONE] and carries
        the deltas — exercising the async event-driven generator end to end."""
        async def go():
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={'model': 'm',
                      'messages': [{'role': 'user', 'content': 'hi'}],
                      'stream': True, 'timeout_s': 5})
            self.assertEqual(r.status_code, 200)
            text = await r.get_data(as_text=True)
            self.assertIn('hello from byo', text)
            self.assertIn('[DONE]', text)
        _new_loop_run(go())

    def test_admission_503_when_saturated(self):
        """When the admission controller is at capacity the handler refuses
        with 503 rather than spawning unbounded work."""
        async def go():
            from lib.agent_core import admission
            import routes.api_v1.agent_run as ar
            # Force a saturated controller for the duration of this test.
            orig_ctrl = ar.controller
            saturated = admission.AdmissionController(max_inflight=1)
            self.assertTrue(saturated.try_acquire())  # consume the only slot
            ar.controller = saturated
            try:
                cli = self.app.test_client()
                r = await cli.post(
                    '/api/v1/agent/run',
                    headers={'Authorization': f'Bearer {self.token}'},
                    json={'model': 'm',
                          'messages': [{'role': 'user', 'content': 'hi'}]})
                self.assertEqual(r.status_code, 503,
                                 await r.get_data(as_text=True))
                body = await r.get_json()
                self.assertEqual(body.get('error_kind'), 'overloaded')
            finally:
                ar.controller = orig_ctrl
        _new_loop_run(go())


if __name__ == '__main__':
    unittest.main()
