"""Actual lazy Trace styles and rendered evidence across themes and narrow screens."""
import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme,width', [(theme, width) for theme in ('light', 'dark', 'tofu') for width in (390, 1180)])
def test_inspector_trace_reading(page, assert_no_js_errors, tmp_path, theme, width):
    from playwright.sync_api import expect
    from tests.visual.test_visual_surfaces import _invoke_action, _wait_ready
    from tests.conftest import _dismiss_onboarding_modals

    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("localStorage.setItem('tofu_ui_lang', 'en')")
    page.reload(wait_until='domcontentloaded')
    _wait_ready(page)
    resources = page.evaluate('performance.getEntriesByType("resource").map(row => row.name)')
    assert not any('request-inspector-trace.css' in name or ('diagnostics-presenters' in name and '.css' in name) for name in resources)
    turn = {'turnId': 'trace-turn', 'taskId': 'trace-task', 'createdAt': 1, 'status': 'done'}
    wire = {'turns': [turn], 'sections': [dict(turn, type='turn', msgIndex=0)],
            'messages': [{'role': 'user', 'content': 'Read exact execution evidence'}]}
    spans = [{'spanId': name, 'parent': None, 'kind': kind, 'name': name, 'depth': depth,
              'tStart': start, 'tEnd': end, 'status': 'done', 'attrs': {}}
             for name, kind, depth, start, end in [('whole run', 'turn', 0, 0, 1000),
                                                  ('logical request', 'llm', 3, 0, 500),
                                                  ('provider attempt', 'llm', 4, 200, 500),
                                                  ('tool evidence', 'tool', 7, 500, 800)]]
    trace = {'version': 1, 'taskId': 'trace-task', 'eventsAvailable': True,
             'tStart': 0, 'totalMs': 1000, 'source': 'turn-snapshot', 'coverage': 'partial', 'coverageReason': 'observer-gap',
             'summary': {'llmMs': 500, 'toolMs': 300, 'waitMs': 0, 'unattributedMs': 200},
             'spans': spans, 'gaps': [{'tStart': 800, 'tEnd': 1000}],
             'statusHistory': [{'id': 'status', 'phase': 'reading trace', 'attention': 'progress', 'tStart': 0, 'tEnd': 500, 'lastObservedAt': 500, 'count': 1}],
             'clientObservations': [{'kind': 'terminal_painted', 'renderMs': 20, 'transportMs': 5}]}
    page.route('**/api/v4/tasks/conv-wire/*', lambda route: route.fulfill(json={'data': wire, 'meta': {}}))
    page.route('**/api/v4/tasks/trace-task/trace', lambda route: route.fulfill(json={'data': trace, 'meta': {}}))
    page.evaluate("window.activeConvId = 'trace-reading'")
    _invoke_action(page, 'toggleDebug')
    _dismiss_onboarding_modals(page)
    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
    page.locator('#debugContent .ri-feed-trace').click()
    content = page.locator('#debugContent')
    provider = content.locator('.tr-bar[title^="provider attempt"]')
    expect(provider).to_be_visible()
    expect(content).to_contain_text('tool evidence')
    expect(content).to_contain_text('observer-gap')
    expect(content).to_contain_text('reading trace 500ms')
    geometry = provider.evaluate('node => ({position:getComputedStyle(node).position, width:node.getBoundingClientRect().width, track:node.parentElement.getBoundingClientRect().width, font:parseFloat(getComputedStyle(node.firstElementChild).fontSize)})')
    assert geometry['position'] == 'absolute'
    assert abs(geometry['width'] - geometry['track'] * .3) <= 1
    assert geometry['font'] >= 11
    expect(content.locator('.tr-k-gap')).to_have_count(1)
    page.screenshot(path=str(tmp_path / f'inspector-trace-{theme}-{width}.png'))
    content.locator('.ri-feed-back button').click()
    trace = {'version': 1, 'taskId': 'trace-task', 'eventsAvailable': True, 'source': 'attempt-receipts',
             'clientObservations': [{'kind': 'terminal_painted', 'renderMs': 20}]}
    page.locator('#debugContent .ri-feed-trace').click()
    expect(content).to_contain_text('retained browser receipts')
    expect(content.locator('.tr-flame')).to_have_count(0)
    expect(content.locator('.ri-feed-back button')).to_be_visible()
