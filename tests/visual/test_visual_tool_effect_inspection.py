"""Real DOM + generated HTTP client: exact occurrence inspection, no write action."""
import json

import pytest

from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


def mount(page, tmp_path, locale='en', mode='http'):
    entry = tmp_path / 'inspection.ts'
    entry.write_text('\n'.join('export * from '+json.dumps(str(ROOT / path))+';' for path in (
        'frontend/src/conversation/ui/renderers/tool-blocks.ts',
        'frontend/src/conversation/ui/renderers/tool-effect-notice.ts',
        'frontend/src/conversation/ui/classic-conversation-renderers.ts',
        'frontend/src/api/conversation-sync.generated.ts')))
    page.add_script_tag(path=native_module_path('.native/tool-effect-inspection.js', entry))
    messages = json.loads((ROOT / f'frontend/src/i18n/locales/{locale}.json').read_text())
    page.evaluate('''({messages,mode})=>{
      document.body.innerHTML='<main class="conversation-surface" id="fixture"><div id="tool"></div><div id="program"></div></main>';
      window.node=document.querySelector('#tool');
      window.reference={conversationId:'producer-conversation',turnId:'producer-turn',operationId:'a'.repeat(32),ownerScope:'a'.repeat(24)};
      window.effect={operationId:reference.operationId,state:'returned',recovery:'reconcile',scope:'attempt',inspection:{...reference},
        reconciliation:{kind:'project_file_write',changeId:'c'.repeat(32),basePath:'/project',path:'file.txt',taskId:'producer',rootIdentity:[1,2],postDigest:'d'.repeat(64),postBytes:6}};
      window.block={id:'tool-1',kind:'tool',name:'write_file',toolCallId:'call',input:{},
        round:{roundNum:1,toolName:'write_file',status:'error',executionEffect:effect},result:{status:'error',content:'ack unavailable'}};
      // Deliberately a different displaying Turn: queries must keep the producer.
      window.turn={turnId:'display-turn',status:'running',metadata:{},source:{conversationId:'display-conversation',projection:{}}};
      window.pending=[];
      window.ports={localizedText:(key,fallback)=>messages[key]??fallback, renderSafeMarkdownHtml:()=>'',
        renderToolBlockHtml:()=>'<details class="ptool-result-block"><summary class="ptool-line">File write</summary><pre>Response unavailable</pre></details>',
        inspectToolEffect:(ref,signal)=>mode==='http'
          ? conversationSyncApi.inspectToolEffect(ref.conversationId,ref.turnId,ref.operationId,ref.ownerScope,{signal})
          : new Promise((resolve,reject)=>pending.push({ref:{...ref},signal,resolve,reject}))};
      window.renderers=createClassicConversationRenderers(ports);
      window.paint=()=>renderToolBlock(node,block,turn,ports);
      window.observe=(index,outcome='file_published',reason='undo_committed')=>pending[index].resolve({ok:true,
        contract:'tofu.conversation-sync.tool-effect-inspection/v1',...pending[index].ref,outcome,reason});
      paint();
    }''', {'messages': messages, 'mode': mode})
    page.add_style_tag(content='body{display:block;height:auto;overflow:auto;margin:0;padding:var(--space-4);background:var(--bg-primary)}#fixture{max-width:var(--conversation-reading-measure);margin-inline:auto;}')
    return messages


@pytest.mark.parametrize('theme,width,locale', [('light', 390, 'en'), ('dark', 1180, 'zh'), ('tofu', 390, 'zh')])
def test_user_inspects_original_occurrence_and_rechecks_with_generated_client(
        frontend_visual_page, tmp_path, theme, width, locale):
    from playwright.sync_api import expect
    page = frontend_visual_page; page.set_viewport_size({'width': width, 'height': 950})
    page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
    messages = mount(page, tmp_path, locale)
    responses = [('file_published', 'undo_committed'), ('file_not_published', 'prepared_file_unpublished'), ('unknown', 'evidence_conflict')]
    requests = []
    def route(request):
        requests.append((request.request.method, request.request.url))
        outcome, reason = responses[len(requests)-1]
        request.fulfill(status=200, content_type='application/json', body=json.dumps({
            'ok': True, 'contract': 'tofu.conversation-sync.tool-effect-inspection/v1',
            'conversationId': 'producer-conversation', 'turnId': 'producer-turn',
            'operationId': 'a' * 32, 'outcome': outcome, 'reason': reason}))
    page.route('**/tool-effects/**', route)
    button = page.locator('#tool [data-tool-effect-inspect]')
    expect(button).to_have_text(messages['tool.effectInspect'])
    expect(page.locator('#tool details')).not_to_have_attribute('open', '')
    button.focus(); button.press('Enter')
    expect(page.locator('#tool')).to_contain_text(messages['tool.effectPublishedBody'])
    assert requests == [('GET', page.url.rstrip('/') + '/api/v4/conversations/producer-conversation/turns/producer-turn/tool-effects/' + 'a' * 32 + '?ownerScope=' + 'a' * 24)]
    expect(button).to_be_focused()
    page.evaluate('window.oldNotice=node.querySelector("[data-tool-effect-notice]");paint()')
    assert page.evaluate('oldNotice===node.querySelector("[data-tool-effect-notice]")')
    assert len(requests) == 1
    button.click(); expect(page.locator('#tool')).to_contain_text(messages['tool.effectUnpublishedBody'])
    button.click(); expect(page.locator('#tool')).to_contain_text(messages['tool.effectEvidenceConflict'])
    assert len(requests) == 3 and all(method == 'GET' for method, _ in requests)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.screenshot(path=str(tmp_path / f'effect-inspection-{theme}-{width}.png'), full_page=True)


def test_inflight_repaint_coalesces_clicks_and_disposal_rejects_late_completion(frontend_visual_page, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page; mount(page, tmp_path, mode='deferred')
    button = page.locator('[data-tool-effect-inspect]')
    button.click(); expect(button).to_be_disabled()
    page.evaluate('node.querySelector("button").click();paint()')
    assert page.evaluate('pending.length') == 1
    assert not page.evaluate('pending[0].signal.aborted')
    page.evaluate('renderers.disposeBlock(node);observe(0)')
    assert page.evaluate('pending[0].signal.aborted')
    expect(page.locator('[data-tool-effect-notice]')).to_have_count(0)


@pytest.mark.parametrize('change', ['operation', 'owner', 'producer'])
def test_changed_occurrence_aborts_old_query_and_late_response_cannot_replace_new_fact(frontend_visual_page, tmp_path, change):
    from playwright.sync_api import expect
    page = frontend_visual_page; messages = mount(page, tmp_path, mode='deferred')
    page.locator('[data-tool-effect-inspect]').click()
    page.evaluate("""change=>{
      const updated=change==='operation'?{operationId:'b'.repeat(32)}:change==='owner'?{ownerScope:'b'.repeat(24)}:{turnId:'another-producer'};
      effect={...effect,operationId:updated.operationId??effect.operationId,inspection:{...reference,...updated}};
      block.round.executionEffect=effect;paint();
    }""", change)
    assert page.evaluate('pending[0].signal.aborted')
    page.locator('[data-tool-effect-inspect]').click()
    page.evaluate("observe(1,'unknown','writer_active');observe(0)")
    expect(page.locator('#tool')).to_contain_text(messages['tool.effectWriterActive'])
    expect(page.locator('#tool')).not_to_contain_text(messages['tool.effectPublishedBody'])


@pytest.mark.parametrize('failure', ['request', 'identity'])
def test_failed_recheck_removes_previous_fact_and_preserves_unknown(frontend_visual_page, tmp_path, failure):
    from playwright.sync_api import expect
    page = frontend_visual_page; messages = mount(page, tmp_path, mode='deferred')
    page.locator('[data-tool-effect-inspect]').click(); page.evaluate('observe(0)')
    expect(page.locator('#tool')).to_contain_text(messages['tool.effectPublishedBody'])
    page.locator('[data-tool-effect-inspect]').click()
    page.evaluate("pending[1].reject(new Error('offline'))" if failure == 'request' else
                  "pending[1].resolve({ok:true,conversationId:'foreign',turnId:'producer-turn',operationId:'a'.repeat(32),outcome:'file_published',reason:'undo_committed'})")
    expect(page.locator('#tool')).to_contain_text(messages['tool.effectInspectionFailed'])
    expect(page.locator('#tool')).not_to_contain_text(messages['tool.effectPublishedBody'])


def test_program_card_repaint_keeps_query_and_kind_change_disposes_it(frontend_visual_page, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page; mount(page, tmp_path, mode='deferred')
    page.evaluate("block={...block,kind:'program'};window.paint=()=>renderers.renderBlock(node,block,{turn});paint()")
    page.locator('[data-tool-effect-inspect]').click(); page.evaluate('paint()')
    assert not page.evaluate('pending[0].signal.aborted')
    page.evaluate("renderers.renderBlock(node,{id:'text',kind:'text',markdown:'replacement'},{turn});observe(0)")
    assert page.evaluate('pending[0].signal.aborted')
    expect(page.locator('[data-tool-effect-notice]')).to_have_count(0)
