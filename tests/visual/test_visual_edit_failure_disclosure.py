"""Chromium: edit failures disclose evidence using the real renderer/loader."""
import json

import pytest

from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


def _fixture(page, *, status='error'):
    for name, source in [
        ('edit-failure-presenter', 'frontend/src/conversation/presentation/tool-result-presentation.ts'),
        ('edit-failure-renderer', 'frontend/src/conversation/ui/classic-conversation-renderers.ts'),
    ]:
        page.add_script_tag(path=native_module_path(f'.native/{name}.js', ROOT / source))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    page.evaluate(r'''({locale,status}) => {
      document.body.innerHTML='<main id="fixture" class="conversation-surface"><div id="tool"></div></main>';
      const translate=(key,args) => (locale[key] || key).replace(/\{(\w+)\}/g,(_,name)=>args?.[name] ?? '');
      const presentation=createToolResultPresentation({translate,
        writeGateRefusal:{resolveRefusal:()=>null,renderNoticeHtml:()=>''}});
      const header={iconHtml:'',queryHtml:'Edit example.py',rootPillHtml:'',badgeHtml:'failed'};
      window.reference={ownerScope:'owner:1',conversationId:'c',turnId:'t',
        projectionRevision:7,roundIndex:0,toolCallId:'edit'};
      window.requests=[];
      const rows=Array.from({length:6},()=>({path:'example.py',operation:'replace',status:'fail',detail:''}));
      window.metadata={editSummaries:rows};
      window.output='Error: write refused before outcomes\n<script>window.editInjected=true</script>\nRecovery: read current file first.';
      if(status==='done') {
        rows[0].status='ok';
        window.output=JSON.stringify({status:'partial',summary:
          'Applied 1/6 edits (5 failed)\n[1] OK example.py [replace]: changed\n'
          + rows.slice(1).map((_,i)=>`[${i+2}] FAIL example.py [replace] [match-not-found]: Anchor missing\nRecovery: read current content`).join('\n')});
      }
      window.round={roundNum:1,toolName:'edit_file',toolCallId:'edit',status,
        toolArgs:{edits:rows.map(()=>({path:'example.py',operation:'replace',anchor:'old',content:'new'}))},
        _snapshotToolResult:{...reference,preview:'Error preview',chars:4096}};
      const renderers=createClassicConversationRenderers({
        localizedText:(key,fallback)=>locale[key] || fallback,
        renderToolBlockHtml:block=>presentation.renderWriteResultHtml(block.round,metadata,header),
        loadToolResult:(ref,signal)=>new Promise((resolve,reject)=>{
          const request={ref,signal,resolve,reject}; requests.push(request);
          signal.addEventListener('abort',()=>reject(new DOMException('Read cancelled','AbortError')),{once:true});
        }),
      });
      const node=document.querySelector('#tool');
      window.repaint=()=>renderers.renderBlock(node,{kind:'tool',name:'edit_file',round}, {turn:{turnId:'t'}});
      window.completeRead=(extra={})=>requests.at(-1).resolve({...reference,toolContent:output,...extra});
      window.dispose=()=>{renderers.disposeBlock(node);node.remove();};
      repaint();
    }''', {'locale': locale, 'status': status})
    page.add_style_tag(content='body{display:block;overflow:auto}#fixture{max-width:100%;padding:16px}')


@pytest.mark.parametrize('width', [1280, 390])
def test_historical_failure_expansion_cancels_and_reopens_without_losing_focus(
        frontend_visual_page, width, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    _fixture(page)
    header = page.locator('.ptool-batch-done-header')
    body = page.locator('.ptool-result-body')
    assert not body.is_visible()
    header.focus()
    page.keyboard.press('Enter')
    page.wait_for_function('requests.length===1')
    assert 'Loading' in body.inner_text()
    page.keyboard.press('Space')
    page.wait_for_function('requests[0].signal.aborted')
    assert not body.is_visible()
    page.keyboard.press('Enter')
    page.wait_for_function('requests.length===2')
    page.evaluate('completeRead()')
    page.wait_for_function("document.querySelectorAll('.ptool-batch-fail-detail').length===6")
    assert page.locator('.ptool-batch-done-block').evaluate('node=>node.open')
    assert header.evaluate('node=>node===document.activeElement')
    row = page.locator('.ptool-batch-fail').first
    row.locator('summary').focus()
    page.keyboard.press('Enter')
    detail = row.locator('.ptool-batch-fail-detail')
    assert detail.is_visible()
    assert 'write refused before outcomes' in detail.inner_text()
    assert '<script>window.editInjected=true</script>' in detail.inner_text()
    assert 'Recovery: read current file first.' in detail.inner_text()
    assert not page.evaluate('Boolean(window.editInjected)')
    assert page.locator('#tool script, .bdiff-block').count() == 0
    page.evaluate('repaint()')
    assert detail.is_visible()
    assert row.locator('summary').evaluate('node=>node===document.activeElement')
    assert page.evaluate('requests.length') == 2
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.screenshot(path=str(tmp_path / f'edit-failure-disclosure-{width}.png'))


@pytest.mark.parametrize('status', ['error', 'done'])
def test_failed_history_read_has_retry_and_loaded_evidence(frontend_visual_page, status):
    page = frontend_visual_page
    _fixture(page, status=status)
    page.locator('.ptool-batch-done-header').click()
    page.wait_for_function('requests.length===1')
    page.evaluate('requests[0].reject({status:503})')
    page.wait_for_function("Boolean(document.querySelector('.ptool-result-body [role=alert]'))")
    assert 'Could not load output' in page.locator('.ptool-result-body').inner_text()
    page.get_by_role('button', name='Retry', exact=True).click()
    page.wait_for_function('requests.length===2')
    page.evaluate('completeRead()')
    page.wait_for_function("Boolean(document.querySelector('.ptool-batch-fail-detail'))")
    row = page.locator('.ptool-batch-fail').first
    row.locator('summary').click()
    assert row.locator('.ptool-batch-fail-detail').is_visible()
    assert ('Anchor missing' if status == 'done' else 'write refused before outcomes') in row.inner_text()
    if status == 'done':
        assert 'Recovery: read current content' in row.inner_text()
    assert row.locator('.bdiff-block').count() == 0
    assert page.evaluate('requests.length') == 2


def test_stale_identity_and_disposal_remain_fenced(frontend_visual_page):
    page = frontend_visual_page
    _fixture(page)
    header = page.locator('.ptool-batch-done-header')
    header.click()
    page.wait_for_function('requests.length===1')
    page.evaluate('completeRead({turnId:"WRONG-TURN"})')
    page.wait_for_function("Boolean(document.querySelector('.ptool-result-body [role=alert]'))")
    assert 'Could not load output' in page.locator('.ptool-result-body').inner_text()
    assert page.locator('.ptool-batch-fail-detail').count() == 0
    page.get_by_role('button', name='Retry', exact=True).click()
    page.wait_for_function('requests.length===2')
    page.evaluate('requests[1].reject({status:409})')
    page.wait_for_function("document.querySelector('.ptool-result-body').textContent.includes('This message changed')")
    assert page.get_by_role('button', name='Retry', exact=True).count() == 0
    header.click()
    header.click()
    page.wait_for_function('requests.length===3')
    page.evaluate('dispose()')
    assert page.evaluate('requests[2].signal.aborted')
    page.evaluate('completeRead()')
    assert page.locator('#tool').count() == 0
