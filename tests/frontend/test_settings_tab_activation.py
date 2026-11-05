"""Independent Settings panels load on first visit and keep their editor intent."""
import json
import subprocess

import pytest

from tests.frontend.test_settings_save_feedback import ROOT, settings_bundle as settings_bundle

pytestmark = pytest.mark.unit


HARNESS = r'''
const assert=require('node:assert/strict');
const {JSDOM}=require(require('node:path').join(ROOT,'node_modules/jsdom'));
const tabs=['general','mcp','skills','tools','preferences','database'];
const dom=new JSDOM('<!doctype html><body><div id="settingsModal">'
 +'<button id="settingsSaveBtn"></button><span id="settingsStatusHint"></span>'
 +tabs.map(tab=>`<button class="settings-tab" data-tab="${tab}"></button><div class="settings-tab-panel" id="settingsTab_${tab}"></div>`).join('')
 +'</div></body>',{url:'http://localhost'});
for(const name of ['window','document','localStorage','HTMLElement','HTMLInputElement','HTMLSelectElement',
 'HTMLButtonElement','HTMLTextAreaElement','HTMLAnchorElement','AbortController','AbortSignal'])
 global[name]=name==='window'?dom.window:dom.window[name];
const {bridge}=require(BUILT),calls=[],destroyed=[];
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};
const config=deferred(),tick=async()=>{for(let i=0;i<4;i++)await new Promise(done=>setImmediate(done));};
bridge.config={};bridge._registeredModels=[];bridge._featureFlags={};
for(const [tab,entry] of Object.entries({mcp:'_populateMcpTab',skills:'_populateSkillsTab',tools:'populateToolsInventory',
 preferences:'_populatePreferencesTab',database:'_populateDatabaseTab'})) bridge[entry]=()=>calls.push(tab);
bridge._destroyDatabaseTab=()=>destroyed.push('database');
bridge.Api={serverConfig:{get:()=>config.promise},modelRouting:{get:async()=>({revision:1,
 model_routing:{contract_version:'tofu.model-routing/v2',revision:1}})}};
async function main(){
 bridge.openSettings();await tick();
 if(SCENARIO==='visit_during_config_read'){
  bridge.switchSettingsTab('mcp');assert.deepEqual(calls,['mcp'],'an independent catalog is usable while config is loading');
  config.resolve({});await tick();assert.deepEqual(calls,['mcp'],'config acknowledgement must not reload a visited panel');
 }else{
  config.resolve({});await tick();
  if(SCENARIO==='opening_is_lazy') assert.deepEqual(calls,[],'opening General must not fetch hidden independent panels');
  else if(SCENARIO==='visit_once'){
   calls.length=0;
   for(const tab of tabs.slice(1)){bridge.switchSettingsTab(tab);bridge.switchSettingsTab('general');bridge.switchSettingsTab(tab);}
   assert.deepEqual(calls,[...tabs.slice(1),'database'],'editor revisits retain drafts; Database refreshes its live status on activation');
  }else if(SCENARIO==='close_database'){
   bridge.switchSettingsTab('database');bridge.closeSettings();assert.deepEqual(destroyed,['database']);
  }else if(SCENARIO==='opening_owns_visits'){
   calls.length=0;bridge.switchSettingsTab('skills');bridge.closeSettings();bridge.openSettings();await tick();
   assert.deepEqual(calls,['skills']);bridge.switchSettingsTab('skills');assert.deepEqual(calls,['skills','skills']);
  }
 }
 bridge.closeSettings();dom.window.close();
}
main().catch(error=>{console.error(error);process.exitCode=1;dom.window.close();});
'''


@pytest.mark.parametrize('scenario', ['opening_is_lazy', 'visit_during_config_read', 'visit_once',
                                      'close_database', 'opening_owns_visits'])
def test_settings_independent_tab_activation(settings_bundle, scenario):
    constants = {'ROOT': str(ROOT), 'BUILT': str(settings_bundle), 'SCENARIO': scenario}
    script = '\n'.join(f'const {key}={json.dumps(value)};' for key, value in constants.items()) + HARNESS
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
