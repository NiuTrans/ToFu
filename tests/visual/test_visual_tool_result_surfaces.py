"""Chromium: shared source policy renders reports, JSON errors, and edit skips."""
import json

import pytest

from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
from lib.tools.meta import build_project_tool_meta
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


@pytest.mark.parametrize('width', [1280, 390], ids=['desktop', 'narrow'])
def test_atomic_edit_skips_keep_visible_indexes_and_separate_reasons(
        frontend_visual_page, width, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    for name, source in [
        ('tool-surface-result', 'frontend/src/conversation/presentation/tool-result-presentation.ts'),
        ('tool-surface-header', 'frontend/src/conversation/presentation/tool-header-presentation.ts'),
        ('tool-surface-renderer', 'frontend/src/conversation/ui/classic-conversation-renderers.ts'),
    ]:
        page.add_script_tag(path=native_module_path(f'.native/{name}.js', ROOT / source))
    edits = [
        {'path': 'example.py', 'operation': operation, 'description': f'Change {index}',
         'anchor': f'old_{index}', 'content': f'new_{index}'}
        for index, operation in enumerate(['insert_before', 'replace', 'insert_after'], 1)
    ]
    skipped = 'Not applied because atomic preflight failed.'
    failure = 'Anchor missing\nRecovery: read current content.\n<script>window.editInjected=true</script>'
    output = (
        'Applied 0/3 edits (1 failed); atomic preflight failed; no files written; 2 skipped\n'
        f'[1] SKIP example.py [insert_before]: {skipped}\n'
        f'[2] FAIL example.py [replace] [match-not-found]: {failure}\n'
        f'[3] SKIP example.py [insert_after]: {skipped}'
    )
    args = {'description': 'Apply atomic edits', 'atomic': True, 'edits': edits}
    metadata = build_project_tool_meta('edit_file', args, output)
    assert [row['status'] for row in metadata['editSummaries']] == ['fail'] * 3
    assert [row.get('skipped', False) for row in metadata['editSummaries']] == [True, False, True]
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    page.evaluate(r'''({locale, args, metadata, output}) => {
      document.body.innerHTML='<main id="fixture" class="conversation-surface"><div id="tool"></div></main>';
      const translate=(key,values) => (locale[key] || key).replace(/\{(\w+)\}/g,(_,name)=>values?.[name] ?? '');
      const refusal={resolveRefusal:()=>null,renderBadgeHtml:()=>'',renderNoticeHtml:()=>''};
      const headers=createToolHeaderPresentation({translate,writeGateRefusal:refusal});
      const results=createToolResultPresentation({translate,writeGateRefusal:refusal});
      const round={roundNum:1,toolName:'edit_file',toolCallId:'edit-skip',status:'error',
        query:'Edit example.py',toolArgs:args,results:[metadata],toolContent:output};
      const header={iconHtml:'',queryHtml:headers.queryText(round),rootPillHtml:'',
        badgeHtml:headers.renderBadgeHtml(round,metadata)};
      const renderers=createClassicConversationRenderers({
        localizedText:(key,fallback)=>locale[key] || fallback,
        renderToolBlockHtml:block=>results.renderWriteResultHtml(block.round,metadata,header),
      });
      window.repaint=()=>renderers.renderBlock(document.querySelector('#tool'),
        {kind:'tool',name:'edit_file',toolCallId:'edit-skip',round:JSON.parse(JSON.stringify(round))},
        {turn:{turnId:'edit-skip',source:{conversationId:'visual-skip'},projectionRevision:1}});
      repaint();
    }''', {'locale': locale, 'args': args, 'metadata': metadata, 'output': output})
    # Host layout only; all card/row/disclosure styles come from the shipped CSS.
    page.add_style_tag(content='body{display:block;overflow:auto}#fixture{max-width:960px;margin:auto;padding:16px}')
    card = page.locator('.ptool-batch-done-block')
    header = card.locator(':scope > .ptool-batch-done-header')
    badge = header.locator('.ptool-badge-err')
    assert badge.inner_text() == '0/3'
    assert not card.evaluate('node=>node.open')
    header.focus()
    page.keyboard.press('Enter')
    rows = card.locator('.ptool-batch-done-list > .ptool-batch-fail')
    assert rows.count() == 3
    assert rows.locator('.ptool-batch-idx').all_text_contents() == ['1', '2', '3']
    assert rows.locator('.ptool-batch-desc').all_text_contents() == ['Change 1', 'Change 2', 'Change 3']
    assert rows.locator('.ptool-op').all_text_contents() == ['insert_before', 'replace', 'insert_after']
    assert card.locator('.ptool-edit-kind').all_text_contents() == ['match-not-found']
    for index, expected in enumerate([skipped, failure, skipped]):
        row = rows.nth(index)
        detail = row.locator('.ptool-batch-fail-detail')
        assert not detail.is_visible()
        row.locator('summary').focus()
        page.keyboard.press('Enter')
        assert detail.is_visible()
        assert expected in detail.inner_text()
        assert (failure if index != 1 else skipped) not in detail.inner_text()
        assert '[3] SKIP' not in detail.inner_text()
    assert card.locator('.ptool-batch-ok, .bdiff-block, script').count() == 0
    assert not page.evaluate('Boolean(window.editInjected)')
    page.evaluate('repaint()')
    assert rows.nth(2).locator('summary').evaluate('node=>node===document.activeElement')
    assert all(rows.nth(index).locator('.ptool-batch-fail-detail').is_visible() for index in range(3))
    assert badge.is_visible()
    assert rows.evaluate_all('nodes=>nodes.every(node=>node.scrollWidth<=node.clientWidth+1)')
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.screenshot(path=str(tmp_path / f'edit-atomic-skips-{width}.png'))
