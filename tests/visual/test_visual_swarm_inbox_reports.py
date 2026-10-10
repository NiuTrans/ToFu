"""Real Chromium: completion report disclosure, safe Markdown, reload and bounds."""
import json

import pytest

from lib.agent_inbox import format_swarm_update
from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1280, 390])
def test_swarm_completion_report_is_visible_after_disclosure_and_reload(
        frontend_visual_page, width, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.add_script_tag(path=native_module_path(
        '.native/swarm-inbox-report-visual.js',
        ROOT / 'frontend/src/conversation/presentation/tool-injection-presentation.ts'))
    page.add_script_tag(path=native_module_path(
        '.native/swarm-inbox-markdown-visual.js', ROOT / 'frontend/src/markdown/index.ts'))
    page.add_script_tag(path=str(ROOT / 'node_modules/marked/lib/marked.umd.js'))
    page.add_script_tag(path=str(ROOT / 'node_modules/dompurify/dist/purify.min.js'))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    report = '**报告结论**\n\n' + ('已验证报告正文。\n\n' * 180) + 'REPORT TAIL\n\n<script>window.reportExecuted=true</script>'
    payload = format_swarm_update(
        agent_id='friday-reconcile', role='general', status='completed',
        elapsed_seconds=2285.2, tokens=2051923, result=report,
        output_file='/synthetic/friday-reconcile.log')
    page.evaluate(r"""({locale,payload}) => {
      document.body.innerHTML='<main id="fixture" class="conversation-surface"></main>';
      const markdown=createMarkdownRenderer({
        getEnvironment:() => ({document,parser:marked,sanitizer:DOMPurify,
          highlighter:null,mathRenderer:null,basePath:'',projectActive:false}),
        maximumCacheEntries:4,ensureMath:() => {},
      });
      window.fixturePresentation=createToolInjectionPresentation({
        translate:(key,params) => (locale[key] || key).replace(/\{(\w+)\}/g,
          (_,name) => params?.[name] ?? ''),
        renderMarkdown:source => markdown.render(source),
        iconHtml:() => '',resolveConversationTitle:() => '',
      });
      window.fixtureRound={_inboxInject:true,roundNum:3,inboxCount:1,
        inboxAgentIds:['friday-reconcile'],inboxPreviews:[{agentId:'friday-reconcile',text:payload}]};
      window.repaint=() => document.querySelector('#fixture').innerHTML=
        fixturePresentation.renderInjectionHtml(JSON.parse(JSON.stringify(fixtureRound)));
      repaint();
    }""", {'locale': locale, 'payload': payload})
    page.add_style_tag(content='body{display:block;overflow:auto}#fixture{max-width:100%;padding:16px}')
    report_node = page.locator('.sw-card-preview')
    assert report_node.is_hidden()
    page.locator('.sw-inbox-row > summary').click()
    assert report_node.is_visible()
    assert 'REPORT TAIL' in report_node.inner_text()
    assert report_node.locator('strong').inner_text() == '报告结论'
    assert page.locator('.sw-card-file').inner_text() == '/synthetic/friday-reconcile.log'
    assert not page.evaluate('Boolean(window.reportExecuted)')
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.screenshot(path=str(tmp_path / f'swarm-report-{width}.png'))

    page.evaluate('repaint()')
    page.locator('.sw-inbox-row > summary').focus()
    page.keyboard.press('Enter')
    assert 'REPORT TAIL' in report_node.inner_text()

    page.evaluate("""() => {
      fixtureRound.inboxPreviews[0].text='<swarm-update><result>REPORT HEAD '
        + '正文'.repeat(40000) + 'OMITTED TAIL</result></swarm-update>';
      repaint();
    }""")
    page.locator('.sw-inbox-row > summary').click()
    assert 'REPORT HEAD' in report_node.inner_text()
    assert 'OMITTED TAIL' not in report_node.inner_text()
    assert page.locator('.ptool-result-trunc').is_visible()
