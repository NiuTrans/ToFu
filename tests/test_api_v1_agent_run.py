"""End-to-end tests for the native agent runtime and model-routing v2.

Covers:
* CAS create/list/delete of one ProviderAccess aggregate
* official and provider-scoped structured model selection
* explicit rejection of removed inline/suffixed provider selectors
* request-scoped route-slot disposal
* trajectory='sharegpt' produces a flattened result
* credential secrets are never echoed back in any response
"""

pytest_plugins = ('tests._credential_sidecar',)

import asyncio
import os
import sys
import unittest

import pytest

from tests.support.model_routing import (
    allow_native_test_endpoint,
    native_test_model,
    native_test_provider_bundle,
    reset_native_test_model_route,
)


pytestmark = pytest.mark.unit


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class AgentRunRouteTest(unittest.TestCase):

    OWNER_USER_ID = 12_010

    @classmethod
    def setUpClass(cls):
        # These tests stub spawn_task and exercise the BYO surface /
        # mint-dispose mechanics — NOT endpoint reachability. The mint-time
        # TCP probe (added 2026-06) would otherwise make a real network call
        # to the sample sglang IP and time out in sandboxed CI. Disable it
        # here; reachability has its own dedicated test in test_ephemeral_slot.
        cls._orig_preflight = os.environ.get('TOFU_EPHEMERAL_PREFLIGHT')
        os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = '0'
        cls._endpoint_allowance = allow_native_test_endpoint()
        cls._endpoint_allowance.__enter__()

        from quart import Quart
        cls.app = Quart(__name__, static_folder=None)
        cls.app.config.setdefault('PROVIDE_AUTOMATIC_OPTIONS', True)
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
        _row, cls.token = create_key(owner_user_id=cls.OWNER_USER_ID,
            name='byo-bot', scopes=['providers', 'agents:run'])

    @classmethod
    def tearDownClass(cls):
        cls._endpoint_allowance.__exit__(None, None, None)
        if cls._orig_preflight is None:
            os.environ.pop('TOFU_EPHEMERAL_PREFLIGHT', None)
        else:
            os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = cls._orig_preflight

    def setUp(self):
        from lib.idempotency import _cache as _id_cache
        # Each method starts from the same owner-scoped CAS authority. The
        # helper also reclaims encrypted secrets left by an interrupted test.
        reset_native_test_model_route(owner_user_id=self.OWNER_USER_ID)
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
        import lib.tasks_pkg.spawn as pkg

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
        import lib.tasks_pkg.spawn as pkg
        pkg.spawn_task = self._orig_spawn
        # Leave the shared runtime_state_store clean so this suite never
        # leaks an in-flight count forward to whatever runs next.
        import lib.runtime_state_store as rss
        rss.reset_for_test()

    # ── Providers CRUD ──────────────────────────────────────────────

    def test_register_list_delete_provider(self):
        async def go():
            cli = self.app.test_client()
            headers = {'Authorization': f'Bearer {self.token}'}
            authority = await cli.get('/api/v1/model-routing', headers=headers)
            revision = (await authority.get_json())['revision']

            r = await cli.post(
                '/api/v1/providers',
                headers=headers,
                json=native_test_provider_bundle(
                    expected_revision=revision,
                    extra_headers={'X-Internal-Tag': 'agent-route-test'}))
            self.assertEqual(r.status_code, 201,
                              await r.get_data(as_text=True))
            body = await r.get_json()
            prov = body['provider']
            self.assertEqual(prov['provider']['provider_id'], 'extra-provider')
            # Credential material is never returned by any aggregate view.
            self.assertNotIn('api_key', prov)
            self.assertTrue(prov['credentials'][0]['key_hint'])
            self.assertNotIn('sk-internal-secret', str(body))

            prov_id = prov['provider']['provider_id']

            r2 = await cli.get('/api/v1/providers', headers=headers)
            self.assertEqual(r2.status_code, 200)
            d2 = await r2.get_json()
            listed_ids = {
                item['provider']['provider_id'] for item in d2['providers']
            }
            self.assertIn(prov_id, listed_ids)
            self.assertNotIn('sk-internal-secret', str(d2))

            r3 = await cli.delete(
                f'/api/v1/providers/{prov_id}',
                headers=headers,
                json={'expected_revision': body['revision']})
            self.assertEqual(r3.status_code, 200)
            r4 = await cli.get('/api/v1/providers', headers=headers)
            remaining_ids = {
                item['provider']['provider_id']
                for item in (await r4.get_json())['providers']
            }
            self.assertNotIn(prov_id, remaining_ids)
        _new_loop_run(go())

    # ── Agent/run with model-routing v2 ──────────────────────────────

    def test_structured_model_mints_and_disposes_ephemeral(self):
        async def go():
            from lib.llm_dispatch.ephemeral import count_ephemeral_slots
            n_before = count_ephemeral_slots()

            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': native_test_model(),
                    'messages': [{'role': 'user', 'content': 'hi'}],
                    'config': {'thinking': 'high', 'memory': True},
                    'timeout_s': 5,
                })
            self.assertEqual(r.status_code, 200,
                              await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertEqual(body['object'], 'agent.run')
            self.assertEqual(body['model'], 'stub-model')
            self.assertEqual(body['content'], 'hello from byo')

            # Ephemeral slot disposal happens in a daemon thread —
            # give it a moment then verify the count is back to baseline.
            import time as _time
            for _ in range(40):  # up to 4s
                if count_ephemeral_slots() == n_before:
                    break
                _time.sleep(0.1)
            self.assertEqual(count_ephemeral_slots(), n_before)

        _new_loop_run(go())

    def test_removed_inline_provider_is_rejected_without_echoing_secret(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': native_test_model(),
                    'provider': {'api_key': 'must-not-echo'},
                    'messages': [{'role': 'user', 'content': 'hi'}],
                })
            self.assertEqual(r.status_code, 400)
            body = await r.get_json()
            self.assertEqual(
                body['error_kind'], 'legacy_inline_provider_removed')
            self.assertNotIn('must-not-echo', str(body))
        _new_loop_run(go())

    def test_legacy_model_suffix_is_rejected(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': 'foo@prov_xxx',
                    'messages': [{'role': 'user', 'content': 'hi'}],
                })
            self.assertEqual(r.status_code, 400)
            body = await r.get_json()
            self.assertEqual(body['error_kind'], 'legacy_model_selector_removed')
        _new_loop_run(go())

    def test_reserved_connection_header_is_rejected_before_persistence(self):
        async def go():
            cli = self.app.test_client()
            headers = {'Authorization': f'Bearer {self.token}'}
            authority = await cli.get('/api/v1/model-routing', headers=headers)
            revision = (await authority.get_json())['revision']
            provider = native_test_provider_bundle(
                expected_revision=revision)
            provider['connections'][0]['extra_headers'] = {
                'Authorization': 'Bearer evil',
            }
            r = await cli.post(
                '/api/v1/providers', headers=headers, json=provider)
            self.assertEqual(r.status_code, 400)
            body = await r.get_json()
            self.assertIn('reserved', str(body))
            self.assertNotIn('Bearer evil', str(body))
        _new_loop_run(go())

    def test_provider_scoped_offering_resolves(self):
        async def go():
            cli = self.app.test_client()
            r2 = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': {
                        'provider_id': 'test-provider',
                        'offering_id': 'test-offering',
                    },
                    'messages': [{'role': 'user', 'content': 'hi'}],
                    'timeout_s': 5,
                })
            self.assertEqual(r2.status_code, 200,
                              await r2.get_data(as_text=True))
            body = await r2.get_json()
            self.assertEqual(body.get('provider_id'), 'test-provider')
            # A confirmed provider-scoped offering projects its official
            # model identity, rather than an empty pending-model placeholder.
            self.assertEqual(body['model'], 'stub-model')
        _new_loop_run(go())

    def test_unknown_provider_scoped_offering_is_typed_bad_request(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': {
                        'provider_id': 'provider-does-not-exist',
                        'offering_id': 'offering-does-not-exist',
                    },
                    'messages': [{'role': 'user', 'content': 'hi'}],
                })
            self.assertEqual(r.status_code, 400)
            body = await r.get_json()
            self.assertEqual(body['error_kind'], 'offering_not_found')
        _new_loop_run(go())

    def test_trajectory_sharegpt_returned(self):
        async def go():
            cli = self.app.test_client()
            r = await cli.post(
                '/api/v1/agent/run',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': native_test_model(),
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
            _row, no_scope_token = create_key(owner_user_id=1, 
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

            import lib.tasks_pkg.spawn as pkg
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
                        'model': native_test_model(),
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
                    'model': native_test_model(),
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
            import lib.tasks_pkg.spawn as pkg
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
                    json={'model': native_test_model(),
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
                json={'model': native_test_model(),
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
            import lib.agent_core.admission as admission
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
                    json={'model': native_test_model(),
                          'messages': [{'role': 'user', 'content': 'hi'}]})
                self.assertEqual(r.status_code, 503,
                                 await r.get_data(as_text=True))
                body = await r.get_json()
                self.assertEqual(body.get('error_kind'), 'overloaded')
            finally:
                ar.controller = orig_ctrl
        _new_loop_run(go())


if __name__ == '__main__':
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_api_v1_agent_run.py')
    unittest.main()
