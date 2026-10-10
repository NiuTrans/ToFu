"""Narrow pane retains a readable fold while the rail mirror has a stale grant."""

import pytest

from tests.turn_lifecycle.test_turn_ctx_rail_geometry import _PLANT, _PROBE

pytestmark = pytest.mark.visual


def test_fold_survives_stale_inline_rail_width(page):
    page.wait_for_selector('#userInput', state='visible', timeout=20000)
    page.wait_for_function(
        "typeof window.TofuModules?.resolveAction('renderTurnCtxNote') === 'function'",
        timeout=20000)
    assert page.evaluate(_PLANT) == 'ok'
    page.set_viewport_size({'width': 1100, 'height': 900})
    page.evaluate("async () => window.TofuModules.invokeFeature('toggleDebug', [], () => {})")
    page.evaluate("() => { const s = document.querySelector('.sidebar');"
                  " s.classList.remove('has-rail','rail-collapsed');"
                  " s.classList.add('collapsed'); }")
    page.wait_for_function(
        "() => { const w = document.querySelector('.chat-container')"
        ".getBoundingClientRect().width; return w >= 205 && w <= 245; }",
        timeout=5000)
    # A last-granted width before ResizeObserver's next delivery. Keep the
    # stale value on the grid intentionally to exercise the CSS fallback.
    # Inject and sample in one browser task: separate evaluate calls allow
    # the normal ResizeObserver mirror to clear the stale value in between,
    # accidentally testing a fresh layout rather than this fault condition.
    result = page.evaluate("() => { const c = document.querySelector('.chat-inner');"
                           " c.style.setProperty('--rail-w','232px');"
                           " c.style.setProperty('--rail-gap','24px');"
                           " return {probe: (" + _PROBE + ")(), rail: {"
                           " inline: c.style.getPropertyValue('--rail-w'),"
                           " computed: getComputedStyle(c).getPropertyValue('--rail-w').trim()}}; }")
    m, rail = result['probe'], result['rail']
    assert 205 <= m['paneWidth'] <= 245
    assert rail == {'inline': '232px', 'computed': '0px'}
    assert not m['railShown'] and m['foldShown']
    assert m['foldW'] >= 20

    assert len(m['foldText']) >= 5
