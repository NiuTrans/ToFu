"""Update-modal panels stay separated with local edits on the latest release.

Run: python -m pytest tests/visual/test_visual_update_spacing.py -m visual -q
The read-only local HTTP fixture serves shipped CSS and the real modal shell;
only update-check transport and shared runtime ports are substituted. No app,
storage, GitHub check, apply, or restart is started by this layout test.
"""

import json

import pytest

from tests.support.frontend_visual import (
    ROOT,
    frontend_visual_origin as frontend_visual_origin,  # noqa: F401
    frontend_visual_page as frontend_visual_page,  # noqa: F401
)

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
@pytest.mark.parametrize('width', [1440, 390])
def test_update_modal_visible_panels_have_spacing(frontend_visual_page, theme, width):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    translations = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    page.evaluate('''async ({theme, translations}) => {
      document.documentElement.dataset.theme = theme;
      document.body.dataset.theme = theme;
      // Parse, but never execute, the application shell and its script graph.
      const response = await fetch('/index.html');
      if (!response.ok) throw new Error('Cannot read shipped modal shell');
      const shell = new DOMParser().parseFromString(await response.text(), 'text/html');
      document.body.replaceChildren(shell.getElementById('updateModal'));
      window.t = key => {
        if (!(key in translations)) throw new Error('Missing translation: ' + key);
        return translations[key];
      };
      window.escapeHtml = value => {
        const span = document.createElement('span');
        span.textContent = String(value);
        return span.innerHTML;
      };
      // Disable unrelated delayed boot checks, not the visible dialog renderer.
      window._onReady = () => {};
      window.Api = {update: {
        check: async () => ({ok: true, current: '0.30.0', latest: '0.30.0',
          update_available: false, dirty: true, code_pending: {pending: true}}),
        listLifecycleApprovals: async () => ({records: []}),
      }};
    }''', {'theme': theme, 'translations': translations})
    page.add_script_tag(path=str(ROOT / 'frontend/src/runtime/sections/update.js'))
    page.evaluate('openUpdateDialog()')
    notice = page.locator('#updateActionArea > .upd-badge.warn')
    success = page.locator('#updateActionArea > .upd-badge.ok')
    notice.wait_for(state='visible')
    success.wait_for(state='visible')
    assert translations['update.codePending'] in notice.inner_text()
    assert translations['update.upToDate'].removeprefix('✅').strip() in success.inner_text()
    # Avoid sampling the opening scale animation's intermediate geometry.
    page.evaluate('''async () => {
      await document.fonts.ready;
      await Promise.all(document.getAnimations().map(animation => animation.finished));
    }''')
    geometry = page.evaluate('''() => {
      const visible = element => element.getClientRects().length > 0 &&
        getComputedStyle(element).visibility !== 'hidden';
      // The action/lifecycle hosts are layout wrappers, not painted panels.
      const panels = [...document.querySelectorAll(
        '#updateModalBody > .upd-hero, #updateActionArea > *, #updateLifecycleApprovals > *'
      )].filter(visible).map(element => {
        const rect = element.getBoundingClientRect();
        return {name: element.className, top: rect.top, bottom: rect.bottom,
          left: rect.left, right: rect.right};
      });
      const containers = [document.documentElement, document.body,
        document.querySelector('.update-modal'), document.querySelector('#updateModalBody'),
        document.querySelector('#updateActionArea')].map(element => ({
          name: element.id || element.className || element.tagName,
          client: element.clientWidth, scroll: element.scrollWidth,
        }));
      return {panels, containers, viewport: innerWidth};
    }''')
    panels = geometry['panels']
    assert len(panels) >= 3, f'Expected version, local-code and up-to-date panels: {panels}'
    for container in geometry['containers']:
        assert container['scroll'] <= container['client'], f'Horizontal overflow: {container}'
    for panel in panels:
        assert panel['left'] >= 0 and panel['right'] <= geometry['viewport'], panel
    for previous, current in zip(panels, panels[1:]):
        gap = current['top'] - previous['bottom']
        assert gap >= 12, (
            f'{theme}/{width}: {previous["name"]} → {current["name"]} '
            f'gap is {gap:.2f}px, expected at least 12px; panels={panels}'
        )
