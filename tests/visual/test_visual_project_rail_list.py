"""Borderless project navigation and a stable count/settings column."""
from pathlib import Path

import pytest
from tests.frontend.test_frontend_folder_ring_and_inbox import rail_server as rail_server

pytestmark = pytest.mark.visual


def _assert_text_contrast(page):
    measurements = page.locator('#folderTabs .folder-tab-name, #folderTabs .folder-tab-count').evaluate_all('''elements => {
      const color = value => {
        const values = value.match(/[\\d.]+/g).map(Number);
        return [...values.slice(0,3).map(v => v / 255), values[3] ?? 1];
      };
      const blend = (fg,bg) => fg.slice(0,3).map((v,i) => v * fg[3] + bg[i] * (1 - fg[3]));
      const background = el => {
        if (!el) return [1,1,1];
        const s = getComputedStyle(el), c = color(s.backgroundColor);
        if (c[3] === 1) return c.slice(0,3);
        return blend(c,background(el.parentElement));
      };
      const luminance = c => c.map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
        .reduce((sum,v,i) => sum + v * [.2126,.7152,.0722][i],0);
      return elements.filter(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden')
        .map(el => {
          const bg = background(el), fg = blend(color(getComputedStyle(el).color),bg);
          const a = luminance(fg), b = luminance(bg);
          return {text:el.textContent, ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
        });
    }''')
    assert measurements and all(row['ratio'] >= 4.5 for row in measurements), measurements
    return measurements


@pytest.mark.parametrize('width', [1280, 390], ids=['wide', 'narrow'])
@pytest.mark.parametrize('theme', ['dark', 'light', 'tofu'])
@pytest.mark.parametrize('touch', [False, True], ids=['desktop', 'touch'])
def test_project_list_geometry(browser, rail_server, theme, touch, width):
    context = browser.new_context(viewport={'width': width, 'height': 900}, has_touch=touch)
    page = context.new_page()
    try:
        page.goto(rail_server)
        page.wait_for_function('window.ownerReady === true')
        page.evaluate('''theme => {
          document.documentElement.dataset.theme = theme;
          document.querySelector('#sidebar').classList.remove('rail-collapsed');
          [...document.querySelectorAll('.project-rail-list .folder-tab')].slice(0,3).forEach((tab,i) => {
            const count = document.createElement('span');
            count.className = 'folder-tab-count'; count.textContent = ['241','4','10'][i];
            tab.append(count);
          });
        }''', theme)
        page.mouse.move(1000, 800)
        tabs = page.locator('.project-rail-list .folder-tab')

        page.evaluate("async () => { for (const selector of ['#sidebar', '#folderTabs', '.folder-tab.active']) await Promise.all(document.querySelector(selector).getAnimations().map(animation => animation.finished)); }")
        geometry = tabs.evaluate_all('''tabs => tabs.slice(0,3).map(tab => {
          const s = getComputedStyle(tab), c = tab.querySelector('.folder-tab-count');
          const cs = getComputedStyle(c), r = c.getBoundingClientRect();
          return {border:s.borderTopWidth, shadow:s.boxShadow, bg:s.backgroundColor,
            height:tab.getBoundingClientRect().height, countRight:r.right,
            countBg:cs.backgroundColor, radius:cs.borderRadius, font:cs.fontVariantNumeric};
        })''')
        assert all(g['border'] == '0px' and g['shadow'] == 'none' for g in geometry), geometry
        assert geometry[0]['bg'] == geometry[2]['bg'] == 'rgba(0, 0, 0, 0)', geometry
        assert geometry[1]['bg'] != geometry[0]['bg'], geometry
        assert all(40 <= g['height'] <= 44 for g in geometry), geometry
        assert max(g['countRight'] for g in geometry) - min(g['countRight'] for g in geometry) <= 1, geometry
        assert all(g['countBg'] == 'rgba(0, 0, 0, 0)' and g['radius'] == '0px' for g in geometry), geometry
        assert all('tabular-nums' in g['font'] for g in geometry), geometry

        assert page.locator('#folderTabs').evaluate('(el) => getComputedStyle(el).backgroundImage') == 'none'
        contrast = _assert_text_contrast(page)

        assert page.locator('#folderTabs').evaluate('(el) => el.scrollWidth <= el.clientWidth')
        tab = tabs.nth(2)
        name = tab.locator('.folder-tab-name')
        before = name.bounding_box()
        tab.hover()
        count = tab.locator('.folder-tab-count')
        assert count.evaluate('(el) => getComputedStyle(el).visibility') == ('visible' if touch else 'hidden')
        settings = page.locator('.folder-tab-settings').nth(1)
        assert settings.evaluate('(el) => getComputedStyle(el).opacity') == '1'
        assert name.bounding_box() == before
        tab.evaluate('(el) => Promise.all(el.getAnimations().map(a => a.finished))')
        _assert_text_contrast(page)
        page.mouse.move(1000, 800)
        tab.focus()
        assert settings.evaluate('(el) => getComputedStyle(el).opacity') == '1'
        assert name.bounding_box() == before
        settings.focus()

        _assert_text_contrast(page)
        assert settings.evaluate('(el) => getComputedStyle(el).boxShadow') != 'none'
        page.evaluate('document.activeElement.blur()')
        out = Path(__file__).resolve().parents[2] / 'output/playwright/project-rail-list'
        out.mkdir(parents=True, exist_ok=True)

        import json
        (out / f'{theme}-{touch}-{width}-contrast.json').write_text(json.dumps(contrast, indent=2))
        page.locator('#folderTabs').screenshot(path=str(out / f'{theme}-{touch}-{width}.png'))
        page.evaluate("document.querySelector('#sidebar').classList.add('rail-collapsed')")
        assert all(c == 'none' for c in page.locator('.folder-tab-count').evaluate_all('(els) => els.map(el => getComputedStyle(el).display)'))
        assert all(c == 'none' for c in page.locator('.folder-tab-settings').evaluate_all('(els) => els.map(el => getComputedStyle(el).display)'))
    finally:
        context.close()
