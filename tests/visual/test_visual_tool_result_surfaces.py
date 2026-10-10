"""Chromium: shared source policy renders report prose and honest JSON errors."""
import json

import pytest

from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
from lib.tools.result_envelope import split_tool_result_delivery
from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1280, 390])
def test_report_surface_uses_safe_markdown_and_keeps_json_error_format(
        frontend_visual_page, width, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    for name, source in [
        ('tool-surface-result', 'frontend/src/conversation/presentation/tool-result-presentation.ts'),
        ('tool-surface-source', 'frontend/src/conversation/presentation/tool-result-surfaces.ts'),
        ('tool-surface-markdown', 'frontend/src/markdown/index.ts'),
    ]:
        page.add_script_tag(path=native_module_path(f'.native/{name}.js', ROOT / source))
    page.add_script_tag(path=str(ROOT / 'node_modules/marked/lib/marked.umd.js'))
    page.add_script_tag(path=str(ROOT / 'node_modules/dompurify/dist/purify.min.js'))
    raw = json.dumps({'agent_id': 'worker', 'status': 'ok', 'agent_status': 'completed',
                      'final_answer': '**Verified report**\n\nEvidence HEAD\n' + 'Observed detail.\n' * 150
                      + '\nEvidence TAIL\n\n<script>window.reportExecuted=true</script>'})
    model = split_tool_result_delivery(budget_tool_result_v2('get_agent_result', raw, user_id=0)).model_text
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    page.evaluate(r'''({model,locale}) => {
      document.body.innerHTML='<main id="fixture" class="conversation-surface"></main>';
      marked.setOptions({breaks:true});
      const markdown=createMarkdownRenderer({maximumCacheEntries:4,ensureMath:() => {},
        getEnvironment:() => ({document,parser:marked,sanitizer:DOMPurify,highlighter:null,
          mathRenderer:null,basePath:'',projectActive:false})});
      const presentation=createToolResultPresentation({
        translate:(key,args) => (locale[key] || key).replace(/\{(\w+)\}/g,(_,name)=>args?.[name] ?? ''),
        renderMarkdown:source=>markdown.render(source),
        writeGateRefusal:{resolveRefusal:()=>null,renderNoticeHtml:()=>''},
      });
      const header={iconHtml:'',queryHtml:'Get worker report',rootPillHtml:'',badgeHtml:''};
      window.fixtureRow={toolName:'get_agent_result',toolCallId:'report',roundNum:1,
        status:'done',toolContent:model,results:[{content:'STALE METADATA'}]};
      window.repaint=() => document.querySelector('#fixture').innerHTML=
        presentation.renderGenericResultHtml(JSON.parse(JSON.stringify(fixtureRow)),{},header);
      repaint();
    }''', {'model': model, 'locale': locale})
    page.add_style_tag(content='body{display:block;overflow:auto}#fixture{max-width:100%;padding:16px}')
    page.locator('.ptool-result-header').click()
    body = page.locator('.ptool-result-body')
    assert body.locator('strong').inner_text() == 'Verified report'
    assert 'Evidence TAIL' in body.inner_text()
    assert 'STALE METADATA' not in body.inner_text()
    assert not page.evaluate('Boolean(window.reportExecuted)')
    assert page.locator('.ptool-result-pre').count() == 0
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.screenshot(path=str(tmp_path / f'tool-result-report-{width}.png'))
    page.evaluate('repaint()')
    page.locator('.ptool-result-header').focus()
    page.keyboard.press('Enter')
    assert 'Evidence TAIL' in body.inner_text()

    page.evaluate('''() => {
      fixtureRow.toolContent='{"status":"error","code":"scope_denied","retryable":false}';
      repaint();
    }''')
    page.locator('.ptool-result-header').click()
    assert page.locator('.ptool-result-pre').count() == 1
    assert 'scope_denied' in body.inner_text()
    assert body.locator('strong').count() == 0
    page.evaluate("fixtureRow.toolContent='[1,2]'; repaint()")
    page.locator('.ptool-result-header').click()
    assert page.locator('.ptool-result-pre').count() == 1
    assert page.evaluate("resolveToolResultText({toolContent:null,result:{content:'stale'}}).text") == 'null'
    assert page.evaluate("toolResultSurfacePolicy('__proto__').modelFormat") == 'producer'
