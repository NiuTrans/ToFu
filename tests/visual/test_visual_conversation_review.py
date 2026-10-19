"""Review badges over real committed Turns, local server and Chromium.

Focus is the only simulated browser capability; no model/provider is invoked.
"""
import uuid

import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize("language", ["zh", "en"])
def test_completed_reply_review_survives_reload_and_clears_on_open(browser, live_server, tmp_path, language):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    context = browser.new_context(service_workers='block', viewport={'width': 1280, 'height': 800})
    prefix = 'review-' + uuid.uuid4().hex[:10]
    target, other = prefix + '-target', prefix + '-other'
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    context.add_init_script(f"localStorage.setItem('tofu_ui_lang', '{language}');")
    context.add_init_script("""
      localStorage.setItem('tofu_onboarding_v1_done', '1');
      window.reviewFocused = false;
      Object.defineProperty(document, 'hasFocus', { value: () => window.reviewFocused });
    """)

    def append(cid, suffix, actor, content, create=False):
        body = {'commandId': prefix + suffix, 'actor': actor, 'kind': 'text',
                'projection': {'content': content}}
        if create:
            body['conversation'] = {'allowCreate': True, 'title': content, 'settings': {}}
        response = context.request.post(f'{live_server}/api/v4/conversations/{cid}/turns/settled', data=body)
        assert response.status == 200, response.text()

    def row(cid):
        return page.locator(f'.conv-item[data-conv-id="{cid}"]')

    try:
        append(target, '-input', 'human', 'Review the proposed optimization', create=True)
        append(other, '-other-input', 'human', 'Another conversation', create=True)
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        expect(row(target)).to_have_count(1, timeout=20000)
        row(target).click(position={'x': 12, 'y': 12})
        expect(page.locator('#chatInner')).to_contain_text('Review the proposed optimization')
        append(target, '-answer', 'assistant', 'A plan is ready. Implementation still needs follow-up.')
        expect(page.locator('#chatInner')).to_contain_text('Implementation still needs follow-up.', timeout=20000)
        badge = row(target).locator('.conv-status-review')
        expect(badge).to_be_visible(timeout=10000)
        expect(badge).to_have_text('待查看' if language == 'zh' else 'To review')
        expect(row(target).locator('.conv-review-dot')).to_be_visible()
        page.mouse.move(600, 700)
        page.locator('#sidebar').screenshot(path=str(tmp_path / 'review-desktop.png'))

        # Merely being the selected conversation in a background window did not read it.
        row(other).click(position={'x': 12, 'y': 12})
        expect(page.locator('#chatInner')).to_contain_text('Another conversation')
        page.reload(wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        expect(badge).to_be_visible(timeout=20000)

        page.evaluate('window.reviewFocused=true; window.dispatchEvent(new Event("focus"))')
        row(target).click(position={'x': 12, 'y': 12})
        expect(page.locator('#chatInner')).to_contain_text('Implementation still needs follow-up.')
        expect(badge).to_have_count(0)
        expect(row(target).locator('.conv-review-dot')).to_have_count(0)
        page.reload(wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        expect(page.locator('#chatInner')).to_contain_text('Implementation still needs follow-up.')
        expect(badge).to_have_count(0)

        # A new completion re-arms the receipt even in the same conversation.
        append(target, '-next-input', 'human', 'Please implement the plan.')
        append(target, '-next-answer', 'assistant', 'Implementation is ready for your review.')
        expect(page.locator('#chatInner')).to_contain_text('Implementation is ready for your review.', timeout=20000)
        expect(badge).to_be_visible()
        page.set_viewport_size({'width': 390, 'height': 844})
        # The same rendered sidebar is inspected at a narrow viewport.
        page.locator('#sidebar').evaluate("el => el.classList.remove('collapsed')")
        expect(badge).to_be_visible()
        assert badge.evaluate('el => {const b=el.getBoundingClientRect(),r=el.closest(".conv-item").getBoundingClientRect(); return b.left>=r.left && b.right<=r.right;}')
        page.mouse.move(380, 800)
        page.locator('#sidebar').screenshot(path=str(tmp_path / 'review-mobile.png'))
        print(f'Review sidebar screenshots: {tmp_path}')
        assert not errors, errors
    finally:
        for cid in (target, other):
            context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
