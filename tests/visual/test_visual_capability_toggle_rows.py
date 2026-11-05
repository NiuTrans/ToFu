"""Capability toggles that are on together stay distinct rows with one on-state.

The Extras toggles are independent, so several are often on at once. Flush rows
fused their highlights into one slab (Tofu's stacked spines drew a curly
bracket) and a stale ID rule painted Local Control a foreign hue.
"""
import pytest

pytestmark = [pytest.mark.visual]

# One synchronous task: the app's own toggle sync cannot interleave, and with
# motion disabled the computed style is already the settled end state.
_TURN_ALL_ON = r'''() => {
  const menu = document.querySelector('#submenuExtras .submenu-dropdown');
  const rows = [...menu.querySelectorAll('.submenu-item[id$="Toggle"]')]
    .filter(row => row.getClientRects().length);
  rows.forEach(row => row.classList.add('active'));
  return rows.map(row => {
    const box = row.getBoundingClientRect(), style = getComputedStyle(row);
    return {id: row.id, top: box.top, bottom: box.bottom, shadow: style.boxShadow,
            paint: `${style.backgroundColor} ${style.backgroundImage}`};
  });
}'''


def _touching(rows):
    return [(upper['id'], lower['id']) for upper, lower in zip(rows, rows[1:])
            if lower['top'] - upper['bottom'] < 1]


@pytest.mark.parametrize('theme', ['tofu', 'light', 'dark'])
def test_concurrent_capability_toggles_render_as_distinct_rows(page, assert_no_js_errors, theme):
    from tests.visual.test_visual_surfaces import _invoke_action, _wait_ready

    page.set_viewport_size({'width': 1280, 'height': 900})
    _wait_ready(page)
    page.evaluate('theme => { document.documentElement.dataset.theme = theme; }', theme)
    page.add_style_tag(content='#submenuExtras, #submenuExtras * {transition:none !important; animation:none !important}')
    _invoke_action(page, 'toggleSubmenu', 'submenuExtras')
    rows = page.evaluate(_TURN_ALL_ON)

    assert len(rows) >= 3, rows
    assert not _touching(rows), rows
    assert len({row['paint'] for row in rows}) == 1, rows
    assert not [row for row in rows if 'inset' in row['shadow']], rows
    if theme == 'tofu':
        selected = page.evaluate(
            "getComputedStyle(document.querySelector('#agentModeMenu .flow-menu-item.selected')).boxShadow")
        assert 'inset' in selected, 'the spine still marks the single-select choice'

    # NEUTER: forcing flush rows must trip the same detector.
    page.add_style_tag(content='#submenuExtras .submenu-item{margin:0 !important}')
    assert _touching(page.evaluate(_TURN_ALL_ON))
