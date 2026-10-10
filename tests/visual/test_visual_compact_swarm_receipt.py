"""Actual local Chromium rendering of compact launch receipt reloads."""
import json
import pytest
from tests._runtime_sections import ROOT, native_module_path, runtime_section
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1100, 390])
def test_compact_launch_recovers_full_objective_without_promoting_completion(frontend_visual_page, width):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    page.evaluate(r'''locale => {
      window.t=(key,params)=> (locale[key]||key).replace(/\{(\w+)\}/g,(_,k)=>params?.[k]??'');
      window.escapeHtml=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
      window.runtimeScope={}; window._TOOL_DISPLAY={}; window.errorEnvelopeMessage=String;
      window.fixtureInterval = window.setInterval; window.setInterval=()=>0;
      document.body.innerHTML='<main id="fixture" style="padding:12px"></main>';
    }''', locale)
    page.add_script_tag(path=native_module_path(
        '.native/compact-spawn-failure.js', ROOT / 'frontend/src/conversation/presentation/swarm-failure-presentation.ts'))
    page.add_script_tag(content=runtime_section('ui/streaming_swarm_panel.js'))
    objective = 'OBJECTIVE HEAD\n' + 'Detailed task description 世界.\n' * 120 + 'OBJECTIVE TAIL'
    receipt = {'status': 'async_launched', 'swarm_key': 'fixture',
               'agents': [{'id': 'audit', 'role': 'coder', 'objective': objective,
                           'required_for_completion': True}]}
    compact = {**receipt, 'agents': [{k: v for k, v in receipt['agents'][0].items() if k != 'objective'}]}
    recovered = page.evaluate('''({receipt,compact})=>{
      const round={toolName:'spawn_agents',roundNum:1,_swarm:true,status:'done',
        toolContent:JSON.stringify(compact),results:[{swarmLaunchReceipt:receipt}]};
      const reloaded=JSON.parse(JSON.stringify(round));
      const agents=_recoverSwarmAgents(reloaded,[reloaded]);
      document.querySelector('#fixture').innerHTML=_buildSwarmPanelHTML(reloaded,[reloaded]);

      document.querySelector('#fixture').addEventListener('click',event=>{
        const agent=event.target.closest('.sw-a-header');
        if(agent){_swarmToggleClass(agent,'.sw-agent','sw-a-open');return;}
        const panel=event.target.closest('.sw-header');
        if(panel)_swarmToggleClass(panel,'.sw-panel','sw-collapsed');
      });
      return agents;
    }''', {'receipt': receipt, 'compact': compact})
    assert recovered[0]['objective'] == objective
    assert recovered[0]['status'] == 'unknown'  # successful launch is not completion
    if not page.locator('.sw-a-header').is_visible():
        page.locator('.sw-header').click()
    if not page.locator('.sw-a-task > summary').is_visible():
        page.locator('.sw-a-header').click()
    page.locator('.sw-a-task > summary').click()
    assert page.locator('.sw-a-objective').inner_text() == objective
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    legacy = page.evaluate('''receipt=>_recoverSwarmAgents({toolContent:JSON.stringify(receipt)},[])''', receipt)
    assert legacy[0]['objective'] == objective
    # A newer settled snapshot remains authoritative over the launch metadata.
    authoritative = page.evaluate('''receipt=>_recoverSwarmAgents({results:[{swarmLaunchReceipt:receipt}],
      _swarmSnapshot:{settled:true,agents:[{id:'audit',objective:'new authoritative task',status:'failed',error:'failure'}]}},[])''', receipt)
    assert authoritative[0]['status'] == 'failed'
    assert authoritative[0]['objective'] == 'new authoritative task'
