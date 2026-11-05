"""Background agents share the production Turn info column, never a new dock track.

Disposable browser surface + canonical execution renderer; no store or model work.
"""
import json
import pytest
from tests._runtime_sections import ROOT, native_module_path, runtime_section
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1280, 390])
@pytest.mark.parametrize('language', ['en', 'zh'])
@pytest.mark.parametrize('theme', ['tofu', 'light', 'dark'])
def test_agent_dock_execution_history_and_keyboard(frontend_visual_page, tmp_path, width, language, theme):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""() => {
      const frame=document.createElement('iframe');frame.id='agent-dock-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;z-index:99999';document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('''<!doctype html><html><body><main class="main" style="height:100vh;width:100%;display:block">
      <div id="chatContainer" class="chat-container" style="height:100vh;width:100%"><div id="chatInner" class="chat-inner"></div></div>
      <div id="convStatusStrip" style="position:fixed;bottom:16px;left:16px"><div id="turnNav"></div></div>
      </main></body></html>''')
    frame.evaluate('theme => document.documentElement.dataset.theme=theme', theme)
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    frame.evaluate(r"""locale => {
      window.t=(key,vars)=>(locale[key]||key).replace(/\{(\w+)\}/g,(match,name)=>vars?.[name]??match);
      window.escapeHtml=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
      window.runtimeScope={};window._TOOL_DISPLAY={};window.errorEnvelopeMessage=String;
      window.activeConvId='fixture';window.getConvById=id=>({id,title:'Turn information fixture'});
      window.loadConversation=id=>{window.openedChild=id;};
      const dispose=[];window.retainedCompositionLifecycle={add:fn=>dispose.push(fn)};
      window.disposeDockFixture=()=>dispose.reverse().forEach(fn=>fn());
      window.Api={activity:{background:()=>Promise.resolve({swarms:[],backgroundCommands:[
        {command_id:'build',conv_id:'fixture',status:'running',elapsed_s:5340,
         command:'cd /mnt/your-fs && npm run build:frontend 2>&1 | tail -40 && python3 -m pytest -q tests/visual'}]})}};
    }""", locale)
    for name in ['background-activity-presentation', 'agent-sidebar-dock', 'agent-sidebar-dock-projection',
                 'conversation/ui/conversation-surface', 'conversation/presentation/swarm-failure-presentation',
                 'conversation/presentation/agent-message-receipts', 'conversation/presentation/swarm-messages-projection']:
        frame.add_script_tag(path=native_module_path(f'.native/dock-{name.replace("/", "-")}.js', ROOT / f'frontend/src/{name}.ts'))
    frame.add_script_tag(content=runtime_section('ui/streaming_swarm_panel.js'))
    frame.evaluate("""() => {
      const makeTurn=(id,actor,blocks)=>({turnId:id,laneId:'main',parentTurnId:null,ordinal:actor==='human'?1:2,
        actor,role:actor==='human'?'user':'assistant',kind:actor==='human'?'input':'reply',status:'completed',
        attemptId:null,projectionRevision:1,subtreeRevisionKey:'1',commandPending:null,finish:null,branches:[],actions:[],metadata:{translation:{pending:false}},source:{},blocks});
      window.makeVm=model=>({conversationId:'fixture',conversationRevision:model.length,transport:'live',orphanLanes:[],queue:[],
        mainLane:{laneId:'main',parentTurnId:null,title:'Conversation',kind:'main',turns:[
          makeTurn('input','human',[{kind:'context',blockId:'context',identitySource:'contract',source:{model},value:{snapshot:{model}}}]),
          makeTurn('reply','assistant',[{kind:'text',blockId:'answer',identitySource:'contract',source:{},markdown:'Working quietly, keeping the details close.',deliverable:true,terminal:true,resumable:false}])
        ]}});
      window.surface=createConversationSurface(document.getElementById('chatInner'),{
        renderTurnContextRail(node,block){node.innerHTML='<div class="tctx-head"><span class="tctx-model">'+escapeHtml(block?.value.snapshot.model||'')+'</span></div><div class="tctx-row"><span class="tctx-row-h">Tools</span><span class="tctx-chip">Code Exec</span></div><div class="tctx-row"><span class="tctx-row-h">Workspace</span><span>tofu</span></div>';node.hidden=!block;},
        renderBlock(node,block){node.className='conversation-block conversation-block--'+block.kind;if(block.kind==='context')node.innerHTML='<div class="tctx-fold">GPT-6.1 Sol · Code Exec · tofu</div>';else node.innerHTML='<h2>'+block.markdown+'</h2><p>'+('The foreground answer is complete; background work remains inspectable. '.repeat(80))+'</p>';}
      });
      surface.render(makeVm('GPT-6.1 Sol'));
      const agent={id:'reader',role:'researcher',status:'running',round:42,objective:'Review contracts and source code',conversationId:'child',

        messageReceipts:[{swarmRunId:'run',agentId:'reader',messageId:'message',state:'submitted',inputInjectedAt:10,content:'Received <script>not executable</script>'}],
        _toolCallsOmitted:7,_toolCalls:[{callId:'actual-read',toolName:'read_files',status:'done',argsBrief:'docs/RENDER_CONTRACT.md',
          preview:'Actual canonical execution result <script>not markup</script>',previewTruncated:true,previewFullChars:8000,elapsed:0.4}]};
      window.round={_swarm:true,_swarmActive:true,_swarmRunId:'run',_swarmKey:'session',roundNum:1,status:'running',
        _swarmAgents:[agent,{id:'builder',role:'coder',status:'running',objective:'Rebuild the frontend bundle, run the visual tier in three themes and two widths, then report every layout regression with its screenshot path and the exact selector involved\\nSecond line stays in the detail.'},{id:'finished',role:'reviewer',status:'done'},
          {id:'failed',role:'tester',status:'failed',error:'Tests need attention'}]};
      round._swarmSnapshot={swarm_id:'run',messageReceiptsAvailable:true,
        messageParticipants:[{id:'run',taskId:'run',label:'Coordinator'},{id:'reader',taskId:'reader-task',label:'Researcher'}],
        messageReceipts:[{swarmRunId:'run',agentId:'run',messageId:'to-root',senderTaskId:'reader-task',
          state:'submitted',submittedAt:1000,content:'Coordinator received <script>safe</script>'}]};
      window.fixtureTurns=[{turnId:'reply',projection:{toolRounds:[round]}}];
      runtimeScope.ConversationSwarmPresentation={candidates:()=>fixtureTurns};
      runtimeScope.ConversationTurnRead={latest:()=>({turnId:'input',actor:'human'})};
      window.beforeWidth=document.getElementById('chatContainer').getBoundingClientRect().width;
      window.beforeMargin=getComputedStyle(document.getElementById('chatContainer')).marginInlineEnd;
    }""")
    frame.add_script_tag(content=runtime_section('background-activity.js', scope_prelude=False))
    frame.evaluate('runtimeScope.updateBackgroundActivity()')
    dock = frame.locator('.agent-dock')
    dock.wait_for(state='visible')
    assert frame.locator('.agent-dock-active .agent-dock-agent').count() == 3
    assert frame.locator('.agent-dock-history .agent-dock-agent').count() == 0
    assert frame.locator('.agent-dock-avatar').count() == 0
    assert frame.locator('main > .agent-dock').count() == 0
    assert frame.evaluate("document.getElementById('chatContainer').getBoundingClientRect().width===beforeWidth")
    assert frame.evaluate("getComputedStyle(document.getElementById('chatContainer')).marginInlineEnd===beforeMargin")
    rows = frame.locator('.agent-dock-active .agent-dock-agent')
    reader = rows.first
    assert reader.locator('.agent-dock-purpose').inner_text() == 'Review contracts and source code'
    assert reader.locator('.agent-dock-status').inner_text() == locale['swarm.phase.running']
    assert reader.locator('.agent-dock-name').inner_text() == 'reader'
    assert reader.get_attribute('data-tone') == 'live'
    # The collapsed row carries the round count beside the state; no count, no slot.
    assert reader.locator('.agent-dock-rounds').inner_text() == locale['agentDock.rounds'].replace('{count}', '42').replace('{s}', 's')
    assert not rows.nth(1).locator('.agent-dock-rounds').is_visible()
    builder = rows.nth(1).locator('.agent-dock-purpose')
    assert builder.evaluate('el=>el.scrollHeight>el.clientHeight+1'), 'a long purpose is clamped'
    assert builder.evaluate('el=>el.getBoundingClientRect().height<=2*parseFloat(getComputedStyle(el).lineHeight)+1')
    assert 'Second line' not in rows.nth(1).locator(':scope > summary').text_content()
    failed = rows.nth(2)
    assert failed.get_attribute('data-tone') == 'error'
    assert failed.locator('.agent-dock-status').inner_text() == locale['swarm.phase.failed']
    assert failed.locator('.agent-dock-purpose').inner_text() == 'failed'
    assert not failed.locator('.agent-dock-name').is_visible()
    assert dock.evaluate("el=>getComputedStyle(el).position==='static' && getComputedStyle(el).boxShadow==='none'")
    if width == 1280:
        assert dock.evaluate("el=>el.parentElement.matches('.turn-ctx > .tctx-card')")
        rail = frame.locator('.user-msg > .turn-ctx')
        assert dock.bounding_box()['x'] >= rail.bounding_box()['x']
        assert dock.bounding_box()['x'] + dock.bounding_box()['width'] <= rail.bounding_box()['x'] + rail.bounding_box()['width'] + 1
    else:
        assert dock.evaluate("el=>el.parentElement.matches('.conversation-block--context')")
    page.locator('#agent-dock-fixture').screenshot(path=str(tmp_path / f'turn-info-agents-{theme}-{language}-{width}.png'))
    all_messages = dock.locator('.agent-dock-messages')
    incoming = all_messages.locator('.sw-messages-row[data-message-id="to-root"]')
    assert not incoming.is_visible()
    assert not all_messages.locator('select').is_visible()
    all_messages.locator(':scope > summary').click()
    assert incoming.is_visible()
    assert 'Researcher' in incoming.locator('.sw-messages-direction').inner_text()
    assert locale['swarm.messagesParent'] in incoming.locator('.sw-messages-direction').inner_text()
    assert incoming.locator('.sw-messages-time').inner_text() == '1970-01-01T00:00:01.000Z'
    incoming.locator(':scope > details > summary').click()
    assert incoming.locator('.sw-messages-body').inner_text() == 'Coordinator received <script>safe</script>'
    assert incoming.locator('script').count() == 0
    incoming.locator(':scope > details > summary').focus()
    frame.evaluate("""() => {
      window.originalMessage=document.querySelector('.agent-dock-messages .sw-messages-row[data-message-id="to-root"]');
      const old=round._swarmSnapshot.messageReceipts[0];
      round._swarmSnapshot.messageReceipts=[{...old,state:'input_confirmed',inputConfirmedAt:1002},old];
      runtimeScope.renderBackgroundAgents();
    }""")
    assert incoming.locator(':scope > details').evaluate('el=>el.open')
    assert incoming.locator(':scope > details > summary').evaluate('el=>document.activeElement===el')
    assert frame.evaluate('originalMessage===document.querySelector(\'.agent-dock-messages .sw-messages-row[data-message-id="to-root"]\')')
    assert all_messages.locator('.sw-messages-row[data-message-id="to-root"]').count() == 1
    assert incoming.locator('.sw-messages-status').inner_text() == locale['tool.agentMessageInputConfirmed']
    assert dock.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    all_messages.locator(':scope > summary').click()
    summary = frame.locator('.agent-dock-active .agent-dock-agent > summary').first
    summary.focus()
    summary.press('Enter')
    detail = frame.locator('.agent-dock-detail').first
    detail.wait_for(state='visible')

    assert detail.locator('.sw-a-messages').count() == 0
    communication = frame.locator('.agent-dock-active .agent-dock-agent').first.locator('.agent-dock-agent-messages')
    assert not communication.locator('.sw-messages-row').first.is_visible()
    communication.locator(':scope > summary').click()
    assert not communication.locator('select').is_visible()
    message = communication.locator('.sw-messages-row[data-message-id="message"]')
    assert message.count() == 1
    message.locator(':scope > details > summary').click()
    assert message.locator('.sw-messages-body').inner_text() == 'Received <script>not executable</script>'
    assert not message.locator('.sw-messages-evidence').is_visible()
    evidence = ROOT / 'output/receipt-timeline-visual'
    evidence.mkdir(parents=True, exist_ok=True)
    message.screenshot(path=str(evidence / f'receipt-{theme}-{language}-{width}.png'))
    detail.locator('.sw-a-tool-history > summary').click()

    detail.screenshot(path=str(evidence / f'timeline-{theme}-{language}-{width}.png'))
    assert message.locator('script').count() == 0

    detail.locator('.sw-tl-omitted').wait_for(state='attached')
    assert detail.locator('.sw-tl-omitted').count() == 1
    assert detail.locator('.sw-tl-truncated').count() >= 1
    assert detail.locator('script').count() == 0
    # The detail opens directly on the canonical execution card, without a disclaimer.
    assert detail.evaluate("el => el.firstElementChild.matches('.sw-a-body')")
    frame.evaluate("window.originalAgent=document.querySelector('.agent-dock-agent'); surface.render(makeVm('Updated model'));runtimeScope.renderBackgroundAgents()")
    assert frame.evaluate("originalAgent===document.querySelector('.agent-dock-agent')")
    assert frame.locator('.agent-dock-agent').first.evaluate('el=>el.open')
    summary.press('Escape')
    assert not frame.locator('.agent-dock-agent').first.evaluate('el=>el.open')
    assert summary.evaluate('el=>document.activeElement===el')
    frame.locator('#bgActivityChip').click()
    popover = frame.locator('#bgActivityPopover')
    assert 'npm run build:frontend' in popover.inner_text()
    assert popover.locator('.bg-act-agent').count() == 0
    command = popover.locator('.bg-act-cmd-text')
    one_line = command.bounding_box()['height']
    assert command.evaluate('el=>el.scrollWidth>el.clientWidth'), 'collapsed row is one ellipsized line'
    popover.locator('.bg-act-cmd-head').click()
    assert popover.locator('.bg-act-cmd').evaluate('el=>el.open')
    assert command.bounding_box()['height'] > 2 * one_line, 'expanded row wraps the whole command'
    assert command.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
    assert popover.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
    assert popover.locator('.bg-act-elapsed').inner_text() == '1h 29m'
    page.locator('#agent-dock-fixture').screenshot(path=str(tmp_path / f'command-expanded-{theme}-{language}-{width}.png'))
    frame.locator('#bgActivityChip').click()
    frame.locator('.agent-dock-history > summary').click()
    frame.wait_for_function("document.querySelectorAll('.agent-dock-history .agent-dock-agent').length===1")
    assert dock.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    summary.click()
    frame.locator('.agent-dock-active .agent-dock-agent button').first.click()
    assert frame.evaluate('openedChild') == 'child'
    frame.evaluate("activeConvId='other';fixtureTurns=[];runtimeScope.updateBackgroundActivity()")
    assert not dock.is_visible()
    frame.evaluate('disposeDockFixture();surface.dispose()')
    assert dock.count() == 0


@pytest.mark.parametrize('width', [1280, 390])
def test_agent_dock_counts_executions_once_during_launch_reconciliation(frontend_visual_page, tmp_path, width):
    """Two launch copies plus live activity still render three workers, not seven."""
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""() => {
      const frame=document.createElement('iframe');frame.id='agent-dedup-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;z-index:99999';document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('<!doctype html><html><body><div id="host" style="max-width:360px;margin:12px"></div></body></html>')
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    for name in ['agent-sidebar-dock', 'agent-sidebar-dock-projection']:
        frame.add_script_tag(path=native_module_path(f'.native/dedup-{name}.js', ROOT / f'frontend/src/{name}.ts'))
    frame.evaluate("""() => {
      const first=[{id:'80024c34',role:'coder',status:'running',round:17},
        {id:'7d68e2ea',role:'reviewer',status:'running',round:15}];
      const later=[{id:'d2c1632d',role:'coder',status:'running',round:8}];
      const launch=(agents,call)=>({_swarm:true,_swarmActive:true,_swarmRunId:'run',_swarmKey:'fixture',
        taskId:'producer',attemptId:'attempt',toolCallId:call,toolName:'spawn_agents',
        _swarmSnapshot:{swarm_id:'run',agents},toolContent:JSON.stringify({swarm_run_id:'run',agents})});
      window.savedFirst=launch(first,'first-launch');window.savedLater=launch(later,'later-launch');
      window.provisional={_swarm:true,_swarmActive:true,_swarmRunId:'run',_swarmKey:'fixture',
        taskId:'producer',attemptId:'attempt',_swarmAgents:first.map(a=>({...a,round:a.round-1}))};
      window.rounds=[savedFirst,savedLater,provisional];
      window.live={swarm_id:'run',swarm_key:'fixture',conv_id:'fixture',created_at:1,
        agents:[...first,...later].map(a=>({...a,round:a.round+1}))};
      window.translate=(key,fallback,vars={})=>fallback.replace(/\\{(\\w+)\\}/g,(match,key)=>String(vars[key]??match));
      window.dock=createAgentSidebarDock({host:document.getElementById('host'),translate});
      window.repaint=()=>dock.update('fixture',agentDockProjection({
        turns:[{turnId:'turn',projection:{toolRounds:rounds}}],swarms:[live],
        recover:r=>r._swarmSnapshot?.agents||[],
        renderPanel:r=>'<div>Launch evidence: '+r._swarmAgents[0].id+'</div>',
        renderActivity:a=>Object.assign(document.createElement('div'),{textContent:a.id}),
        openConversation:()=>{},
        translate,
      }));
      repaint();
    }""")
    dock = frame.locator('.agent-dock')
    dock.wait_for(state='visible')
    assert dock.locator('.agent-dock-active .agent-dock-agent').count() == 3
    assert dock.locator('.agent-dock-counts').inner_text() == '3 running'
    assert dock.locator('.agent-dock-purpose').all_inner_texts() == ['80024c34', '7d68e2ea', 'd2c1632d']
    assert dock.locator('.agent-dock-rounds').all_inner_texts() == ['18 rounds', '16 rounds', '9 rounds']
    first = dock.locator('.agent-dock-active .agent-dock-agent').first
    first.locator(':scope > summary').click()
    first.locator(':scope > summary').focus()
    frame.evaluate("window.originalAgent=document.querySelector('.agent-dock-active .agent-dock-agent')")
    frame.evaluate("""() => {
      rounds=[{toolName:'read_files'},savedFirst,savedLater];
      live={...live,created_at:2,agents:live.agents.map(a=>({...a,round:a.round+1,objective:'Live brief '+a.id}))};
      repaint();
    }""")
    assert dock.locator('.agent-dock-active .agent-dock-agent').count() == 3
    assert first.evaluate('el=>el===originalAgent && el.open && document.activeElement===el.querySelector("summary")')
    # Live evidence still binds to the retained execution after its launch copies move.
    assert first.locator('.agent-dock-purpose').inner_text() == 'Live brief 80024c34'
    assert dock.locator('.agent-dock-rounds').all_inner_texts() == ['19 rounds', '17 rounds', '10 rounds']
    frame.evaluate("""() => {
      for(const launch of [savedFirst,savedLater]) launch._swarmSnapshot.agents=
        launch._swarmSnapshot.agents.map(a=>({...a,executionTaskId:'worker-'+a.id}));
      live={...live,agents:live.agents.map(a=>({...a,executionTaskId:'worker-'+a.id}))};
      repaint();
    }""")
    assert first.evaluate('el=>el===originalAgent && el.open && document.activeElement===el.querySelector("summary")')
    assert dock.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
    page.locator('#agent-dedup-fixture').screenshot(path=str(tmp_path / f'agent-dedup-{width}.png'))
    frame.evaluate("""() => {
      savedFirst._swarmSnapshot={...savedFirst._swarmSnapshot,settled:true,
        agents:savedFirst._swarmSnapshot.agents.map((a,i)=>({...a,status:i===0?'done':'unknown'}))};
      repaint();
    }""")
    # The completed row being read stays mounted until the reader closes it.
    assert first.evaluate('el=>el===originalAgent && el.open')
    assert '1 running' in dock.locator('.agent-dock-counts').inner_text()
    first.locator(':scope > summary').click()
    frame.wait_for_function("document.querySelectorAll('.agent-dock-active .agent-dock-agent').length===2")
    # Unknown is not running, but stays prominent until its outcome is known.
    assert dock.locator('.agent-dock-active .agent-dock-agent').evaluate_all(
        "rows => rows.map(row => row.dataset.status)"
    ) == ['unknown', 'running']
    dock.locator('.agent-dock-history > summary').click()
    frame.wait_for_function("document.querySelectorAll('.agent-dock-history .agent-dock-agent').length===1")
    assert dock.locator('.agent-dock-history .agent-dock-agent').get_attribute('data-status') == 'done'
    frame.evaluate('dock.dispose()')
    assert dock.count() == 0


def test_agent_dock_hydration_retains_focus_and_reconciles_conversation_button(frontend_visual_page):
    """Hydration retains the reader; only the current conversation control acts."""
    page = frontend_visual_page
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.evaluate("""() => {
      const frame=document.createElement('iframe');frame.id='agent-hydration-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;z-index:99999';document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('<!doctype html><html><body><div id="host" style="max-width:360px;margin:12px"></div></body></html>')
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    frame.evaluate(r"""locale => {
      window.t=(key,vars)=>(locale[key]||key).replace(/\{(\w+)\}/g,(match,name)=>vars?.[name]??match);
      window.escapeHtml=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
      window.runtimeScope={};window._TOOL_DISPLAY={};window.errorEnvelopeMessage=String;
      window.activeConvId='';
    }""", locale)
    for name in ['agent-sidebar-dock', 'agent-sidebar-dock-projection']:
        frame.add_script_tag(path=native_module_path(f'.native/hydration-{name}.js', ROOT / f'frontend/src/{name}.ts'))
    frame.add_script_tag(content=runtime_section('ui/streaming_swarm_panel.js'))
    frame.evaluate("""() => {
      window.translate=(key,fallback,vars)=>t(key,vars);
      window.opened=[];window.openChild=id=>opened.push({callback:'first',id});
      window.agent={id:'reader',role:'researcher',status:'running',objective:'Current assignment'};
      window.round={_swarm:true,_swarmRunId:'run',_swarmKey:'fixture',taskId:'producer',attemptId:'attempt',
        toolName:'spawn_agents',toolCallId:'launch',_swarmSnapshot:{swarm_id:'run',agents:[agent]}};
      window.dock=createAgentSidebarDock({host:document.getElementById('host'),translate});
      window.project=(round,swarms=[])=>agentDockProjection({
        turns:[{turnId:'turn',projection:{toolRounds:[round]}}],swarms,
        recover:r=>r._swarmSnapshot?.agents||[],renderPanel:_buildSwarmPanelHTML,
        renderActivity:a=>Object.assign(document.createElement('div'),{textContent:a.id}),
        openConversation:openChild,translate,
      });
      window.repaint=()=>{round._swarmSnapshot.agents=[agent];dock.update('fixture',project(round));};
      repaint();
    }""")
    dock = frame.locator('.agent-dock')
    row = dock.locator('.agent-dock-active .agent-dock-agent')
    summary = row.locator(':scope > summary')
    button = row.locator(':scope > .agent-dock-actions > button')
    assert row.count() == 1
    assert button.count() == 0
    summary.focus()
    summary.press('Enter')
    row.locator('.sw-a-body').wait_for(state='attached')
    frame.evaluate("""() => {
      window.originalRow=document.querySelector('.agent-dock-agent');
      window.originalSummary=originalRow.querySelector('summary');
      window.originalKey=originalRow.dataset.agentKey;
      agent={...agent,executionTaskId:'worker-one'};repaint();
    }""")
    assert row.evaluate('el=>el===originalRow && el.open && el.dataset.agentKey!==originalKey')
    assert summary.evaluate('el=>el===originalSummary && document.activeElement===el')
    assert button.count() == 0

    # The child conversation may arrive after the worker stamp, without a new row.
    frame.evaluate("agent={...agent,conversationId:'child-one'};repaint()")
    assert row.count() == 1
    assert row.evaluate('el=>el===originalRow && el.open')
    assert summary.evaluate('el=>el===originalSummary && document.activeElement===el')
    assert button.count() == 1
    assert button.inner_text() == locale['agentDock.openConversation']
    button.click()
    assert frame.evaluate('opened') == [{'callback': 'first', 'id': 'child-one'}]
    frame.evaluate("""() => {
      window.oldButton=originalRow.querySelector(':scope > .agent-dock-actions > button');
      agent={...agent,conversationId:'child-two'};
      openChild=id=>opened.push({callback:'latest',id});repaint();
    }""")
    assert button.evaluate('el=>el===oldButton && document.activeElement===el')
    assert row.evaluate('el=>el===originalRow && el.open')
    button.click()
    expected = [{'callback': 'first', 'id': 'child-one'}, {'callback': 'latest', 'id': 'child-two'}]
    assert frame.evaluate('opened') == expected

    # Removing a focused control returns focus to the retained native summary.
    frame.evaluate('delete agent.conversationId;repaint()')
    assert button.count() == 0
    assert row.evaluate('el=>el===originalRow && el.open')
    assert summary.evaluate('el=>el===originalSummary && document.activeElement===el')
    assert frame.evaluate('!oldButton.isConnected')
    frame.evaluate('oldButton.click()')
    assert frame.evaluate('opened') == expected

    frame.evaluate("""() => {
      agent={...agent,conversationId:'child-three'};
      openChild=id=>opened.push({callback:'reappeared',id});repaint();
    }""")
    assert row.count() == 1
    assert row.evaluate('el=>el===originalRow && el.open')
    assert summary.evaluate('el=>el===originalSummary && document.activeElement===el')
    assert button.count() == 1
    assert button.evaluate('el=>el!==oldButton')
    # A detached control must not borrow the callback of its replacement.
    frame.evaluate('oldButton.click()')
    assert frame.evaluate('opened') == expected
    button.click()
    expected.append({'callback': 'reappeared', 'id': 'child-three'})
    assert frame.evaluate('opened') == expected
    frame.evaluate('dock.dispose();oldButton.click()')
    assert frame.evaluate('opened') == expected
    assert dock.count() == 0

    # Separate fixture: a reused agent/run is not permission to merge workers.
    frame.evaluate("""() => {
      const old={id:'reader',executionTaskId:'worker-old',role:'researcher',status:'done',objective:'Old assignment',
        _toolCalls:[{callId:'old-tool',toolName:'read_files',status:'done',argsBrief:'old-only.txt',preview:'Old worker evidence'}]};
      const current={id:'reader',executionTaskId:'worker-new',role:'researcher',status:'running',objective:'New assignment'};
      const conflict={...round,_swarmAgents:[current],_swarmSnapshot:{swarm_id:'run',agents:[old]}};
      window.conflictBefore=JSON.stringify(conflict);window.conflictRound=conflict;
      window.conflictDock=createAgentSidebarDock({host:document.getElementById('host'),translate});
      conflictDock.update('fixture',project(conflict));
    }""")
    current = dock.locator('.agent-dock-active .agent-dock-agent')
    assert current.count() == 1
    assert current.get_attribute('data-status') == 'running'
    current.locator(':scope > summary').click()
    current.locator('.sw-a-body').wait_for(state='attached')

    current.locator('.sw-a-task > summary').click()
    assert 'New assignment' in current.locator('.agent-dock-detail').inner_text()
    assert 'Old assignment' not in current.locator('.agent-dock-detail').text_content()
    assert 'old-only.txt' not in current.locator('.agent-dock-detail').text_content()
    assert 'Old worker evidence' not in current.locator('.agent-dock-detail').text_content()
    dock.locator('.agent-dock-history > summary').click()
    old = dock.locator('.agent-dock-history .agent-dock-agent')
    old.wait_for(state='visible')
    assert old.get_attribute('data-status') == 'done'
    assert old.get_attribute('data-agent-key') != current.get_attribute('data-agent-key')
    old.locator(':scope > summary').click()
    old.locator('.sw-a-latest').wait_for(state='visible')
    assert 'old-only.txt' in old.locator('.agent-dock-detail').inner_text()
    assert 'Old worker evidence' in old.locator('.agent-dock-detail').inner_text()
    assert dock.locator('.agent-dock-agent').count() == 2
    assert frame.evaluate('JSON.stringify(conflictRound)===conflictBefore')
    frame.evaluate('conflictDock.dispose()')
    assert dock.count() == 0

    # A late producer stamp cannot leave a stale running copy after completion.
    frame.evaluate("""() => {
      const launch={...round,taskId:undefined,_swarmAgents:[],_swarmSnapshot:{swarm_id:'run',
        agents:[{id:'reader',status:'running',objective:'Late producer assignment'}]}};
      const completed={swarm_id:'run',attemptId:'attempt',executionTaskId:'producer',
        agents:[{id:'reader',executionTaskId:'worker',status:'done'}]};
      window.lateDock=createAgentSidebarDock({host:document.getElementById('host'),translate});
      lateDock.update('fixture',project(launch));
      lateDock.update('fixture',project(launch,[completed]));
    }""")
    assert dock.locator('.agent-dock-active .agent-dock-agent').count() == 0
    dock.locator('.agent-dock-history > summary').click()
    history = dock.locator('.agent-dock-history .agent-dock-agent')
    history.wait_for(state='visible')
    assert history.count() == 1
    assert history.get_attribute('data-status') == 'done'
    assert dock.locator('.agent-dock-agent').count() == 1
    frame.evaluate('lateDock.dispose()')
    assert dock.count() == 0


@pytest.mark.parametrize('width', [1280, 390])
@pytest.mark.parametrize('language', ['en', 'zh'])
def test_agent_dock_stops_a_running_agent_from_its_opened_row(frontend_visual_page, tmp_path, width, language):
    """Stop sits beside the conversation control at rail width; the snapshot, not the
    request, settles the row."""
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate("""() => {
      const frame=document.createElement('iframe');frame.id='agent-stop-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;z-index:99999';document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('<!doctype html><html><body><div id="host" style="max-width:280px;margin:12px"></div></body></html>')
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    frame.evaluate(r"""locale => {
      window.t=(key,vars)=>(locale[key]||key).replace(/\{(\w+)\}/g,(match,name)=>vars?.[name]??match);
    }""", locale)
    for name in ['agent-sidebar-dock', 'agent-sidebar-dock-projection']:
        frame.add_script_tag(path=native_module_path(f'.native/stop-{name}.js', ROOT / f'frontend/src/{name}.ts'))
    frame.evaluate("""() => {
      window.translate=(key,fallback,vars)=>t(key,vars);
      window.stops=[];window.accept=null;window.liveStatus='running';
      const round={_swarm:true,_swarmRunId:'run',_swarmKey:'fixture',taskId:'producer',attemptId:'attempt',
        toolName:'spawn_agents',toolCallId:'launch',_swarmAgents:[{id:'reader',role:'researcher',status:'running',
          objective:'Fix the embedded-data-islands page projection',conversationId:'child'}]};
      window.dock=createAgentSidebarDock({host:document.getElementById('host'),translate});
      window.repaint=()=>dock.update('fixture',agentDockProjection({
        turns:[{turnId:'turn',projection:{toolRounds:[round]}}],
        swarms:[{swarm_id:'run',swarm_key:'fixture',conv_id:'fixture',agents:[{id:'reader',status:liveStatus}]}],
        recover:()=>[],renderPanel:()=>'<div></div>',
        renderActivity:a=>Object.assign(document.createElement('div'),{textContent:a.id}),
        openConversation:()=>{},
        stopAgent:(run,agent)=>new Promise(resolve=>{stops.push([run,agent]);accept=resolve;}),
        translate,
      }));
      repaint();
    }""")
    dock = frame.locator('.agent-dock')
    row = dock.locator('.agent-dock-active .agent-dock-agent')
    row.locator(':scope > summary').click()
    actions = row.locator(':scope > .agent-dock-actions > button')
    assert actions.all_inner_texts() == [locale['agentDock.openConversation'], locale['agentDock.stop']]
    stop = actions.nth(1)
    assert dock.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
    box, bounds = stop.bounding_box(), dock.bounding_box()
    assert box['x'] + box['width'] <= bounds['x'] + bounds['width'] + 1
    page.locator('#agent-stop-fixture').screenshot(path=str(tmp_path / f'agent-stop-{language}-{width}.png'))
    stop.click()
    assert frame.evaluate('stops') == [['run', 'reader']]
    assert stop.is_disabled() and stop.inner_text() == locale['agentDock.stopping']
    frame.evaluate("accept(true);liveStatus='cancelled';repaint()")
    assert actions.all_inner_texts() == [locale['agentDock.openConversation']]
    assert row.evaluate('el=>el.open')
    frame.evaluate('dock.dispose()')
    assert dock.count() == 0


def test_agent_dock_follows_the_latest_round_input_rail(frontend_visual_page):
    """A swarm settlement continues the round as a virtual user. Its rail, not the
    human rail above it in history, hosts the agents and stays hit-testable where it
    overhangs the later reply."""
    page = frontend_visual_page
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.evaluate("""() => {
      const frame=document.createElement('iframe');frame.id='agent-dock-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;z-index:99999';document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('''<!doctype html><html><body><main class="main" style="height:100vh;width:100%;display:block">
      <div id="chatContainer" class="chat-container" style="height:100vh;width:100%"><div id="chatInner" class="chat-inner"></div></div>
      <div id="convStatusStrip" style="position:fixed;bottom:16px;left:16px"><div id="turnNav"></div></div>
      </main></body></html>''')
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    frame.evaluate("""() => {
      window.t=key=>key;
      window.escapeHtml=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
      window.runtimeScope={};window._TOOL_DISPLAY={};window.errorEnvelopeMessage=String;
      window.activeConvId='fixture';window.getConvById=id=>({id,title:'Round input fixture'});window.loadConversation=()=>{};
      const dispose=[];window.retainedCompositionLifecycle={add:fn=>dispose.push(fn)};
      window.disposeDockFixture=()=>dispose.reverse().forEach(fn=>fn());
      window.Api={activity:{background:()=>Promise.resolve({swarms:[],backgroundCommands:[]})}};
    }""")
    for name in ['background-activity-presentation', 'agent-sidebar-dock', 'agent-sidebar-dock-projection',
                 'conversation/ui/conversation-surface', 'conversation/presentation/swarm-failure-presentation',
                 'conversation/presentation/agent-message-receipts', 'conversation/presentation/swarm-messages-projection']:
        frame.add_script_tag(path=native_module_path(f'.native/dock-{name.replace("/", "-")}.js', ROOT / f'frontend/src/{name}.ts'))
    frame.add_script_tag(content=runtime_section('ui/streaming_swarm_panel.js'))
    frame.evaluate("""() => {
      const turn=(id,ordinal,actor,blocks)=>({turnId:id,laneId:'main',parentTurnId:null,ordinal,actor,
        role:actor==='assistant'?'assistant':'user',kind:actor==='assistant'?'reply':'input',status:'completed',
        attemptId:null,projectionRevision:1,subtreeRevisionKey:id,commandPending:null,finish:null,branches:[],actions:[],
        metadata:{translation:{pending:false}},source:{},blocks});
      const context=model=>[{kind:'context',blockId:'context',identitySource:'contract',source:{model},value:{snapshot:{model}}}];
      const answer=[{kind:'text',blockId:'answer',identitySource:'contract',source:{},markdown:'Spawning the next batch. ',deliverable:true,terminal:true,resumable:false}];
      window.turns=[turn('input',1,'human',context('Human round')),turn('reply',2,'assistant',answer),
        turn('settled',3,'virtual_user',context('Autopilot round')),turn('settled-reply',4,'assistant',answer)];
      window.surface=createConversationSurface(document.getElementById('chatInner'),{
        renderTurnContextRail(node,block){node.innerHTML='<div class="tctx-head"><span class="tctx-model">'+escapeHtml(block?.value.snapshot.model||'')+'</span></div>';node.hidden=!block;},
        renderBlock(node,block){node.className='conversation-block conversation-block--'+block.kind;node.innerHTML=block.kind==='context'?'':'<p>'+block.markdown.repeat(60)+'</p>';}
      });
      surface.render({conversationId:'fixture',conversationRevision:1,transport:'live',orphanLanes:[],queue:[],
        mainLane:{laneId:'main',parentTurnId:null,title:'Conversation',kind:'main',turns}});
      const round={_swarm:true,_swarmActive:true,_swarmRunId:'run',_swarmKey:'session',roundNum:1,status:'running',
        _swarmAgents:[{id:'team',role:'coder',status:'running'},{id:'paper',role:'coder',status:'running'},{id:'poster',role:'coder',status:'running'}]};
      runtimeScope.ConversationSwarmPresentation={candidates:()=>[{turnId:'settled-reply',projection:{toolRounds:[round]}}]};
      runtimeScope.ConversationTurnRead={latest:(id,predicate,lane)=>(
        id==='fixture'&&lane==='main'?[...turns].reverse().find(predicate):undefined)};
    }""")
    frame.add_script_tag(content=runtime_section('background-activity.js', scope_prelude=False))
    frame.evaluate('runtimeScope.updateBackgroundActivity()')
    dock = frame.locator('.agent-dock')
    dock.wait_for(state='visible')
    assert dock.evaluate("el=>el.parentElement.matches('.turn-ctx > .tctx-card')")
    assert dock.evaluate("el=>el.closest('.conversation-turn').dataset.turnId") == 'settled'
    assert frame.locator('.agent-dock-active .agent-dock-agent').count() == 3
    row = frame.locator('.agent-dock-active .agent-dock-agent > summary').last
    row.scroll_into_view_if_needed()
    host_bottom = frame.evaluate("""document.querySelector('.conversation-turn[data-turn-id="settled"]')
      .getBoundingClientRect().bottom""")
    assert row.bounding_box()['y'] > host_bottom, 'fixture must place the row over the later reply'
    hit = row.evaluate("""el=>{const r=el.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
      const top=document.elementFromPoint(x,y);
      return {ok:el.contains(top),x,y,viewport:innerHeight,top:top&&(top.className||top.tagName),
        turn:top?.closest?.('[data-turn-id]')?.dataset.turnId??null};}""")
    assert hit['ok'], hit
    row.click()
    assert frame.locator('.agent-dock-active .agent-dock-agent').last.evaluate('el=>el.open')
    frame.evaluate('disposeDockFixture();surface.dispose()')
    assert dock.count() == 0
