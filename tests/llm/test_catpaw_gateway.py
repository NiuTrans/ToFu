"""tests/llm/test_catpaw_gateway.py — CatPaw mcli gateway request-shape adapter.

Covers ``lib.llm.catpaw_gateway``: the base-url detector, the ``system``
identity enforcement the mcli gateway demands (HTTP 400 otherwise), and the
mandatory non-empty ``X-Working-Dir`` header.  Also pins the onboarding
template (``static/provider_templates/catpaw.json``) as loadable and
Anthropic-protocol.
"""

import json
import unittest
from pathlib import Path

import pytest

from lib.llm.anthropic_outbound import openai_body_to_anthropic
from lib.llm.catpaw_gateway import (
    CATPAW_IDENTITY,
    apply_catpaw_shape,
    catpaw_headers,
    is_catpaw_base_url,
)

pytestmark = pytest.mark.unit


class BaseUrlDetectionTest(unittest.TestCase):
    def test_gateway_host_matches(self):
        self.assertTrue(is_catpaw_base_url('https://mcli.internal.example.com'))
        self.assertTrue(is_catpaw_base_url('https://mcli.internal.example.com/v1'))
        self.assertTrue(is_catpaw_base_url('https://mcli.internal.example.com/'))

    def test_non_gateway_hosts_rejected(self):
        self.assertFalse(is_catpaw_base_url('https://api.openai.com/v1'))
        self.assertFalse(is_catpaw_base_url('https://api.anthropic.com/v1'))
        self.assertFalse(is_catpaw_base_url('https://mcli.internal.example.com.evil.com'))
        self.assertFalse(is_catpaw_base_url(''))
        self.assertFalse(is_catpaw_base_url(None))


class ApplyShapeTest(unittest.TestCase):
    def test_missing_system_becomes_identity(self):
        body = apply_catpaw_shape({'messages': []})
        self.assertEqual(body['system'], CATPAW_IDENTITY)

    def test_string_system_is_prefixed_not_replaced(self):
        body = apply_catpaw_shape({'system': 'Tofu system prompt.'})
        self.assertTrue(body['system'].startswith(CATPAW_IDENTITY))
        self.assertIn('Tofu system prompt.', body['system'])

    def test_existing_identity_string_untouched(self):
        original = CATPAW_IDENTITY + '\n\nExtra.'
        body = apply_catpaw_shape({'system': original})
        self.assertEqual(body['system'], original)

    def test_identity_match_is_case_insensitive(self):
        original = CATPAW_IDENTITY.lower()
        body = apply_catpaw_shape({'system': original})
        self.assertEqual(body['system'], original)

    def test_block_system_gets_identity_block_prepended(self):
        blocks = [
            {'type': 'text', 'text': 'Tofu block.',
             'cache_control': {'type': 'ephemeral'}},
        ]
        body = apply_catpaw_shape({'system': blocks})
        system = body['system']
        self.assertEqual(system[0], {'type': 'text', 'text': CATPAW_IDENTITY})
        self.assertEqual(system[1:], blocks)

    def test_block_system_with_identity_first_untouched(self):
        blocks = [
            {'type': 'text', 'text': CATPAW_IDENTITY},
            {'type': 'text', 'text': 'Tofu block.'},
        ]
        body = apply_catpaw_shape({'system': list(blocks)})
        self.assertEqual(body['system'], blocks)

    def test_openai_translation_pipeline_shape(self):
        # The real call path: tofu's OpenAI body (system message hoisted by
        # openai_body_to_anthropic) must leave this adapter gateway-legal.
        anthropic_body = openai_body_to_anthropic({
            'model': 'kimi-k3',
            'messages': [
                {'role': 'system', 'content': 'You are Tofu.'},
                {'role': 'user', 'content': 'hi'},
            ],
        })
        body = apply_catpaw_shape(anthropic_body)
        first_text = (body['system'] if isinstance(body['system'], str)
                      else body['system'][0]['text'])
        self.assertTrue(first_text.lower().startswith('you are claude code'))
        self.assertIn('You are Tofu.', json.dumps(body['system']))

    def test_openai_translation_without_system_message(self):
        anthropic_body = openai_body_to_anthropic({
            'model': 'kimi-k3',
            'messages': [{'role': 'user', 'content': 'hi'}],
        })
        body = apply_catpaw_shape(anthropic_body)
        self.assertEqual(body['system'], CATPAW_IDENTITY)


class HeadersTest(unittest.TestCase):
    def test_working_dir_present_and_nonempty(self):
        hdrs = catpaw_headers()
        self.assertIn('X-Working-Dir', hdrs)
        self.assertTrue(hdrs['X-Working-Dir'].strip())


class TemplateTest(unittest.TestCase):
    def test_catpaw_template_loads_as_anthropic_protocol(self):
        from lib.provider.provider_template_recipes import load_provider_templates

        templates = {t['key']: t for t in load_provider_templates()}
        self.assertIn('catpaw', templates)
        tpl = templates['catpaw']
        self.assertEqual(tpl.get('protocol'), 'anthropic')
        self.assertEqual(tpl['base_url'], 'https://mcli.internal.example.com')
        model_ids = {r['model_id'] for r in tpl['offering_recipes']}
        self.assertIn('kimi-k3', model_ids)
        self.assertIn('glm-5.3', model_ids)

    def test_template_roster_stays_within_doc_whitelist(self):
        path = (Path(__file__).parents[2] / 'static' / 'provider_templates'
                / 'catpaw.json')
        roster = {r['model_id']
                  for r in json.loads(path.read_text())['offering_recipes']}
        whitelist = {
            'deepseek-v4-flash', 'deepseek-v4-pro',
            'kimi-k2.6', 'kimi-k2.7-code', 'kimi-k3',
            'MiniMax-M2.7', 'MiniMax-M3', 'gpt-5.4',
            'glm-5.1', 'glm-5.2', 'glm-5.3', 'glm-5.3-flash',
            'LongCat-2.0',
        }
        self.assertLessEqual(roster, whitelist)


if __name__ == '__main__':
    unittest.main()
