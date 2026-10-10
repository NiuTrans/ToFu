"""Goal evidence in Chromium using the real app, Surface and Inspector owners.

Synthetic projections live in a disposable iframe, never in user state. The
Sidecar contract suite separately proves the same live/settled/replay DTOs.
"""
import json

import pytest

from lib.agent_core.output_settlement import seal_output_projection
from lib.turn_projection_segments import projection_with_stable_segments
from tests._runtime_sections import ROOT, native_module_path, runtime_section

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1100, 390])
@pytest.mark.parametrize('late_ref', [False, True], ids=['initial-ref', 'late-ref'])
def test_goal_history_and_inspector_survive_settlement_reload(
        page, assert_no_js_errors, tmp_path, width, late_ref):
    page.set_viewport_size({'width': width, 'height': 900})
    ref = {'taskId': 'goal-fixture#agent:verifier', 'roundNum': 1, 'turn': 'swarm-agent:original'}
    live = projection_with_stable_segments({
        'content': 'Checking the actual files', 'thinking': 'Verify carefully',

        'segments': [
            {'type': 'thinking', 'blockId': 'goal:thinking:1', 'text': 'Verify carefully', 'terminal': True},
            {'type': 'text', 'blockId': 'goal:text:1', 'text': 'Checking the actual files', 'terminal': True},
        ],
        'toolRounds': [{'toolName': 'run_command', 'toolCallId': 'verify-call',
                        'roundNum': 1, 'llmRound': 1, 'inspectorRef': ref,
                        'toolArgs': {'command': 'pytest -q'},
                        'toolContent': '2 passed\nExact full result', 'status': 'done'}],
    }, status='running')
    settled = projection_with_stable_segments(seal_output_projection(
        live, {'content': 'Fix the remaining test', 'thinking': '',
               'toolRounds': [{'toolCallId': 'verify-call', 'toolContent': 'short preview'}]}))
    if late_ref:
        # The producing request may become addressable after its live tool card.
        # Keep the settled DTO intact; only the earlier snapshot lacks the ref.
        live = json.loads(json.dumps(live))
        live['toolRounds'][0].pop('inspectorRef')
    page.evaluate("""() => {
      const frame = document.createElement('iframe');
      frame.id = 'goal-evidence-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;z-index:99999;background:white';
      document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('<!doctype html><html data-theme="tofu"><body><main id="chat" style="padding:12px"></main></body></html>')
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    # Translation content has its own owner tests. This isolated surface uses
    # deterministic labels; it must not inspect shipped locale source text.
    frame.evaluate(r"""() => {
      window.t = (key, params) => ({'ri.loadFailed':'Failed to load','ri.retry':'Retry'}[key]||key)
        .replace(/\{(\w+)\}/g, (m,n)=>params?.[n]??m);
      window.escapeHtml = s => String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;')
        .replaceAll('>','&gt;').replaceAll('"','&quot;');
      window.Icon = () => ''; window.runtimeScope = {};
      window.conversations=[]; window.activeConvId='fixture'; window.config={}; window.debugVisible=false;
      window.retainedCompositionLifecycle={add(){}};
      window._featureFlags={debug_mode:true}; window._TOOL_DISPLAY={};
      window.errorEnvelopeMessage=String;
      window.payloadCalls=[]; window.requestOnly=false; window.roundReadFails=false;
      window.goalScriptExecuted=false;
      window.surfaceErrors=[];
      window.Api={clientError:{report(error){surfaceErrors.push(error);}},tasks:{getRequestPayload:async(task,round,turn,kind)=>{
        payloadCalls.push({task,round,turn,kind:kind||'request'});
        if(roundReadFails){const error=new Error('storage unavailable');error.status=503;throw error;}
        if(turn!=='swarm-agent:original') throw Error('wrong invocation');
        if(kind==='state' && requestOnly) return null;
        return {messages:kind==='state' ? [
          {role:'user',content:'Original objective'},
          {role:'assistant',content:'Checking files',tool_calls:[{id:'verify-call',type:'function',
            function:{name:'run_command',arguments:'{"command":"pytest -q"}'}}]},
          {role:'tool',tool_call_id:'verify-call',content:'2 passed\nExact full result <script>window.goalScriptExecuted=true</script>'}
        ]:[{role:'user',content:'Exact producing request'}],label:'Round 1',model:'fixture'};
      }}};
    }""")
    frame.add_script_tag(path=native_module_path(
        '.native/goal-evidence-surface.js', ROOT / 'frontend/src/conversation/index.ts'))
    frame.add_script_tag(content=runtime_section('core/debug_panel.js'))
    frame.add_script_tag(content=runtime_section('core/request_inspector.js'))
    frame.add_script_tag(content=runtime_section('ui/tool_rounds.js'))
    frame.evaluate("""data => {
      const turn={turnId:'goal-turn',conversationId:'fixture',laneId:'main',actor:'virtual_user',
        kind:'autopilot_virtual_user',status:'running',ordinal:1,currentAttemptId:'goal-attempt',
        createdAt:1,updatedAt:1,projectionRevision:1,projection:data.live};
      let state=reduceTurnState(createTurnState('fixture'), {type:'snapshot', snapshot:{
        conversationRevision:1, authoritativeFull:true, turns:[turn], attempts:[], queueItems:[]}});
      const renderers=createClassicConversationRenderers({renderSafeMarkdownHtml:escapeHtml,
        renderToolBlockHtml:block=>'<div data-prn="1">'+_renderDebugEntry(block.round)+
          '<pre>'+escapeHtml(block.round.toolContent)+'</pre></div>'});
      let surface=createConversationSurface(document.getElementById('chat'),renderers);
      const paint=()=>surface.render(selectConversationViewModel(state));
      window.goalPaint=(cold=false)=>{
        turn.status='completed'; turn.projection=data.settled; turn.projectionRevision=2;

        state=reduceTurnState(cold ? createTurnState('fixture') : state, {type:'snapshot', snapshot:{
          conversationRevision:2, authoritativeFull:true, turns:[JSON.parse(JSON.stringify(turn))],
          attempts:[], queueItems:[]}});
        if(cold){surface.dispose();document.getElementById('chat').replaceChildren();
          surface=createConversationSurface(document.getElementById('chat'),renderers);}
        paint();
      };
      window.goalDispose=()=>surface.dispose();
      document.getElementById('chat').addEventListener('click',e=>{
        const anchor=e.target.closest('.ri-tool-anchor'); if(anchor)
          openToolDebugPanel(data.ref.taskId,1,anchor);
      });
      paint(); window.firstThinking=document.querySelector('.thinking-block');
      window.evidenceBlocks=()=>Array.from(document.querySelectorAll('[data-block-id]'))
        .filter(node=>['thinking','text','tool'].includes(node.dataset.blockKind));
      window.firstBlocks=evidenceBlocks();
    }""", {'live': live, 'settled': settled, 'ref': ref})
    assert 'Checking the actual files' in frame.locator('#chat').inner_text()

    live_blocks = frame.evaluate('evidenceBlocks().map(n=>({id:n.dataset.blockId,kind:n.dataset.blockKind}))')
    assert [block['kind'] for block in live_blocks] == ['thinking', 'text', 'tool']
    if late_ref:
        assert frame.locator('.ri-tool-anchor').count() == 0
    frame.evaluate('goalPaint()')
    assert frame.evaluate('firstThinking === document.querySelector(".thinking-block")')

    assert frame.evaluate('firstBlocks.every((node,i)=>node===evidenceBlocks()[i])')
    settled_blocks = frame.evaluate('evidenceBlocks().map(n=>({id:n.dataset.blockId,kind:n.dataset.blockKind}))')
    assert settled_blocks[:len(live_blocks)] == live_blocks
    assert [block['kind'] for block in settled_blocks] == ['thinking', 'text', 'tool', 'text']
    assert len({block['id'] for block in settled_blocks}) == len(settled_blocks)
    thinking = frame.locator('.thinking-block')
    thinking.locator('summary').click()
    assert thinking.locator('.thinking-text').inner_text() == 'Verify carefully'
    for text in ('Checking the actual files', 'Fix the remaining test', 'Exact full result'):
        assert text in frame.locator('#chat').inner_text()
    anchor = frame.locator('.ri-tool-anchor')
    assert anchor.get_attribute('data-ri-turn') == ref['turn']
    anchor.click()
    panel = frame.locator('.ri-state-panel')
    panel.wait_for()
    assert panel.get_attribute('data-ri-kind') == 'state'
    assert 'Exact full result' in panel.inner_text()
    assert panel.locator('script').count() == 0

    assert not frame.evaluate('goalScriptExecuted')
    assert '<script>window.goalScriptExecuted=true</script>' in panel.inner_text()
    assert frame.evaluate('payloadCalls') == [dict(task=ref['taskId'], round=1, turn=ref['turn'], kind='state')]
    # Navigation/cold hydration reconstructs from the same durable blocks.
    frame.evaluate('goalPaint(true)')

    assert frame.evaluate('evidenceBlocks().map(n=>({id:n.dataset.blockId,kind:n.dataset.blockKind}))') == settled_blocks
    assert frame.evaluate('firstBlocks.every(node=>!node.isConnected)')
    frame.locator('.thinking-block > summary').click()
    assert 'Verify carefully' in frame.locator('#chat').inner_text()
    assert frame.evaluate('surfaceErrors') == []
    frame.evaluate("requestOnly=true; Object.keys(_riPayloadCache).forEach(k=>delete _riPayloadCache[k])")
    frame.locator('.ri-tool-anchor').click()
    panel = frame.locator('.ri-state-panel')
    frame.wait_for_function("document.querySelector('.ri-state-panel')?.dataset.riKind==='request'")
    assert panel.get_attribute('data-ri-kind') == 'request'
    assert 'Exact producing request' in panel.inner_text()
    assert frame.evaluate('payloadCalls.every(call=>call.turn==="swarm-agent:original")')

    assert frame.evaluate('payloadCalls') == [dict(task=ref['taskId'], round=1, turn=ref['turn'], kind=kind)
                                              for kind in ('state', 'state', 'request')]
    assert not frame.evaluate('goalScriptExecuted')
    assert frame.locator('#chat').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    reports = frame.evaluate('surfaceErrors')
    assert len(reports) == 1
    assert '[debugLog][warn] [ri] payload' in reports[0]['message']
    assert ':state:swarm-agent:original:1' in reports[0]['message']
    assert 'null (404 or error)' in reports[0]['message']
    screenshot = tmp_path / f'goal-evidence-{width}-{"late" if late_ref else "initial"}-ref.png'
    page.locator('#goal-evidence-fixture').screenshot(path=str(screenshot))
    print(f'Goal evidence screenshot: {screenshot}')
    if width == 390 and not late_ref:
        anchor = frame.locator('.ri-tool-anchor')
        anchor.click()  # close the request fallback
        frame.evaluate("roundReadFails=true; Object.keys(_riPayloadCache).forEach(k=>delete _riPayloadCache[k])")
        anchor.click()
        failed = frame.locator('.ri-state-panel')
        failed.locator('.ri-state-body .ri-retry').wait_for()
        assert 'No state mirror' not in failed.inner_text()
        assert 'Failed to load' in failed.inner_text()
        frame.evaluate('roundReadFails=false')
        failed.locator('.ri-state-body .ri-retry').click()
        frame.wait_for_function("document.querySelector('.ri-state-panel')?.dataset.riKind==='request'")
        assert 'Exact producing request' in failed.inner_text()
    frame.evaluate('goalDispose(); window.dispatchEvent(new Event("pagehide"))')
