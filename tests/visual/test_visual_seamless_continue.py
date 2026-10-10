"""Real Chromium reads continuous rounds and ack-only tail deletion."""
import json
import pytest
from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin as _origin, frontend_visual_page as _page
frontend_visual_origin = _origin
frontend_visual_page = _page
pytestmark = pytest.mark.visual

@pytest.mark.parametrize('width', [390, 1180])
def test_seamless_rounds_and_authoritative_tail(frontend_visual_page, tmp_path, width):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    entry = tmp_path / 'continue.ts'
    entry.write_text(''.join('export * from ' + json.dumps(str(ROOT / path)) + ';' for path in [
        'frontend/src/conversation/presentation/tool-execution-groups.ts',
        'frontend/src/core/turn-state.ts',
    ]))
    page.add_script_tag(path=native_module_path('.native/visual-seamless-continue.js', entry))
    page.evaluate('''()=>{
      const rows=[{llmRound:0,roundNum:1,attemptId:'old',toolCallId:'saved',toolContent:'Saved result'},
        {llmRound:0,roundNum:1,attemptId:'new',toolCallId:'new',toolContent:'New result'}];
      const groups=computeToolBatches(rows);
      document.body.innerHTML='<main id="rounds"></main>';
      for(const group of groups){const p=document.createElement('p');
        p.textContent='Round '+toolGroupRoundDisplay(group)+': '+group.rounds[0].toolContent;
        p.dataset.scope=group.scope;document.querySelector('main').append(p);}
      const turn=(id,n)=>({turnId:id,ordinal:n,laneId:'main',status:'failed',projectionRevision:1,
        projection:{content:'',toolRounds:rows}});
      window.state=reduceTurnState(createTurnState('test'),{type:'snapshot',snapshot:{
        conversationRevision:10,turns:[turn('target',1),turn('later',2)]}});
      window.laterTurn=turn('later',2);
      window.state=reduceTurnState(state,{type:'command_pending',turnId:'target',operation:'continue'});
    }''')
    assert page.locator('#rounds p').all_text_contents() == ['Round 1: Saved result', 'Round 2: New result']
    assert page.evaluate("state.laneOrder.main.join(',')") == 'target,later'
    assert 'A2' not in page.locator('body').inner_text()
    assert page.locator('#rounds p').nth(0).get_attribute('data-scope') == 'old'
    assert page.locator('#rounds p').nth(1).get_attribute('data-scope') == 'new'
    page.evaluate('''()=>{
      state=reduceTurnState(state,{type:'command_response',response:{conversationRevision:11,deletedTurnIds:['later']}});
      state=reduceTurnState(state,{type:'command_response',response:{conversationRevision:10,turn:laterTurn}});
    }''')
    assert page.evaluate("state.laneOrder.main.join(',')") == 'target'
    assert page.evaluate("state.turnsById.target.projection.toolRounds.length") == 2
    page.screenshot(path=str(tmp_path / f'seamless-continue-{width}.png'))
