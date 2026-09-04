"""tests/test_sdk_sse_parser.py — _parse_sse covers all three conventions.

The SDK's parser handles:
  1. OpenAI: bare `data: {...}` lines, `data: [DONE]` terminator.
  2. Anthropic: `event: <name>` + `data: {...}`, `message_stop` terminator.
  3. Generic task stream: `id:` + `data: {...}`, terminal types
     (done/error/aborted) auto-close.
"""

from __future__ import annotations

import os
import sys
import unittest

import pytest


pytestmark = pytest.mark.unit


_SDK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         '..', 'clients', 'python')
sys.path.insert(0, os.path.abspath(_SDK_DIR))


class _FakeResp:
    """Minimal stand-in for ``requests.Response`` for ``iter_lines``."""

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


class ParseSSETest(unittest.TestCase):

    def setUp(self):
        from tofu_sdk import _parse_sse
        self.parse = _parse_sse

    def test_native_model_selection_is_structured_and_copied(self):
        from tofu_sdk import _native_model_payload

        source = {'creator_id': 'anthropic', 'model_id': 'claude'}
        payload = _native_model_payload(source)
        source['model_id'] = 'mutated-after-call'
        self.assertEqual(payload, {
            'creator_id': 'anthropic',
            'model_id': 'claude',
        })
        with self.assertRaises(TypeError):
            _native_model_payload('claude')
        with self.assertRaises(ValueError):
            _native_model_payload({'creator_id': 'anthropic'})

    def test_sync_and_async_agents_share_native_payload_builder(self):
        from tofu_sdk import _native_agent_payload

        model = {'creator_id': 'anthropic', 'model_id': 'claude'}
        routing = {'preferred_provider_id': 'provider-a'}
        payload = _native_agent_payload(
            messages=[{'role': 'user', 'content': 'hi'}],
            model=model,
            routing=routing,
        )
        model['model_id'] = 'mutated'
        routing['preferred_provider_id'] = 'mutated'
        self.assertEqual(payload['model']['model_id'], 'claude')
        self.assertEqual(
            payload['routing']['preferred_provider_id'], 'provider-a')
        with self.assertRaisesRegex(ValueError, 'inline provider blocks'):
            _native_agent_payload(
                messages=[{'role': 'user', 'content': 'hi'}],
                provider={'api_key': 'secret'},
            )

    def test_openai_simple(self):
        resp = _FakeResp([
            'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"hi"}}]}',
            '',
            'data: [DONE]',
            '',
        ])
        out = list(self.parse(resp))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['object'], 'chat.completion.chunk')

    def test_openai_multi_chunks(self):
        resp = _FakeResp([
            'data: {"i":1}', '',
            'data: {"i":2}', '',
            'data: {"i":3}', '',
            'data: [DONE]', '',
        ])
        out = list(self.parse(resp))
        self.assertEqual([x['i'] for x in out], [1, 2, 3])

    def test_anthropic_named_events(self):
        resp = _FakeResp([
            'event: message_start', 'data: {"type":"message_start"}',
            '',
            'event: content_block_delta',
            'data: {"type":"content_block_delta","delta":{"text":"hi"}}',
            '',
            'event: message_stop', 'data: {"type":"message_stop"}',
            '',
        ])
        out = list(self.parse(resp))
        self.assertEqual(len(out), 3)
        # Event name attached to each payload as `event` field.
        self.assertEqual(out[0]['event'], 'message_start')
        self.assertEqual(out[1]['event'], 'content_block_delta')
        self.assertEqual(out[2]['event'], 'message_stop')

    def test_generic_task_stream_auto_terminates_on_done(self):
        """Even if the server forgets to close the stream, parser stops
        on the first ``done``/``error``/``aborted`` payload."""
        resp = _FakeResp([
            'id: 0', 'data: {"type":"phase","detail":"x"}',
            '',
            'id: 1', 'data: {"type":"delta","content":"abc"}',
            '',
            'id: 2', 'data: {"type":"done","status":"done"}',
            '',
            # Server forgot to terminate — these MUST NOT be yielded:
            'data: {"type":"phase","detail":"would never see this"}',
            '',
        ])
        out = list(self.parse(resp))
        self.assertEqual(len(out), 3)
        self.assertEqual(out[-1]['type'], 'done')

    def test_heartbeat_lines_ignored(self):
        resp = _FakeResp([
            ': heartbeat',
            'data: {"i":1}', '',
            ': heartbeat',
            'data: {"i":2}', '',
            'data: [DONE]', '',
        ])
        out = list(self.parse(resp))
        self.assertEqual([x['i'] for x in out], [1, 2])

    def test_unparseable_chunks_skipped(self):
        resp = _FakeResp([
            'data: {valid_json:false',  # malformed
            '',
            'data: {"i":1}', '',
            'data: not json at all',
            '',
            'data: [DONE]', '',
        ])
        out = list(self.parse(resp))
        self.assertEqual(out, [{'i': 1}])

    def test_empty_data_lines_skipped(self):
        resp = _FakeResp([
            'data:',  # no payload
            '',
            'data: ',  # whitespace only
            '',
            'data: {"k":"v"}', '',
            'data: [DONE]', '',
        ])
        out = list(self.parse(resp))
        self.assertEqual(out, [{'k': 'v'}])

    def test_terminates_on_message_stop_anthropic(self):
        resp = _FakeResp([
            'event: content_block_delta',
            'data: {"type":"content_block_delta"}',
            '',
            'event: message_stop',
            'data: {"type":"message_stop"}',
            '',
            # MUST NOT be yielded:
            'event: should_be_unreachable',
            'data: {"type":"unreachable"}',
            '',
        ])
        out = list(self.parse(resp))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[-1]['event'], 'message_stop')


if __name__ == '__main__':
    unittest.main()
