"""Cache accounting through isolated storage, real HTTP, shipped UI and CSS.

Only synthetic test data in the repository's disposable app fixture is written;
no model calls, live conversations or shared configuration are used.
"""
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]


@pytest.fixture
def cache_turn(page):
    from lib.storage import get_storage_client
    from lib.turn_lifecycle import append_settled_turn

    usage = {'input_tokens': 60, 'output_tokens': 200,
             'cache_read_input_tokens': 8448, 'cache_creation_input_tokens': 27392}
    cost = {'totalInputTokens': 35900, 'inputTokens': 60, 'outputTokens': 200,
            'cacheReadTokens': 8448, 'cacheWriteTokens': 27392, 'costCny': 0.4}
    allocation = {'write': 27392, 'prevOutput': 0, 'toolResults': 0, 'contextWrite': 0,
                  'recacheBody': 26592, 'envelope': 800, 'readDrop': 25856,
                  'allocation': 'estimated', 'totalsSource': 'provider_usage'}
    legacy_allocation = {key: value for key, value in allocation.items()
                         if key not in ('allocation', 'totalsSource')}
    rounds = [
        {'round': 1, 'usage': usage, 'cost': cost, 'writeBreakdown': allocation},
        {'round': 2, 'usage': usage, 'cost': {'costCny': 0.4},
         'writeBreakdown': legacy_allocation, 'cacheBreak': {
             'tools': 'changed: search', 'prefix_mutation': 'prefix changed',
             'reasons': [
                 {'code': 'tools', 'detail': 'Tools changed: search', 'evidence': 'source_only'},
                 {'code': 'prefix_mutation', 'detail': 'Prefix changed <script>unsafe()</script>',
                  'evidence': 'wire_verified'},
             ]}},
        {'round': 3, 'usage': usage, 'cost': {'inputTokens': 60}},
        {'round': 4, 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'cache_read_tokens': 0},
         'cost': {'costCny': 0}},
        {'round': 5, 'usage': {}, 'cost': {}},
        {'round': 6, 'usage': {'prompt_tokens': 1000, 'cache_read_tokens': 1000,
                             'completion_tokens': 0}, 'cost': {}},
    ]
    conv_id = f'visual-cache-{uuid4().hex[:12]}'
    client = get_storage_client(write=True)
    try:
        result = append_settled_turn(
            conv_id, command_id=conv_id, actor='assistant', user_id=1,
            projection={'content': 'Synthetic cache accounting evidence', 'usage': usage,
                        'cost': cost, 'apiRounds': rounds, 'model': 'fixture'},
            conversation_defaults={'allowCreate': True, 'title': 'Cache browser evidence',
                                   'createdAt': 1, 'settings': {}})
        yield {'convId': conv_id, 'turnId': result['turn']['turnId']}
    finally:
        client.command('conversation.delete', {'conv_id': conv_id, 'user_id': 1},
                       f'{conv_id}:cleanup')


@pytest.mark.parametrize('language', ['zh', 'en'])
@pytest.mark.parametrize('width', [1100, 390])
def test_cache_accounting_popover(page, cache_turn, tmp_path, language, width):
    from tests.conftest import _dismiss_onboarding_modals

    page.set_viewport_size({'width': width, 'height': 900})
    # Browser-local preference only; do not save shared user configuration.
    page.evaluate("language => localStorage.setItem('tofu_ui_lang', language)", language)
    page.reload(wait_until='domcontentloaded')
    _dismiss_onboarding_modals(page)
    page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation') === 'function'")
    page.wait_for_function("language => document.documentElement.lang.startsWith(language)", arg=language)
    page.evaluate("id => window.TofuModules.resolveAction('loadConversation')(id)", cache_turn['convId'])
    turn = page.locator(f'[data-turn-id="{cache_turn["turnId"]}"]')
    turn.get_by_text('Synthetic cache accounting evidence', exact=True).wait_for()
    assert '35.9k' in turn.locator('.token-tag').inner_text()
    assert '60' in turn.locator('.token-tag').inner_text()
    turn.locator('.cost-tag-detail').click()
    popover = page.locator('.cost-popover')
    popover.wait_for(state='visible')
    rows = popover.locator('.cp-round')
    assert rows.count() == 6
    for index in (0, 1, 2):
        text = rows.nth(index).locator('.cp-round-tokens').inner_text()
        assert '35.9k' in text and '60' in text and '8.4k' in text
    assert rows.nth(2).locator('.cp-round-cost').inner_text() == ('待计算' if language == 'zh' else 'pending')
    assert rows.nth(3).locator('.cp-round-cost').inner_text() == '¥0'
    unknown = '未知' if language == 'zh' else 'unknown'
    assert unknown in rows.nth(4).locator('.cp-round-tokens').inner_text().lower()
    equality = rows.nth(5).locator('.cp-round-tokens').inner_text()
    assert '1.0k' in equality and '2.0k' not in equality
    estimated = '估算' if language == 'zh' else 'estimated'
    for index in (0, 1):
        allocation = rows.nth(index).locator('.cp-round-inflow')
        assert estimated in allocation.inner_text().lower()
        assert estimated in allocation.get_attribute('title').lower()
    waste = rows.nth(0).locator('.cp-round-waste')
    assert estimated in waste.inner_text().lower()
    assert '25.9k' in waste.inner_text()
    diagnostic = rows.nth(1).locator('.cp-round-break')
    assert ('Tools 改动: search' if language == 'zh' else 'Tools changed: search') in diagnostic.inner_text()
    assert 'Prefix changed <script>unsafe()</script>' in diagnostic.inner_text()
    assert ('不证明服务端原因' if language == 'zh' else 'not proof of provider cause') in diagnostic.inner_text()
    assert diagnostic.locator('script').count() == 0
    assert rows.nth(1).locator('.cp-break-badge-unproven').count() == 1
    assert '真实浪费' not in popover.inner_html()
    assert 'real waste' not in popover.inner_html()
    assert popover.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
    screenshot = tmp_path / f'cache-accounting-{language}-{width}.png'
    popover.screenshot(path=str(screenshot))
    print(f'Cache accounting screenshot: {screenshot}')
    turn.locator('.cost-tag-detail').click()
    popover.wait_for(state='hidden')
