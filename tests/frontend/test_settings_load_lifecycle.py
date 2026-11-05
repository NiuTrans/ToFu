"""A Settings opening owns its initial reads and their presentation lifetime."""
import json
import subprocess

import pytest

from tests.frontend.test_settings_save_feedback import ROOT, settings_bundle as settings_bundle

pytestmark = pytest.mark.unit

HARNESS = r'''
const assert = require('node:assert/strict');
const {JSDOM} = require(require('node:path').join(ROOT, 'node_modules/jsdom'));
const dom = new JSDOM(`<!doctype html><body><div id="settingsModal">
  <button id="settingsSaveBtn"></button><span id="settingsStatusHint"></span>
  <div id="settingsTab_search" class="settings-tab-panel"><input id="searchField"></div>
  <div id="stgProviderList"></div><div id="stgModelCatalog"></div><div id="stgPresetTable"></div>
  <div id="settingsVersion"></div><div id="settingsUpdateVersion"></div>
  <div id="settingsMobileCard"><a id="settingsMobileAndroid"></a><span id="settingsMobileAndroidVersion"></span>
    <a id="settingsMobileIos" class="stg-mobile-row-soon"><span id="settingsMobileIosBadge"></span></a></div>
  <div id="settingsDesktopCard"><div id="settingsDesktopRows"></div></div>
</div></body>`, {url:'http://localhost'});
for (const name of ['window','document','localStorage','HTMLElement','HTMLInputElement','HTMLSelectElement',
    'HTMLButtonElement','HTMLTextAreaElement','HTMLAnchorElement','AbortController','AbortSignal']) {
  global[name] = name === 'window' ? dom.window : dom.window[name];
}
const {bridge,runModelRoutingMutation} = require(BUILT);
const requests = {config:[],health:[],desktop:[]}, projections = [], prices=[];
const deferred = () => {let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {resolve,reject,promise};};
const read = (kind, options) => {const request=deferred();requests[kind].push({...request,options});return request.promise;};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const config = tag => ({presets:{tag}, model_pricing:{tag}, model_price_display:{usd_rates:{tag}}, tag});
bridge.config={}; bridge._featureFlags={};bridge._registeredModels=[];
bridge._populateSearchTab = cfg => {projections.push(cfg.tag);document.getElementById('searchField').value=cfg.tag;};
bridge._setModelPricingCache = value => prices.push(value.tag);
bridge._renderModelRoutingTabs=()=>{};bridge._renderPresetsTab=()=>{};
bridge.Api={serverConfig:{get:opts=>read('config',opts)},health:{info:opts=>read('health',opts)},
  desktop:{status:(arch,opts)=>read('desktop',opts)},
  modelRouting:{get:async()=>({revision:1,model_routing:{contract_version:'tofu.model-routing/v2',revision:1}})}};
async function main() {
  bridge.openSettings();await tick();
  if (SCENARIO==='closed' || SCENARIO==='pagehide') {
    if (SCENARIO==='closed') bridge.closeSettings();
    else window.dispatchEvent(new window.Event('pagehide'));
    for (const kind of ['config','health','desktop']) assert.equal(requests[kind][0].options.signal.aborted,true,kind+' read cancelled');
    requests.config[0].resolve(config('retired')); requests.health[0].resolve({version:'old'});
    requests.desktop[0].resolve({download_url:'https://example.invalid/old'});await tick();await tick();
    assert.deepEqual(projections,[],'closed form must not populate');
    assert.equal(document.getElementById('settingsTab_search').hasAttribute('inert'),false,'closing restores the owned form attributes');
    assert.equal(document.getElementById('settingsVersion').textContent,'');
    assert.deepEqual(prices,[],'retired config must not replace global pricing');
  } else if (SCENARIO==='reentrant_open') {
    bridge._renderPresetsTab=()=>{bridge._renderPresetsTab=()=>{};bridge.closeSettings();bridge.openSettings();};
    requests.config[0].resolve(config('retired'));await tick();await tick();
    assert.deepEqual(projections,[],'a populator may retire its opening synchronously');
    assert.equal(requests.config.length,2);
    requests.config[1].resolve(config('current'));await tick();await tick();
    assert.deepEqual(projections,['current']);
  } else if (SCENARIO==='coalesced') {
    const refresh=bridge._refreshSubscriptionModelCatalog();await tick();
    assert.equal(requests.config.length,1,'one config read per opening while pending');
    requests.config[0].resolve(config('shared'));await refresh;await tick();
    assert.equal(bridge._serverConfig.tag,'shared');
    assert.equal(document.getElementById('settingsTab_search').hasAttribute('inert'),false);
  } else if (SCENARIO==='update_pill_live' || SCENARIO==='update_pill_absent') {
    let updates=0;
    // A lazy feature may publish this optional port after Settings has opened.
    if (SCENARIO==='update_pill_live') bridge._renderSettingsUpdatePill=()=>{updates++;};
    requests.config[0].resolve(config('current'));
    requests.health[0].resolve({version:'new',mobile_client_url:'/new.apk'});await tick();await tick();
    assert.equal(updates,SCENARIO==='update_pill_live'?1:0);
    assert.equal(document.getElementById('settingsVersion').textContent,'vnew');
    assert.equal(document.getElementById('settingsMobileAndroid').getAttribute('href'),'/new.apk');
  } else if (SCENARIO==='save_waits' || SCENARIO==='save_read_failure' || SCENARIO==='close_before_save') {
    let writes=0;
    bridge._persistSttProvider=async()=>({refreshed:true});bridge._persistModelRoutingNow=async()=>{};
    bridge.Api.serverConfig.update=async()=>{writes++;return {json:async()=>({ok:true})};};
    assert.equal(document.getElementById('settingsTab_search').hasAttribute('inert'),true);
    const prompt=document.createElement('textarea');prompt.id='settingSystem';
    document.getElementById('settingsModal').append(prompt);
    const saving=bridge.saveSettings();await tick();assert.equal(writes,0);
    prompt.value='written while initial config is loading';
    prompt.dispatchEvent(new window.Event('input',{bubbles:true}));
    if(SCENARIO==='close_before_save') bridge.closeSettings();
    if(SCENARIO==='save_read_failure') requests.config[0].reject(Error('synthetic read refusal'));
    else requests.config[0].resolve(config('loaded'));
    await saving;await tick();
    assert.equal(writes,SCENARIO==='save_waits'?1:0);
    assert.equal(document.getElementById('settingsSaveBtn').disabled,false);
    if(SCENARIO==='save_waits'){
      assert.equal(bridge.config.systemPrompt,prompt.value,'the initial readiness gate precedes input capture');
      assert.equal(document.getElementById('settingsModal').classList.contains('open'),false,'captured loading-time edits are already saved');
    }
    if(SCENARIO==='save_read_failure') {
      assert.equal(document.getElementById('settingsModal').classList.contains('open'),true);
      assert.equal(document.getElementById('settingsTab_search').hasAttribute('inert'),true);
      assert.notEqual(document.getElementById('settingsStatusHint').textContent,'');
    }
  } else if (SCENARIO==='accepted_write') {
    const write=deferred();
    const saving=runModelRoutingMutation(()=>write.promise);await tick();
    bridge.closeSettings();
    const document={contract_version:'tofu.model-routing/v2',revision:2};
    write.resolve({revision:2,authority:{revision:2,model_routing:document}});await saving;
    assert.equal(bridge._stgModelRoutingRevision,2,'closing presentation cannot cancel accepted routing writes');
    requests.config[0].resolve(config('retired'));await tick();assert.deepEqual(projections,[]);
  } else if (SCENARIO==='export_isolated') {
    const blobs=[];global.URL.createObjectURL=blob=>{blobs.push(blob);return 'blob:synthetic';};
    global.URL.revokeObjectURL=()=>{};window.HTMLAnchorElement.prototype.click=()=>{};
    bridge.exportServerConfig();await tick();assert.equal(requests.config.length,2);
    bridge.closeSettings();bridge.openSettings();await tick();
    requests.config[2].resolve(config('current'));await tick();await tick();
    requests.config[1].resolve(config('exported'));await tick();
    assert.equal(JSON.parse(await blobs[0].text()).tag,'exported');
    assert.equal(bridge._serverConfig.tag,'current');assert.deepEqual(prices,['current']);
    requests.config[0].resolve(config('retired'));await tick();
    assert.deepEqual(projections,['current']);
  } else if (SCENARIO==='mobile_removed') {
    requests.config[0].resolve(config('first'));
    requests.health[0].resolve({mobile_client_url:'/first.apk',ios_client_url:'https://example.invalid/ios'});await tick();await tick();
    assert.equal(document.getElementById('settingsMobileIos').getAttribute('target'),'_blank');
    bridge.closeSettings();bridge.openSettings();await tick();
    requests.config[1].resolve(config('second'));requests.health[1].resolve({mobile_client_url:'/second.apk'});await tick();await tick();
    assert.equal(document.getElementById('settingsMobileIos').hasAttribute('href'),false);
    assert.equal(document.getElementById('settingsMobileIos').getAttribute('target'),null);
    assert.equal(document.getElementById('settingsMobileIosBadge').style.display,'');
  } else {
    bridge.closeSettings();bridge.openSettings();await tick();
    requests.config[1].resolve(config('current'));requests.health[1].resolve({version:'new',mobile_client_url:'/new.apk'});
    requests.desktop[1].resolve({download_url:'https://example.invalid/new'});await tick();await tick();
    if (SCENARIO==='stale_success') {
      requests.config[0].resolve(config('old'));requests.health[0].resolve({version:'old',mobile_client_url:'/old.apk'});
      requests.desktop[0].resolve({download_url:'https://example.invalid/old'});
    } else {
      requests.config[0].reject(Error('old config failure'));requests.health[0].reject(Error('old health failure'));
      requests.desktop[0].reject(Error('old desktop failure'));
    }
    await tick();await tick();
    assert.equal(document.getElementById('searchField').value,'current');
    assert.deepEqual(projections,['current']);assert.deepEqual(bridge._stgPresets,{tag:'current'});
    assert.equal(bridge._serverConfig.tag,'current');assert.deepEqual(prices,['current']);
    assert.equal(document.getElementById('settingsVersion').textContent,'vnew');
    assert.equal(document.getElementById('settingsMobileAndroid').getAttribute('href'),'/new.apk');
    assert.match(document.getElementById('settingsDesktopRows').innerHTML,/example.invalid\/new/);
    assert.notEqual(document.getElementById('settingsDesktopCard').style.display,'none');
    assert.equal(document.getElementById('settingsStatusHint').textContent,'');
  }
  dom.window.close();
}
main().catch(error=>{console.error(error);process.exitCode=1;dom.window.close();});
'''


@pytest.mark.parametrize('scenario', ['closed', 'pagehide', 'stale_success', 'stale_failure',
    'coalesced', 'save_waits', 'save_read_failure', 'close_before_save', 'accepted_write',
    'export_isolated', 'mobile_removed', 'reentrant_open', 'update_pill_live', 'update_pill_absent'])
def test_settings_initial_reads_retire(settings_bundle, scenario):
    script = HARNESS.replace('BUILT', json.dumps(str(settings_bundle))).replace(
        'ROOT', json.dumps(str(ROOT))).replace('SCENARIO', json.dumps(scenario))
    result = subprocess.run(['node', '-e', script], cwd=ROOT,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
