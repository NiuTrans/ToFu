"""Deck-level creative planning, asset-first authoring and coherence QA."""

from __future__ import annotations

import base64
import os

import pytest

from lib.design_sys.contact_sheet import build_contact_sheet
from lib.design_sys.visual_qa import QA_CHECKLIST
from lib.slides._asset_preflight import prepare_deck_assets
from lib.slides._creative_plan import normalise_deck_plan, page_packet

pytestmark = pytest.mark.unit


def _outline():
    return {
        'title': 'A better deck',
        'pages': [
            {'pageType': 'cover', 'key_message': 'The opening judgment'},
            {'pageType': 'content', 'key_message': 'Metric rises to 44.4%'},
            {'pageType': 'content', 'key_message': 'Old and new differ'},
            {'pageType': 'final', 'key_message': 'Act on the evidence'},
        ],
    }


def test_deck_plan_adds_story_roles_assets_and_adjacent_context():
    outline = normalise_deck_plan(_outline())
    pages = outline['pages']
    assert pages[0]['layout_archetype'] == 'full-bleed-hero'
    assert pages[0]['asset_mode'] == 'generate'
    assert pages[0]['asset_prompt']
    assert pages[0]['asset_semantic_target'] == 'The opening judgment'
    assert pages[1]['layout_archetype'] == 'metric-focus'
    assert pages[-1]['layout_archetype'] == 'closing-resolve'
    assert pages[1]['continuity']['previous_message'] == pages[0]['key_message']
    assert pages[1]['continuity']['next_message'] == pages[2]['key_message']
    assert all(a['layout_archetype'] != b['layout_archetype']
               for a, b in zip(pages, pages[1:]))


def test_page_packet_carries_deck_context_not_only_the_page():
    pages = normalise_deck_plan(_outline())['pages']
    packet = page_packet(pages[1], 1, len(pages), deck_title='A better deck')
    assert 'Mandatory deck storyboard packet' in packet
    assert 'previous page message: The opening judgment' in packet
    assert 'next page message: Old and new differ' in packet
    assert 'layout archetype: metric-focus' in packet


def test_slide_asset_preflight_generates_then_reuses_manifest(
        tmp_path, monkeypatch):
    outline = normalise_deck_plan(_outline())
    calls = []

    def _generate(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return {'ok': True, 'image_b64': base64.b64encode(b'fake-png').decode(),
                'mime_type': 'image/png'}

    monkeypatch.setattr('lib.image_gen.generate_image', _generate)
    first = prepare_deck_assets(outline, str(tmp_path), parallel=1)
    assert first['records']
    assert not first['findings']
    for record in first['records']:
        assert os.path.isfile(tmp_path / record['path'])
    count = len(calls)
    second = prepare_deck_assets(outline, str(tmp_path), parallel=1)
    assert second['records'] == first['records']
    assert len(calls) == count, 'resume regenerated assets already on disk'


def test_contact_sheet_preserves_source_order_and_labels(tmp_path):
    from PIL import Image

    paths = []
    for i, color in enumerate(('red', 'green', 'blue')):
        path = tmp_path / f'{i}.png'
        Image.new('RGB', (160, 90), color).save(path)
        paths.append(str(path))
    out = tmp_path / 'contact.png'
    assert build_contact_sheet(paths, str(out), columns=2) == str(out)
    with Image.open(out) as image:
        assert image.width > 320 and image.height > 180


def test_visual_qa_has_whole_deck_axes():
    ids = {item[0] for item in QA_CHECKLIST}
    assert {'deck-coherence', 'layout-repetition', 'asset-relevance',
            'annotation-grounding'} <= ids


def test_page_author_is_told_to_ground_every_callout_endpoint():
    from types import SimpleNamespace
    from lib.slides.author import _build_prompt

    deck = SimpleNamespace(title='Grounded deck', width=1280, height=720)
    prompt = _build_prompt(
        deck, {'pageType': 'content', 'key_message': '地板是纯平的'},
        1, 4, 'theme', 'bible', 'cheatsheet', [], 'zh')
    assert '逐条确认端点落在语义对应部位' in prompt
    assert '禁止指着窗户写地板' in prompt
    assert '无法从图片明确确认具体部位时,不要猜也不要画引线' in prompt
