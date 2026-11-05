"""Models tab: AA coverage counts published catalog scores and names the version.

After Artificial Analysis moved to Intelligence Index v5 the card claimed
"238 models matched real AA scores" beside a 22-point chart: it counted raw
score keys, including hidden drafts and rows AA lists without a v5 score.
"""
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


def _model(creator_id, model_id, name, *, state='ready'):
    return {
        'creator_id': creator_id, 'model_id': model_id, 'display_name': name,
        'capabilities': ['text'], 'context_window': 1_000_000, 'quality_rank': 1,
        'registration_state': state,
        'registration_issues': [] if state == 'ready' else ['registration_metadata.release_date'],
        'list_pricing': {'input': 1, 'output': 5, 'currency': 'USD',
                         'unit': 'per_million_tokens'},
    }


def _score(value, name):
    return {'intelligence': value, 'coding': None, 'agentic': None, 'math': None,
            'aa_name': name, 'aa_slug': name.lower().replace(' ', '-')}


def test_aa_card_counts_catalog_scores_and_chart_names_unscored_models(browser, live_server):
    from playwright.sync_api import expect
    from lib.model_routing.domain import empty_document

    routing = empty_document()
    routing['creators'] = [{'creator_id': 'openai', 'name': 'OpenAI'},
                           {'creator_id': 'anthropic', 'name': 'Anthropic'}]
    routing['models'] = [
        _model('openai', 'gpt-6-astra', 'GPT-6 Astra'),
        _model('anthropic', 'claude-opus-5.5', 'Claude Opus 5.5'),
        # Listed by AA without a v5 score / not listed at all: no score entry.
        _model('openai', 'gpt-5.4', 'GPT-5.4'),
        _model('openai', 'gpt-5.6', 'GPT-5.6'),
        # A scored draft is not a catalog Model and must not be counted.
        _model('openai', 'gpt-6-luna', 'GPT-6 Luna', state='pending_metadata'),
    ]
    aa_block = {
        'status': 'ok', 'index_version': 5, 'source': 'Artificial Analysis',
        'source_url': 'https://artificialanalysis.ai/', 'fetched_at': 1.0,
        'key_source': 'env', 'key_hint': '••••',
        'scores': {
            'openai::gpt-6-astra': _score(56.9, 'GPT-6 Astra'),
            'anthropic::claude-opus-5.5': _score(58.4, 'Claude Opus 5.5'),
            'openai::gpt-6-luna': _score(27.8, 'GPT-6 Luna'),
        },
    }
    context = browser.new_context(viewport={'width': 1280, 'height': 950}, service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done', '1');"
                            "localStorage.setItem('tofu_ui_lang', 'zh');")

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
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.locator('.settings-tab[data-tab="models"]').click()

        source = page.locator('#stgModelCatalog .stg-mc-aa-identity')
        expect(source).to_contain_text('AA Index v5')
        expect(source).to_contain_text('已为 2 个模型匹配真实 AA 分数')
        entry = page.locator('#stgModelCatalog .stg-mc-pareto-entry')
        expect(entry).to_contain_text('成本 × AA Index v5')
        expect(entry).to_contain_text('2 个模型 · 官方价格 · Pareto 前沿')

        entry.click()
        expect(page.locator('.stg-mc-pareto-head')).to_contain_text('成本 × AA Index v5')
        expect(page.locator('.stg-mc-pareto-note')).to_contain_text(
            '另有 2 个目录模型暂无 AA 分数，未入图')
    finally:
        context.close()
