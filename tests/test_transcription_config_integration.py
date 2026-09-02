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

from lib.mcp.registry import is_opensource_build
from lib.provider_template_recipes import offering_recipes

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

# The two Doubao-ASR guards drive the REAL shipped internal provider template
# (static/provider_templates/meituan.json), which is NOT exported to
# opensource builds — they would fail there on a missing file, not on the
# invariant they pin.
_REQUIRES_INTERNAL_TEMPLATE = pytest.mark.skipif(
    is_opensource_build(),
    reason='drives the internal provider template '
           '(static/provider_templates/meituan.json) — not shipped in '
           'opensource builds')


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


def _load_meituan_template_as_provider():
    """Load the real static/provider_templates/meituan.json as a provider config.

    Returns a single-provider list in server_config shape so
    ``_build_slots_from_providers`` exercises the ACTUAL shipped template — the
    only way to catch the DEFAULT_SLOT_CONFIGS caps-override regression (a
    stubbed provider would mask it).
    """
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'provider_templates', 'meituan.json'),
              encoding='utf-8') as f:
        tpl = json.load(f)
    return [{
        'id': tpl['key'],
        'base_url': tpl['base_url'],
        'api_keys': ['sk-mt'],
        'enabled': True,
        'extra_headers': tpl.get('extra_headers') or {},
        'models': offering_recipes(tpl, allow_legacy=False),
    }]


@_REQUIRES_INTERNAL_TEMPLATE
def test_doubao_asr_slot_built_from_real_meituan_template(monkeypatch):
    """Doubao-Seed-ASR-2.0 in the SHIPPED meituan template builds a slot that
    actually carries the 'transcription' cap and makes STT available.

    This is the load-bearing caps-override guard: the built slot's caps come
    from DEFAULT_SLOT_CONFIGS[mid] when present (dispatcher.py:405-410), NOT the
    template's capabilities list — so a template-only edit would silently DROP
    the cap and this would fail. Drives the real _build_slots_from_providers on
    the real template, not a stub.
    """
    import lib.transcription as tr

    d = _dispatcher_with(_load_meituan_template_as_provider())
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d)

    asr = [s for s in d.slots if s.model == 'Doubao-Seed-ASR-2.0']
    assert asr, 'Doubao-Seed-ASR-2.0 slot was not built from the meituan template'
    assert all('transcription' in s.capabilities for s in asr), \
        'ASR slot lost the transcription cap (DEFAULT_SLOT_CONFIGS override trap)'

    assert tr.transcription_available() is True
    assert {'model': 'Doubao-Seed-ASR-2.0', 'provider_id': 'meituan',
            'mode': 'endpoint'} in tr.list_transcription_models()


@_REQUIRES_INTERNAL_TEMPLATE
def test_doubao_asr_is_non_chat_and_distinct_from_doubao_chat(monkeypatch):
    """The ASR slot is never chat-picked and never carries cache markers, and it
    does not collide with the existing Doubao-Seed-2.0-pro chat entry.

    Correctness check the owner asked for: a pure 'transcription' slot must be
    excluded from the chat picker (it's in _NON_CHAT_CAPS), while the sibling
    Doubao chat model stays chat-capable. Cache markers only ever attach to
    chat requests, so a slot that is never chat-picked never gets them.
    """
    d = _dispatcher_with(_load_meituan_template_as_provider())
    d._initialized = True

    chat_models = {s.model for s in d.slots if d._is_chat_compatible(s)}
    # The ASR model is a pure transcription slot → excluded from chat.
    assert 'Doubao-Seed-ASR-2.0' not in chat_models
    # The pre-existing Doubao chat model is unaffected — still chat-capable.
    assert 'Doubao-Seed-2.0-pro' in chat_models

    # The two are genuinely distinct model ids with disjoint cap roles.
    asr_caps = {frozenset(s.capabilities) for s in d.slots
                if s.model == 'Doubao-Seed-ASR-2.0'}
    assert asr_caps == {frozenset({'transcription'})}


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
