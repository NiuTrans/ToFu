"""tests/test_oauth_outbound.py — subscription-OAuth outbound bridge tests.

Covers lib/oauth/outbound: live-token + identity-header + body resolution
for Claude / Codex, the Claude ``?beta=true`` URL helper, and the managed
server_config provider provision/deprovision round-trip.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

from lib.oauth import outbound


class TestClaudeResolve(unittest.TestCase):

    def test_resolves_headers_without_mutating_messages(self):
        # Since S1 (2026 cloaking port) the system structure is owned by
        # apply_claude_cloak at the Anthropic-body boundary — resolve only
        # swaps in the live token + the identity HEADER suite.
        body = {'messages': [{'role': 'user', 'content': 'hi'}]}
        with mock.patch('lib.oauth.claude.claude_get_valid_token',
                        return_value='sk-ant-oat01-AAA'):
            key, hdrs, out = outbound.resolve_oauth_request('claude', body, None)
        self.assertEqual(key, 'sk-ant-oat01-AAA')
        self.assertEqual(out['messages'], [{'role': 'user', 'content': 'hi'}])
        self.assertIn('claude-code-20250219', hdrs['anthropic-beta'])
        self.assertIn('oauth-2025-04-20', hdrs['anthropic-beta'])
        self.assertEqual(hdrs['x-app'], 'cli')
        self.assertTrue(hdrs['User-Agent'].startswith('claude-cli/'))

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
        with mock.patch('lib.oauth.codex.codex_get_valid_token',
                        return_value='access-tok'), \
             mock.patch('lib.oauth.token_store.load_token',
                        return_value={'account_id': 'acc-xyz'}):
            key, hdrs, _out = outbound.resolve_oauth_request('codex', {'messages': []}, None)
        self.assertEqual(key, 'access-tok')
        self.assertEqual(hdrs['originator'], 'codex_cli_rs')
        self.assertTrue(hdrs['User-Agent'].startswith('codex_cli_rs'))
        self.assertEqual(hdrs['OpenAI-Beta'], 'responses=experimental')
        self.assertEqual(hdrs['chatgpt-account-id'], 'acc-xyz')

    def test_no_token_raises(self):
        with mock.patch('lib.oauth.codex.codex_get_valid_token', return_value=None):
            with self.assertRaises(RuntimeError):
                outbound.resolve_oauth_request('codex', {'messages': []}, None)


class TestProvisioning(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cfg_path = os.path.join(self._tmp.name, 'server_config.json')
        with open(self._cfg_path, 'w') as f:
            json.dump({'providers': [
                {'id': 'user_prov', 'name': 'mine', 'enabled': True,
                 'api_keys': ['k'], 'models': [{'model_id': 'foo'}]},
            ]}, f)

    def tearDown(self):
        self._tmp.cleanup()

    def _patches(self):
        # Patch the config path + the two side-effecting reloads so the test
        # only exercises the JSON mutation.
        return (
            mock.patch('lib._SERVER_CONFIG_PATH', self._cfg_path),
            mock.patch('lib.reload_config', lambda: None),
            mock.patch('lib.llm_dispatch.reset_dispatcher', lambda: None),
        )

    def _run(self, fn, *a):
        ctxs = self._patches()
        with ctxs[0], ctxs[1], ctxs[2]:
            return fn(*a)

    def _load(self):
        with open(self._cfg_path) as f:
            return json.load(f)

    def test_provision_adds_managed_provider(self):
        ok = self._run(outbound.provision_oauth_provider, 'codex')
        self.assertTrue(ok)
        cfg = self._load()
        ids = [p['id'] for p in cfg['providers']]
        self.assertIn('user_prov', ids)        # user provider preserved
        self.assertIn('oauth_codex', ids)
        managed = next(p for p in cfg['providers'] if p['id'] == 'oauth_codex')
        self.assertEqual(managed['oauth'], 'codex')
        self.assertTrue(managed['api_keys'])   # sentinel key present
        self.assertTrue(all(m.get('stream_only') for m in managed['models']))

    def test_provision_is_idempotent(self):
        self._run(outbound.provision_oauth_provider, 'codex')
        self._run(outbound.provision_oauth_provider, 'codex')
        cfg = self._load()
        self.assertEqual(sum(1 for p in cfg['providers'] if p['id'] == 'oauth_codex'), 1)

    def test_deprovision_removes_only_managed(self):
        self._run(outbound.provision_oauth_provider, 'claude')
        removed = self._run(outbound.deprovision_oauth_provider, 'claude')
        self.assertTrue(removed)
        cfg = self._load()
        ids = [p['id'] for p in cfg['providers']]
        self.assertNotIn('oauth_claude', ids)
        self.assertIn('user_prov', ids)

    def test_managed_models_are_current(self):
        # Guards the preset model lists against silently drifting stale — the
        # managed providers must ship the current flagship IDs.
        self._run(outbound.provision_oauth_provider, 'claude')
        # Codex provision reads plan_type from the stored token — stub it out
        # so the test never touches the real data/config token file.
        with mock.patch('lib.oauth.token_store.load_token', return_value=None):
            self._run(outbound.provision_oauth_provider, 'codex')
        cfg = self._load()
        claude = next(p for p in cfg['providers'] if p['id'] == 'oauth_claude')
        codex = next(p for p in cfg['providers'] if p['id'] == 'oauth_codex')
        claude_ids = [m['model_id'] for m in claude['models']]
        codex_ids = [m['model_id'] for m in codex['models']]
        # Latest verified flagships (Anthropic 2025-11-24 / CLIProxyAPI v7
        # codex registry, synced 2026-07-31). Unknown plan → full pro table.
        self.assertIn('claude-opus-4-5-20251101', claude_ids)
        self.assertIn('gpt-5.4', codex_ids)
        self.assertIn('gpt-5.3-codex-spark', codex_ids)
        # The retired pre-S1 list must stay retired.
        self.assertNotIn('gpt-5.2-codex', codex_ids)
        # Claude models must keep the thinking capability.
        self.assertTrue(all('thinking' in m['capabilities'] for m in claude['models']))


if __name__ == '__main__':
    unittest.main()
