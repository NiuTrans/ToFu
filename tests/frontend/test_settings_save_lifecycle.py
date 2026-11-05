"""Settings saves own captured form values and feedback in their original view."""
import json
import subprocess

import pytest

from tests.frontend.test_settings_save_feedback import ROOT, settings_bundle as settings_bundle

pytestmark = pytest.mark.unit

HARNESS = r'''
const assert=require('node:assert/strict');
const {JSDOM}=require(require('node:path').join(ROOT,'node_modules/jsdom'));
const dom=new JSDOM(`<!doctype html><body><div id="settingsModal">
 <button id="settingsSaveBtn"></button><span id="settingsStatusHint"></span>
 <input type="checkbox" id="settingTradingEnabled"><input type="checkbox" id="settingPptxTranslateEnabled">
 <div id="settingsTab_search"><div id="settingSkipDomains" class="chip-input"><input class="chip-add" id="skipDomainsAdd"></div></div>
 <div id="searchGatewayCard"><select id="settingSearchGatewayId"><option value="fixture">fixture</option></select>
 <input id="settingSearchGatewayKey"></div>
</div></body>`,{url:'http://localhost'});
for(const name of ['window','document','localStorage','HTMLElement','HTMLInputElement','HTMLSelectElement',
 'HTMLButtonElement','HTMLTextAreaElement','HTMLAnchorElement','AbortController','AbortSignal'])
 global[name]=name==='window'?dom.window:dom.window[name];
const {bridge,t}=require(BUILT);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {resolve,reject,promise};};
const clone=value=>JSON.parse(JSON.stringify(value));
const $=id=>document.getElementById(id), gate=deferred(), writes=[], features=[], timers=[];
global.setTimeout=callback=>{timers.push(callback);return timers.length;};
const cfg={presets:{mode:'original'},hidden_models:['hidden']};
let feishu={label:'original'};
bridge.config={};bridge._registeredModels=[];bridge._featureFlags={};
bridge._configForPersist=()=>({});bridge._persistModelRoutingNow=async()=>{};
bridge._collectFeishuConfig=()=>feishu;
let blocked=[];bridge.ChipInput={getValues:()=>clone(blocked)};
bridge._populateSearchTab=()=>{blocked=['original.example'];};
bridge.Api={
 serverConfig:{get:async()=>clone(cfg),update:async value=>{
   writes.push(clone(value));
   if(SCENARIO!=='captured_values' && SCENARIO!=='reopen_during_feature')await gate.promise;
   if(SCENARIO==='server_no_receipt' && writes.length===1)return null;
   if(SCENARIO==='server_denied' && writes.length===1)return {json:async()=>({ok:false,error:'synthetic server refusal'})};
   return {json:async()=>({ok:true})};}},
 features:{set:async value=>{features.push(value);if(features.length===1)await gate.promise;return {json:async()=>({ok:true})};}},
 modelRouting:{get:async()=>({revision:1,model_routing:{contract_version:'tofu.model-routing/v2',revision:1}})}
};
async function open(){bridge.openSettings();await tick();await tick();}
async function main(){
 await open();
 $('settingSearchGatewayKey').value='synthetic-first';
 if(SCENARIO==='captured_values' || SCENARIO==='reopen_during_feature'){
   $('settingTradingEnabled').checked=true;$('settingPptxTranslateEnabled').checked=true;
 }
 const saving=bridge.saveSettings();await tick();
 if(SCENARIO==='captured_values' || SCENARIO==='reopen_during_feature'){
   if(SCENARIO==='reopen_during_feature'){bridge.closeSettings();await open();}
   $('settingPptxTranslateEnabled').checked=false;blocked=['newer.example'];
   bridge._stgPresets.mode='newer';bridge._serverConfig.hidden_models.push('newer');feishu.label='newer';
   $('settingSearchGatewayKey').value='synthetic-newer';gate.resolve();await saving;
   assert.deepEqual(features,[{trading_enabled:true},{pptx_translate_enabled:true}]);
   assert.deepEqual(writes[0].search.skip_domains,['original.example'],'send the captured search inputs');
   assert.deepEqual(writes[0].presets,{mode:'original'});
   assert.deepEqual(writes[0].hidden_models,['hidden']);assert.deepEqual(writes[0].feishu,{label:'original'});
   assert.equal(writes[0].search.gateway.api_key,'synthetic-first');
   assert.equal($('settingSearchGatewayKey').value,'synthetic-newer');
   assert.equal($('settingsModal').classList.contains('open'),true,'later edits remain open');
 }else if(SCENARIO==='late_key' || SCENARIO==='late_field'){
   const field=$(SCENARIO==='late_key'?'settingSearchGatewayKey':'skipDomainsAdd');
   if(SCENARIO==='late_field')blocked=['later.example'];
   field.value=SCENARIO==='late_key'?'synthetic-newer':'later.example';field.dispatchEvent(new window.Event('input',{bubbles:true}));
   gate.resolve();await saving;
   assert.equal($('settingsModal').classList.contains('open'),true,'do not close over newer edits');
   assert.equal(field.value,SCENARIO==='late_key'?'synthetic-newer':'later.example');
   if(SCENARIO==='late_field')assert.deepEqual(blocked,['later.example'],'do not repopulate over newer edits');
   assert.match($('settingsStatusHint').textContent,new RegExp(t('context.unsaved')));
 }else if(SCENARIO==='reopen_success' || SCENARIO==='reopen_failure'){
   bridge.closeSettings();await open();$('settingsStatusHint').textContent='current view';
   $('settingSearchGatewayKey').value='synthetic-reopened';
   if(SCENARIO==='reopen_failure')gate.reject(Error('retired write refusal'));else gate.resolve();
   await saving;
   assert.equal($('settingsModal').classList.contains('open'),true,'retired save cannot close a new opening');
   assert.equal($('settingsStatusHint').textContent,'current view');
   assert.equal($('settingSearchGatewayKey').value,'synthetic-reopened');
 }else if(SCENARIO==='retired_timer'){
   gate.resolve();await saving;await open();$('settingsStatusHint').textContent=t('settings.saved');
   for(const callback of timers)callback();
   assert.equal($('settingsStatusHint').textContent,t('settings.saved'),'a retired notice timer cannot clear a newer notice');
 }else if(SCENARIO==='server_denied' || SCENARIO==='server_no_receipt'){
   gate.resolve();await saving;
   assert.equal($('settingsModal').classList.contains('open'),true);
   assert.notEqual($('settingsStatusHint').textContent,'','an unacknowledged save must remain visibly failed');
   assert.equal($('settingSearchGatewayKey').value,'synthetic-first');
   await bridge.saveSettings();
   assert.equal(writes.length,2);assert.equal($('settingsModal').classList.contains('open'),false);
 }else{
   gate.resolve();await saving;assert.equal($('settingsModal').classList.contains('open'),false);
   assert.equal(writes.length,1);assert.equal($('settingSearchGatewayKey').value,'');
 }
 assert.equal($('settingsSaveBtn').disabled,false);dom.window.close();
}
main().catch(error=>{console.error(error);process.exitCode=1;dom.window.close();});
'''


@pytest.mark.parametrize('scenario', ['captured_values', 'reopen_during_feature',
    'late_key', 'late_field', 'reopen_success', 'reopen_failure', 'retired_timer', 'unchanged',
    'server_denied', 'server_no_receipt'])
def test_settings_save_inputs_and_feedback(settings_bundle, scenario):
    script = HARNESS.replace('ROOT', json.dumps(str(ROOT))).replace(
        'BUILT', json.dumps(str(settings_bundle))).replace('SCENARIO', json.dumps(scenario))
    result = subprocess.run(['node', '-e', script], cwd=ROOT,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
