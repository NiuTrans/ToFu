"""tests/oauth/test_oauth_outbound.py — subscription-OAuth outbound bridge tests.

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
from lib.model_routing import (
    InMemoryModelRoutingRepository,
    ModelRoutingError,
    OwnerBoundary,
)


@pytest.fixture(autouse=True)
def isolated_oauth_inputs(monkeypatch):
    """Never read real tokens/catalogs or fetch a version in bridge unit tests."""
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda *a, **kw: None)
    monkeypatch.setattr(
        'lib.oauth.codex_catalog.cached_codex_provider_models', lambda: [])
    monkeypatch.setattr(
        'lib.oauth.codex_version.codex_client_version', lambda: '0.150.0')


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
        self.assertTrue(hdrs['User-Agent'].startswith('codex-tui/0.150.0'))
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


class _RacyRepository(InMemoryModelRoutingRepository):
    """In-memory repo that injects transient revision/integrity races."""

    def __init__(self, *, torn_reads=0, commit_conflicts=0):
        super().__init__()
        self._torn_reads = torn_reads
        self._commit_conflicts = commit_conflicts

    def get(self, boundary):
        if self._torn_reads > 0:
            self._torn_reads -= 1
            raise ModelRoutingError(
                'stored aggregate revision does not match its repository row',
                kind='model_routing_storage_integrity',
            )
        return super().get(boundary)

    def compare_and_swap(self, boundary, document, *, expected_revision,
                         migration_receipt=None):
        if self._commit_conflicts > 0:
            self._commit_conflicts -= 1
            raise ModelRoutingError(
                'model-routing aggregate changed concurrently',
                kind='model_routing_revision_conflict',
            )
        return super().compare_and_swap(
            boundary, document, expected_revision=expected_revision,
            migration_receipt=migration_receipt)


class TestProvisioning(unittest.TestCase):

    def test_new_catalog_window_replaces_inherited_model_fallback(self):
        model = {'model_id': 'gpt-6-astra',
                 'capabilities': ['text', 'vision', 'thinking'],
                 'inherited_model_fields': ['context_window']}
        with mock.patch('lib.oauth.codex_catalog.cached_codex_provider_models',
                        side_effect=lambda: [dict(model)]):
            self._run(outbound.provision_oauth_provider, 'codex')

            def offering(document):
                return next(o for o in document['offerings']
                            if o.get('model', {}).get('model_id') == 'gpt-6-astra')

            initial = self._load()
            self.assertIn('context_window', offering(initial)['inherited_model_fields'])
            models_before = initial['models']
            model.update(context_window=272000, inherited_model_fields=[])
            self._run(outbound.provision_oauth_provider, 'codex')
            refreshed = self._load()
            self.assertEqual(refreshed['models'], models_before)
            self.assertEqual(offering(refreshed)['context_window'], 272000)
            self.assertEqual(offering(refreshed)['catalog_context_window'], 272000)
            self.assertNotIn('context_window', offering(refreshed)['inherited_model_fields'])
            self.assertFalse(self._run(outbound.provision_oauth_provider, 'codex'))

    def test_catalog_window_recovers_after_stale_model_limit_is_repaired(self):
        model = {'model_id': 'gpt-6-astra',
                 'capabilities': ['text', 'vision', 'thinking'],
                 'inherited_model_fields': ['context_window']}
        with mock.patch('lib.oauth.codex_catalog.cached_codex_provider_models',
                        side_effect=lambda: [dict(model)]):
            self._run(outbound.provision_oauth_provider, 'codex')

            def set_model_window(window):
                current = self.repository.get(self.boundary)
                document = current.document
                next(m for m in document['models']
                     if m['model_id'] == 'gpt-6-astra')['context_window'] = window
                self.repository.compare_and_swap(
                    self.boundary, document, expected_revision=current.revision)

            def offering():
                return next(o for o in self._load()['offerings']
                            if o.get('model', {}).get('model_id') == 'gpt-6-astra')

            set_model_window(128000)
            model.update(context_window=272000, inherited_model_fields=[])
            self._run(outbound.provision_oauth_provider, 'codex')
            self.assertEqual(offering()['context_window'], 128000)
            self.assertEqual(offering()['catalog_context_window'], 128000)
            # Catalog refresh cannot author official Model facts. Once the
            # owner repairs those facts through registration, supply can grow.
            set_model_window(1050000)
            self._run(outbound.provision_oauth_provider, 'codex')
            self.assertEqual(offering()['context_window'], 272000)
            self.assertEqual(offering()['catalog_context_window'], 272000)
            self.assertNotIn('context_window', offering()['inherited_model_fields'])

    def test_catalog_window_tracks_updates_but_preserves_owner_override(self):
        model = {'model_id': 'gpt-6.1-sol',
                 'capabilities': ['text', 'vision', 'thinking'],
                 'context_window': 272000, 'inherited_model_fields': []}
        with mock.patch('lib.oauth.codex_catalog.cached_codex_provider_models',
                        side_effect=lambda: [dict(model)]):
            self._run(outbound.provision_oauth_provider, 'codex')
            def offering(document):
                return next(o for o in document['offerings']
                            if o.get('model', {}).get('model_id') == 'gpt-6.1-sol')
            self.assertEqual(offering(self._load())['catalog_context_window'], 272000)
            model['context_window'] = 500000
            self._run(outbound.provision_oauth_provider, 'codex')
            self.assertEqual(offering(self._load())['context_window'], 500000)
            self.assertEqual(offering(self._load())['catalog_context_window'], 500000)
            current = self.repository.get(self.boundary)
            document = current.document
            offering(document)['context_window'] = 200000
            self.repository.compare_and_swap(self.boundary, document,
                                             expected_revision=current.revision)
            model['context_window'] = 600000
            self._run(outbound.provision_oauth_provider, 'codex')
            self.assertEqual(offering(self._load())['context_window'], 200000)
            self.assertEqual(offering(self._load())['catalog_context_window'], 500000)
            self.assertFalse(self._run(outbound.provision_oauth_provider, 'codex'))

    def test_catalog_window_does_not_raise_unmarked_legacy_limit(self):
        model = {'model_id': 'gpt-6.1-sol',
                 'capabilities': ['text', 'vision', 'thinking'],
                 'context_window': 272000, 'inherited_model_fields': []}
        with mock.patch('lib.oauth.codex_catalog.cached_codex_provider_models',
                        side_effect=lambda: [dict(model)]):
            self._run(outbound.provision_oauth_provider, 'codex')
            current = self.repository.get(self.boundary)
            document = current.document
            offer = next(o for o in document['offerings']
                         if o.get('model', {}).get('model_id') == 'gpt-6.1-sol')
            offer.pop('catalog_context_window')
            self.repository.compare_and_swap(self.boundary, document,
                                             expected_revision=current.revision)
            model['context_window'] = 500000
            self._run(outbound.provision_oauth_provider, 'codex')
            offer = next(o for o in self._load()['offerings']
                         if o.get('model', {}).get('model_id') == 'gpt-6.1-sol')
            self.assertEqual(offer['context_window'], 272000)
            self.assertNotIn('catalog_context_window', offer)

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

    def test_provision_recovers_from_torn_read(self):
        # A concurrent aggregate writer (e.g. the world-catalogue sync) can
        # leave repo.get() observing a revision/document skew for one read.
        # The upsert must re-read and self-heal instead of wedging.
        repo = _RacyRepository(torn_reads=1)
        with mock.patch('lib.llm_dispatch.reset_dispatcher', lambda: None):
            ok = outbound.provision_oauth_provider(
                'codex',
                owner_user_id=self.boundary.owner_user_id,
                repository=repo,
            )
        self.assertTrue(ok)
        ids = [p['provider_id'] for p in repo.get(self.boundary).document['providers']]
        self.assertIn('oauth_codex', ids)

    def test_provision_retries_revision_conflict_on_commit(self):
        # A CAS conflict (lost race to commit) must also be retried by
        # re-reading, mirroring the managed_provider.py concurrency idiom.
        repo = _RacyRepository(commit_conflicts=1)
        with mock.patch('lib.llm_dispatch.reset_dispatcher', lambda: None):
            ok = outbound.provision_oauth_provider(
                'codex',
                owner_user_id=self.boundary.owner_user_id,
                repository=repo,
            )
        self.assertTrue(ok)

    def test_provision_gives_up_after_attempt_cap(self):
        repo = _RacyRepository(torn_reads=99)
        with mock.patch('lib.llm_dispatch.reset_dispatcher', lambda: None):
            with self.assertRaises(ModelRoutingError) as ctx:
                outbound.provision_oauth_provider(
                    'codex',
                    owner_user_id=self.boundary.owner_user_id,
                    repository=repo,
                )
        self.assertEqual(ctx.exception.kind, 'model_routing_revision_conflict')

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

    def test_catalog_repairs_proven_stale_capability_without_overriding_controls(self):
        """Only a matching prior catalog baseline permits an automatic
        capability update; unrelated access and credential switches stay put.
        """
        model = {
            'model_id': 'gpt-6-astra',
            'capabilities': ['text', 'vision', 'thinking'],
            'supported_reasoning_levels': ['low', 'medium', 'max'],
            'default_reasoning_level': 'medium',
            'inference_profile': {
                'source': 'provider_catalog',
                'reasoning': {'support': 'supported',
                              'supported_efforts': ['low', 'medium', 'max'],
                              'default_effort': 'medium',
                              'wire_format': 'reasoning_effort'},
            },
            'inherited_model_fields': ['context_window'],
        }
        with mock.patch('lib.oauth.codex_catalog.cached_codex_provider_models',
                        return_value=[model]):
            self._run(outbound.provision_oauth_provider, 'codex')
            current = self.repository.get(self.boundary)
            document = current.document
            offer = next(row for row in document['offerings']
                         if (row.get('model') or {}).get('model_id') == 'gpt-6-astra')
            offer['capabilities'] = ['text', 'vision']
            offer['enabled'] = False

            offer['catalog_capabilities'] = ['text', 'vision']
            credential = next(row for row in document['credentials']
                              if row['provider_access_id'] == offer['provider_access_id'])
            credential['enabled'] = False
            self.repository.compare_and_swap(
                self.boundary, document, expected_revision=current.revision)

            self.assertTrue(self._run(outbound.provision_oauth_provider, 'codex'))
            updated = self._load()
            offer = next(row for row in updated['offerings']
                         if (row.get('model') or {}).get('model_id') == 'gpt-6-astra')
            self.assertEqual(offer['capabilities'], ['text', 'thinking', 'vision'])
            self.assertFalse(offer['enabled'])
            self.assertFalse(next(row for row in updated['credentials']
                                  if row['provider_access_id'] == offer['provider_access_id'])['enabled'])
            self.assertFalse(self._run(outbound.provision_oauth_provider, 'codex'))

    def test_cold_fallback_never_replaces_admitted_codex_supply(self):
        model = {'model_id': 'gpt-6-astra',
                 'capabilities': ['text', 'vision', 'thinking']}
        with mock.patch('lib.oauth.codex_catalog.cached_codex_provider_models', return_value=[model]):
            self._run(outbound.provision_oauth_provider, 'codex')
        before = self._load()
        with mock.patch('lib.oauth.codex_catalog.cached_codex_provider_models', return_value=[]):
            self.assertFalse(self._run(outbound.provision_oauth_provider, 'codex'))
        self.assertEqual(self._load(), before)

    def test_user_narrowed_capabilities_remain_explicit_even_when_profile_is_catalog_owned(self):
        model = {'model_id': 'gpt-6-astra',
                 'capabilities': ['text', 'vision', 'thinking'],
                 'inference_profile': {'source': 'provider_catalog',
                                       'reasoning': {'support': 'supported',
                                                     'supported_efforts': ['low', 'max'],
                                                     'default_effort': 'low',
                                                     'wire_format': 'reasoning_effort'}}}
        with mock.patch('lib.oauth.codex_catalog.cached_codex_provider_models',
                        return_value=[model]):
            self._run(outbound.provision_oauth_provider, 'codex')
            current = self.repository.get(self.boundary)
            document = current.document
            offer = next(row for row in document['offerings']
                         if (row.get('model') or {}).get('model_id') == 'gpt-6-astra')
            self.assertEqual(offer['catalog_capabilities'],
                             ['text', 'thinking', 'vision'])
            offer['capabilities'] = ['text', 'vision']
            self.repository.compare_and_swap(
                self.boundary, document, expected_revision=current.revision)
            self._run(outbound.provision_oauth_provider, 'codex')
            offer = next(row for row in self._load()['offerings']
                         if (row.get('model') or {}).get('model_id') == 'gpt-6-astra')
            self.assertEqual(offer['capabilities'], ['text', 'vision'])
            self.assertNotIn('catalog_capabilities', offer)

    def test_legacy_unmarked_capability_stays_explicit(self):
        model = {'model_id': 'gpt-6-astra',
                 'capabilities': ['text', 'thinking', 'vision'],
                 'inference_profile': {'source': 'provider_catalog',
                                       'reasoning': {'support': 'supported',
                                                     'supported_efforts': ['low'],
                                                     'default_effort': 'low',
                                                     'wire_format': 'reasoning_effort'}}}
        with mock.patch('lib.oauth.codex_catalog.cached_codex_provider_models',
                        return_value=[model]):
            self._run(outbound.provision_oauth_provider, 'codex')
            current = self.repository.get(self.boundary)
            document = current.document
            offer = next(row for row in document['offerings']
                         if (row.get('model') or {}).get('model_id') == 'gpt-6-astra')
            offer.pop('catalog_capabilities', None)
            offer['capabilities'] = ['text', 'vision']
            self.repository.compare_and_swap(
                self.boundary, document, expected_revision=current.revision)
            self._run(outbound.provision_oauth_provider, 'codex')
            offer = next(row for row in self._load()['offerings']
                         if (row.get('model') or {}).get('model_id') == 'gpt-6-astra')
            self.assertEqual(offer['capabilities'], ['text', 'vision'])
            self.assertNotIn('catalog_capabilities', offer)

    def test_explicitly_unsupported_reasoning_is_not_reenabled_by_catalog(self):
        model = {
            'model_id': 'gpt-6-astra', 'capabilities': ['text', 'vision', 'thinking'],
            'supported_reasoning_levels': ['low', 'max'],
            'default_reasoning_level': 'low',
            'inference_profile': {
                'source': 'provider_catalog',
                'reasoning': {'support': 'supported',
                              'supported_efforts': ['low', 'max'],
                              'default_effort': 'low',
                              'wire_format': 'reasoning_effort'},
            },
        }
        with mock.patch('lib.oauth.codex_catalog.cached_codex_provider_models',
                        return_value=[model]):
            self._run(outbound.provision_oauth_provider, 'codex')
            current = self.repository.get(self.boundary)
            document = current.document
            offer = next(row for row in document['offerings']
                         if (row.get('model') or {}).get('model_id') == 'gpt-6-astra')
            offer['capabilities'] = ['text', 'vision']
            offer['inference_profile'] = {'source': 'configured',
                                          'reasoning': {'support': 'unsupported',
                                                        'wire_format': 'none'}}
            self.repository.compare_and_swap(
                self.boundary, document, expected_revision=current.revision)

            self._run(outbound.provision_oauth_provider, 'codex')
            updated = self._load()
            offer = next(row for row in updated['offerings']
                         if (row.get('model') or {}).get('model_id') == 'gpt-6-astra')
            self.assertEqual(offer['capabilities'], ['text', 'vision'])
            self.assertEqual(offer['inference_profile']['reasoning']['support'],
                             'unsupported')

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
        # Shipped fallback model tables; unknown plan uses the full pro table.
        # Cached-catalog replacement is covered separately above.
        self.assertIn('claude-opus-4-5-20251101', claude_ids)
        self.assertIn('claude-sonnet-4-6', claude_ids)
        self.assertIn('claude-opus-5', claude_ids)
        self.assertIn('gpt-5.5', codex_ids)
        self.assertIn('gpt-6-astra', codex_ids)
        self.assertNotIn('gpt-5.4', codex_ids)
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
        self.assertTrue(all('enabled' not in row for row in document['deployments']))
        self.assertEqual({row['probe_status'] for row in document['deployments']},
                         {'unprobed'})
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


@pytest.mark.parametrize('blocked', ['model_grant', 'connection_grant', 'connection', 'stale', 'credential'])
def test_subscription_readiness_uses_actual_route_authorization(blocked):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(41)
    with mock.patch('lib.llm_dispatch.reset_dispatcher', lambda: None):
        outbound.provision_oauth_provider('codex', owner_user_id=41, repository=repository)
    current = repository.get(boundary)
    document = current.document
    if blocked in ('model_grant', 'connection_grant'):
        for credential in document['credentials']:
            credential['authorization']['models' if blocked == 'model_grant' else 'connection_ids'] = []
    elif blocked == 'connection':
        for connection in document['connections']:
            connection['enabled'] = False
    elif blocked == 'stale':
        for offering in document['offerings']:
            offering['stale'] = True
    else:
        for credential in document['credentials']:
            credential['enabled'] = False
    repository.compare_and_swap(boundary, document, expected_revision=current.revision)
    status = outbound.managed_oauth_provider_status('codex', owner_user_id=41, repository=repository)
    assert status['model_count'] > 0
    assert status['provider_ready'] is False
