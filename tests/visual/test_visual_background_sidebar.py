"""Real sidebar/chip renderers in Chromium; synthetic state stays in an iframe."""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path, runtime_section

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1100, 390])
@pytest.mark.parametrize('language', ['en', 'zh'])
def test_background_sidebar_remains_active_after_parent_settles(page, tmp_path, width, language):
    page.set_viewport_size({'width': width, 'height': 850})
    page.evaluate("""() => {
      const frame=document.createElement('iframe');frame.id='background-sidebar-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;z-index:99999;background:white';
      document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('''<!doctype html><html data-theme="tofu"><body>
      <aside class="sidebar" style="position:relative;transform:none;width:310px;max-width:100%;height:580px">
        <div style="padding:16px;font-weight:600">Background activity</div>
        <div id="sidebarSearchStats"></div><div id="convList"></div>
      </aside><div class="conv-status-strip" id="convStatusStrip"
        style="position:fixed;bottom:16px;left:12px;right:12px"><div id="turnNav"></div></div>
      </body></html>''')
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    frame.evaluate(r"""locale => {
      window.t=(key,vars)=>(locale[key]||key).replace(/\{(\w+)\}/g,(match,name)=>vars?.[name]??match);
      document.documentElement.lang=locale['sidebar.answering']==='Answering'?'en':'zh';
      window.escapeHtml=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;')
        .replaceAll('>','&gt;').replaceAll('"','&quot;');
      window.runtimeScope={ConversationTurnRead:{state:()=>({turnsById:{}}),ordered:()=>[]}};
      const disposers=[];
      window.retainedCompositionLifecycle={add:fn=>disposers.push(fn),listen:()=>{}};
      window.disposeFixture=()=>disposers.reverse().forEach(fn=>fn());
      window.activeConvId='parent';window.sidebarSearchQuery='';
      window._conversationDisplayTitle=(title,fallback)=>title||fallback;
      window.conversationTimestampLabels=()=>({date:'Today',time:'11:37:42'});
      window.presentConversationRateLimit=()=>null;
      window.conversations=[
        {id:'parent',title:'Completed reply — workers still running'},
        {id:'command',title:'Background build in another conversation'},
        {id:'idle',title:'No remaining work'}
      ].map(row=>({...row,_serverTurnCount:1,updatedAt:Date.now(),lastMsgRole:'assistant',
        lastFinishReason:'stop',lastMsgHasOutput:true}));
      window.getConvById=id=>conversations.find(row=>row.id===id);
      window.snapshot={swarms:[{swarm_key:'s',conv_id:'parent',agents:[
        {id:'first',status:'running'},{id:'second',status:'running'}]}],backgroundCommands:[
        {command_id:'build',conv_id:'command',status:'running',command:'npm run build'}]};
      window.Api={activity:{background:()=>Promise.resolve(snapshot)}};
    }""", locale)
    frame.add_script_tag(path=native_module_path('.native/sidebar-visual-activity.js',
                                               ROOT / 'frontend/src/background-activity-presentation.ts'))
    frame.add_script_tag(path=native_module_path('.native/sidebar-visual-search.js',
                                               ROOT / 'frontend/src/sidebar-search-controller.ts'))
    frame.add_script_tag(content=runtime_section('ui/conversation_list.js', scope_prelude=False)
                         + '\n' + runtime_section('background-activity.js', scope_prelude=False))
    frame.evaluate('renderConversationList(); runtimeScope.updateBackgroundActivity()')
    parent = frame.locator('.conv-item[data-conv-id="parent"]')
    badge = parent.locator('.conv-status-background')
    badge.wait_for(state='visible')
    assert badge.text_content() == locale['sidebar.backgroundRunningTag'].replace('{count}', '2')
    assert frame.locator('.conv-item[data-conv-id="command"] .conv-status-background').is_visible()
    assert frame.locator('.conv-item[data-conv-id="idle"] .conv-status-tag').count() == 0
    assert not frame.locator('#bgActivityChip').is_visible(), 'agent activity no longer belongs in the composer'
    assert frame.evaluate('convIsBusy(conversations[0])') is False
    frame.evaluate('window.originalSidebarRow=document.querySelector(".conv-item[data-conv-id=parent]")')
    screenshot = tmp_path / f'background-sidebar-{language}-{width}.png'
    page.locator('#background-sidebar-fixture').screenshot(path=str(screenshot))
    print(f'Background sidebar screenshot: {screenshot}')
    frame.evaluate("""() => {
      activeConvId='idle';snapshot.swarms[0].agents.forEach(agent=>agent.status='pending');
      runtimeScope.updateBackgroundActivity();
    }""")
    frame.wait_for_function('document.querySelector(".conv-item[data-conv-id=parent] .conv-waiting-dot")')
    assert badge.text_content() == locale['sidebar.backgroundWaitingTag'].replace('{count}', '2')
    assert not frame.locator('#bgActivityChip').is_visible()
    assert frame.evaluate('originalSidebarRow===document.querySelector(".conv-item[data-conv-id=parent]")')
    assert badge.evaluate('(el)=>{const row=el.closest(".conv-item").getBoundingClientRect();const b=el.getBoundingClientRect();return b.right<=row.right && b.left>=row.left;}')
    assert frame.locator('#convList').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    frame.evaluate('snapshot={swarms:[],backgroundCommands:[]};runtimeScope.updateBackgroundActivity()')
    frame.wait_for_function('!document.querySelector(".conv-status-background")')
    assert parent.locator('.conv-background-dot').count() == 0
    frame.evaluate('disposeFixture()')
