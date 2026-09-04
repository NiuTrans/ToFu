"""tests/test_oauth_outbound.py — subscription-OAuth outbound bridge tests.

Covers lib/oauth/outbound: live-token + identity-header + body resolution
for Claude / Codex, the Claude ``?beta=true`` URL helper, and the managed
model-routing v2 ProviderAccess provision/deprovision round-trip.
"""

from __future__ import annotations

import unittest
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

import lib.oauth.outbound as outbound
from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary


class TestClaudeResolve(unittest.TestCase):

    def test_resolves_headers_without_mutating_messages(self):
        # Since S1 (2026 cloaking port) the system structure is owned by
        # apply_claude_cloak at the Anthropic-body boundary — resolve only
        # swaps in the live token + the identity HEADER suite.
        body = {'messages': [{'role': 'user', 'content': 'hi'}]}
        with mock.patch('lib.oauth.claude.claude_get_valid_token',
                        return_value='sk-ant-oat01-AAA') as get_token:
            key, hdrs, out = outbound.resolve_oauth_request(
                'claude', body, None, user_id='41')
        self.assertEqual(key, 'sk-ant-oat01-AAA')
        self.assertEqual(out['messages'], [{'role': 'user', 'content': 'hi'}])
        self.assertIn('claude-code-20250219', hdrs['anthropic-beta'])
        self.assertIn('oauth-2025-04-20', hdrs['anthropic-beta'])
        self.assertEqual(hdrs['x-app'], 'cli')
        self.assertTrue(hdrs['User-Agent'].startswith('claude-cli/'))
        get_token.assert_called_once_with(user_id='41')

    def test_stream_preflight_forwards_owner_to_oauth_refresh(self):
        from lib.llm._sse_core import prepare_request

        captured = {}

        def _resolve(oauth, body, extra_headers, user_id=''):
            captured.update(oauth=oauth, user_id=user_id)
            return 'token', {}, body

        with mock.patch(
                'lib.oauth.outbound.resolve_oauth_request', _resolve):
            prepare_request(
                {'model': 'claude-sonnet-4',
                 'messages': [{'role': 'user', 'content': 'hi'}],
                 'stream': True},
                api_key='stale', base_url='https://api.anthropic.com/v1',
                api_protocol='anthropic', oauth='claude',
                owner_user_id=41,
            )

        self.assertEqual(captured, {'oauth': 'claude', 'user_id': '41'})

    def test_merge_betas_leads_with_mandatory(self):
        body = {'messages': []}
        with mock.patch('lib.oauth.claude.claude_get_valid_token',
                        return_value='t'):
            _key, hdrs, _out = outbound.resolve_oauth_request(
                'claude', body, {'anthropic-beta': 'extended-cache-ttl-2025-04-11'})
        betas = hdrs['anthropic-beta'].split(',')
        self.assertEqual(betas[0], 'claude-code-20250219')
        self.assertEqual(betas[1], 'oauth-2025-04-20')
        self.assertIn('extended-cache-ttl-2025-04-11', betas)

    def test_no_token_raises(self):
        with mock.patch('lib.oauth.claude.claude_get_valid_token',
                        return_value=None):
            with self.assertRaises(RuntimeError):
                outbound.resolve_oauth_request('claude', {'messages': []}, None)

    def test_claude_url_appends_beta(self):
        self.assertEqual(
            outbound.claude_oauth_url('https://api.anthropic.com/v1/messages'),
            'https://api.anthropic.com/v1/messages?beta=true')
        # Idempotent when already present.
        self.assertEqual(
            outbound.claude_oauth_url('https://x/messages?beta=true'),
            'https://x/messages?beta=true')


class TestCodexResolve(unittest.TestCase):

    def test_identity_headers_and_account_id(self):
        body = {'messages': [], '_conv_id': 'private-conversation-id'}
        with mock.patch('lib.oauth.codex.codex_get_valid_token',
                        return_value='access-tok'), \
             mock.patch('lib.oauth.token_store.load_token',
                        return_value={'account_id': 'acc-xyz'}):
            key, hdrs, _out = outbound.resolve_oauth_request(
                'codex', body, None)
        self.assertEqual(key, 'access-tok')
        self.assertEqual(hdrs['originator'], 'codex-tui')
        self.assertTrue(hdrs['User-Agent'].startswith('codex-tui/0.146.0'))
        self.assertEqual(hdrs['OpenAI-Beta'], 'responses=experimental')
        self.assertEqual(hdrs['chatgpt-account-id'], 'acc-xyz')
        self.assertRegex(
            hdrs['session-id'],
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
        self.assertNotIn('private-conversation-id', hdrs['session-id'])
        self.assertEqual(hdrs['thread-id'], hdrs['session-id'])
        self.assertEqual(hdrs['x-client-request-id'], hdrs['session-id'])
        self.assertNotIn('session_id', hdrs)
        from lib.llm.responses_outbound import openai_body_to_responses
        wire, _reverse = openai_body_to_responses(
            {'model': 'gpt-5.6-luna', **body}, profile='codex')
        self.assertEqual(wire['prompt_cache_key'], hdrs['session-id'])

    def test_cache_affinity_headers_are_stable_per_conversation(self):
        def resolve(conv_id):
            with mock.patch('lib.oauth.codex.codex_get_valid_token',
                            return_value='access-tok'), \
                 mock.patch('lib.oauth.token_store.load_token',
                            return_value={}):
                return outbound.resolve_oauth_request(
                    'codex', {'messages': [], '_conv_id': conv_id}, None)[1]

        first = resolve('conv-a')
        same = resolve('conv-a')
        different = resolve('conv-b')

        self.assertEqual(first['session-id'], same['session-id'])
        self.assertNotEqual(first['session-id'], different['session-id'])

    def test_no_token_raises(self):
        with mock.patch('lib.oauth.codex.codex_get_valid_token', return_value=None):
            with self.assertRaises(RuntimeError):
                outbound.resolve_oauth_request('codex', {'messages': []}, None)


class TestProvisioning(unittest.TestCase):

    def setUp(self):
        self.repository = InMemoryModelRoutingRepository()
        self.boundary = OwnerBoundary.create(41)

    def _run(self, fn, *a):
        with mock.patch('lib.llm_dispatch.reset_dispatcher', lambda: None):
            return fn(
                *a,
                owner_user_id=self.boundary.owner_user_id,
                repository=self.repository,
            )

    def _load(self):
        return self.repository.get(self.boundary).document

    @staticmethod
    def _provider_models(document, provider_id):
        access_ids = {
            row['provider_access_id'] for row in document['provider_accesses']
            if row['provider_id'] == provider_id
        }
        offerings = [
            row for row in document['offerings']
            if row['provider_access_id'] in access_ids
        ]
        return [
            str((row.get('model') or {}).get('model_id')
                or row.get('pending_model_id') or '')
            for row in offerings
        ]

    def test_provision_adds_managed_provider(self):
        ok = self._run(outbound.provision_oauth_provider, 'codex')
        self.assertTrue(ok)
        document = self._load()
        ids = [p['provider_id'] for p in document['providers']]
        self.assertIn('oauth_codex', ids)
        access_id = next(
            row['provider_access_id'] for row in document['provider_accesses']
            if row['provider_id'] == 'oauth_codex')
        credentials = [
            row for row in document['credentials']
            if row['provider_access_id'] == access_id
        ]
        self.assertEqual([row['kind'] for row in credentials], ['oauth'])
        self.assertTrue(credentials[0]['secret_reference'])

    def test_provision_is_idempotent(self):
        self._run(outbound.provision_oauth_provider, 'codex')
        self._run(outbound.provision_oauth_provider, 'codex')
        document = self._load()
        self.assertEqual(sum(
            1 for row in document['providers']
            if row['provider_id'] == 'oauth_codex'), 1)
        self.assertEqual(document['revision'], 1)

    def test_cached_catalog_replaces_static_codex_table(self):
        dynamic = [{
            'model_id': 'gpt-from-live-catalog',
            'capabilities': ['text', 'thinking'],
            'thinking_default': True,
            'catalog_visibility': 'list',
        }]
        with mock.patch(
                'lib.oauth.codex_catalog.cached_codex_provider_models',
                return_value=dynamic):
            self._run(outbound.provision_oauth_provider, 'codex')
        self.assertEqual(
            self._provider_models(self._load(), 'oauth_codex'),
            ['gpt-from-live-catalog'],
        )

    def test_deprovision_removes_only_managed(self):
        self._run(outbound.provision_oauth_provider, 'claude')
        removed = self._run(outbound.deprovision_oauth_provider, 'claude')
        self.assertTrue(removed)
        document = self._load()
        ids = [p['provider_id'] for p in document['providers']]
        self.assertNotIn('oauth_claude', ids)

    def test_managed_models_are_current(self):
        # Guards the preset model lists against silently drifting stale — the
        # managed providers must ship the current flagship IDs.
        self._run(outbound.provision_oauth_provider, 'claude')
        # Codex provision reads plan_type from the stored token — stub it out
        # so the test never touches the real data/config token file.
        with mock.patch('lib.oauth.token_store.load_token', return_value=None):
            self._run(outbound.provision_oauth_provider, 'codex')
        document = self._load()
        claude_ids = self._provider_models(document, 'oauth_claude')
        codex_ids = self._provider_models(document, 'oauth_codex')
        # Latest verified flagships (Anthropic 2025-11-24 / CLIProxyAPI v7
        # codex registry, synced 2026-07-31). Unknown plan → full pro table.
        self.assertIn('claude-opus-4-5-20251101', claude_ids)
        self.assertIn('claude-sonnet-4-6', claude_ids)
        self.assertIn('claude-opus-5', claude_ids)
        self.assertIn('gpt-5.4', codex_ids)
        self.assertIn('gpt-5.3-codex-spark', codex_ids)
        # The retired pre-S1 list must stay retired.
        self.assertNotIn('gpt-5.2-codex', codex_ids)
        # Registry parity: all thinking-capable Claude entries advertise it;
        # legacy 3.5 Haiku correctly does not.
        claude_models = {
            row['model_id']: row for row in document['models']
            if row['creator_id'] == 'anthropic' and row['model_id'] in claude_ids
        }
        self.assertTrue(all('thinking' in row['capabilities']
                            for model_id, row in claude_models.items()
                            if model_id != 'claude-3-5-haiku-20241022'))
        haiku35 = claude_models['claude-3-5-haiku-20241022']
        self.assertNotIn('thinking', haiku35['capabilities'])

    def test_reconcile_repairs_missing_and_removes_orphan(self):
        self._run(outbound.provision_oauth_provider, 'claude')

        def token(provider):
            return ({'access_token': 'codex-token', 'plan_type': 'team'}
                    if provider == 'codex' else None)

        with mock.patch('lib.oauth.token_store.load_token', side_effect=token):
            result = self._run(outbound.reconcile_oauth_providers)
        document = self._load()
        ids = [p['provider_id'] for p in document['providers']]
        self.assertNotIn('oauth_claude', ids)
        self.assertIn('oauth_codex', ids)
        self.assertTrue(result['codex']['provider_ready'])
        self.assertNotIn('gpt-5.3-codex-spark',
                         self._provider_models(document, 'oauth_codex'))

    def test_reconcile_is_noop_when_projection_is_current(self):
        token = {'access_token': 'tok', 'plan_type': 'pro'}
        with mock.patch('lib.oauth.token_store.load_token', return_value=token):
            self._run(outbound.reconcile_oauth_providers)
            revision = self._load()['revision']
            with mock.patch.object(outbound, '_activate_oauth_config_change') as activate:
                self._run(outbound.reconcile_oauth_providers)
        activate.assert_not_called()
        self.assertEqual(self._load()['revision'], revision)


if __name__ == '__main__':
    unittest.main()
