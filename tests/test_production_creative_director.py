"""Executable contracts for bounded candidate-and-critic production plans."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


def _slide_outline(title: str, modalities: list[str]) -> str:
    pages = []
    page_types = ['cover', 'content', 'final']
    layouts = ['full-bleed-hero', 'metric-focus', 'closing-resolve']
    for index in range(3):
        pages.append({
            'pageType': page_types[index],
            'purpose': f'purpose {index}',
            'key_message': f'{title} judgment {index}.',
            'layout_hint': layouts[index],
            'content_notes': f'evidence {index}',
            'layout_archetype': layouts[index],
            'visual_modality': modalities[index],
            'visual_anchor': 'one recurring line',
            'handoff': f'question {index + 1}',
            'asset_mode': 'code' if index == 1 else 'none',
        })
    return json.dumps({
        'title': title,
        'scenario': 'tech-engineering',
        'pages': pages,
    })


def test_slide_director_screens_two_candidates_and_obeys_critic(monkeypatch):
    from lib.slides import recipe

    replies = iter([
        (_slide_outline('Candidate A',
                        ['hero-image', 'native-chart', 'minimal-type']), {}),
        (_slide_outline('Candidate B',
                        ['hero-image', 'comparison', 'minimal-type']), {}),
        (json.dumps({'winner': 2, 'reason': 'B has the clearer visual handoff',
                     'scores': [{}, {}]}), {'total_tokens': 42}),
    ])
    calls = []

    def fake_chat(_messages, **kwargs):
        calls.append(kwargs['log_prefix'])
        return next(replies)

    monkeypatch.setattr(recipe, '_llm_chat', fake_chat)
    artifact = recipe._run_outline({
        'topic': 'T', 'lang': 'en', 'creative_mode': 'director',
        'max_pages': 3, 'artifacts': {'research': {'cards': []}},
    })

    assert artifact['title'] == 'Candidate B'
    assert artifact['creative_mode'] == 'director'
    assert artifact['director']['candidate_count'] == 2
    assert artifact['director']['winner_lens'] == 'spatial-narrative'
    assert calls == [
        '[Slides:outline:evidence-editorial]',
        '[Slides:outline:spatial-narrative]',
        '[Slides:outline-critic]',
    ]


def _motion_script(title: str, modality: str) -> str:
    return json.dumps({
        'title': title,
        'beats': [{
            'text': f'spoken {index}',
            'on_screen': f'caption {index}',
            'visual': f'distinct composition {index}',
            'visual_modality': modality if index == 0 else 'native-diagram',
            'assets': [{
                'role': 'subject', 'prompt': f'object {index}, no text',
                'semantic_target': f'visible object {index}',
            }],
            'media_queries': ([{
                'kind': 'video', 'query': 'specific factory robot footage',
                'semantic_target': 'robot arm moving a chassis',
            }] if modality == 'stock-video' and index == 0 else []),
        } for index in range(3)],
    })


def test_motion_director_preserves_multimodal_intent(monkeypatch):
    from lib.motion_video import _recipe as recipe

    replies = iter([
        (_motion_script('A', 'generated-still'), {}),
        (_motion_script('B', 'stock-video'), {}),
        (json.dumps({'winner': 2, 'reason': 'B uses claim-relevant footage',
                     'scores': [{}, {}]}), {}),
    ])
    monkeypatch.setattr(recipe, '_llm_chat',
                        lambda _messages, **_kwargs: next(replies))
    artifact = recipe._run_script({
        'topic': 'T', 'lang': 'en', 'creative_mode': 'director',
        'max_scenes': 5, 'artifacts': {'research': {'cards': []}},
    })

    assert artifact['title'] == 'B'
    assert artifact['director']['winner_lens'] == 'documentary-multimodal'
    assert artifact['beats'][0]['visual_modality'] == 'stock-video'
    assert artifact['beats'][0]['media_queries'][0]['kind'] == 'video'


def test_creative_mode_changes_checkpoint_identity():
    from lib.motion_video._recipe import video_recipe_stages
    from lib.slides.recipe import slides_recipe_stages

    slide_standard = slides_recipe_stages('standard')[1].checkpoint_version
    slide_director = slides_recipe_stages('director')[1].checkpoint_version
    motion_standard = video_recipe_stages('standard')[1].checkpoint_version
    motion_director = video_recipe_stages('director')[1].checkpoint_version
    assert slide_standard != slide_director
    assert motion_standard != motion_director


def test_high_level_tools_expose_director_and_standard_controls():
    from lib.tools.produce import PRODUCE_SLIDES_TOOL, PRODUCE_VIDEO_TOOL

    for tool in (PRODUCE_SLIDES_TOOL, PRODUCE_VIDEO_TOOL):
        creative = tool['function']['parameters']['properties']['creative_mode']
        assert creative['enum'] == ['director', 'standard']


def test_slide_job_rejects_unknown_creative_mode():
    from lib.slides.engine import start_slides_job

    with pytest.raises(ValueError, match='creative_mode'):
        start_slides_job(
            'topic', creative_mode='surprise-me', user_id=1)


def test_motion_api_rejects_unknown_creative_mode(flask_client):
    response = flask_client.post('/api/v1/motion/videos', json={
        'topic': 'topic', 'creative_mode': 'surprise-me',
    })
    assert response.status_code == 400
    assert response.get_json()['ok'] is False


def test_shared_media_query_contract_is_bounded_and_semantic():
    from lib.production.contracts import normalise_media_queries

    queries = normalise_media_queries([
        {'kind': 'video', 'query': '  assembly line  ',
         'must_show': 'robot arm installs door'},
        {'kind': 'invented', 'query': 'x', 'semantic_target': 'y'},
        {'kind': 'gif', 'query': '', 'semantic_target': 'missing query'},
    ], max_items=2)
    assert queries == [
        {'kind': 'video', 'query': 'assembly line',
         'semantic_target': 'robot arm installs door'},
        {'kind': 'image', 'query': 'x', 'semantic_target': 'y'},
    ]


def test_stock_preflight_materialises_and_attributes_media(tmp_path,
                                                            monkeypatch):
    from lib.motion_video._asset_preflight import (
        collect_media_attribution,
        prepare_scene_assets,
    )
    import lib.production.stock_media as stock_media

    workdir = tmp_path / 'job'
    scene_dir = workdir / 'scenes' / 'scene-001'
    (scene_dir / 'assets').mkdir(parents=True)

    def fake_resolve(request):
        return {
            'ok': True, 'data': b'bounded fake mp4', 'suffix': '.mp4',
            'provider': 'Pexels',
            'requested_kind': request['kind'], 'media_kind': 'video',
            'query': request['query'], 'page_url': 'https://pexels.com/v/1',
            'creator': 'A Creator', 'creator_url': 'https://pexels.com/@a',
            'provider_url': 'https://www.pexels.com',
            'license_hint': 'Pexels API terms',
        }

    monkeypatch.setattr(stock_media, 'resolve_stock_media', fake_resolve)
    scene = {'id': 'scene-001', 'assets': [], 'media_queries': [{
        'kind': 'video', 'query': 'factory robot arm',
        'semantic_target': 'arm installs the vehicle door',
    }]}
    prepared = prepare_scene_assets(scene, str(scene_dir))
    assert prepared['findings'] == []
    assert prepared['resolved'][0]['path'].startswith('assets/stock_')
    ledger = collect_media_attribution(str(workdir))
    assert ledger['records'] == 1
    assert (workdir / 'media_attribution.txt').read_text().startswith(
        'Media provided by Pexels')


def test_stock_provider_without_key_degrades_without_network(monkeypatch):
    from lib.production.stock_media import resolve_stock_media

    monkeypatch.delenv('PEXELS_API_KEY', raising=False)
    result = resolve_stock_media({
        'kind': 'video', 'query': 'factory',
        'semantic_target': 'working robot arm',
    })
    assert result == {
        'ok': False, 'reason': 'PEXELS_API_KEY is not configured'}


def test_stock_provider_bounds_download_and_never_returns_credential(
        monkeypatch):
    import contextlib
    import json

    import lib.http_client as http_client
    from lib.production.stock_media import resolve_stock_media

    captured = {}

    class SearchResponse:
        content = json.dumps({'videos': [{
            'url': 'https://www.pexels.com/video/123/',
            'user': {'name': 'Creator',
                     'url': 'https://www.pexels.com/@creator'},
            'video_files': [{
                'file_type': 'video/mp4', 'width': 1280,
                'link': 'https://videos.pexels.com/video-files/123.mp4',
            }],
        }]}).encode()

        @staticmethod
        def raise_for_status():
            return None

    class MediaResponse:
        headers = {'content-type': 'video/mp4', 'content-length': '8'}

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def iter_content(chunk_size):
            assert chunk_size == 256 * 1024
            yield b'fake-mp4'

    def fake_get(_url, **kwargs):
        captured.update(kwargs)
        return SearchResponse()

    @contextlib.contextmanager
    def fake_stream(_method, _url, **_kwargs):
        yield MediaResponse()

    monkeypatch.setenv('PEXELS_API_KEY', 'super-secret')
    monkeypatch.setattr(http_client, 'http_get', fake_get)
    monkeypatch.setattr(http_client, 'http_stream', fake_stream)
    result = resolve_stock_media({
        'kind': 'video', 'query': 'robot arm',
        'semantic_target': 'arm moving',
    })
    assert result['ok'] and result['data'] == b'fake-mp4'
    assert captured['headers']['Authorization'] == 'super-secret'
    public = {key: value for key, value in result.items() if key != 'data'}
    assert 'super-secret' not in repr(public)
