"""Goal progress on the actual Surface graph; synthetic state, no execution writes."""
import json

import re
from tests._runtime_sections import runtime_section
import pytest
from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('language,width', [('en', 1100), ('zh', 1100), ('en', 390), ('zh', 390)])
def test_goal_root_and_current_worker_progress(frontend_visual_page, tmp_path, language, width):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    styles = page.locator('link[rel="stylesheet"]').evaluate_all('(nodes)=>nodes.map(n=>n.href)')
    page.evaluate('''() => {
      const frame=document.createElement('iframe');frame.id='goal-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;border:0;z-index:99999';
      document.body.append(frame);
    }''')
    frame = page.frames[-1]
    frame.set_content('<html data-theme="light"><body><div class="chat-inner"><main id="surface"></main></div></body></html>')
    for url in styles:
        frame.add_style_tag(url=url)
    frame.add_style_tag(content='body{display:block;height:auto;overflow:auto;margin:0;padding:12px}.chat-inner{margin-inline:auto;min-height:0;padding:0}')
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    for name, source in [('feature', 'frontend/src/conversation/index.ts'), ('state', 'frontend/src/core/turn-state.ts'),
                         ('activity', 'frontend/src/conversation/presentation/conversation-activity.ts')]:
        frame.add_script_tag(path=native_module_path(f'.native/goal-{name}.js', ROOT / source))
    frame.evaluate(r'''locale => {
      const t=(key,fallback,values)=> (locale[key]||fallback).replace(/\{(\w+)\}/g,(_,k)=>values?.[k]??'');
      window.state=createTurnState('goal');
      const make=(id,ordinal,actor,status,iteration)=>({turnId:id,presentationId:id,conversationId:'goal',laneId:'main',
        ordinal,actor,status,kind:'flow_node',runId:'run',currentAttemptId:id,projectionRevision:1,
        projection:{content: id==='root'?'Goal request':id==='vu'?'Continue iteration':'',orchestration:{iteration}},settlement:{}});
      state.turnsById={root:make('root',150,'assistant','running',1),
        vu:make('vu',155,'virtual_user','completed',4),worker:make('worker',156,'assistant','running',4)};
      state.laneOrder.main=['root','vu','worker'];
      for(const id of ['root','vu','worker']) state.attemptsById[id]={attemptId:id,turnId:id,taskId:'physical',status:state.turnsById[id].status};
      state.turnsById.worker.projection.toolRounds=[{toolName:'spawn_agents',taskId:'nested-producer',
        _swarmSnapshot:{agents:[{id:'audit',name:'<Audit>',status:'running',toolCalls:[{toolName:'read_file',argsBrief:'actual.py',error:'tool denied'}]}]}}];
      state.livePhase={phase:'waiting_model',detailKey:'stream.phase.waitingModel'};
      window.surface=createConversationSurface(document.getElementById('surface'),createClassicConversationRenderers({
        renderSafeMarkdownHtml:s=>s,localizedText:t}));
      window.render=()=>surface.render(selectConversationViewModel(state));render();
    }''', locale)
    # Exercise the production retained sidebar bridge/markup with the same graph.
    sidebar_source = runtime_section('ui/conversation_list.js')
    sidebar_functions = '\n'.join(re.search(
        rf'function {name}\([^\n]*\) \{{.*?\n\}}', sidebar_source, re.S).group(0)
        for name in ('_convStatusFlags', '_convStatusHtml'))
    frame.add_script_tag(content=sidebar_functions)
    frame.evaluate(r'''locale => {
      window.runtimeScope=window;
      window.ConversationTurnRead={ordered:()=>state.laneOrder.main.map(id=>state.turnsById[id]),state:()=>state};
      window.convIsBusy=()=>Object.values(state.turnsById).some(turn=>['pending','running'].includes(turn.status));
      window.t=(key,values)=>(locale[key]||key).replace(/\{(\w+)\}/g,(_,k)=>values?.[k]??'');
      window.escapeHtml=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
      const row=document.createElement('aside');row.id='sidebar-status';document.body.append(row);
      window.renderSidebar=()=>{const html=_convStatusHtml(_convStatusFlags({id:'goal'}));row.innerHTML=html.dotHtml+html.statusTag;};
      renderSidebar();
    }''', locale)
    assert frame.locator('#sidebar-status .conv-streaming-dot').count() == 1
    frame.evaluate("state.livePhase={phase:'retrying',detailKey:'stream.phase.retryRateLimited'};renderSidebar()")
    assert frame.locator('#sidebar-status .conv-ratelimit-dot').count() == 1
    assert frame.locator('#sidebar-status .conv-status-ratelimit').inner_text() == locale['sidebar.rateLimitedTag']
    frame.evaluate("state.livePhase={phase:'waiting_model',detailKey:'stream.phase.waitingModel'};renderSidebar()")
    live = frame.locator('.stream-phase-text')
    assert live.count() == 2
    root_text, worker_text = live.all_text_contents()
    assert ('Run ongoing' if language == 'en' else '运行持续进行中') in root_text
    assert ('Iteration 4' if language == 'en' else '第 4 轮') in root_text
    assert locale['stream.phase.waitingWorkerStatus'] not in root_text
    assert locale['stream.phase.waitingModel'] in worker_text

    assert locale['stream.phase.waitingModel'] in root_text
    assert '<Audit>' in root_text and 'read_file' in root_text and 'actual.py' in root_text
    assert locale['stream.goal.toolError'].replace('{error}', 'tool denied') in root_text
    assert frame.locator('audit').count() == 0
    assert not frame.evaluate('document.documentElement.scrollWidth > innerWidth')
    live_path = tmp_path / f'goal-live-{language}-{width}.png'
    page.locator('#goal-fixture').screenshot(path=str(live_path))
    frame.evaluate("state.livePhase={phase:'tool_exec',tools:['inspect_runtime']};render()")
    assert 'inspect_runtime' in live.first.inner_text()
    frame.evaluate("state.livePhase={phase:'retrying'};render()")
    assert locale['stream.phase.retrying'] in live.first.inner_text()
    frame.evaluate("state.livePhase={phase:'thinking_active'};render()")
    assert locale['stream.thinking.active'] in live.first.inner_text()
    frame.evaluate('''() => {for(const id of ['root','worker'])state.turnsById[id]={...state.turnsById[id],status:'completed'};render()}''')
    assert frame.locator('.stream-phase-text').count() == 0

    frame.evaluate('renderSidebar()')
    assert frame.locator('#sidebar-status .conv-streaming-dot').count() == 0
    assert frame.locator('#sidebar-status .conv-incomplete-dot').count() == 0
    assert frame.locator('#sidebar-status .conv-error-dot').count() == 0
    terminal_path = tmp_path / f'goal-terminal-{language}-{width}.png'
    page.locator('#goal-fixture').screenshot(path=str(terminal_path))
    print(f'Goal screenshots: {live_path} {terminal_path}')
