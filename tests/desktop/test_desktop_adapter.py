"""tests/desktop/test_desktop_adapter.py — 订阅适配器服务器层守卫（E4 server 侧）。

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
import threading
import time
import unittest
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

from lib.desktop import adapter, egress

OWNER_ID = '1'


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
                agent_id='agent-1', user_id=OWNER_ID,
                target='loopback', loopback_port=8317)
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
                    agent_id='agent-1', user_id=OWNER_ID,
                    target='loopback', loopback_port=8317)
        self.assertEqual(send.call_count, 1)

    def test_egress_http_loopback_bad_port_refused_before_enqueue(self):
        with mock.patch('lib.desktop.send_desktop_command') as send:
            with self.assertRaises(egress.EgressUnavailable):
                egress.egress_http(
                    'http://127.0.0.1:9999/v1/models', method='GET',
                    agent_id='agent-1', user_id=OWNER_ID,
                    target='loopback', loopback_port=8317)
        self.assertFalse(send.called)


class TestRelayAndModels(unittest.TestCase):

    def test_relay_http_shape(self):
        with mock.patch.object(egress, 'egress_http',
                               return_value='RESP') as eh:
            out = adapter.relay_http('agent-1', 8317, '/v1/models',
                                     headers={'Authorization': 'Bearer k'},
                                     user_id=OWNER_ID)
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
            self.assertEqual(adapter.fetch_models(
                'a', 8317, 'k', user_id=OWNER_ID), ['m1', 'm2'])

    def test_fetch_models_empty_and_error(self):
        with mock.patch.object(adapter, 'relay_http',
                               return_value=self._models_resp([])):
            with self.assertRaises(RuntimeError):
                adapter.fetch_models('a', 8317, 'k', user_id=OWNER_ID)
        with mock.patch.object(adapter, 'relay_http',
                               return_value=self._models_resp([], status=401)):
            with self.assertRaises(RuntimeError):
                adapter.fetch_models('a', 8317, 'k', user_id=OWNER_ID)

    def _management_resp(self, data, status=200):
        return egress.EgressResponse(
            status=status, headers={}, content=json.dumps(data).encode())

    def test_accounts_are_sanitized_and_grouped_by_provider(self):
        raw = {'files': [
            {'name': 'claude-a.json', 'type': 'claude', 'email': 'a@x',
             'auth_index': 1, 'access_token': 'must-not-leak'},
            {'name': 'codex-b.json', 'provider': 'codex', 'email': 'b@x',
             'status': 'active'},
        ]}
        policy = {'port': 8317, 'mgmt_secret': 'secret', 'api_key': 'key'}
        with mock.patch.object(adapter, 'policy_for', return_value=policy), \
             mock.patch.object(adapter, 'relay_http',
                               return_value=self._management_resp(raw)) as relay:
            accounts = adapter.adapter_accounts(
                'acct-agent', user_id=OWNER_ID, force=True)
        self.assertEqual([a['provider'] for a in accounts], ['claude', 'codex'])
        self.assertFalse(any('access_token' in a for a in accounts))
        self.assertEqual(relay.call_args.kwargs['headers']['Authorization'],
                         'Bearer secret')

    def test_start_oauth_uses_provider_management_endpoint(self):
        policy = {'port': 8317, 'mgmt_secret': 'secret', 'api_key': 'key'}
        response = self._management_resp(
            {'status': 'ok', 'url': 'https://auth.example/', 'state': 'abc_123'})
        with mock.patch.object(adapter, 'policy_for', return_value=policy), \
             mock.patch.object(adapter, 'relay_http', return_value=response) as relay:
            out = adapter.start_adapter_oauth(
                'agent', 'codex', user_id=OWNER_ID)
        self.assertEqual(out['state'], 'abc_123')
        self.assertIn('/codex-auth-url?is_webui=true', relay.call_args.args[2])

    def test_oauth_success_syncs_provider_before_reporting_success(self):
        with mock.patch.object(adapter, '_management_request',
                               return_value={'status': 'ok'}), \
             mock.patch.object(adapter, 'sync_provider',
                               return_value={'provider_id': 'adapter_a',
                                             'models': 7}) as sync, \
             mock.patch.object(adapter, 'adapter_accounts', return_value=[]):
            out = adapter.adapter_oauth_status(
                'agent', 'state', agent_name='box', user_id=OWNER_ID)
        self.assertEqual(out['status'], 'ok')
        self.assertEqual(out['models'], 7)
        sync.assert_called_once()

    def test_running_status_self_heals_missing_provider(self):
        adapter._status_cache.invalidate((OWNER_ID, 'heal-agent'))
        with mock.patch('lib.desktop.send_desktop_command',
                        return_value=({'running': True, 'installed': True}, None)), \
             mock.patch.object(adapter, 'adapter_accounts', return_value=[
                 {'name': 'codex.json', 'provider': 'codex',
                  'disabled': False, 'unavailable': False}]), \
             mock.patch.object(adapter, '_adapter_provider_status',
                               side_effect=[
                                   {'provider_id': 'adapter_heal-age',
                                    'provider_ready': False, 'model_count': 0},
                                   {'provider_id': 'adapter_heal-age',
                                    'provider_ready': True, 'model_count': 8},
                               ]), \
             mock.patch.object(adapter, 'sync_provider',
                               return_value={'models': 8}) as sync:
            status = adapter.adapter_status(
                'heal-agent', agent_name='box', user_id=OWNER_ID)
        self.assertTrue(status['provider_ready'])
        self.assertEqual(status['provider_counts']['codex'], 1)
        sync.assert_called_once()

    def test_account_cache_is_partitioned_by_owner(self):
        first = {'files': [{'name': 'one.json', 'provider': 'codex'}]}
        second = {'files': [{'name': 'two.json', 'provider': 'claude'}]}
        with mock.patch.object(
                adapter, '_management_request', side_effect=[first, second]) as request:
            owner_one = adapter.adapter_accounts(
                'shared-agent-id', user_id='1', force=True)
            owner_two = adapter.adapter_accounts(
                'shared-agent-id', user_id='2')
        self.assertEqual(owner_one[0]['name'], 'one.json')
        self.assertEqual(owner_two[0]['name'], 'two.json')
        self.assertEqual(request.call_count, 2)


class TestProvisioning(unittest.TestCase):

    def setUp(self):
        from lib.model_routing import (
            InMemoryModelRoutingRepository,
            OwnerBoundary,
            empty_document,
        )
        self._repo = InMemoryModelRoutingRepository()
        self._boundary = OwnerBoundary.create(int(OWNER_ID))
        self._repo.compare_and_swap(
            self._boundary, empty_document(), expected_revision=0)

    def _run(self, fn, *a):
        with mock.patch('lib.llm_dispatch.reset_dispatcher', lambda: None):
            return fn(
                *a, user_id=OWNER_ID, repository=self._repo)

    def _load(self):
        return self._repo.get(self._boundary).document

    def test_provision_and_deprovision_roundtrip(self):
        self._run(adapter.provision_provider, 'agent-123456', 'box', 8317,
                  'ta_key', ['claude-opus-4-5', 'gpt-5.5'])
        document = self._load()
        provider = next(
            row for row in document['providers']
            if row['provider_id'] == 'adapter_agent-12')
        self.assertEqual(provider['scope'], 'owner')
        connection = document['connections'][0]
        self.assertEqual(connection['adapter'],
                         {'agent_id': 'agent-123456', 'port': 8317})
        self.assertEqual(connection['base_url'],
                         'http://127.0.0.1:8317/v1')
        self.assertTrue(adapter.is_adapter_provider(connection))
        self.assertEqual(
            sorted(row['pending_model_id'] for row in document['offerings']),
            ['claude-opus-4-5', 'gpt-5.5'])
        credential = document['credentials'][0]
        self.assertNotEqual(credential['secret_reference'], '')
        self.assertEqual(
            self._repo.resolve_secret(
                self._boundary, credential['secret_reference']),
            'ta_key')

        self.assertTrue(all('enabled' not in row for row in document['deployments']))
        self.assertEqual({row['probe_status'] for row in document['deployments']},
                         {'unprobed'})
        status = self._run(adapter._adapter_provider_status, 'agent-123456')
        self.assertTrue(status['provider_ready'])

        revision = self._repo.get(self._boundary).revision
        repeated = self._run(
            adapter.provision_provider, 'agent-123456', 'box', 8317,
            'ta_key', ['claude-opus-4-5', 'gpt-5.5'])
        self.assertFalse(repeated)
        self.assertEqual(self._repo.get(self._boundary).revision, revision)

        self._run(adapter.provision_provider, 'agent-123456', 'box', 8317,
                  'ta_key', ['m1'])
        document = self._load()
        self.assertEqual(sum(
            1 for row in document['providers']
            if row['provider_id'] == 'adapter_agent-12'), 1)
        self.assertEqual(
            [row['pending_model_id'] for row in document['offerings']], ['m1'])
        # Deprovision.
        removed = self._run(adapter.deprovision_provider, 'agent-123456')
        self.assertTrue(removed)
        self.assertFalse(any(
            row['provider_id'] == 'adapter_agent-12'
            for row in self._load()['providers']))


class TestEnsureTask(unittest.TestCase):

    def test_only_reconstructible_terminal_state_has_cache_bounds(self):
        self.assertEqual(adapter._status_cache.stats()['max_size'], 64)
        self.assertEqual(adapter._accounts_cache.stats()['max_size'], 64)
        self.assertEqual(adapter._ensure_tasks.stats()['max_size'], 128)

    def test_burst_is_not_rejected_or_evicted_by_terminal_cache_capacity(self):
        release = threading.Event()
        entered = {f'agent-{index}': threading.Event() for index in range(144)}
        calls = []
        def send(command, params, **kwargs):
            calls.append(kwargs['target_agent_id'])
            self.assertEqual(kwargs['user_id'], str(OWNER_ID))
            entered[kwargs['target_agent_id']].set()
            if not release.wait(5):
                raise RuntimeError('test did not release worker')
            return {'port': 8317, 'version': 'fixture'}, None
        with (
            mock.patch.object(adapter, '_ensure_tasks', adapter.TTLCache(ttl=60, max_size=1)),
            mock.patch.object(adapter, 'policy_for', return_value={
                'port': 8317, 'api_key': 'fixture', 'mgmt_secret': 'fixture'}),
            mock.patch('lib.desktop.send_desktop_command', side_effect=send),
            mock.patch.object(adapter, 'fetch_models', side_effect=RuntimeError('empty list')),
            mock.patch.object(adapter, 'deprovision_provider'),
        ):
            try:
                for agent in entered:
                    adapter.ensure_adapter(agent, user_id=OWNER_ID)
                self.assertTrue(all(event.wait(2) for event in entered.values()))
                self.assertEqual(adapter.ensure_task_state('agent-0', user_id=OWNER_ID)['state'], 'ensuring')
                self.assertEqual(adapter.ensure_adapter('agent-0', user_id=OWNER_ID)['state'], 'ensuring')
                self.assertEqual(len(calls), 144)
            finally:
                release.set()
                deadline = time.monotonic() + 3
                while any((str(OWNER_ID), agent) in adapter._active_ensure_tasks for agent in entered):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.005)

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
                cache_key = (OWNER_ID, 'agent-xyz')
                adapter._status_cache.set(
                    cache_key, {'ok': True, 'running': False})
                adapter._accounts_cache.set(cache_key, [])
                task = adapter.ensure_adapter(
                    'agent-xyz', agent_name='box', user_id=OWNER_ID)
                self.assertEqual(task.get('state'), 'ensuring')
                deadline = time.time() + 5
                while time.time() < deadline:
                    state = adapter.ensure_task_state(
                        'agent-xyz', user_id=OWNER_ID)
                    if state.get('state') != 'ensuring':
                        break
                    time.sleep(0.05)
                state = adapter.ensure_task_state(
                    'agent-xyz', user_id=OWNER_ID)
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
            self.assertNotIn(cache_key, adapter._status_cache)
            self.assertNotIn(cache_key, adapter._accounts_cache)

    def test_ensure_agent_error_surfaces(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(adapter, '_policy_path',
                               return_value=os.path.join(td, 'p.json')):
            with mock.patch('lib.desktop.send_desktop_command',
                            return_value=({'error': 'download failed'}, None)):
                adapter.ensure_adapter('agent-err', user_id=OWNER_ID)
                deadline = time.time() + 5
                while time.time() < deadline:
                    state = adapter.ensure_task_state(
                        'agent-err', user_id=OWNER_ID)
                    if state.get('state') != 'ensuring':
                        break
                    time.sleep(0.05)
            self.assertEqual(state.get('state'), 'error')
            self.assertIn('download failed', state.get('detail', ''))

    def test_first_run_without_accounts_is_ready_for_login(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(adapter, '_policy_path',
                               return_value=os.path.join(td, 'p.json')):
            ensure_result = {'port': 8317, 'version': 'v7', 'running': True}
            with mock.patch('lib.desktop.send_desktop_command',
                            return_value=(ensure_result, None)), \
                 mock.patch.object(adapter, 'fetch_models',
                                   side_effect=RuntimeError(
                                       'adapter /v1/models returned an empty list — no account')), \
                 mock.patch.object(adapter, 'deprovision_provider',
                                   return_value=False), \
                 mock.patch.object(adapter, 'provision_provider') as provision:
                adapter.ensure_adapter('agent-empty', user_id=OWNER_ID)
                deadline = time.time() + 5
                while time.time() < deadline:
                    state = adapter.ensure_task_state(
                        'agent-empty', user_id=OWNER_ID)
                    if state.get('state') != 'ensuring':
                        break
                    time.sleep(0.05)
        self.assertEqual(state.get('state'), 'ready')
        self.assertTrue(state.get('accounts_needed'))
        self.assertEqual(state.get('models'), 0)
        provision.assert_not_called()

    def test_stop_adapter_deprovisions(self):
        with mock.patch('lib.desktop.send_desktop_command',
                        return_value=({'running': False}, None)), \
             mock.patch.object(adapter, 'deprovision_provider',
                               return_value=True) as deprov:
            out = adapter.stop_adapter('agent-xyz', user_id=OWNER_ID)
        self.assertTrue(out.get('ok'))
        self.assertTrue(deprov.called)

    def test_stop_transport_failure_keeps_managed_provider(self):
        with mock.patch('lib.desktop.send_desktop_command',
                        return_value=(None, 'agent offline')), \
             mock.patch.object(adapter, 'deprovision_provider') as deprov:
            out = adapter.stop_adapter('agent-xyz', user_id=OWNER_ID)
        self.assertFalse(out.get('ok'))
        deprov.assert_not_called()

    def test_ownerless_adapter_command_fails_before_enqueue(self):
        with mock.patch('lib.desktop.send_desktop_command') as send:
            with self.assertRaises(ValueError):
                adapter.stop_adapter('agent-xyz', user_id='')
        send.assert_not_called()


class TestQuotaOpportunitySync(unittest.TestCase):
    """note_adapter_quota_observed：额度快照时机触发的节流目录刷新。"""

    def setUp(self):
        from lib.model_routing import (
            InMemoryModelRoutingRepository,
            OwnerBoundary,
            empty_document,
        )
        self._repo = InMemoryModelRoutingRepository()
        self._boundary = OwnerBoundary.create(int(OWNER_ID))
        self._repo.compare_and_swap(
            self._boundary, empty_document(), expected_revision=0)
        adapter._quota_sync_marks.clear()
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        patchers = [
            mock.patch.object(
                adapter, '_policy_path',
                return_value=os.path.join(self._td.name, 'p.json')),
            mock.patch('lib.model_routing.ModelRoutingRepository',
                       return_value=self._repo),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self._real_provision = adapter.provision_provider

    def _provision_into_repo(self, agent_id, agent_name, port, api_key,
                             model_ids, *, user_id):
        with mock.patch('lib.llm_dispatch.reset_dispatcher', lambda: None):
            return self._real_provision(
                agent_id, agent_name, port, api_key, model_ids,
                user_id=user_id, repository=self._repo)

    def _seed_provider(self, agent_id='agent-quota', agent_name='box',
                       models=('m1', 'm2')):
        adapter.policy_for(agent_id, create=True)
        self._provision_into_repo(agent_id, agent_name, 8317, 'ta_key',
                                  list(models), user_id=OWNER_ID)

    def _worker(self, agent_id='agent-quota'):
        adapter._quota_sync_worker(
            agent_id, adapter.policy_for(agent_id), user_id=OWNER_ID)

    def test_changed_models_reprovision_preserving_display_name(self):
        self._seed_provider()
        with mock.patch.object(adapter, 'fetch_models',
                               return_value=['m1', 'm2', 'm3-new']), \
             mock.patch.object(adapter, 'provision_provider',
                               side_effect=self._provision_into_repo) as prov:
            self._worker()
        prov.assert_called_once()
        self.assertEqual(prov.call_args[0][1], 'box')
        document = self._repo.get(self._boundary).document
        self.assertEqual(
            sorted(row['pending_model_id'] for row in document['offerings']),
            ['m1', 'm2', 'm3-new'])
        provider = next(row for row in document['providers']
                        if row['provider_id'] == 'adapter_agent-qu')
        self.assertEqual(provider['name'], '订阅适配器 · box')

    def test_unchanged_model_set_skips_reprovision(self):
        self._seed_provider()
        with mock.patch.object(adapter, 'fetch_models',
                               return_value=['m2', 'm1']), \
             mock.patch.object(adapter, 'provision_provider') as prov:
            self._worker()
        prov.assert_not_called()

    def test_unprovisioned_agent_provisions_on_first_observation(self):
        adapter.policy_for('agent-quota', create=True)
        with mock.patch.object(adapter, 'fetch_models',
                               return_value=['m1']), \
             mock.patch.object(adapter, 'provision_provider',
                               side_effect=self._provision_into_repo) as prov:
            self._worker()
        prov.assert_called_once()
        document = self._repo.get(self._boundary).document
        self.assertEqual(
            [row['pending_model_id'] for row in document['offerings']],
            ['m1'])

    def test_worker_failure_is_swallowed(self):
        self._seed_provider()
        with mock.patch.object(adapter, 'fetch_models',
                               side_effect=RuntimeError('agent offline')), \
             mock.patch.object(adapter, 'provision_provider') as prov:
            self._worker()
        prov.assert_not_called()

    def test_hook_throttles_one_attempt_per_ttl(self):
        adapter.policy_for('agent-quota', create=True)
        with mock.patch.object(threading, 'Thread') as thread_cls:
            adapter.note_adapter_quota_observed(
                'agent-quota', user_id=OWNER_ID)
            adapter.note_adapter_quota_observed(
                'agent-quota', user_id=OWNER_ID)
            adapter.note_adapter_quota_observed(
                'agent-other', user_id=OWNER_ID)
        self.assertEqual(thread_cls.call_count, 1)
        thread_cls.return_value.start.assert_called_once_with()

    def test_hook_without_policy_starts_no_thread(self):
        with mock.patch.object(threading, 'Thread') as thread_cls:
            adapter.note_adapter_quota_observed(
                'agent-never-seen', user_id=OWNER_ID)
        thread_cls.assert_not_called()

class TestRouteRegistration(unittest.TestCase):

    def test_blueprint_registered_in_v1(self):
        from routes.api_v1 import ALL_V1_BLUEPRINTS
        from routes.api_v1.adapter import api_v1_adapter_bp
        self.assertIn(api_v1_adapter_bp, ALL_V1_BLUEPRINTS)


if __name__ == '__main__':
    unittest.main()
