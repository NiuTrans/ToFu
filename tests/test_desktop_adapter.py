"""tests/test_desktop_adapter.py — 订阅适配器服务器层守卫（E4 server 侧）。

Covers lib/desktop/adapter.py + the loopback target class in
lib/desktop/egress.py:

  * policy minting (random per-agent api-key/mgmt secret, idempotent,
    redacted public view);
  * loopback whitelist class (right port ok, everything else refused);
  * egress_http pinned-agent loopback relay (target param passthrough,
    no candidate chain);
  * relay_http URL/kwarg shape; fetch_models happy/empty/non-200;
  * provision/deprovision of the managed adapter_<id> provider;
  * ensure_adapter background task happy path (fake bridge);
  * stop_adapter stops + deprovisions.

Failure-first: lib/desktop/adapter.py did not exist before E4.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

from lib.desktop import adapter, egress


class TestPolicy(unittest.TestCase):

    def test_mint_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(adapter, '_policy_path',
                               return_value=os.path.join(td, 'p.json')):
            p1 = adapter.policy_for('agent-1', create=True)
            self.assertTrue(p1['api_key'].startswith('ta_'))
            self.assertEqual(len(p1['mgmt_secret']), 64)
            self.assertEqual(p1['port'], adapter.DEFAULT_PORT)
            p2 = adapter.policy_for('agent-1', create=True)
            self.assertEqual(p1['api_key'], p2['api_key'])  # stable, not reminted
            # A second agent gets a DIFFERENT key.
            p3 = adapter.policy_for('agent-2', create=True)
            self.assertNotEqual(p1['api_key'], p3['api_key'])

    def test_public_view_redacted(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(adapter, '_policy_path',
                               return_value=os.path.join(td, 'p.json')):
            adapter.policy_for('agent-1', create=True)
            pub = adapter.adapter_policy_public('agent-1')
            self.assertNotIn('api_key', pub)
            self.assertNotIn('mgmt_secret', pub)
            self.assertEqual(pub['port'], adapter.DEFAULT_PORT)


class TestLoopbackTargetServer(unittest.TestCase):

    def test_loopback_allowed_matrix(self):
        self.assertTrue(egress._loopback_allowed(
            'http://127.0.0.1:8317/v1/x', 8317))
        for bad in ('http://127.0.0.1:8318/v1/x',
                    'http://127.0.0.1/v1/x',
                    'http://192.168.1.5:8317/v1/x',
                    'https://api.anthropic.com/v1/x'):
            self.assertFalse(egress._loopback_allowed(bad, 8317), bad)

    def test_check_target_raises(self):
        with self.assertRaises(egress.EgressUnavailable):
            egress._check_target('http://127.0.0.1:9999/x', 'loopback', 8317)
        with self.assertRaises(egress.EgressUnavailable):
            egress._check_target('https://evil.com/x', 'subscription', 0)

    def test_egress_http_loopback_pins_agent_and_passes_target(self):
        good = {'status': 200, 'headers': {}, 'body_b64': '', 'elapsed_ms': 3}
        with mock.patch('lib.desktop.send_desktop_command',
                        return_value=(good, None)) as send:
            resp = egress.egress_http(
                'http://127.0.0.1:8317/v1/models', method='GET',
                agent_id='agent-1', target='loopback', loopback_port=8317)
        self.assertEqual(resp.status_code, 200)
        _args, kwargs = send.call_args
        self.assertEqual(kwargs.get('target_agent_id'), 'agent-1')
        params = send.call_args[0][1]
        self.assertEqual(params.get('target'), 'loopback')

    def test_egress_http_loopback_no_candidate_chain(self):
        # Even with a bridge failure, a pinned loopback relay must NOT roam
        # to another agent (a different machine = a different adapter with
        # different credentials).
        with mock.patch('lib.desktop.send_desktop_command',
                        return_value=(None, 'agent offline')) as send:
            with self.assertRaises(egress.EgressUnavailable):
                egress.egress_http(
                    'http://127.0.0.1:8317/v1/models', method='GET',
                    agent_id='agent-1', target='loopback', loopback_port=8317)
        self.assertEqual(send.call_count, 1)

    def test_egress_http_loopback_bad_port_refused_before_enqueue(self):
        with mock.patch('lib.desktop.send_desktop_command') as send:
            with self.assertRaises(egress.EgressUnavailable):
                egress.egress_http(
                    'http://127.0.0.1:9999/v1/models', method='GET',
                    agent_id='agent-1', target='loopback', loopback_port=8317)
        self.assertFalse(send.called)


class TestRelayAndModels(unittest.TestCase):

    def test_relay_http_shape(self):
        with mock.patch.object(egress, 'egress_http',
                               return_value='RESP') as eh:
            out = adapter.relay_http('agent-1', 8317, '/v1/models',
                                     headers={'Authorization': 'Bearer k'})
        self.assertEqual(out, 'RESP')
        _args, kwargs = eh.call_args
        self.assertEqual(kwargs.get('agent_id'), 'agent-1')
        self.assertEqual(kwargs.get('target'), 'loopback')
        self.assertEqual(kwargs.get('loopback_port'), 8317)
        self.assertEqual(eh.call_args[0][0], 'http://127.0.0.1:8317/v1/models')

    def _models_resp(self, ids, status=200):
        return egress.EgressResponse(
            status=status, headers={},
            content=json.dumps({'data': [{'id': i} for i in ids]}).encode())

    def test_fetch_models_happy(self):
        with mock.patch.object(adapter, 'relay_http',
                               return_value=self._models_resp(['m1', 'm2'])):
            self.assertEqual(adapter.fetch_models('a', 8317, 'k'), ['m1', 'm2'])

    def test_fetch_models_empty_and_error(self):
        with mock.patch.object(adapter, 'relay_http',
                               return_value=self._models_resp([])):
            with self.assertRaises(RuntimeError):
                adapter.fetch_models('a', 8317, 'k')
        with mock.patch.object(adapter, 'relay_http',
                               return_value=self._models_resp([], status=401)):
            with self.assertRaises(RuntimeError):
                adapter.fetch_models('a', 8317, 'k')


class TestProvisioning(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cfg = os.path.join(self._tmp.name, 'server_config.json')
        with open(self._cfg, 'w') as f:
            json.dump({'providers': []}, f)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, fn, *a):
        with mock.patch('lib._SERVER_CONFIG_PATH', self._cfg), \
             mock.patch('lib.reload_config', lambda: None), \
             mock.patch('lib.llm_dispatch.reset_dispatcher', lambda: None):
            return fn(*a)

    def _load(self):
        with open(self._cfg) as f:
            return json.load(f)

    def test_provision_and_deprovision_roundtrip(self):
        self._run(adapter.provision_provider, 'agent-123456', 'box', 8317,
                  'ta_key', ['claude-opus-4-5', 'gpt-5.5'])
        cfg = self._load()
        prov = next(p for p in cfg['providers'] if p['id'] == 'adapter_agent-12')
        self.assertEqual(prov['adapter'],
                         {'agent_id': 'agent-123456', 'port': 8317})
        self.assertEqual(prov['api_keys'], ['ta_key'])
        self.assertEqual(prov['base_url'], 'http://127.0.0.1:8317/v1')
        self.assertEqual([m['model_id'] for m in prov['models']],
                         ['claude-opus-4-5', 'gpt-5.5'])
        self.assertTrue(adapter.is_adapter_provider(prov))
        # Idempotent re-provision.
        self._run(adapter.provision_provider, 'agent-123456', 'box', 8317,
                  'ta_key', ['m1'])
        cfg = self._load()
        self.assertEqual(sum(1 for p in cfg['providers']
                             if p['id'] == 'adapter_agent-12'), 1)
        # Deprovision.
        removed = self._run(adapter.deprovision_provider, 'agent-123456')
        self.assertTrue(removed)
        self.assertFalse(any(p['id'] == 'adapter_agent-12'
                             for p in self._load()['providers']))


class TestEnsureTask(unittest.TestCase):

    def test_ensure_happy_path_background(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(adapter, '_policy_path',
                               return_value=os.path.join(td, 'p.json')):
            ensure_result = {'port': 8317, 'version': 'v7.2.116',
                             'running': True}
            with mock.patch('lib.desktop.send_desktop_command',
                            return_value=(ensure_result, None)) as send, \
                 mock.patch.object(adapter, 'fetch_models',
                                   return_value=['m1', 'm2', 'm3']), \
                 mock.patch.object(adapter, 'provision_provider',
                                   return_value=True) as prov:
                task = adapter.ensure_adapter('agent-xyz', agent_name='box')
                self.assertEqual(task.get('state'), 'ensuring')
                deadline = time.time() + 5
                while time.time() < deadline:
                    state = adapter.ensure_task_state('agent-xyz')
                    if state.get('state') != 'ensuring':
                        break
                    time.sleep(0.05)
                state = adapter.ensure_task_state('agent-xyz')
            self.assertEqual(state.get('state'), 'ready')
            self.assertEqual(state.get('models'), 3)
            self.assertEqual(state.get('version'), 'v7.2.116')
            # adapter_ensure went to the right agent with minted credentials.
            _args, kwargs = send.call_args
            self.assertEqual(kwargs.get('target_agent_id'), 'agent-xyz')
            params = send.call_args[0][1]
            self.assertTrue(params['api_key'].startswith('ta_'))
            self.assertTrue(params['mgmt_secret'])
            self.assertEqual(kwargs.get('ttl'), 600)
            self.assertTrue(prov.called)

    def test_ensure_agent_error_surfaces(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(adapter, '_policy_path',
                               return_value=os.path.join(td, 'p.json')):
            with mock.patch('lib.desktop.send_desktop_command',
                            return_value=({'error': 'download failed'}, None)):
                adapter.ensure_adapter('agent-err')
                deadline = time.time() + 5
                while time.time() < deadline:
                    state = adapter.ensure_task_state('agent-err')
                    if state.get('state') != 'ensuring':
                        break
                    time.sleep(0.05)
            self.assertEqual(state.get('state'), 'error')
            self.assertIn('download failed', state.get('detail', ''))

    def test_stop_adapter_deprovisions(self):
        with mock.patch('lib.desktop.send_desktop_command',
                        return_value=({'running': False}, None)), \
             mock.patch.object(adapter, 'deprovision_provider',
                               return_value=True) as deprov:
            out = adapter.stop_adapter('agent-xyz')
        self.assertTrue(out.get('ok'))
        self.assertTrue(deprov.called)


class TestRouteRegistration(unittest.TestCase):

    def test_blueprint_registered_in_v1(self):
        from routes.api_v1 import ALL_V1_BLUEPRINTS
        from routes.api_v1.adapter import api_v1_adapter_bp
        self.assertIn(api_v1_adapter_bp, ALL_V1_BLUEPRINTS)


if __name__ == '__main__':
    unittest.main()
