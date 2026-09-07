"""Deck-level creative planning, asset-first authoring and coherence QA."""

from __future__ import annotations

import base64
import contextlib
import os
import threading

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
    monkeypatch.setenv('TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS', '7')

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
    assert all(kwargs['max_retries'] == 1
               and kwargs['max_429_attempts'] == 7
               for _prompt, kwargs in calls)

    damaged = tmp_path / first['records'][0]['path']
    damaged.write_bytes(b'corrupt!'[:first['records'][0]['bytes']])
    third = prepare_deck_assets(outline, str(tmp_path), parallel=1)
    assert not third['findings']
    assert len(calls) == count + 1


def test_slide_asset_preflight_abort_stops_new_admission(tmp_path, monkeypatch):
    outline = {'pages': [
        {'pageType': 'content', 'asset_mode': 'generate',
         'asset_prompt': f'editorial image {index}'}
        for index in range(4)
    ]}
    abort_event = threading.Event()
    calls = []

    def late_image(_prompt, **kwargs):
        calls.append(kwargs)
        abort_event.set()
        return {'ok': True, 'image_b64': base64.b64encode(b'late').decode(),
                'mime_type': 'image/png'}

    monkeypatch.setattr('lib.image_gen.generate_image', late_image)
    result = prepare_deck_assets(
        outline, str(tmp_path), parallel=1,
        abort_check=abort_event.is_set, max_429_attempts=7)

    assert len(calls) == 1
    assert calls[0]['max_retries'] == 1
    assert calls[0]['max_429_attempts'] == 7
    assert calls[0]['abort_check']() is True
    assert result['aborted'] is True
    assert result['records'] == []


def test_slide_asset_preflight_zero_budget_dispatches_nothing(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        'lib.image_gen.generate_image',
        lambda *_args, **_kwargs: pytest.fail('image call was not expected'))
    result = prepare_deck_assets(
        {'pages': [{'asset_mode': 'generate', 'asset_prompt': 'unused'}]},
        str(tmp_path), max_assets=0, parallel=1)

    assert result['records'] == []


def test_slide_media_io_bounds_base64_local_files_and_http_streams(
        tmp_path, monkeypatch):
    from lib.slides._media_io import (
        copy_file_bounded,
        decode_image_base64_bounded,
        download_file_bounded,
        hash_file_bounded,
    )

    with pytest.raises(ValueError, match='encoded slide image exceeds'):
        decode_image_base64_bounded('A' * 1000, max_bytes=100)

    oversized = tmp_path / 'oversized.png'
    oversized.write_bytes(b'x' * 101)
    with pytest.raises(ValueError, match='outside'):
        hash_file_bounded(str(oversized), max_bytes=100)

    source = tmp_path / 'source.png'
    source.write_bytes(b'y' * 100)
    digest, size = hash_file_bounded(str(source), max_bytes=100)
    copied = tmp_path / 'copied.png'
    copy_file_bounded(
        str(source), str(copied), expected_sha256=digest,
        expected_bytes=size, max_bytes=100)
    assert copied.read_bytes() == source.read_bytes()

    class Response:
        status_code = 200
        headers = {}

        @staticmethod
        def iter_content(*, chunk_size):
            assert chunk_size > 0
            yield b'a' * 60
            yield b'b' * 60

    @contextlib.contextmanager
    def stream(*_args, **_kwargs):
        yield Response()

    monkeypatch.setattr('lib.http_client.http_stream', stream)
    remote = tmp_path / 'remote.png'
    with pytest.raises(ValueError, match='stream exceeds'):
        download_file_bounded(
            'https://example.test/image.png', str(remote), max_bytes=100)
    assert not remote.exists()


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


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_page_author_shared_design_contract_precedes_page_divergence(lang):
    """Sibling prompts expose the full reusable design prefix first."""
    import os
    from types import SimpleNamespace
    from lib.slides.author import _build_prompt

    deck = SimpleNamespace(
        title='Stable deck', width=1280, height=720)
    theme = 'THEME-CONTEXT-' + ('theme ' * 800)
    bible = 'BIBLE-CONTEXT-' + ('discipline ' * 500)
    cheatsheet = 'PPTD-CONTEXT-' + ('schema ' * 900)
    prompts = [
        _build_prompt(
            deck,
            {'pageType': 'content', 'purpose': f'Unique purpose {index}',
             'key_message': f'Unique message {index}',
             'layout_hint': 'editorial', 'content_notes': 'grounded notes'},
            index, 8, theme, bible, cheatsheet, [], lang)
        for index in range(8)
    ]
    common_prefix = os.path.commonprefix(prompts)

    assert len(common_prefix) > 15_000
    assert all(marker in common_prefix
               for marker in ('THEME-CONTEXT', 'BIBLE-CONTEXT',
                              'PPTD-CONTEXT'))
    for index, prompt in enumerate(prompts):
        assert prompt.index('PPTD-CONTEXT') < prompt.index(
            f'Unique purpose {index}')


def test_prepared_page_author_context_preserves_exact_prompt():
    """Batch reuse changes construction cost, never page prompt identity."""
    from types import SimpleNamespace
    from lib.design_sys.themes import default_theme_id, get_theme
    from lib.slides.author import (
        _author_prompt, prepare_author_prompt_context)

    deck = SimpleNamespace(
        title='Context parity deck', width=1280, height=720)
    theme = get_theme(default_theme_id('tech-engineering'))
    brief = {
        'pageType': 'content', 'purpose': 'Explain the result',
        'key_message': 'A stable conclusion', 'layout_hint': 'editorial',
        'content_notes': 'Grounded notes',
    }
    context = prepare_author_prompt_context(deck, theme)

    direct_theme, direct_prompt = _author_prompt(
        deck, brief, 1, 8, theme=theme, lang='en')
    shared_theme, shared_prompt = _author_prompt(
        deck, brief, 1, 8, theme=theme, lang='en',
        prompt_context=context)

    assert shared_theme == direct_theme
    assert shared_prompt == direct_prompt
