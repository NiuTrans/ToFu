"""Request-local multimodal history projection for text-model fallback."""

from __future__ import annotations

import copy
import json

import pytest


pytestmark = pytest.mark.unit


def _text_blocks(message):
    content = message.get('content')
    if isinstance(content, str):
        return [content]
    return [
        block.get('text', '')
        for block in (content or [])
        if isinstance(block, dict) and block.get('type') == 'text'
    ]


def _image_blocks(messages):
    return [
        block
        for message in messages
        for block in (message.get('content') or [])
        if isinstance(message.get('content'), list)
        and isinstance(block, dict)
        and block.get('type') == 'image_url'
    ]


def test_vlm_to_text_projection_preserves_locality_and_durable_input(
        monkeypatch):
    import lib.llm.body._build as body_build

    monkeypatch.setattr(
        body_build, 'model_supports_vision',
        lambda model: model == 'vision-primary')
    messages = [
        {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': 'Before the figures.'},
                {'type': 'image_url', 'image_url': {
                    'url': 'data:image/png;base64,SECRET-PIXELS-A'}},
                {'type': 'text', 'text': '[image ref: /api/images/a.png]'},
                {'type': 'image_url', 'image_url': {
                    'url': 'https://images.invalid/b.png'}},
                {'type': 'text', 'text': 'After the figures.'},
            ],
        },
        {
            'role': 'assistant',
            'content': 'Earlier assistant description: the chart rises.',
        },
        {
            'role': 'user',
            'content': [
                {'type': 'image_url', 'image_url': {
                    'url': 'data:image/png;base64,SECRET-PIXELS-C'}},
                {'type': 'text', 'text': 'Caption: final panel.'},
            ],
        },
    ]
    durable_before = copy.deepcopy(messages)

    body = body_build.build_body(
        'text-fallback',
        messages,
        max_tokens=256,
        vision_fallback_from='vision-primary',
    )

    assert messages == durable_before, 'request projection mutated durable history'
    assert _image_blocks(body['messages']) == []
    serialized = json.dumps(body['messages'], ensure_ascii=False)
    assert 'SECRET-PIXELS' not in serialized
    assert '[image ref: /api/images/a.png]' in serialized
    assert 'Earlier assistant description: the chart rises.' in serialized
    assert 'Caption: final panel.' in serialized

    notices = [
        text
        for message in body['messages']
        for text in _text_blocks(message)
        if text.startswith('[Vision fallback projection:')
    ]
    assert len(notices) == 2, 'projection must add one marker per image message'
    assert '2 image(s)' in notices[0]
    assert '1 image(s)' in notices[1]
    assert 'vision-primary' not in serialized
    assert 'text-fallback' not in serialized
    assert 'Treat the pixels as unseen' in notices[0]

    first_texts = _text_blocks(body['messages'][0])
    assert first_texts[0] == 'Before the figures.'
    assert first_texts[1].startswith('[Vision fallback projection:')
    assert first_texts[2] == '[image ref: /api/images/a.png]'
    assert first_texts[3] == 'After the figures.'


def test_projection_growth_is_one_marker_per_message(monkeypatch):
    import lib.llm.body._build as body_build

    monkeypatch.setattr(
        body_build, 'model_supports_vision', lambda _model: False)
    messages = [{
        'role': 'user',
        'content': [
            {'type': 'image_url', 'image_url': {
                'url': f'https://images.invalid/{index}.png'}}
            for index in range(12)
        ],
    }]

    body = body_build.build_body('text-only', messages, max_tokens=128)
    content = body['messages'][0]['content']

    assert isinstance(content, str)
    assert content.count('[Text-only image projection:') == 1
    assert '12 image(s)' in content


def test_vision_target_keeps_image_blocks(monkeypatch):
    import lib.llm.body._build as body_build

    monkeypatch.setattr(
        body_build, 'model_supports_vision', lambda _model: True)
    messages = [{
        'role': 'user',
        'content': [
            {'type': 'text', 'text': 'Inspect this.'},
            {'type': 'image_url', 'image_url': {
                'url': 'https://images.invalid/kept.png'}},
        ],
    }]

    body = body_build.build_body(
        'vision-target', messages, max_tokens=128,
        vision_fallback_from='other-vision-model')

    images = _image_blocks(body['messages'])
    assert len(images) == 1
    assert images[0]['image_url']['url'] == \
        'https://images.invalid/kept.png'
    assert 'projection' not in json.dumps(body['messages']).lower()
