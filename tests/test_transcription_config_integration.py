"""Configured runtime slots drive the voice-input capability surface.

Model-routing v2 owns how ProviderAccess resources become Slots; this suite
owns the downstream transcription contract: endpoint/audio-chat capability
projection, HTTP visibility, chat exclusion, and subscription exclusion.
"""

from __future__ import annotations

import asyncio

import pytest


pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _dispatcher_with(rows):
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.llm_dispatch.slot import Slot

    dispatcher = LLMDispatcher()
    dispatcher.slots = [
        Slot(
            key_name=f'credential:{index}',
            api_key='test-key',
            model=row['model'],
            logical_model=row['model'],
            capabilities=set(row['capabilities']),
            base_url=row.get('base_url', 'https://speech.example/v1'),
            provider_id=row.get('provider_id', 'stt-provider'),
            oauth=row.get('oauth', ''),
        )
        for index, row in enumerate(rows)
    ]
    dispatcher.initialize = lambda: None
    return dispatcher


def _speech_rows():
    return [
        {
            'model': 'my-whisper',
            'provider_id': 'stt-provider',
            'capabilities': ['transcription'],
        },
        {
            'model': 'my-chat',
            'provider_id': 'stt-provider',
            'capabilities': ['text'],
        },
    ]


def test_transcription_slot_makes_voice_input_available(monkeypatch):
    import lib.transcription as transcription

    dispatcher = _dispatcher_with(_speech_rows())
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: dispatcher)

    assert transcription.transcription_available() is True
    assert {
        'model': 'my-whisper',
        'provider_id': 'stt-provider',
        'mode': 'endpoint',
    } in transcription.list_transcription_models()
    assert all(
        row['model'] != 'my-chat'
        for row in transcription.list_transcription_models())


def test_audio_chat_slot_is_reported_as_chat_mode(monkeypatch):
    import lib.transcription as transcription

    dispatcher = _dispatcher_with([{
        'model': 'gemini-audio',
        'provider_id': 'omni-provider',
        'capabilities': ['text', 'vision', 'audio_chat'],
    }])
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: dispatcher)

    assert transcription.transcription_available() is True
    assert {
        'model': 'gemini-audio',
        'provider_id': 'omni-provider',
        'mode': 'chat',
    } in transcription.list_transcription_models()


def test_chat_only_pool_stays_unavailable(monkeypatch):
    import lib.transcription as transcription

    dispatcher = _dispatcher_with([{
        'model': 'chat-only',
        'capabilities': ['text'],
    }])
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: dispatcher)

    assert transcription.transcription_available() is False
    assert transcription.list_transcription_models() == []


def test_capabilities_endpoint_projects_owner_route(client, monkeypatch):
    from types import SimpleNamespace

    import lib.model_routing as routing

    def _route_groups(_repository, _boundary, requirements):
        return {
            capability: ([SimpleNamespace(
                model_id='my-whisper',
                provider_id='stt-provider',
                offering_id='offering-stt',
            )] if capability == 'transcription' else [])
            for capability in requirements
        }

    monkeypatch.setattr(routing, 'list_capability_route_groups', _route_groups)

    async def go():
        response = await client.get('/api/v1/audio/capabilities')
        assert response.status_code == 200
        data = await response.get_json()
        assert data['available'] is True
        assert {
            'model': 'my-whisper',
            'provider_id': 'stt-provider',
            'mode': 'endpoint',
        } in data['models']

    _run_async(go())


def test_transcription_only_slot_is_not_chat_compatible():
    dispatcher = _dispatcher_with(_speech_rows())

    chat_models = {
        slot.model for slot in dispatcher.slots
        if dispatcher._is_chat_compatible(slot)
    }

    assert 'my-chat' in chat_models
    assert 'my-whisper' not in chat_models


def test_subscription_slot_with_transcription_cap_is_excluded(monkeypatch):
    import lib.transcription as transcription

    dispatcher = _dispatcher_with([{
        'model': 'subscription-whisper',
        'capabilities': ['transcription'],
        'oauth': 'claude',
    }])
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: dispatcher)

    assert transcription.transcription_available() is False
