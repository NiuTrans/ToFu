"""The context rail's effort badge belongs beside the model, not at the far edge."""

import pytest

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from tests.turn_lifecycle.test_turn_ctx_rail_geometry import _PLANT

pytestmark = pytest.mark.visual


def test_model_and_depth_remain_a_compact_group(page):
    page.wait_for_selector('#userInput', state='visible', timeout=20000)
    page.wait_for_function(
        "typeof window.TofuModules?.resolveAction('renderTurnCtxNote') === 'function'",
        timeout=20000)
    # Use the production renderer with a short model and an explicit effort.
    plant = _PLANT.replace("model: 'claude-opus-5'", "model: 'kimi-k3'")
    plant = plant.replace("depth: 'think'", "depth: 'max'")
    assert page.evaluate(plant) == 'ok'

    # The runtime action can be ready before the hashed stylesheet finishes
    # loading. Without the group rule, the model fills the header and MAX
    # wraps below it; wait for the actual rule rather than testing an
    # intermediate unstyled frame. A missing/broken rule still times out.
    try:
        page.wait_for_function(
            "() => { const group = document.querySelector('.message.__probe .tctx-model-group');"
            " if (!group) return false; const style = getComputedStyle(group);"
            " return style.display.endsWith('flex') && style.columnGap === '6px'; }")
    except PlaywrightTimeoutError:
        # A late conversation render may replace the planted probe; a missing
        # stylesheet instead leaves the group unstyled. Keep both observable.
        state = page.evaluate("""() => {
            const probe = document.querySelector('.message.__probe');
            const group = probe?.querySelector('.tctx-model-group');
            const style = group && getComputedStyle(group);
            return {
                probePresent: !!probe,
                groupPresent: !!group,
                groupDisplay: style?.display,
                groupGap: style?.columnGap,
                chatChildren: document.querySelector('.chat-inner')?.childElementCount,
                stylesheets: [...document.querySelectorAll('link[rel=stylesheet]')]
                    .filter(link => link.href.includes('/styles-'))
                    .map(link => ({href: link.href, loaded: !!link.sheet})),
            };
        }""")
        pytest.fail(f'rail badge styles did not settle: {state}', pytrace=False)
    page.evaluate("() => { const s = document.querySelector('.sidebar');"
                  " s.classList.remove('has-rail','rail-collapsed');"
                  " s.classList.add('collapsed'); }")

    for theme in ('dark', 'light', 'tofu'):
        page.evaluate("t => document.documentElement.setAttribute('data-theme', t)",
                      theme)
        for viewport in (1100, 1920):
            page.set_viewport_size({'width': viewport, 'height': 900})
            page.wait_for_function(
                "() => { const rail = document.querySelector('.message.__probe .turn-ctx');"
                " return rail && getComputedStyle(rail).display !== 'none'"
                " && rail.getBoundingClientRect().width > 100; }")
            labels = ('Kimi K3', 'Extremely long model name that should truncate ' * 4)
            for label in labels:
                page.evaluate(
                    "text => { document.querySelector('.message.__probe .tctx-model').textContent = text; }",
                    label)
                measured = page.evaluate("""() => {
                    const rail = document.querySelector('.message.__probe .turn-ctx');
                    const model = rail.querySelector('.tctx-model');
                    const badge = rail.querySelector('.tctx-depth');
                    const a = model.getBoundingClientRect();
                    const b = badge.getBoundingClientRect();
                    const r = rail.getBoundingClientRect();
                    return {gap: b.left - a.right, sameRow: Math.abs(a.top - b.top) < 4,
                            inRail: b.right <= r.right + .5,
                            truncated: model.scrollWidth > model.clientWidth,
                            modelWidth: a.width, railWidth: r.width,
                            badge: badge.textContent.trim()};
                }""")
                assert measured['badge'].lower() == 'max', measured
                assert measured['sameRow'] and measured['inRail'], measured
                assert 0 <= measured['gap'] <= 12, measured
                assert measured['modelWidth'] > 20, measured
                if label != 'Kimi K3':
                    assert measured['truncated'], measured
