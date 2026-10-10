"""Failed write disclosure through the built app's actual tool-round renderer."""

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]


@pytest.mark.parametrize('width', [1100, 390])
@pytest.mark.parametrize('status', ['done', 'error'])
def test_failed_write_shows_reason_and_recovery_without_unapplied_diff(page, tmp_path, width, status):
    page.set_viewport_size({'width': width, 'height': 800})
    page.wait_for_function('typeof renderToolRoundsHTML === "function"')
    page.evaluate('''status => {
      const round = {
        roundNum: 2, toolName: 'write_file', status,
        query:'Write /handoff.md', results:[{writeOk:false}],
        toolArgs: JSON.stringify({path:'/handoff.md', content:'Never applied'}),
        toolContent: 'Write failed: Refusing to write to system path /handoff.md. '
          + 'Your content was NOT lost — staged to /tmp/recovery. <script>unsafe()</script>',
      };
      const host = document.createElement('section');
      host.id = 'write-failure-fixture';
      host.style.cssText = 'position:fixed;inset:30px 16px auto;z-index:99999;background:white;padding:16px;';
      host.innerHTML = renderToolRoundsHTML([round], false, null);
      document.body.appendChild(host);
    }''', status)
    card = page.locator('#write-failure-fixture')
    assert card.locator('summary').count() == 1
    assert 'Refusing to write to system path' in card.locator('summary').inner_text()
    assert card.locator('.ptool-badge-err').count() == 1
    assert card.locator('.ptool-badge-ok, .ptool-spinner').count() == 0
    assert not card.locator('.ptool-batch-fail-detail').is_visible()
    card.locator('summary').click()
    detail = card.locator('.ptool-batch-fail-detail')
    assert detail.is_visible()
    assert 'Your content was NOT lost' in detail.inner_text()
    assert '<script>unsafe()</script>' in detail.inner_text()
    assert card.locator('script, .bdiff-add').count() == 0
    assert card.evaluate('(node) => node.scrollWidth <= node.clientWidth + 1')
    screenshot = tmp_path / f'write-failure-{status}-{width}.png'
    card.screenshot(path=str(screenshot))
    print(f'Write failure screenshot: {screenshot}')
    card.locator('summary').click()
    assert not detail.is_visible()
