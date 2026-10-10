"""Demand-loaded Inspector evidence stays readable and keyboard-operable.

Production command port and shipped bundles run on the isolated app fixture.
Only a synthetic conversation's wire read is intercepted; no state is written.
"""
import json

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]


@pytest.mark.parametrize('theme,width', [(theme, width) for theme in ('light', 'dark', 'tofu')
                                       for width in (390, 1180)])
def test_inspector_reading_and_native_disclosure(page, assert_no_js_errors, tmp_path, theme, width):
    from tests.visual.test_visual_surfaces import _invoke_action, _wait_ready
    from tests.conftest import _dismiss_onboarding_modals

    page.set_viewport_size({'width': width, 'height': 1000})
    _wait_ready(page)
    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
    resources = '() => performance.getEntriesByType("resource").filter(e => /diagnostics-presenters.*\\.css/.test(e.name)).length'
    assert page.evaluate(resources) == 0, 'closed Inspector must not fetch its stylesheet'
    turn = {'type': 'turn', 'msgIndex': 0, 'turnId': 'reading-turn',
            'taskId': 'reading-task', 'createdAt': 1, 'status': 'done'}
    nav = {key: value for key, value in turn.items() if key != 'type'}
    evidence = '执行失败：保留完整证据。\n' + 'https://example.test/' + 'long-path/' * 40
    messages = [{'role': 'user', 'content': '检查这段执行证据和恢复操作。'},
                {'role': 'assistant', 'content': 'Inspect the complete evidence.',
                 'tool_calls': [{'id': 'reading-call', 'type': 'function',
                                 'function': {'name': 'read_files', 'arguments': json.dumps({'path': evidence})}}]},
                {'role': 'tool', 'name': 'read_files', 'tool_call_id': 'reading-call',
                 'content': evidence}]
    wire = {'messages': messages, 'sections': [turn], 'turns': [nav]}
    page.route('**/api/v4/tasks/conv-wire/*', lambda route: route.fulfill(
        status=200, content_type='application/json', body=json.dumps(wire)))
    page.evaluate("window.activeConvId = 'visual-inspector-reading'")
    _invoke_action(page, 'toggleDebug')
    headers = page.locator('#debugContent .debug-msg-header')
    headers.first.wait_for()
    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
    _dismiss_onboarding_modals(page)
    assert page.evaluate(resources) > 0, 'opening Inspector must load its owned CSS'
    header = headers.last
    assert header.evaluate('(n) => n.tagName') == 'BUTTON'
    header.focus()
    header.press('Enter')
    assert header.get_attribute('aria-expanded') == 'true'
    body = page.locator('#debugContent .debug-msg-block').last.locator('.debug-struct')
    assert body.is_visible()
    assert evidence in body.inner_text()
    geometry = page.evaluate('''() => {
      const drawer = document.getElementById('riDrawer');
      const text = document.querySelector('#debugContent .debug-msg-block:last-child .debug-struct');
      const input = document.getElementById('userInput');
      const style = getComputedStyle(text), field = getComputedStyle(input);
      return {theme: document.documentElement.dataset.theme,
        composerRight: document.querySelector('.input-box').getBoundingClientRect().right,
        sendRight: document.getElementById('sendBtn').getBoundingClientRect().right,
        drawerLeft: drawer.getBoundingClientRect().left,
        drawer: drawer.getBoundingClientRect().width,
        chat: document.querySelector('.chat-container').getBoundingClientRect().width,
        size: parseFloat(style.fontSize), leading: parseFloat(style.lineHeight),
        overflow: text.scrollWidth - text.clientWidth,
        inputSize: parseFloat(field.fontSize), inputLeading: parseFloat(field.lineHeight)};
    }''')
    assert geometry['theme'] == theme
    assert abs(geometry['drawer'] - (width if width == 390 else width / 2)) <= 1
    if width == 1180:
        assert geometry['chat'] >= 300
        assert geometry['composerRight'] <= geometry['drawerLeft'] + 1
        assert geometry['sendRight'] <= geometry['composerRight'] + 1
    assert geometry['size'] >= 14 and geometry['leading'] >= geometry['size'] * 1.6
    assert geometry['overflow'] <= 1, 'long evidence must wrap inside its panel'
    assert geometry['inputSize'] >= 16 and geometry['inputLeading'] >= geometry['inputSize'] * 1.6
    contrasts = page.evaluate('''() => {
      const ctx = document.createElement('canvas').getContext('2d');
      function rgba(paint) {
        ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = paint; ctx.fillRect(0, 0, 1, 1);
        return [...ctx.getImageData(0, 0, 1, 1).data];
      }
      function luminance(rgb) {
        return rgb.slice(0, 3).map(v => v / 255).map(v =>
          v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
          .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
      }
      return [...document.querySelectorAll('#debugContent .debug-msg-header [class^="role-"],'
        + '#debugContent .debug-msg-summary, #debugContent pre [class^="debug-"]')].map(node => {
        const ancestors = []; for (let n = node; n; n = n.parentElement) ancestors.unshift(n);
        let bg = [255, 255, 255];
        for (const n of ancestors) {
          const paint = rgba(getComputedStyle(n).backgroundColor), alpha = paint[3] / 255;
          bg = bg.map((v, i) => paint[i] * alpha + v * (1 - alpha));
        }
        const a = luminance(rgba(getComputedStyle(node).color)), b = luminance(bg);
        return {class: node.className, ratio: (Math.max(a, b) + .05) / (Math.min(a, b) + .05)};
      });
    }''')
    assert contrasts and all(item['ratio'] >= 4.5 for item in contrasts), contrasts
    # A real wire refresh retains disclosure state and keyboard focus.
    identity = header.evaluate('(n) => n.parentElement.dataset.mid')
    _invoke_action(page, 'riRefreshTasks')
    current = page.locator('#debugContent .debug-msg-block.open')
    assert current.count() == 1
    assert current.get_attribute('data-mid') == identity
    assert header.evaluate('(n) => document.activeElement === n')
    (tmp_path / f'inspector-{theme}-{width}.json').write_text(json.dumps(geometry, indent=2))
    page.screenshot(path=str(tmp_path / f'inspector-{theme}-{width}.png'))
    header.press('Space')
    assert header.get_attribute('aria-expanded') == 'false'
    assert not body.is_visible()
    _invoke_action(page, 'toggleDebug')
    page.emulate_media(reduced_motion='reduce')
    page.locator('#userInput').focus()
    halo = page.locator('.input-box').evaluate('''n => {
      const before = getComputedStyle(n, '::before');
      return {display: before.display, animation: before.animationName};
    }''')
    assert halo == {'display': 'none', 'animation': 'none'}
