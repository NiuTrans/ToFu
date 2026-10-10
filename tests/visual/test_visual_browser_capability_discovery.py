"""Real-browser rendering of backend-owned capability-unavailable evidence."""
from __future__ import annotations

import json

import pytest

from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import (
    ROOT, frontend_visual_origin as frontend_visual_origin, frontend_visual_page as frontend_visual_page,  # noqa: F401
)

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('language,width', [('zh', 390), ('en', 1280)])
def test_disconnected_browser_search_explains_recovery(
    frontend_visual_page, frontend_visual_origin, tmp_path, language, width,
):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 800})
    page.goto(frontend_visual_origin)
    page.add_script_tag(path=native_module_path(
        '.native/browser-availability-search.js',
        ROOT / 'frontend/src/conversation/presentation/tool-search-presentation.ts'))
    messages = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    page.evaluate(r'''(messages) => {
      const presentation = createToolSearchPresentation({
        translate: (key, params) => (messages[key] || key).replace(
          /\{(\w+)\}/g, (_, name) => String(params?.[name] ?? '')),
        iconHtml: () => '',
      });
      document.body.innerHTML = presentation.renderSearchHtml({
        toolName:'search_tools', status:'done', toolSearchTotal:0,
      }, [{
        type:'tool_catalog_availability', namespace:'browser',
        reasonCode:'browser_extension_disconnected',
        message:'Browser extension disconnected', recoveryHint:'Reconnect',
      }], {iconHtml:'', queryHtml:'browser_list_tabs', rightControlsHtml:''});
    }''', messages)
    note = page.locator('[data-capability-reason="browser_extension_disconnected"]')
    assert note.is_visible()
    assert messages['toolSearch.browserDisconnected'] in note.inner_text()
    assert messages['toolSearch.browserReconnect'] in note.inner_text()
    assert messages['toolSearch.none'] not in page.locator('body').inner_text()
    assert page.locator('.ptool-tool-search-card').count() == 0
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.screenshot(path=str(tmp_path / f'browser-unavailable-{language}.png'))
