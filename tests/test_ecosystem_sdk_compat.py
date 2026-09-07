"""tests/test_ecosystem_sdk_compat.py — Real OpenAI SDK against /v1/.

Validates that an unmodified `openai` Python SDK works against Tofu's
``/v1/chat/completions`` and ``/v1/models`` endpoints. This is the
canonical drop-in test — if the SDK breaks, the compat shim is broken.

We share the boot fixture with `test_sdk_e2e.py` so we only spin up
one Hypercorn instance per pytest run.
"""

from __future__ import annotations

import os
import sys
import unittest

import pytest

from tests.support.model_routing import (
    allow_native_test_endpoint,
    native_test_model,
)


# Importing ``test_sdk_e2e`` below reuses its Python helper, but pytest does not
# inherit plugins from imported modules.  Declare the storage authority here as
# well so setUpClass cannot mint credentials against an unstarted/fenced global
# runtime when this module is collected on its own or assigned to another
# xdist worker.
pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.api


@pytest.fixture(scope='module', autouse=True)
def _native_test_endpoint_policy():
    with allow_native_test_endpoint():
        yield


# Reuse the fixture machinery from test_sdk_e2e.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope='module', autouse=True)
def _ecosystem_server_lifecycle(credential_storage):
    """Order the shared server after storage and never leak its daemon thread."""
    yield
    from test_sdk_e2e import _shutdown_real_server
    _shutdown_real_server()


@unittest.skipIf(
    os.environ.get('TOFU_SKIP_NETWORK_E2E') == '1',
    'TOFU_SKIP_NETWORK_E2E=1 set — skipping real-network compat test')
class OpenAISDKCompatTest(unittest.TestCase):

    # The invalid-key→401 contract only holds when the credential gate is
    # active (private/multi-user mode); the per-test conftest fixture forces
    # private and the in-process server reads the mode per-request.
    pytestmark = pytest.mark.auth_mode('private')

    @classmethod
    def setUpClass(cls):
        try:
            import openai  # noqa
        except ImportError:
            raise unittest.SkipTest('openai SDK not installed')
        from test_sdk_e2e import _boot_real_server, _STATE
        _boot_real_server()
        cls.base = f'http://127.0.0.1:{_STATE["port"]}/v1'
        cls.user_token = _STATE['user_token']
        cls.admin_token = _STATE['admin_token']

    def _client(self):
        from openai import OpenAI
        return OpenAI(api_key=self.user_token, base_url=self.base)

    def test_chat_completion_via_openai_sdk(self):
        """The canonical drop-in test: unmodified openai SDK → Tofu."""
        client = self._client()
        resp = client.chat.completions.create(
            model=native_test_model()['model_id'],
            messages=[{'role': 'user', 'content': 'OPENAI_SDK_PING'}],
        )
        self.assertEqual(resp.object, 'chat.completion')
        self.assertEqual(resp.model, native_test_model()['model_id'])
        # Stub echoes the prompt → confirms the SDK delivered it
        self.assertIn('OPENAI_SDK_PING',
                       resp.choices[0].message.content or '')
        self.assertIn(resp.choices[0].finish_reason,
                       {'stop', 'length', 'tool_calls', 'content_filter'})
        self.assertGreater(resp.usage.total_tokens, 0)

    def test_chat_streaming_via_openai_sdk(self):
        """Real OpenAI SDK streaming over real bytes."""
        client = self._client()
        accumulated = []
        for chunk in client.chat.completions.create(
            model=native_test_model()['model_id'],
            messages=[{'role': 'user', 'content': 'OAS_STREAM'}],
            stream=True,
        ):
            self.assertEqual(chunk.object, 'chat.completion.chunk')
            d = chunk.choices[0].delta
            if d.content:
                accumulated.append(d.content)
        joined = ''.join(accumulated)
        self.assertIn('OAS_STREAM', joined)

    def test_models_via_openai_sdk(self):
        client = self._client()
        models = client.models.list()
        # SDK iterates the list; should be at least the defaults
        ids = [m.id for m in models.data]
        # Models depend on the running config; just verify shape works
        for m in models.data:
            self.assertEqual(m.object, 'model')
            self.assertTrue(m.id)

    def test_invalid_key_raises_authentication_error(self):
        from openai import OpenAI, AuthenticationError
        bad = OpenAI(api_key='tofu_live_' + 'z' * 32, base_url=self.base)
        with self.assertRaises(AuthenticationError):
            bad.chat.completions.create(
                model=native_test_model()['model_id'],
                messages=[{'role': 'user', 'content': 'x'}])


@unittest.skipIf(
    os.environ.get('TOFU_SKIP_NETWORK_E2E') == '1',
    'TOFU_SKIP_NETWORK_E2E=1 set')
class AnthropicSDKCompatTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import anthropic  # noqa
        except ImportError:
            raise unittest.SkipTest('anthropic SDK not installed')
        from test_sdk_e2e import _boot_real_server, _STATE
        _boot_real_server()
        # Anthropic SDK appends /v1/messages itself, base is the host.
        cls.base = f'http://127.0.0.1:{_STATE["port"]}'
        cls.user_token = _STATE['user_token']

    def _client(self):
        from anthropic import Anthropic
        return Anthropic(api_key=self.user_token, base_url=self.base)

    def test_messages_via_anthropic_sdk(self):
        client = self._client()
        msg = client.messages.create(
            model=native_test_model()['model_id'],
            max_tokens=100,
            messages=[{'role': 'user', 'content': 'ANTHRO_SDK_PING'}],
        )
        self.assertEqual(msg.type, 'message')
        self.assertEqual(msg.role, 'assistant')
        # Find text block
        text = ''
        for block in msg.content:
            if block.type == 'text':
                text += block.text
        self.assertIn('ANTHRO_SDK_PING', text)
        self.assertIn(msg.stop_reason,
                       {'end_turn', 'max_tokens', 'stop_sequence',
                        'tool_use'})

    def test_messages_streaming_via_anthropic_sdk(self):
        client = self._client()
        accumulated = []
        with client.messages.stream(
            model=native_test_model()['model_id'],
            max_tokens=100,
            messages=[{'role': 'user', 'content': 'ANTHRO_SDK_STREAM'}],
        ) as stream:
            for text in stream.text_stream:
                accumulated.append(text)
        joined = ''.join(accumulated)
        self.assertIn('ANTHRO_SDK_STREAM', joined)


if __name__ == '__main__':
    unittest.main()
