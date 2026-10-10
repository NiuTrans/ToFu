"""Real-browser rendering of saved tool-result continuation headers.

The card HTML comes from the same retained renderer as conversation turns;
the isolated visual page supplies the shipped CSS and a narrow viewport.
"""
from __future__ import annotations

import pytest

from tests.frontend.test_artifact_continuation_chip import _render

pytestmark = [pytest.mark.visual, pytest.mark.slow]


def test_saved_result_cards_remain_readable_at_narrow_width(page, live_server, tmp_path):
    ref = 'tool-result:' + 'a' * 64
    rounds = [{
        'status': 'done', 'toolName': 'read_tool_artifact', 'roundNum': 2,
        'query': 'Read 1 saved tool results',
        'toolArgs': {'reads': [{'artifact_ref': ref, 'cursor': '16384',
                                'selector': '/items/0', 'limit': 8192}]},
        'toolContent': 'Read result preview', 'results': [],
    }, {
        'status': 'done', 'toolName': 'search_tool_artifact', 'roundNum': 3,
        'query': 'Search 2 saved tool results',
        'toolArgs': {'searches': [
            {'artifact_ref': ref, 'query': 'fetch_url', 'cursor': '0'},
            {'artifact_ref': ref, 'query': 'truncated evidence',
             'cursor': 'trq1.signed'},
        ]},
        'toolContent': 'Search result preview', 'results': [],
    }]
    markup = ''.join(_render(rounds, tmp_path))
    # The app is already booted by the browser fixture. Navigate away before
    # adding synthetic cards so its async conversation projection cannot replace
    # the test host while Playwright is capturing the element screenshot.
    page.goto('about:blank')
    page.set_viewport_size({'width': 420, 'height': 700})
    page.set_content(
        '<!doctype html><html><head><meta charset="utf-8"></head>'
        '<body><main id="artifact-visual-fixture" class="conversation-surface"'
        ' style="width:380px;max-width:calc(100vw - 32px);margin:16px">'
        f'{markup}</main></body></html>'
    )
    stylesheet = page.request.get(f'{live_server}/static/styles.css')
    assert stylesheet.ok
    page.add_style_tag(content=stylesheet.text())
    host = page.locator('#artifact-visual-fixture')
    assert host.locator('.ptool-artifact-details').count() == 2
    assert host.locator('.ptool-result-block').count() == 2
    assert 'byte 16,384 · selector /items/0' in host.inner_text()
    assert 'fetch_url' in host.inner_text()
    assert 'truncated evidence' in host.inner_text()
    assert 'trq1.signed' not in host.inner_text()
    assert page.evaluate("""() => {
      const host = document.getElementById('artifact-visual-fixture');
      return [...host.querySelectorAll('.ptool-result-header')]
        .every(row => row.scrollWidth <= row.clientWidth + 1);
    }""")
    assert host.is_visible()
    # CSS must be the shipped asset, not the unstyled browser default.
    assert host.locator('.ptool-text').first.evaluate(
        "node => getComputedStyle(node).fontFamily.includes('JetBrains Mono')")
    host.screenshot(path=str(tmp_path / 'artifact-continuation-narrow.png'))
