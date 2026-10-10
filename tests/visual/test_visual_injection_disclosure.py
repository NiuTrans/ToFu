"""A closed injection disclosure must not paint its model-facing payload.

Uses Chromium with the retained presentation owner and delivered CSS. No
conversation data or storage service is touched; the payload is synthetic.
"""
from __future__ import annotations

import pytest

from tests._runtime_sections import ROOT, native_module_path

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1100, 390])
@pytest.mark.parametrize('lane', ['_userSteerInject', '_peerInject', '_inboxInject',
                                   '_bgCommandInject', '_stallNudge'])
def test_injection_disclosure_hides_body_until_open(browser, width, lane):
    context = browser.new_context(viewport={'width': width, 'height': 760})
    page = context.new_page()
    page.set_content('<!doctype html><html data-theme="tofu"><body>'
                     '<main id="timeline" class="conversation-surface"></main>'
                     '</body></html>')
    page.add_style_tag(path=str(ROOT / 'static/styles.css'))
    page.add_style_tag(content='body{display:block!important;padding:16px}'
                               '#timeline{width:100%;--rail-w:0px}')
    page.add_script_tag(path=native_module_path(
        '.native/visual-tool-injection-presentation.js',
        ROOT / 'frontend/src/conversation/presentation/tool-injection-presentation.ts'))
    payload = {
        '_userSteerInject': {'steerCount': 1, 'steerPreviews': [{'text': 'operator direction'}]},
        '_peerInject': {'peerCount': 1, 'peerPreviews': [{'text': 'peer delivery'}]},
        '_inboxInject': {'inboxCount': 1, 'inboxPreviews': [{'text': 'swarm report'}]},
        '_bgCommandInject': {'bgCommandCount': 1, 'bgCommandPreviews': [
            {'commandId': 'job', 'text': '<background-command>done</background-command>'}]},
        '_stallNudge': {'stallPrompt': 'resume the model'},
    }[lane]
    page.evaluate(r"""([lane, payload]) => {
      const escape = text => String(text).replaceAll('&','&amp;')
        .replaceAll('<','&lt;').replaceAll('>','&gt;');
      const presentation = createToolInjectionPresentation({
        translate: key => key,
        renderMarkdown: text => `<p>${escape(text)}</p>`,
        iconHtml: () => '',
        resolveConversationTitle: () => '',
      });
      document.querySelector('#timeline').innerHTML = presentation.renderInjectionHtml({
        [lane]: true, ...payload, roundNum: 1,
      });
    }""", [lane, payload])
    row = page.locator('#timeline .sw-inbox-row')
    body = row.locator('.sw-inbox-row-body')
    assert row.count() == 1
    assert row.get_attribute('open') is None
    assert not body.is_visible(), (lane, width)
    assert body.evaluate('node => getComputedStyle(node).display') == 'none'
    row.locator('summary').click()
    assert body.is_visible(), (lane, width)
    assert body.inner_text().strip()
    assert row.evaluate('node => node.scrollWidth <= node.clientWidth + 1')
    row.locator('summary').click()
    assert not body.is_visible()
    context.close()
