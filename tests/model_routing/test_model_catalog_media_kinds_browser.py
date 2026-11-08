"""Models tab: kinds group media models and media facts describe them.

Image, video, music, speech-synthesis and speech-recognition Models used to
render exactly like chat models: raw capability tags, an AA badge, a token
context window and "$0 / $0 per 1M tokens". The catalog now filters by kind
and shows each media Model's official media price and documented profile
(projected read-only from lib/model_info/media_model_facts.json).
"""
import re
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


def _model(creator_id, model_id, name, capabilities, **facts):
    return {
        'creator_id': creator_id, 'model_id': model_id, 'display_name': name,
        'capabilities': capabilities, 'context_window': 32_768, 'quality_rank': 1,
        'registration_state': 'ready', 'registration_issues': [],
        'inference_profile': {'reasoning': {'support': 'unsupported'}}, **facts,
    }


def _routing():
    from lib.model_routing.domain import empty_document, public_projection

    document = empty_document()
    document['creators'] = [{'creator_id': 'openai', 'name': 'OpenAI'},
                            {'creator_id': 'google', 'name': 'Google'}]
    document['models'] = [
        _model('openai', 'gpt-6-astra', 'GPT-6 Astra', ['text', 'vision'],
               list_pricing={'input': 1, 'output': 5, 'currency': 'USD',
                             'unit': 'per_million_tokens'}),
        _model('openai', 'gpt-image-2', 'GPT Image 2', ['image_gen']),
        _model('google', 'veo-3.1-generate-001', 'Veo 3.1', ['video_gen']),
        _model('google', 'lyria-3.5', 'Lyria 3.5', ['audio_gen']),
        _model('openai', 'tts-1', 'TTS-1', ['tts']),
        _model('openai', 'whisper-1', 'Whisper', ['transcription']),
    ]
    return public_projection(document)


def test_models_tab_filters_by_kind_and_shows_media_facts(browser, live_server):
    from playwright.sync_api import expect

    routing = _routing()
    aa_block = {'status': 'ok', 'index_version': 5, 'source': 'Artificial Analysis',
                'source_url': 'https://artificialanalysis.ai/', 'fetched_at': 1.0,
                'key_source': 'env', 'key_hint': '••••',
                'scores': {'openai::gpt-6-astra': {
                    'intelligence': 57.0, 'coding': None, 'agentic': None, 'math': None,
                    'aa_name': 'GPT-6 Astra', 'aa_slug': 'gpt-6-astra'}}}
    context = browser.new_context(viewport={'width': 1280, 'height': 1000}, service_workers='block')
    # Settings actions bind at boot completion; a click before it is lost.
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done', '1');"
                            "localStorage.setItem('tofu_ui_lang', 'zh');"
                            "window.__catalogAppReady = false;"
                            "window.addEventListener('tofu:app-ready', () => { window.__catalogAppReady = true; });")

    def route_request(route):
        url = urlparse(route.request.url)
        if url.netloc != urlparse(live_server).netloc:
            return route.abort()
        if url.path == '/api/v4/model-routing':
            return route.fulfill(json={'ok': True, 'revision': 1,
                'model_routing': routing, 'reasoning_controls': {},
                'metadata_inheritance_supported': True})
        if url.path == '/api/v4/model-intelligence/aa':
            return route.fulfill(json={'ok': True, 'aa': aa_block})
        if url.path == '/api/v4/update/check':
            return route.fulfill(json={'update_available': False})
        return route.continue_()

    context.route('**/*', route_request)
    page = context.new_page()
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.__catalogAppReady === true')
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.locator('.settings-tab[data-tab="models"]').click()

        bar = page.locator('#stgModelCatalog .stg-mc-kind-bar')
        expect(bar).to_be_visible()
        chips = bar.locator('.stg-mc-kind-chip')
        expect(chips).to_have_text([
            re.compile(r'全部\s*6'), re.compile(r'对话\s*1'), re.compile(r'图像生成\s*1'),
            re.compile(r'视频生成\s*1'), re.compile(r'音乐与音效\s*1'),
            re.compile(r'语音合成\s*1'), re.compile(r'语音识别\s*1'),
        ])
        expect(page.locator('#stgModelCatalog .stg-mc-aa-source')).to_be_visible()

        # Video: only Google remains, AA and the cost chart step aside.
        bar.locator('.stg-mc-kind-chip[data-kind="video_generation"]').click()
        expect(page.locator('#stgModelCatalog .stg-mc-aa-source')).to_have_count(0)
        tiles = page.locator('#stgModelCatalog .stg-mc-vendor-tile')
        expect(tiles).to_have_count(1)
        tiles.first.click()
        row = page.locator('#stgModelCatalog .stg-mc-model').filter(has_text='Veo 3.1')
        expect(row.locator('.stg-mc-kind-badge')).to_have_text('视频生成')
        expect(row.locator('.stg-mc-score')).to_have_count(0)
        brief = row.locator('.stg-mc-model-brief')
        expect(brief).to_contain_text('/秒')
        expect(brief).to_contain_text('起')
        expect(brief).not_to_contain_text('33K')
        row.locator('summary').click()
        facts = row.locator('.stg-mc-media-facts')
        expect(facts).to_contain_text('文本 + 图像 → 视频')
        expect(facts).to_contain_text('16:9 / 9:16')
        expect(facts).to_contain_text('4 / 6 / 8 秒')
        expect(facts).to_contain_text('原生音轨')
        expect(row.locator('.stg-mc-media-prices tr')).to_have_count(4)
        expect(row.locator('.stg-reasoning-editor')).to_have_count(0)

        # Speech synthesis: per-character price, voices and formats.
        bar.locator('.stg-mc-kind-chip[data-kind="speech_synthesis"]').click()
        page.locator('#stgModelCatalog .stg-mc-vendor-tile').first.click()
        tts = page.locator('#stgModelCatalog .stg-mc-model').filter(has_text='TTS-1')
        expect(tts.locator('.stg-mc-model-brief')).to_contain_text('/百万字符')
        tts.locator('summary').click()
        expect(tts.locator('.stg-mc-media-facts')).to_contain_text('alloy')
        expect(tts.locator('.stg-mc-media-facts')).to_contain_text('4,096')

        # Chat keeps the AA badge, context window and reasoning surface; the
        # open vendor (OpenAI, opened for TTS-1) stays open across kinds.
        bar.locator('.stg-mc-kind-chip[data-kind="chat"]').click()
        expect(page.locator('#stgModelCatalog .stg-mc-aa-source')).to_be_visible()
        expect(page.locator('#stgModelCatalog .stg-mc-vendor-tile.is-active')).to_contain_text('OpenAI')
        chat = page.locator('#stgModelCatalog .stg-mc-model').filter(has_text='GPT-6 Astra')
        expect(chat.locator('.stg-mc-score')).to_have_text('AA 57')
        expect(chat.locator('.stg-mc-kind-badge')).to_have_count(0)
        expect(chat.locator('.stg-mc-model-brief')).to_contain_text('33K')
    finally:
        context.close()
