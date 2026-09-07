"""Knowledge visual enrichment must support stream-only vision slots."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_describe_uses_stream_dispatch_and_extracts_terminal_message(monkeypatch):
    from lib.knowledge import enrichment
    import lib.llm_dispatch as dispatch

    seen = {}

    def fake_stream(messages, *, on_content=None, **kwargs):
        seen.update(messages=messages, on_content=on_content, kwargs=kwargs)
        on_content('streamed fallback')
        return ({'role': 'assistant', 'content': 'factual description'},
                'stop', {'_dispatch': {'model': 'vision-stream-model'}})

    monkeypatch.setattr(dispatch, 'dispatch_stream', fake_stream)
    monkeypatch.setattr(enrichment, 'model_ready_image',
                        lambda raw, mime: (raw, 'image/png'))

    description, model = enrichment._describe(
        b'png-bytes', 'image/png', {'page': 3})

    assert description == 'factual description'
    assert model == 'vision-stream-model'
    assert seen['kwargs']['capability'] == 'vision'
    assert seen['kwargs']['max_tokens'] == 2200
    assert callable(seen['on_content'])
    assert seen['messages'][0]['content'][0]['image_url']['url'].startswith(
        'data:image/png;base64,')


def test_describe_falls_back_to_accumulated_stream_chunks(monkeypatch):
    from lib.knowledge import enrichment
    import lib.llm_dispatch as dispatch

    def fake_stream(messages, *, on_content=None, **kwargs):
        on_content('part one ')
        on_content('part two')
        return ({'role': 'assistant', 'content': ''}, 'stop', {})

    monkeypatch.setattr(dispatch, 'dispatch_stream', fake_stream)
    monkeypatch.setattr(enrichment, 'model_ready_image',
                        lambda raw, mime: (raw, 'image/png'))

    description, _ = enrichment._describe(b'x', 'image/png', {})

    assert description == 'part one part two'


def test_owner_description_uses_bounded_pinned_v2_route(monkeypatch):
    from types import SimpleNamespace

    from lib.knowledge import enrichment
    import lib.llm_dispatch as dispatch
    import lib.model_routing as routing
    from lib.llm_dispatch.provider_pin import get_pinned_provider

    group = SimpleNamespace(pin_id='knowledge-owner-vision')
    observed = {'pins': [], 'disposed': [], 'mint': []}

    def _mint(*_args, **kwargs):
        observed['mint'].append(kwargs)
        return 'vision-model', group

    def _stream(_messages, *, on_content=None, **_kwargs):
        observed['pins'].append(get_pinned_provider())
        return ({'role': 'assistant', 'content': 'owner description'},
                'stop', {})

    monkeypatch.setattr(routing, 'mint_capability_slot_group', _mint)
    monkeypatch.setattr(
        routing, 'dispose_routed_slot_group', observed['disposed'].append)
    monkeypatch.setattr(dispatch, 'dispatch_stream', _stream)
    monkeypatch.setattr(
        enrichment, 'model_ready_image', lambda raw, mime: (raw, 'image/png'))

    description, _model = enrichment._describe(
        b'png', 'image/png', {}, owner_user_id=73)

    assert description == 'owner description'
    assert observed['pins'] == ['knowledge-owner-vision']
    assert observed['disposed'] == [group]
    assert observed['mint'][0]['max_candidates'] == 8
