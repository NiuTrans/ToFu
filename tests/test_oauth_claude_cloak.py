"""tests/test_oauth_claude_cloak.py — CLIProxyAPI 2026 cloaking 移植守卫（S1）。

Covers the Claude-Code cloaking spec ported into lib/oauth/outbound.py:
  * billing header (``x-anthropic-billing-header: cc_version=…; cch=00000;``)
    injected as system[0] — fingerprint algorithm frozen against CLIProxyAPI
    vectors (sha256(salt + text[4] + text[7] + text[20] + version)[:3]);
  * system rebuilt to [billing, identity, Claude Code static prompt] with the
    user's own system text moved into a stable post-first-user carrier;
  * the full 9-flag anthropic-beta set + X-Stainless header suite;
  * OpenCode→Claude-Code tool-name remapping (request) + per-request reverse
    map restore (response, incl. the SSE translator);
  * sampling normalisation (temperature/top_p dropped; thinking/tool_choice
    interplay);
  * Codex chatgpt_plan_type extraction + per-plan model gating.

Failure-first: every test here is red against the pre-S1 code (apply_claude_cloak
does not exist; resolve_oauth_request mutates messages and ships 2 betas).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import unittest
from unittest import mock

import pytest

pytestmark = pytest.mark.unit

import lib.oauth.outbound as outbound
from lib.oauth.outbound import (
    CLAUDE_CODE_IDENTITY,
    apply_claude_cloak,
    restore_claude_tool_names,
)


def _anthropic_body():
    """A representative Anthropic-shaped body (post openai_body_to_anthropic)."""
    return {
        'model': 'claude-opus-4-5-20251101',
        'max_tokens': 1024,
        'stream': True,
        'temperature': 0.7,
        'top_p': 0.9,
        'system': [
            {'type': 'text', 'text': 'You are Tofu, a self-hosted AI assistant.'},
            {'type': 'text', 'text': 'Extra operator rules.'},
        ],
        'messages': [
            {'role': 'user', 'content': [
                {'type': 'text', 'text': 'hi'}]},
            {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 'tu1', 'name': 'bash',
                 'input': {'command': 'ls'}}]},
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'tu1', 'content': 'ok'}]},
        ],
        'tools': [
            {'name': 'bash', 'description': 'run', 'input_schema': {'type': 'object'}},
            {'name': 'read_files', 'description': 'tofu own', 'input_schema': {'type': 'object'}},
        ],
        'tool_choice': {'type': 'tool', 'name': 'bash'},
    }


class TestBillingHeader(unittest.TestCase):

    def test_fingerprint_frozen_vectors(self):
        # Frozen against the CLIProxyAPI Go algorithm:
        #   sha256("59cf53e54c78" + t[4] + t[7] + t[20] + "2.1.220")[:3]
        # (rune-wise indexing, '0' padding for short texts). The '' vector
        # cross-validates against CLIProxyAPI's own Go test ("x" → 04c,
        # claude_executor_test.go:1036).
        f = outbound._compute_billing_fingerprint
        self.assertEqual(f('You are a helpful assistant.'), '304')
        self.assertEqual(f('You are Tofu, a self-hosted AI assistant.'), '48b')
        self.assertEqual(f(''), '04c')
        self.assertEqual(f('短'), '04c')          # <4 runes → all padded
        self.assertEqual(f('hello world, this is a longer system prompt text'), '55f')

    def test_system0_is_billing_header_oauth_branch(self):
        body, _rev = apply_claude_cloak(_anthropic_body())
        sys0 = body['system'][0]
        self.assertEqual(sys0['type'], 'text')
        self.assertTrue(sys0['text'].startswith('x-anthropic-billing-header: '))
        # OAuth tokens take the SIGNING branch: fixed cch=00000 (not a payload hash).
        self.assertIn('cc_version=2.1.220.48b;', sys0['text'])  # fp of first sys text
        self.assertIn('cc_entrypoint=cli;', sys0['text'])
        self.assertIn('cch=00000;', sys0['text'])


class TestSystemStructure(unittest.TestCase):

    def test_three_block_structure_and_user_system_moved(self):
        body, _rev = apply_claude_cloak(_anthropic_body())
        system = body['system']
        self.assertEqual(len(system), 3)
        self.assertEqual(system[1]['text'], CLAUDE_CODE_IDENTITY)
        # Static Claude Code prompt (verbatim port) occupies system[2].
        self.assertIn('interactive agent', system[2]['text'])
        self.assertIn('# Doing tasks', system[2]['text'])
        # User's own system text no longer lives in system[]; it rides a new
        # fixed-prefix user carrier inside a <system-reminder> wrapper.
        for blk in system:
            self.assertNotIn('You are Tofu', blk.get('text', ''))
        self.assertEqual(body['messages'][0], _anthropic_body()['messages'][0])
        reminder_user = body['messages'][1]
        self.assertEqual(reminder_user['role'], 'user')
        self.assertIn('<system-reminder>', reminder_user['content'][0]['text'])
        self.assertIn('You are Tofu, a self-hosted AI assistant.',
                      reminder_user['content'][0]['text'])
        self.assertIn('Extra operator rules.', reminder_user['content'][0]['text'])

    def test_idempotent_second_application(self):
        body1, _ = apply_claude_cloak(_anthropic_body())
        body2, _ = apply_claude_cloak(body1)
        self.assertEqual(body1['system'], body2['system'])
        # The reminder block must not be duplicated either.
        n_reminders = sum(
            1 for message in body2['messages']
            for b in (message.get('content') or [])
            if isinstance(message, dict) and isinstance(b, dict)
            and '<system-reminder>' in (b.get('text') or ''))
        self.assertEqual(n_reminders, 1)

    def test_string_system_shape_supported(self):
        body = _anthropic_body()
        body['system'] = 'Plain string system prompt.'
        out, _ = apply_claude_cloak(body)
        self.assertEqual(len(out['system']), 3)
        self.assertIn('Plain string system prompt.',
                      out['messages'][1]['content'][0]['text'])

    def test_system_carrier_position_is_stable_as_history_grows(self):
        first_body = _anthropic_body()
        first, _ = apply_claude_cloak(first_body)
        stable_prefix = copy.deepcopy(first['messages'][:2])

        later_body = _anthropic_body()
        later_body['messages'].extend([
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'later'}]},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'continue'}]},
        ])
        later, _ = apply_claude_cloak(later_body)

        self.assertEqual(later['messages'][:2], stable_prefix)

    def test_tool_result_first_user_message_keeps_results_first(self):
        # Continuation round: first user message leads with tool_result —
        # Anthropic requires those to stay first, so the reminder is appended
        # AFTER them, not prepended.
        body = _anthropic_body()
        body['messages'] = [
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'tu1', 'content': 'ok'}]},
        ]
        out, _ = apply_claude_cloak(body)
        original_content = out['messages'][0]['content']
        self.assertEqual(original_content, [
            {'type': 'tool_result', 'tool_use_id': 'tu1', 'content': 'ok'}])
        self.assertIn(
            '<system-reminder>', out['messages'][-1]['content'][0]['text'])


class TestToolRename(unittest.TestCase):

    def test_request_side_rename_and_reverse_map(self):
        body, rev = apply_claude_cloak(_anthropic_body())
        names = [t['name'] for t in body['tools']]
        self.assertIn('Bash', names)
        self.assertIn('read_files', names)      # Tofu-own tool: untouched (O2)
        self.assertEqual(body['tool_choice']['name'], 'Bash')
        # tool_use block inside messages renamed too.
        assistant = next(message for message in body['messages']
                         if message.get('role') == 'assistant')
        tu = assistant['content'][0]
        self.assertEqual(tu['name'], 'Bash')
        # Per-request reverse map: only what WE renamed.
        self.assertEqual(rev, {'Bash': 'bash'})

    def test_preexisting_titlecase_not_in_reverse_map(self):
        # A client that sent 'Bash' originally must NOT get it lowercased back
        # on the response (CLIProxyAPI's per-request-map lesson).
        body = _anthropic_body()
        body['tools'] = [{'name': 'Bash', 'description': 'run',
                          'input_schema': {'type': 'object'}}]
        body['tool_choice'] = {'type': 'auto'}
        body['messages'] = [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]
        _b, rev = apply_claude_cloak(body)
        self.assertNotIn('Bash', rev)

    def test_restore_on_openai_tool_calls(self):
        tcs = [{'id': 'tu1', 'type': 'function',
                'function': {'name': 'Bash', 'arguments': '{}'}},
               {'id': 'tu2', 'type': 'function',
                'function': {'name': 'Glob', 'arguments': '{}'}}]
        restore_claude_tool_names(tcs, {'Bash': 'bash'})
        self.assertEqual(tcs[0]['function']['name'], 'bash')
        self.assertEqual(tcs[1]['function']['name'], 'Glob')   # not renamed → untouched


class TestMetadataAndSampling(unittest.TestCase):

    def test_fake_user_id_injected_and_valid_preserved(self):
        body, _ = apply_claude_cloak(_anthropic_body())
        uid = body['metadata']['user_id']
        self.assertRegex(uid, r'^user_[a-f0-9]{64}_account_[0-9a-f-]{36}_session_[0-9a-f-]{36}$')
        valid = ('user_' + 'a' * 64 +
                 '_account_123e4567-e89b-42d3-a456-426614174000'
                 '_session_123e4567-e89b-42d3-a456-426614174001')
        body2 = _anthropic_body()
        body2['metadata'] = {'user_id': valid}
        out2, _ = apply_claude_cloak(body2)
        self.assertEqual(out2['metadata']['user_id'], valid)

    def test_sampling_normalised(self):
        body, _ = apply_claude_cloak(_anthropic_body())
        self.assertNotIn('temperature', body)
        self.assertNotIn('top_p', body)

    def test_thinking_drops_topk_and_forced_tool_choice_drops_thinking(self):
        body = _anthropic_body()
        body['thinking'] = {'type': 'enabled', 'budget_tokens': 2048}
        body['top_k'] = 40
        out, _ = apply_claude_cloak(body)
        self.assertNotIn('top_k', out)
        # tool_choice 'tool' (forced) is incompatible with thinking upstream.
        out2, _ = apply_claude_cloak(_anthropic_body())
        self.assertNotIn('thinking', out2)


class TestResolveHeaders(unittest.TestCase):

    def _resolve(self, extra=None, body=None):
        with mock.patch('lib.oauth.claude.claude_get_valid_token',
                        return_value='sk-ant-oat01-AAA'):
            return outbound.resolve_oauth_request(
                'claude', body if body is not None else {'messages': []}, extra)

    def test_full_beta_set_in_order(self):
        _key, hdrs, _out = self._resolve()
        betas = hdrs['anthropic-beta'].split(',')
        # Claude Code 2.1.220 wire order (no tools in this body →
        # advanced-tool-use absent; drift guard: test_oauth_cloaking_drift).
        expected_head = ['claude-code-20250219', 'oauth-2025-04-20',
                         'interleaved-thinking-2025-05-14',
                         'redact-thinking-2026-02-12',
                         'thinking-token-count-2026-05-13',
                         'context-management-2025-06-27',
                         'prompt-caching-scope-2026-01-05',
                         'mid-conversation-system-2026-04-07',
                         'effort-2025-11-24',
                         'fallback-credit-2026-06-01',
                         'extended-cache-ttl-2025-04-11']
        self.assertEqual(betas[:11], expected_head)

    def test_tools_request_inserts_advanced_tool_use(self):
        _key, hdrs, _out = self._resolve(body={'messages': [], 'tools': [
            {'name': 'bash', 'description': 'x', 'input_schema': {}}]})
        betas = hdrs['anthropic-beta'].split(',')
        i = betas.index('mid-conversation-system-2026-04-07')
        self.assertEqual(betas[i + 1], 'advanced-tool-use-2025-11-20')
        self.assertEqual(betas[i + 2], 'effort-2025-11-24')

    def test_caller_betas_appended_not_duplicated(self):
        _key, hdrs, _out = self._resolve(
            {'anthropic-beta': 'extended-cache-ttl-2025-04-11,oauth-2025-04-20'})
        betas = hdrs['anthropic-beta'].split(',')
        self.assertIn('extended-cache-ttl-2025-04-11', betas)
        self.assertEqual(betas.count('oauth-2025-04-20'), 1)

    def test_claude_code_header_suite(self):
        _key, hdrs, _out = self._resolve()
        self.assertEqual(hdrs['x-app'], 'cli')
        self.assertEqual(hdrs['User-Agent'], 'claude-cli/2.1.220 (external, cli)')
        self.assertEqual(hdrs['X-Stainless-Retry-Count'], '0')
        self.assertEqual(hdrs['X-Stainless-Runtime'], 'node')
        self.assertEqual(hdrs['X-Stainless-Lang'], 'js')
        self.assertEqual(hdrs['X-Stainless-Timeout'], '600')
        # Device-profile kit (2.1.220 capture, CLIProxyAPI device_profile).
        self.assertEqual(hdrs['X-Stainless-Package-Version'], '0.94.0')
        self.assertEqual(hdrs['X-Stainless-Runtime-Version'], 'v26.3.0')
        self.assertEqual(hdrs['X-Stainless-Os'], 'MacOS')
        self.assertEqual(hdrs['X-Stainless-Arch'], 'arm64')
        # Sent since 2026-08-04: CLIProxyAPI's live-captured 2.1.220 kit
        # carries it (our earlier "real Claude Code doesn't" comment was
        # the untested assumption — see outbound.py).
        self.assertEqual(hdrs['Anthropic-Dangerous-Direct-Browser-Access'],
                         'true')
        # Session id present + stable per token; request id unique per call.
        self.assertTrue(hdrs['X-Claude-Code-Session-Id'])
        self.assertTrue(hdrs['x-client-request-id'])
        _k2, hdrs2, _o2 = self._resolve()
        self.assertEqual(hdrs['X-Claude-Code-Session-Id'],
                         hdrs2['X-Claude-Code-Session-Id'])
        self.assertNotEqual(hdrs['x-client-request-id'],
                            hdrs2['x-client-request-id'])

    def test_token_rotation_session_cache_is_bounded(self):
        outbound._session_ids.clear()
        try:
            newest = ''
            for index in range(260):
                newest = outbound._session_id_for_token(f'token-{index}')
            self.assertEqual(len(outbound._session_ids), 256)
            self.assertEqual(
                outbound._session_id_for_token('token-259'), newest)
        finally:
            outbound._session_ids.clear()

    def test_resolve_no_longer_mutates_messages(self):
        # The system structure is owned by apply_claude_cloak at the Anthropic
        # boundary — resolve_oauth_request must NOT prepend anything itself.
        body = {'messages': [{'role': 'user', 'content': 'hi'}]}
        _key, _hdrs, out = self._resolve(body=body)
        self.assertEqual(out['messages'], [{'role': 'user', 'content': 'hi'}])


class TestCodexPlanGating(unittest.TestCase):

    @staticmethod
    def _jwt(plan):
        payload = {
            'email': 'u@example.com',
            'https://api.openai.com/auth': {
                'chatgpt_account_id': 'acc-1',
                'chatgpt_plan_type': plan,
            },
        }
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        return f'eyJhbGciOiJub25lIn0.{raw}.sig'

    def test_parse_jwt_extracts_plan_type(self):
        from lib.oauth.codex import _parse_jwt_claims
        email, account_id, plan = _parse_jwt_claims(self._jwt('plus'))
        self.assertEqual((email, account_id, plan), ('u@example.com', 'acc-1', 'plus'))

    def test_parse_jwt_missing_plan_tolerated(self):
        from lib.oauth.codex import _parse_jwt_claims
        _e, _a, plan = _parse_jwt_claims('not-a-jwt')
        self.assertEqual(plan, '')

    def _provision(self, plan_type):
        from lib.model_routing import (
            InMemoryModelRoutingRepository,
            OwnerBoundary,
        )

        repository = InMemoryModelRoutingRepository()
        with mock.patch.object(outbound, '_activate_oauth_config_change'):
            outbound.provision_oauth_provider(
                'codex',
                plan_type=plan_type,
                owner_user_id=1,
                repository=repository,
            )
        document = repository.get(OwnerBoundary.create(1)).document
        access = next(
            row for row in document['provider_accesses']
            if row['provider_id'] == 'oauth_codex'
        )
        return [
            row['model']['model_id']
            for row in document['offerings']
            if row['provider_access_id'] == access['provider_access_id']
            and row['identity_state'] == 'confirmed'
        ]

    def test_free_plan_gated_to_free_tier(self):
        ids = self._provision('free')
        self.assertIn('gpt-5.4-mini', ids)
        self.assertNotIn('gpt-5.3-codex-spark', ids)
        self.assertNotIn('gpt-5.4', ids)

    def test_business_maps_to_team_tier(self):
        ids = self._provision('business')
        self.assertIn('gpt-5.4', ids)
        self.assertIn('gpt-5.6-sol', ids)
        self.assertNotIn('gpt-5.3-codex-spark', ids)

    def test_pro_and_unknown_get_full_table(self):
        ids_pro = self._provision('pro')
        ids_unknown = self._provision('weird-new-plan')
        for ids in (ids_pro, ids_unknown):
            self.assertIn('gpt-5.3-codex-spark', ids)
            self.assertIn('gpt-5.6-luna', ids)

    def test_stale_codex_ids_retired(self):
        # The pre-S1 list (gpt-5.2/5.1/5-codex) is not in CLIProxyAPI's current
        # registry at any tier — provisioning must not resurrect it.
        ids = self._provision('pro')
        for stale in ('gpt-5.2-codex', 'gpt-5.1-codex', 'gpt-5-codex'):
            self.assertNotIn(stale, ids)


class TestStreamReverseRename(unittest.TestCase):

    def test_translator_restores_renamed_tool(self):
        from lib.llm.anthropic_outbound import AnthropicSSETranslator
        tr = AnthropicSSETranslator(model='claude-opus-4-5-20251101')
        tr.tool_name_reverse = {'Bash': 'bash'}
        chunks = tr.translate(json.dumps({
            'type': 'content_block_start', 'index': 1,
            'content_block': {'type': 'tool_use', 'id': 'tu1', 'name': 'Bash'}}))
        name = chunks[0]['choices'][0]['delta']['tool_calls'][0]['function']['name']
        self.assertEqual(name, 'bash')

    def test_translator_keeps_unmapped_name(self):
        from lib.llm.anthropic_outbound import AnthropicSSETranslator
        tr = AnthropicSSETranslator(model='m')
        tr.tool_name_reverse = {'Bash': 'bash'}
        chunks = tr.translate(json.dumps({
            'type': 'content_block_start', 'index': 1,
            'content_block': {'type': 'tool_use', 'id': 'tu1', 'name': 'Read'}}))
        name = chunks[0]['choices'][0]['delta']['tool_calls'][0]['function']['name']
        self.assertEqual(name, 'Read')


if __name__ == '__main__':
    unittest.main()
