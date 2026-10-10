"""Shipped app interaction owners: viewport, typing, scrolling and shortcuts.

Runs the production composition on an isolated app. Synthetic reading content
is transient DOM; no conversation, task or model request is submitted.
"""
import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]


@pytest.mark.parametrize('width,height', [(390, 844), (1180, 1000)])
def test_shipped_input_geometry_and_shell_shortcuts(page, assert_no_js_errors, tmp_path, width, height):
    from tests.visual.test_visual_surfaces import _wait_ready
    from tests.conftest import _dismiss_onboarding_modals

    page.set_viewport_size({'width': width, 'height': height})
    _wait_ready(page)
    _dismiss_onboarding_modals(page)
    input_field = page.locator('#userInput')
    page.evaluate(r'''() => {
      const chat = document.getElementById('chatContainer');
      const transcript = document.createElement('div'); transcript.id = 'interaction-reading';
      transcript.style.cssText = 'min-height:3000px;white-space:pre-wrap;padding:var(--space-5)';
      transcript.textContent = 'Reading evidence remains stable while typing.\n'.repeat(100);
      chat.append(transcript); chat.scrollTop = 300;
    }''')
    input_field.fill('A draft\n' * 30)
    page.wait_for_function("parseFloat(document.getElementById('userInput').style.height) > 100")
    # Growing overflow skips the temporary collapse that clamps a reader's scroll.
    page.evaluate("document.getElementById('chatContainer').scrollTop = 300")
    input_field.press('End')
    input_field.press('x')
    page.wait_for_function("document.getElementById('userInput').value.endsWith('x')")
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    geometry = page.evaluate('''() => {
      const root = document.documentElement, wrapper = document.querySelector('.chat-wrapper');
      const area = document.querySelector('.input-area');
      return {viewport: window.innerHeight, root: root.getBoundingClientRect().height,
        heightToken: parseFloat(root.style.getPropertyValue('--vh100')),
        area: area.offsetHeight, areaToken: parseFloat(wrapper.style.getPropertyValue('--input-area-h')),
        scroll: document.getElementById('chatContainer').scrollTop,
        input: getComputedStyle(document.getElementById('userInput')).fontSize};
    }''')
    assert abs(geometry['root'] - geometry['viewport']) <= 1
    assert geometry['heightToken'] == geometry['viewport']
    assert abs(geometry['area'] - geometry['areaToken']) <= 1
    assert geometry['scroll'] >= 295
    assert float(geometry['input'].removesuffix('px')) >= 16
    # Use the real document listener, including the first Escape target.
    page.keyboard.press('Control+k')
    page.locator('#sidebarSearchWrapper').wait_for(state='visible')
    page.keyboard.press('Escape')
    page.locator('#sidebarSearchWrapper').wait_for(state='hidden')
    assert input_field.input_value().endswith('x')
    page.set_viewport_size({'width': width, 'height': height - 100})
    page.wait_for_function('(expected) => parseFloat(document.documentElement.style.getPropertyValue("--vh100")) === expected',
                           arg=height - 100)
    page.screenshot(path=str(tmp_path / f'application-interactions-{width}.png'))


def test_real_paste_detection_does_not_overwrite_a_later_draft(page, assert_no_js_errors):
    from tests.visual.test_visual_surfaces import _wait_ready

    _wait_ready(page)
    pending = []
    page.route('**/api/v4/logs/clean', lambda route: pending.append(route))

    def paste(text):
        with page.expect_request('**/api/v4/logs/clean'):
            page.evaluate('''text => {
              const input = document.getElementById('userInput');
              const transfer = new DataTransfer(); transfer.setData('text/plain', text);
              input.dispatchEvent(new ClipboardEvent('paste', {clipboardData: transfer, bubbles: true, cancelable: true}));
              input.value = text; input.dispatchEvent(new Event('input', {bubbles: true}));
            }''', text)
        page.evaluate('() => Promise.resolve()')
        assert pending
        return pending.pop(0)

    original = '2026-10-03 INFO repeated diagnostic evidence\n' * 12
    route = paste(original)
    route.fulfill(json={'originalText': original, 'cleanedText': 'A concise log',
                        'savedChars': 300, 'savedPct': 70, 'ops': []})
    page.locator('#logCleanBanner').wait_for(state='visible')
    # Positive response above proves this is the real detector/banner path.
    second = original + 'second request\n'
    route = paste(second)
    page.locator('#userInput').fill('The draft was edited while inspection was pending.')
    with page.expect_response('**/api/v4/logs/clean'):
        route.fulfill(json={'originalText': second, 'cleanedText': 'Stale result',
                            'savedChars': 300, 'savedPct': 70, 'ops': []})
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    assert not page.locator('#logCleanBanner').is_visible()
    assert page.locator('#userInput').input_value() == 'The draft was edited while inspection was pending.'
