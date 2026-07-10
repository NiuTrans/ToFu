#!/usr/bin/env python3
"""Config → mic-appears integration for voice input.

The other transcription tests (tests/test_audio_transcribe.py) STUB
``_transcription_slots`` to isolate the route. This suite closes the loop those
don't cover: it drives the REAL dispatcher slot-build path
(``LLMDispatcher._build_slots_from_providers``) from a provider config whose
model carries ``capabilities: ['transcription']`` and asserts:

  * ``transcription_available()`` flips to True (was False with no such slot)
  * ``list_transcription_models()`` reports that model
  * ``GET /api/v1/audio/capabilities`` then returns ``available: true`` with the
    model listed
  * a transcription-only slot is NEVER selected by the chat picker
  * an ``oauth`` (subscription) slot with the cap is excluded (no
    /audio/transcriptions endpoint there)

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_transcription_config_integration.py -v
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


@pytest.fixture
def client(flask_app):
    """Raw Quart test client (async), matching tests/test_server_async.py."""
    return flask_app.test_client()


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _dispatcher_with(providers):
    """Build a throwaway dispatcher's slots from a provider list (real path)."""
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    d = LLMDispatcher()
    d.slots = []
    d._build_slots_from_providers(providers)
    # _transcription_slots() calls dispatcher.initialize(); make it a no-op so
    # our hand-built slot list is not overwritten by config discovery.
    d.initialize = lambda: None
    return d


def _transcription_provider():
    return [{
        'id': 'stt_prov',
        'base_url': 'https://stt.example/v1',
        'api_keys': ['sk-stt'],
        'enabled': True,
        'models': [
            {'model_id': 'my-whisper', 'capabilities': ['transcription'], 'rpm': 60},
            {'model_id': 'my-chat', 'capabilities': ['text'], 'rpm': 60},
        ],
    }]


# ── The core loop ────────────────────────────────────────────────────────

def test_configured_transcription_model_makes_it_available(monkeypatch):
    """A real slot built from config flips transcription_available() to True."""
    import lib.transcription as tr

    d = _dispatcher_with(_transcription_provider())
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d)

    # The transcription-capable slot was built and is discoverable.
    assert tr.transcription_available() is True
    models = tr.list_transcription_models()
    assert {'model': 'my-whisper', 'provider_id': 'stt_prov',
            'mode': 'endpoint'} in models
    # The plain chat model must NOT be reported as a transcription target.
    assert all(m['model'] != 'my-chat' for m in models)


def test_configured_audio_chat_model_is_available_as_chat_mode(monkeypatch):
    """An omni model tagged audio_chat is discoverable with mode='chat' via the
    REAL slot-build path (not just the multipart 'transcription' cap)."""
    import lib.transcription as tr

    d = _dispatcher_with([{
        'id': 'meituan', 'base_url': 'https://aigc.example/v1/openai/native',
        'api_keys': ['sk-mt'], 'enabled': True,
        'models': [
            {'model_id': 'gemini-3-flash-preview',
             'capabilities': ['text', 'vision', 'audio_chat'], 'rpm': 60},
        ],
    }])
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d)
    assert tr.transcription_available() is True
    assert {'model': 'gemini-3-flash-preview', 'provider_id': 'meituan',
            'mode': 'chat'} in tr.list_transcription_models()


def test_no_transcription_model_stays_unavailable(monkeypatch):
    """A config with only chat models leaves transcription unavailable."""
    import lib.transcription as tr

    d = _dispatcher_with([{
        'id': 'chatonly', 'base_url': 'https://x/v1', 'api_keys': ['sk'],
        'enabled': True,
        'models': [{'model_id': 'gpt-chat', 'capabilities': ['text']}],
    }])
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d)
    assert tr.transcription_available() is False
    assert tr.list_transcription_models() == []


def test_capabilities_endpoint_reports_configured_model(client, monkeypatch):
    """GET /api/v1/audio/capabilities reflects the real configured slot."""
    import lib.transcription as tr
    d = _dispatcher_with(_transcription_provider())
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d)

    async def go():
        r = await client.get('/api/v1/audio/capabilities')
        assert r.status_code == 200, r.status_code
        data = await r.get_json()
        assert data['available'] is True
        assert {'model': 'my-whisper', 'provider_id': 'stt_prov',
                'mode': 'endpoint'} in data['models']
    _run_async(go())


def test_transcription_slot_is_not_chat_picked(monkeypatch):
    """A transcription-only slot must never be chosen for a chat request.

    'transcription' is in _NON_CHAT_CAPS, so _is_chat_compatible excludes it.
    """
    d = _dispatcher_with(_transcription_provider())
    d._initialized = True
    chat_slots = [s for s in d.slots if d._is_chat_compatible(s)]
    # my-chat is chat-compatible; my-whisper (transcription-only) is not.
    assert any(s.model == 'my-chat' for s in chat_slots)
    assert all(s.model != 'my-whisper' for s in chat_slots)


def test_oauth_slot_with_cap_is_excluded(monkeypatch):
    """A subscription (oauth) slot carrying the cap is not a transcription target.

    The Claude/Codex subscription endpoints do not expose /audio/transcriptions.
    """
    import lib.transcription as tr
    d = _dispatcher_with([{
        'id': 'sub', 'base_url': 'https://sub/v1', 'api_keys': ['sk'],
        'enabled': True, 'oauth': 'claude',
        'models': [{'model_id': 'sub-whisper', 'capabilities': ['transcription']}],
    }])
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d)
    # Every built slot is oauth → filtered out → nothing available.
    assert all(s.oauth for s in d.slots)
    assert tr.transcription_available() is False


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
