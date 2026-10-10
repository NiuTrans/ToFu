"""Swarm presentation in Chromium with the real local app, CSS and Surface owner.

Synthetic receipts/pushes stay inside a disposable iframe; no model or user
conversation is created or changed. The fixture reuses production renderers,
overlay/reducer and background-chip code rather than a copied HTML mock.
"""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path, runtime_section

from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1100, 390])
@pytest.mark.parametrize('language', ['en', 'zh'])
def test_live_swarm_activity_is_readable_and_consistent(frontend_visual_page, tmp_path, width, language):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""() => {
      const frame = document.createElement('iframe');
      frame.id = 'swarm-activity-fixture';
      frame.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;z-index:99999;background:white';
      document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('''<!doctype html><html data-theme="tofu"><body>
      <main id="chat" style="padding:12px"></main>
      <div id="convStatusStrip" style="position:fixed;bottom:12px;left:12px;right:12px"><div id="turnNav"></div></div>
      </body></html>''')
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    frame.evaluate(r"""locale => {
      window.t = (key, params) => (locale[key] || key).replace(/\{(\w+)\}/g,
        (match, name) => params?.[name] ?? match);
      window.escapeHtml = s => String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;')
        .replaceAll('>','&gt;').replaceAll('"','&quot;');
      window.runtimeScope = {}; window._TOOL_DISPLAY = {};
      window.errorEnvelopeMessage = String;
      window.activeConvId = 'fixture';
      window.getConvById = id => ({id, title:'Swarm activity verification'});
      window.loadConversation = () => { window.unexpectedNavigation = true; };
      window.activitySnapshot = {swarms:[{swarm_key:'fixture',conv_id:'fixture',running:1,pending:0,completed:0,
        created_at:Date.now()/1000-60,agents:[{id:'rust_port',role:'coder',status:'running',round:24,
        objective:'Port the storage response envelope'}]}],backgroundCommands:[]};
      window.Api = {activity:{background:()=>Promise.resolve(window.activitySnapshot)}};
    }""", locale)
    frame.add_script_tag(path=native_module_path(
        '.native/swarm-visual-surface.js', ROOT / 'frontend/src/conversation/index.ts'))
    frame.add_script_tag(path=native_module_path(
        '.native/swarm-visual-chip.js', ROOT / 'frontend/src/background-activity-presentation.ts'))
    frame.add_script_tag(path=native_module_path(
        '.native/swarm-visual-failure.js', ROOT / 'frontend/src/conversation/presentation/swarm-failure-presentation.ts'))
    frame.add_script_tag(content=runtime_section('ui/streaming_swarm_panel.js'))
    frame.add_script_tag(content=runtime_section('ui/swarm_live_projection.js'))
    frame.add_script_tag(content=runtime_section('background-activity.js'))
    frame.evaluate("""() => {
      // Only external recovery is stubbed; push/rebase/DOM are the real owners.
      _swDemandReconciliation = () => {};
      const round = {toolName:'spawn_agents',toolCallId:'launch',roundNum:1,llmRound:1,taskId:'run',
        status:'done',toolContent:JSON.stringify({swarm_run_id:'run',agents:[{id:'rust_port',role:'coder'}]})};
      const turn = {turnId:'turn',conversationId:'fixture',laneId:'main',actor:'assistant',kind:'reply',
        status:'running',ordinal:1,currentAttemptId:null,createdAt:Date.now(),updatedAt:Date.now(),
        projectionRevision:1,projection:{toolRounds:[round],segments:[{type:'tool_use',blockId:'tool:launch',id:'launch',
        name:'spawn_agents',taskId:'run',input:{},result:{status:'done',content:''}}]}};
      const state = {conversationId:'fixture',conversationRevision:1,transport:'live',turnsById:{turn},
        laneOrder:{main:['turn']},attemptsById:{},queueItems:[],pendingEventsByTurn:{},commandPending:{},liveRoundUsageByTurn:{}};
      const overlays = new Map();
      const surface = createConversationSurface(document.getElementById('chat'),createClassicConversationRenderers({
        renderSafeMarkdownHtml:escapeHtml, renderToolBlockHtml:block=>_buildSwarmPanelHTML(block.round,[block.round]),
      }));
      const paint = () => surface.render(selectConversationViewModel(controller.presentation.compose({id:'fixture'},state,state)));
      window.failureNotices = [];
      const controller = createSwarmPushPresentationController({
        translate: t, notifyFailure: notice => window.failureNotices.push(notice),
        findConversation:id=>id==='fixture'?{id}:null,readTurnState:()=>state,readOverlay:(_conv,id)=>overlays.get(id),
        upsertOverlay:(_conv,value)=>{overlays.set(value.turnId,value);paint();},removeOverlay:()=>{},
        hydrateConversation:()=>null,attachAutoContinue(){},reducePhase:_handleSwarmPhase,reduceAgent:_handleSwarmAgent,
        debug:(_message,error)=>{throw error;},warn(){}
      });
      window.sendSwarm = frame => controller.handleFrame({channel:'swarm',conversationId:'fixture',
        taskId:'fixture',executionTaskId:'run',swarmId:'run',...frame});
      window.swarmController = controller;
      window.swarmState = state;
      window.swarmOverlays = overlays;
      window.renderSwarm = paint;
      window.fixtureSurface = surface;
      sendSwarm({type:'swarm_phase',phase:'spawning',agents:[{id:'rust_port',role:'coder',model:'test-model',
        objective:'Port the storage response envelope.\\n'+'Detailed task instruction.\\n'.repeat(35)}]});
      // Exercise native disclosures plus the two unchanged registry action ports.
      document.getElementById('chat').addEventListener('click',event=>{
        const copy=event.target.closest('.sw-a-id');
        if(copy){_swarmCopyAgentId(copy,event);return;}
        const agent=event.target.closest('.sw-a-header');
        if(agent){_swarmToggleClass(agent,'.sw-agent','sw-a-open');return;}
        const panel=event.target.closest('.sw-header');
        if(panel)_swarmToggleClass(panel,'.sw-panel','sw-collapsed');
      });
      window.copiedId='';
      Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.copiedId=text;}}});
      runtimeScope.updateBackgroundActivity();
    }""")
    agent = frame.locator('.sw-agent')
    assert 'sw-a-pending' in agent.get_attribute('class')
    assert agent.evaluate('(el)=>getComputedStyle(el).opacity') == '1'
    frame.evaluate("sendSwarm({type:'swarm_agent_phase',agentId:'rust_port',phase:'running'})")
    assert 'sw-a-running' in agent.get_attribute('class')
    assert frame.locator('.sw-a-body').is_visible(), 'physical start automatically opens the card'
    assert frame.locator('.sw-header-subtitle').inner_text() == locale['bgActivity.summaryRunning'].replace('{count}', '1')
    assert 'queued' not in frame.locator('.sw-header-subtitle').inner_text()
    task_details = frame.locator('.sw-a-task')
    assert not task_details.evaluate('(details) => details.open')
    # The generic activity outline must not open the panel's nested task
    # disclosure. Only the reader controls task/history folds across pushes.
    assert frame.locator('.sw-a-objective').is_hidden()
    assert frame.locator('.sw-a-task > summary').is_visible()
    frame.locator('.sw-a-task > summary').click()
    assert task_details.evaluate('(details) => details.open')
    assert frame.locator('.sw-a-objective').is_visible()
    frame.evaluate("window.taskBeforePush = document.querySelector('.sw-a-task')")
    frame.evaluate("""sendSwarm({type:'swarm_agent_tool_call',agentId:'rust_port',callId:'read-1',
      toolName:'read_files',callStatus:'running',argsBrief:'artifact.rs',roundNum:24})""")
    assert frame.locator('.sw-a-task').get_attribute('open') is not None
    assert frame.evaluate("taskBeforePush === document.querySelector('.sw-a-task')")
    frame.evaluate("""sendSwarm({type:'swarm_agent_tool_call',agentId:'rust_port',callId:'read-1',
      toolName:'read_files',callStatus:'done',preview:'Verified live file result <script>not markup</script>',callElapsed:0.4})""")
    detail = frame.locator('.sw-a-latest .sw-tl-detail')
    assert detail.is_visible()
    assert 'Verified live file result' in detail.inner_text()
    assert detail.locator('script').count() == 0
    assert frame.locator('.sw-tl-row').evaluate('(el)=>getComputedStyle(el).animationName') == 'none'
    frame.locator('.sw-a-task > summary').click()
    frame.locator('.sw-a-id').click()
    assert frame.evaluate('copiedId') == 'agent-coder-rust_port'
    frame.locator('#bgActivityChip').click()
    assert frame.locator('.bg-act-agent-id').inner_text() == 'agent-coder-rust_port'
    assert locale['bgActivity.agentRound'].replace('{round}', '24') in frame.locator('.bg-act-agent-status').inner_text()
    assert frame.locator('.bg-act-swarm-summary').inner_text() == locale['bgActivity.summaryRunning'].replace('{count}', '1')
    # Reproduce the old mixed snapshot: 2 running / 0 queued, two pending rows.
    frame.evaluate("""() => {
      activitySnapshot.swarms[0].running = 2;
      activitySnapshot.swarms[0].agents = [
        {id:'rust_port',role:'coder',status:'pending',waitReason:'capacity',objective:'Port the storage response envelope'},
        {id:'review',role:'reviewer',status:'pending',waitReason:'dependencies',dependsOn:['rust_port'],objective:'Review changes'}];
      runtimeScope.updateBackgroundActivity();
    }""")
    frame.locator('#bgActivityChip').click()
    frame.wait_for_function("document.querySelectorAll('.bg-act-agent').length === 2")
    assert frame.locator('.bg-act-swarm-summary').inner_text() == locale['bgActivity.summaryWaiting'].replace('{count}', '2')
    assert frame.locator('.bg-act-agent-status').all_inner_texts() == [
        locale['swarm.wait.capacity'], locale['swarm.wait.dependencies']]
    assert frame.locator('.bg-act-agent-status').nth(1).get_attribute('title') == 'rust_port'
    assert '0' not in frame.locator('.bg-act-swarm-summary').inner_text()
    frame.locator('.bg-act-agent-id').first.click()
    assert not frame.evaluate('Boolean(window.unexpectedNavigation)')
    assert frame.locator('#bgActivityPopover').is_visible()
    bounds = frame.locator('.sw-panel').bounding_box()
    assert bounds and bounds['width'] <= width
    assert frame.locator('.sw-agent').evaluate('(el)=>el.scrollWidth <= el.clientWidth+1')
    assert frame.locator('#bgActivityPopover').evaluate('(el)=>el.scrollWidth <= el.clientWidth+1')
    screenshot = tmp_path / f'swarm-activity-{language}-{width}.png'
    page.locator('#swarm-activity-fixture').screenshot(path=str(screenshot))
    print(f'Swarm screenshot: {screenshot}')
    # Reader collapse is not undone by the next actual push.
    frame.locator('#bgActivityChip').click()
    frame.locator('.sw-a-header .sw-a-role-tag').click()
    assert not frame.locator('.sw-a-body').is_visible(), 'reader closes card before next push'
    frame.evaluate("""sendSwarm({type:'swarm_agent_tool_call',agentId:'rust_port',callId:'read-2',
      toolName:'grep_search',callStatus:'done',preview:'Second result'})""")
    assert not frame.locator('.sw-a-body').is_visible()
    frame.locator('.sw-a-header .sw-a-role-tag').click()
    assert 'Second result' in frame.locator('.sw-a-latest').inner_text()
    frame.locator('.sw-a-tool-history > summary').click()
    frame.locator('details.sw-tl-row > summary').click()
    assert 'Verified live file result' in frame.locator('details.sw-tl-row .sw-tl-detail').inner_text()
    frame.evaluate("window.beforeRetryAdmission = Date.now()")
    frame.evaluate("""sendSwarm({type:'swarm_agent_phase',agentId:'rust_port',
      phase:'pending',status:'pending',waitReason:'capacity'})""")
    assert frame.locator('.sw-a-phase-pill').inner_text() == locale['swarm.wait.capacity']
    assert frame.locator('.sw-header-subtitle').inner_text() == locale['bgActivity.summaryWaiting'].replace('{count}', '1')
    assert frame.locator('.sw-status-pill').inner_text() == locale['swarm.phase.queued']
    assert frame.locator('.sw-status-pill').inner_text() not in ['Queued', '排队中']
    assert 'sw-a-pending' in agent.get_attribute('class')
    assert frame.locator('.sw-a-timer').count() == 0
    frame.evaluate("sendSwarm({type:'swarm_agent_phase',agentId:'rust_port',phase:'running'})")
    assert frame.locator('.sw-a-phase-pill').inner_text() == locale['swarm.phase.running']
    assert 'sw-a-running' in agent.get_attribute('class')
    assert frame.locator('.sw-a-timer').evaluate('(el)=>Number(el.dataset.swStart) >= beforeRetryAdmission')

    frame.evaluate("sendSwarm({type:'swarm_phase',phase:'interrupted'})")
    assert frame.locator('.sw-status-pill').inner_text() == locale['swarm.phase.interrupted']
    assert frame.locator('.sw-header-subtitle').inner_text() == locale['swarm.summary.interrupted'].replace('{count}', '1')
    assert frame.locator('.sw-a-timer').count() == 0
    frame.evaluate("sendSwarm({type:'swarm_agent_phase',agentId:'rust_port',phase:'running'})")
    assert frame.locator('.sw-status-pill').inner_text() == locale['swarm.phase.running']
    assert frame.locator('.sw-a-timer').count() == 1
    frame.evaluate("""sendSwarm({type:'swarm_agent_complete',agentId:'rust_port',status:'failed',
      error:'Provider unavailable <img src=x onerror=alert(1)>'})""")
    assert frame.evaluate('failureNotices.length') == 1
    assert 'Provider unavailable <img' in frame.evaluate('failureNotices[0].detail')
    frame.evaluate("""sendSwarm({type:'swarm_agent_complete',agentId:'rust_port',status:'failed',
      error:'Provider unavailable <img src=x onerror=alert(1)>'})""")
    assert frame.evaluate('failureNotices.length') == 1, 'duplicate push must not alert twice'
    assert frame.locator('.sw-status-pill').inner_text() == locale['swarm.phase.failed']
    assert frame.locator('.sw-header-subtitle').inner_text() == locale['swarm.summary.failed'].replace('{count}', '1')
    assert locale['swarm.failure.generic'] in frame.locator('.sw-a-err').inner_text()
    assert locale['swarm.failure.genericRecovery'] in frame.locator('.sw-a-err').inner_text()
    assert not frame.locator('.sw-a-err-details .sw-a-err-reason').is_visible()
    frame.locator('.sw-a-err-details > summary').click()
    assert 'Provider unavailable <img' in frame.locator('.sw-a-err-reason').inner_text()
    assert frame.locator('.sw-a-err img').count() == 0
    assert locale['swarm.failure.caution'] not in frame.locator('.sw-a-err').inner_text()
    frame.evaluate("""() => { const row=document.querySelector('.sw-agent');
      const round=window.fixtureFailureRound={toolName:'spawn_agents',_swarm:true,_swarmEndTime:Date.now(),
        _swarmSnapshot:{settled:true,agents:[{id:'rust_port',role:'coder',status:'failed',
          error:'No slot available for model=kimi-k3; no upstream request was sent',modifiedFiles:0}]}};
      const host=document.createElement('div');host.id='failure-fixture';
      document.getElementById('chat').append(host);
      host.innerHTML=_buildSwarmPanelHTML(round,[round]);
    }""")
    failure = frame.locator('#failure-fixture')
    assert locale['swarm.failure.noSlot'] in failure.locator('.sw-a-err').inner_text()
    assert locale['swarm.failure.noSlotRecovery'] in failure.locator('.sw-a-err').inner_text()
    assert failure.locator('.sw-a-err-details .sw-a-err-reason').is_hidden()
    failure.locator('.sw-a-err-details > summary').click()
    assert 'No slot available for model=kimi-k3' in failure.locator('.sw-a-err-reason').inner_text()
    assert failure.locator('.sw-agent').evaluate('(el)=>el.scrollWidth <= el.clientWidth+1')
    frame.evaluate("""() => {
      const round={toolName:'spawn_agents',status:'done',toolContent:
        \"Swarm tool error: ImportError: cannot import name 'observed_usage_totals' <script>unsafe()</script>\"};
      document.querySelector('#failure-fixture').innerHTML=_buildSwarmPanelHTML(round,[round]);
    }""")
    assert failure.locator('.sw-status-pill').inner_text() == locale['swarm.launchFailed']
    assert failure.locator('.sw-empty, .sw-agent, [data-sw-start]').count() == 0
    failure.locator('.sw-a-err-details > summary').click()
    assert 'observed_usage_totals' in failure.locator('.sw-a-err-reason').inner_text()
    assert failure.locator('script').count() == 0
    frame.evaluate('fixtureSurface.dispose(); window.dispatchEvent(new Event("pagehide"))')


@pytest.mark.parametrize('live_parent', [True, False])
def test_missed_swarm_push_recovers_and_resumes_with_real_scheduler(frontend_visual_page, live_parent):
    """Dropped transport cannot strand an active card; no user state is touched."""
    page = frontend_visual_page
    page.evaluate("""() => { const frame=document.createElement('iframe');
      frame.id='swarm-recovery-fixture';document.body.append(frame); }""")
    frame = page.frames[-1]
    frame.set_content('<!doctype html><html><body><main id="chat"></main></body></html>')
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    frame.evaluate(r"""locale => {
      window.t=(key,params)=>(locale[key]||key).replace(/\{(\w+)\}/g,(_,name)=>params?.[name]??name);
      window.escapeHtml=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
      window._TOOL_DISPLAY={};window.runtimeScope={};
      window.fixtureHidden=false;window.conversations=[{id:'fixture'}];
      Object.defineProperty(document,'hidden',{configurable:true,get:()=>fixtureHidden});
      Object.defineProperty(document,'visibilityState',{
        configurable:true,get:()=>fixtureHidden?'hidden':'visible'});
      window.fakeNow=Date.now();Date.now=()=>window.fakeNow;
      window.recoveryTimers=new Map();let nextId=0;
      window.setTimeout=(callback,delay)=>{const id=++nextId;
        recoveryTimers.set(id,{at:fakeNow+Number(delay),callback});return id;};
      window.clearTimeout=id=>recoveryTimers.delete(id);
      window.advanceRecovery=async ms=>{const target=fakeNow+ms;let cycles=0;
        while(true){const due=[...recoveryTimers.entries()].filter(([,v])=>v.at<=target)
          .sort((a,b)=>a[1].at-b[1].at)[0];if(!due)break;
          if(++cycles>30)throw Error('recovery hot loop');
          fakeNow=due[1].at;recoveryTimers.delete(due[0]);await due[1].callback();}
        fakeNow=target;return cycles;};
    }""", locale)
    frame.add_script_tag(path=native_module_path(
        '.native/swarm-recovery-surface.js', ROOT / 'frontend/src/conversation/index.ts'))

    frame.add_script_tag(content=runtime_section('ui/streaming_swarm_panel.js'))
    frame.add_script_tag(content=runtime_section('ui/swarm_live_projection.js'))
    frame.evaluate("""liveParent => {
      _swEnsureTicker=()=>{};
      const round={toolName:'spawn_agents',toolCallId:'reused-launch',taskId:'execution',
        attemptId:'attempt',roundNum:1,llmRound:0,status:'done',
        _swarmSnapshot:{swarm_id:'execution',settled:false,agents:[{id:'child',status:'running',toolCalls:[]}]}};

      if(!liveParent){
        delete round._swarmSnapshot;
        round.toolContent=JSON.stringify({swarm_run_id:'execution',agents:[{id:'child',status:'running'}]});
      }
      window.durableTurn={turnId:'turn',conversationId:'fixture',laneId:'main',actor:'assistant',kind:'reply',
        status:liveParent?'running':'completed',ordinal:1,currentAttemptId:'attempt',
        createdAt:fakeNow,updatedAt:fakeNow,projectionRevision:1,projection:{toolRounds:[round],segments:[{
          type:'tool_use',blockId:'tool:launch',id:'reused-launch',name:'spawn_agents',
          taskId:'execution',attemptId:'attempt',llmRound:0,input:{},result:{status:'done',content:''}}]}};
      window.durableBefore=JSON.stringify(durableTurn);
      const state={conversationId:'fixture',conversationRevision:1,transport:'live',
        turnsById:{turn:durableTurn},laneOrder:{main:['turn']},attemptsById:{},queueItems:[],
        pendingEventsByTurn:{},commandPending:{},liveRoundUsageByTurn:{}};
      const overlays=new Map();window.recoveryRequests=[];
      const surface=createConversationSurface(document.getElementById('chat'),createClassicConversationRenderers({
        renderSafeMarkdownHtml:escapeHtml,renderToolBlockHtml:block=>_buildSwarmPanelHTML(block.round,[block.round])}));
      const paint=()=>surface.render(selectConversationViewModel(controller.presentation.compose({id:'fixture'},state,state)));
      const controller=createSwarmPushPresentationController({findConversation:id=>id==='fixture'?{id}:null,
        readTurnState:()=>state,readOverlay:(_conv,id)=>overlays.get(id)||null,
        upsertOverlay:(_conv,value)=>{overlays.set(value.turnId,value);paint();return true;},
        removeOverlay(){},hydrateConversation:()=>null,attachAutoContinue(){},
        reducePhase:_handleSwarmPhase,reduceAgent:_handleSwarmAgent,debug:(_m,e)=>{throw e;},warn(){}});
      runtimeScope.ConversationSwarmPresentation=controller.presentation;
      window.Api={swarm:{status:async id=>{recoveryRequests.push(id);return {active:true,known:true,agents:[{
        id:'child',status:'running',round:11,toolCalls:[{callId:'missed-call',toolName:'read_files',status:'done',
          preview:'Recovered missing tool history'}]},{id:'another-wave',status:'running',toolCalls:[]}]};}}};
      window.recoverySurface=surface;
      paint();
    }""", live_parent)
    assert frame.locator('.sw-a-latest').count() == 0
    # Receipt-only reload has no worker phase yet: its existing first-truth
    # probe is 800ms, versus the regular 20s running-snapshot interval.
    first_probe_ms = 20_000 if live_parent else 800
    assert frame.evaluate('ms => advanceRecovery(ms)', first_probe_ms - 1) == 0
    assert frame.evaluate('recoveryRequests.length') == 0
    frame.evaluate('advanceRecovery(1)')
    assert frame.locator('.sw-a-latest').inner_text().find('Recovered missing tool history') >= 0
    assert frame.evaluate('recoveryRequests') == ['fixture']
    assert frame.locator('.sw-agent').count() == 1, 'foreign launch roster must not be grafted'
    assert frame.evaluate('JSON.stringify(durableTurn) === durableBefore')
    frame.evaluate("fixtureHidden=true;document.dispatchEvent(new Event('visibilitychange'))")
    requests_before = frame.evaluate('recoveryRequests.length')
    frame.evaluate('advanceRecovery(240000)')
    assert frame.evaluate('recoveryRequests.length') == requests_before
    frame.evaluate("fixtureHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
    frame.evaluate('advanceRecovery(0)')
    assert frame.evaluate('recoveryRequests.length') == requests_before + 1
    assert 'Recovered missing tool history' in frame.locator('.sw-a-latest').inner_text()
    assert frame.evaluate('JSON.stringify(durableTurn) === durableBefore')
    frame.evaluate('_swReconciliationScheduler.stop();recoverySurface.dispose()')
