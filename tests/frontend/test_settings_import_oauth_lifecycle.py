"""Late Settings import/login results retain their command and presentation owners."""
import json
import subprocess

import pytest

from tests.frontend.test_settings_save_feedback import ROOT, _require_frontend_bundler

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def settings_oauth_bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp('settings-import-oauth')
    entry = directory / 'entry.ts'
    exports = {
        'featureRegistry as bridge': 'feature-registry',
        'beginSettingsReadSession, currentSettingsReadSession': 'features/settings/panel-read-session',
        'renderCodexSharedLogin': 'features/settings/codex-shared-login',
        'populatePreferencesTab': 'features/memory/preferences',
        'populateToolsInventory': 'features/settings/tools-inventory',
        'captureOAuthPanel': 'features/settings/oauth-panel-lifetime',
        'captureMcpSettingsReads, notifyMcpSettingsVisibility, refreshMcpSettingsAfterWrite': 'features/settings/mcp-settings-reads',
        'createMcpCommands': 'features/settings/mcp-commands',
        'createMcpToolSelection': 'features/settings/mcp-tool-selection',
    }
    entry.write_text('\n'.join([
        f'export {{ {names} }} from {json.dumps(str(ROOT / "frontend/src" / source))};'
        for names, source in exports.items()
    ] + [f'import {json.dumps(str(ROOT / "frontend/src/features/settings" / source))};'
         for source in ('core-panel', 'save-export')]))
    output = directory / 'settings.cjs'
    _require_frontend_bundler()
    result = subprocess.run(['node', 'scripts/vite_test_bundle.mjs', str(entry),
        '--bundle', '--platform=node', '--format=cjs', f'--outfile={output}'],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return output


HARNESS = r'''
const assert = require('node:assert/strict');
const {JSDOM} = require(require('node:path').join(ROOT, 'node_modules/jsdom'));
const dom = new JSDOM(`<!doctype html><body><div id="settingsModal" class="open">
 <button id="settingsSaveBtn"></button><span id="settingsStatusHint"></span>
 <input id="importFile"><input id="draft">
 <div id="oauthCodexCard"><p class="oauth-provider-desc"></p></div>
 <button id="oauthCodexLoginBtn"></button><button id="oauthCodexDeviceBtn"></button>
 <span id="oauthCodexStatus"></span><div id="oauthCodexManual"></div>
 <input id="oauthCodexManualUrl"><input id="oauthCodexAuthUrl">
 <button id="oauthCodexResetBtn"></button></div></body>`, {url:'http://localhost'});
for (const name of ['window','document','localStorage','HTMLElement','HTMLInputElement',
 'HTMLSelectElement','HTMLButtonElement','HTMLTextAreaElement','AbortController','AbortSignal']) {
 global[name] = name === 'window' ? dom.window : dom.window[name];
}
const built = require(BUILT), {bridge, beginSettingsReadSession, currentSettingsReadSession,
 renderCodexSharedLogin} = built;
const deferred = () => {let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const tick = async () => {for(let i=0;i<5;i++) await new Promise(resolve=>setImmediate(resolve));};
const byId = id=>document.getElementById(id);
bridge.config={};bridge._featureFlags={};bridge._registeredModels=[];
const logs=[];bridge.debugLog=(message)=>logs.push(message);
const start = () => {const view=beginSettingsReadSession();view.completeLoading(true);return view;};
let view=start(), openings=0;
bridge.openSettings=()=>{openings++;view=start();byId('settingsModal').classList.add('open');};
const retire = () => bridge.closeSettings();
const reopen = () => {retire();bridge.openSettings();byId('settingsStatusHint').textContent='new view';};
const pending=deferred();let writes=0;
bridge.Api={serverConfig:{update:()=>{writes++;return pending.promise;}}};
async function importScenario(){
 const readers=[];
 global.FileReader=class {
  readAsText(){readers.push(this);}
  abort(){this.aborted=true;this.onabort?.({target:this});}
 };
 const input=byId('importFile');Object.defineProperty(input,'files',{value:[{}]});
 const importing=bridge.importServerConfig({target:input});
 if(SCENARIO==='import_read_retired'){
  retire();assert.equal(readers[0].aborted,true,'closing aborts a file read before any command is accepted');
  readers[0].onload?.({target:{result:'{"tag":"old"}'}});await tick();
  assert.equal(writes,0);return;
 }
 if(SCENARIO==='import_read_error'){
  readers[0].onerror?.({target:readers[0]});await tick();
  assert.equal(byId('settingsSaveBtn').disabled,false);
  assert.notEqual(byId('settingsStatusHint').textContent,'','file read failures remain visible');return;
 }
 readers[0].onload({target:{result:'{"tag":"imported"}'}});await tick();assert.equal(writes,1);
 if(SCENARIO==='import_duplicate'){
  bridge.importServerConfig({target:input});await tick();assert.equal(readers.length,1,'one accepted Settings write owns Save/import');
 } else if(SCENARIO==='import_reopen'){
  reopen();bridge._serverConfig={tag:'current'};
 } else if(SCENARIO==='import_edited'){
  byId('draft').value='new draft';byId('draft').dispatchEvent(new window.Event('input',{bubbles:true}));
 }
 pending.resolve({json:async()=>({ok:true})});await importing;await tick();
 if(SCENARIO==='import_reopen'){
  assert.equal(openings,1,'an old import must not open/reset the new Settings view');
  assert.equal(bridge._serverConfig.tag,'current');assert.equal(byId('settingsStatusHint').textContent,'new view');
 } else if(SCENARIO==='import_edited'){
  assert.equal(openings,0,'accepted import must preserve subsequent editor changes');
  assert.equal(byId('draft').value,'new draft');
 } else assert.equal(openings,1,'a successful current import refreshes its own editor');
 assert.equal(byId('settingsSaveBtn').disabled,false);
}
async function sharedLoginScenario(){
 const seen=[],committed=[];
 global.fetch=()=>{writes++;return pending.promise;};
 renderCodexSharedLogin({shared_login_reuse_supported:true},status=>seen.push(['old',status]),()=>committed.push('old'));
 byId('oauthCodexReuseBtn').click();await tick();assert.equal(writes,1);
 reopen();renderCodexSharedLogin({shared_login_reuse_supported:true,authenticated:false},status=>seen.push(['new',status]),()=>committed.push('new'));
 pending.resolve(new Response(JSON.stringify(SCENARIO==='shared_login_error'
  ? {status:'error',auth_issue:'codex_not_signed_in'} : {status:'success'}),{status:SCENARIO==='shared_login_error'?400:200}));
 await tick();assert.deepEqual(seen,[],'a retired command cannot invoke a new opening callback');
 assert.deepEqual(committed,SCENARIO==='shared_login_error'?[]:['old'],'accepted reuse retains its committed-policy callback');
 assert.equal(byId('oauthCodexSharedLogin').dataset.state,'idle');
 assert.equal(byId('oauthCodexReuseBtn').disabled,false,'the command latch releases after acknowledgement');
}
async function oauthScenario(){
 const intervals=new Map(),timeouts=new Map();let timerId=0;
 window.setInterval=global.setInterval=(fn,delay)=>{intervals.set(++timerId,{fn,delay});return timerId;};
 window.clearInterval=global.clearInterval=id=>intervals.delete(id);
 window.setTimeout=(fn,delay)=>{timeouts.set(++timerId,{fn,delay});return timerId;};
 window.clearTimeout=id=>timeouts.delete(id);
 global.BroadcastChannel=class{close(){}};
 global.screen={width:1200,height:900};
 global.escapeHtml=value=>String(value);global.errorEnvelopeMessage=value=>String(value||'');
 const popups=[];window.open=(...args)=>{popups.push(args);return {closed:false};};
 const alerts=[],painted=[],catalog=[];global.showAlert=async text=>alerts.push(text);
 const confirmation=deferred();global.showConfirm=()=>confirmation.promise;
 global.t=(key)=>key;global.renderCodexSharedLogin=()=>{};
 global.captureOAuthPanel=built.captureOAuthPanel;
 global.Api={oauth:{status:options=>{writes++;return pending.promise;},
  loginPost:()=>{writes++;return pending.promise;},resetOfferConsume:()=>{writes++;return pending.promise;},
  logoutPost:()=>{writes++;return pending.promise;}}};
 require('node:vm').runInThisContext(require('node:fs').readFileSync(OAUTH,'utf8'),{filename:OAUTH});
 const originalRender=_updateOAuthCard;
 _updateOAuthCard=(provider,status)=>painted.push([provider,status]);
 _refreshSubscriptionModelCatalog=session=>{catalog.push(session);return Promise.resolve(null);};
 if(SCENARIO==='oauth_exchange_retired'){
  const origin=view;_oauthRecordPendingFlow('codex',1455,'state');
  _serverExchange=()=>{writes++;return pending.promise;};
  _completeLogin('codex','code','state');await tick();assert.equal(writes,1);painted.length=0;
  reopen();_oauthRecordPendingFlow('codex',1455,'new-state');
  pending.resolve({ok:true});await tick();
  assert.deepEqual(painted,[]);assert.deepEqual(catalog,[origin]);
  assert.equal(_oauthCallbackMessageAllowed('codex','new-state','http://localhost:1455'),true,
    'an old completion cannot clear a later authorization gate');
 } else if(SCENARIO==='oauth_exchange_params'){
  _oauthRecordPendingFlow('codex',1455,'state');_oauthExchangeParams.codex={code_verifier:'original'};
  _serverExchange=()=>pending.promise;let exchange;
  _browserExchange=(provider,code,state,params)=>{exchange=params;return Promise.resolve({});};
  _storeBrowserToken=async()=>({ok:true});_completeLogin('codex','code','state');
  reopen();_oauthExchangeParams.codex={code_verifier:'new'};
  pending.reject(Object.assign(new Error('unavailable'),{_statusCode:403}));await tick();
  assert.equal(exchange.code_verifier,'original','fallback uses the PKCE parameters captured with that accepted code');
 } else if(SCENARIO==='oauth_status_invalidated'){
  _loadOAuthStatus();await tick();_serverExchange=async()=>({ok:true});
  _completeLogin('codex','code','state');await tick();
  const count=painted.length;pending.resolve({codex:{status:'not_started'}});await tick();
  assert.equal(painted.length,count,'a pre-command status read cannot overwrite acknowledged login');
 } else if(SCENARIO==='oauth_popup_cleanup'){
  _oauthLogin('codex');await tick();
  pending.resolve({ok:true,json:async()=>({auth_url:'https://example.invalid/login',exchange:{state:'state'}})});
  await tick();assert.equal(intervals.size,1);retire();assert.equal(intervals.size,0);
 } else if(SCENARIO==='oauth_device_resume'){
  _updateOAuthCard=originalRender;
  _loadOAuthStatus();await tick();
  pending.resolve({codex:{status:'waiting_callback',device:{user_code:'ABC',verification_url:'https://example.invalid/device'}}});
  await tick();assert.equal(intervals.size,1,'a pending status projection resumes device observation');
  _updateOAuthCard('codex',{status:'waiting_callback',device:{user_code:'ABC'}});
  assert.equal(intervals.size,1,'rendering the same device state keeps one observer');
  retire();assert.equal(intervals.size,0);
 } else if(SCENARIO==='oauth_repoll_budget'){
  Api.oauth.status=async()=>{writes++;return {codex:{reset_offer:{refreshing:true}}};};
  _loadOAuthStatus();await tick();
  for(let count=0;count<8;count++){
    assert.equal(timeouts.size,1);const [id,timer]=[...timeouts][0];timeouts.delete(id);
    assert.equal(timer.delay,2000);timer.fn();await tick();
  }
  assert.equal(writes,9);assert.equal(timeouts.size,0,'the existing 8-repoll limit is retained');
 } else if(SCENARIO==='oauth_device_fallback_retired'){
  let fallback=0;_oauthLogin=()=>{fallback++;};showAlert=()=>confirmation.promise;
  Api.oauth.deviceLoginPost=async()=>({ok:false,status:503,text:async()=>'{"status_code":0}'});
  _oauthDeviceLogin('codex');await tick();reopen();confirmation.resolve(true);await tick();
  assert.equal(fallback,0,'a dismissed old alert cannot start a new login');
 } else if(SCENARIO==='oauth_reset_single_confirmation'){
  let confirmations=0;showConfirm=()=>{confirmations++;return confirmation.promise;};
  _oauthConsumeReset();_oauthConsumeReset();assert.equal(confirmations,1);
  confirmation.resolve(false);await tick();_oauthConsumeReset();await tick();assert.equal(confirmations,2);
 } else if(SCENARIO==='oauth_status_retired'){
  _loadOAuthStatus();await tick();reopen();pending.resolve({codex:{authenticated:true}});await tick();
  assert.deepEqual(painted,[],'late status must not repaint the reopened view');
 } else if(SCENARIO==='oauth_read_coalesced'){
  _loadOAuthStatus();_loadOAuthStatus();_startDeviceStatusPoll('codex');
  for(const timer of intervals.values()) timer.fn();await tick();
  assert.equal(writes,1,'status and device polling share an in-flight read');
  retire();assert.equal(intervals.size,0,'closing releases device polling');
  pending.resolve({codex:{authenticated:true}});await tick();assert.deepEqual(painted,[]);
 } else if(SCENARIO==='oauth_login_retired'){
  _oauthLogin('codex');await tick();reopen();
  pending.resolve({ok:true,json:async()=>({auth_url:'https://example.invalid/oauth',exchange:{state:'old'}})});
  await tick();assert.equal(popups.length,0,'a retired preparation cannot open a login popup');assert.deepEqual(painted,[]);
 } else if(SCENARIO==='oauth_logout_confirmation'){
  const command=_oauthLogout('codex');reopen();confirmation.resolve(true);await command;await tick();
  assert.equal(writes,0,'retired confirmation cannot authorize logout');
 } else if(SCENARIO==='oauth_logout_settle'){
  const origin=view;const command=_oauthLogout('codex');confirmation.resolve(true);await tick();assert.equal(writes,1);
  reopen();pending.resolve({json:async()=>({ok:true})});await command;await tick();
  assert.deepEqual(painted,[]);assert.deepEqual(catalog,[origin],'committed refresh retains its original view');
 } else if(SCENARIO==='oauth_reset_confirmation'){
  _oauthConsumeReset();_oauthConsumeReset();reopen();confirmation.resolve(true);await tick();
  assert.equal(writes,0,'retired reset confirmation cannot issue a command');
 } else if(SCENARIO==='oauth_reset_settle'){
  _oauthConsumeReset();confirmation.resolve(true);await tick();assert.equal(writes,1);reopen();
  pending.resolve({json:async()=>({ok:true})});await tick();assert.deepEqual(alerts,[]);assert.equal(writes,1,'retired reset does not refresh the new view');
 }
}
async function main(){
 if(SCENARIO==='catalog_preserves_new_draft'){
  const origin=view;reopen();
  const draft={contract_version:'tofu.model-routing/v2',revision:2,providers:[{provider_id:'edited'}]};
  bridge._stgModelRouting=draft;bridge._stgModelRoutingRevision=2;bridge._stgModelRoutingSavedJson='{}';
  bridge._serverConfig={tag:'current'};let adopted=0,reads=0,rendered=0;
  bridge._adoptModelRoutingResponse=()=>{adopted++;};
  bridge._renderPresetsTab=()=>rendered++;
  bridge.Api={serverConfig:{get:async()=>{reads++;return {}; }},
    modelRouting:{get:async()=>({revision:3,model_routing:{contract_version:'tofu.model-routing/v2',revision:3}})}};
  await bridge._refreshSubscriptionModelCatalog(origin);
  assert.equal(adopted,1,'the committed model picker still follows the successful command');
  assert.equal(reads,0);assert.equal(rendered,0);
  assert.equal(bridge._stgModelRouting,draft,'retired subscription refresh cannot replace an already edited new draft');
  assert.equal(bridge._serverConfig.tag,'current');
 } else if(SCENARIO.startsWith('import_')) await importScenario();
 else if(SCENARIO.startsWith('shared_login_')) await sharedLoginScenario();
 else await oauthScenario();
 retire();dom.window.close();console.log('ok');
}
main().catch(error=>{console.error(error);process.exitCode=1;dom.window.close();});
'''


@pytest.mark.parametrize('scenario', [
    'import_read_retired', 'import_read_error', 'import_reopen', 'import_edited', 'import_duplicate',
    'shared_login_success', 'shared_login_error', 'oauth_status_retired', 'oauth_read_coalesced',
    'oauth_login_retired', 'oauth_logout_confirmation', 'oauth_logout_settle',
    'oauth_reset_confirmation', 'oauth_reset_settle', 'catalog_preserves_new_draft',
    'oauth_exchange_retired', 'oauth_exchange_params', 'oauth_status_invalidated',
    'oauth_popup_cleanup', 'oauth_device_resume', 'oauth_repoll_budget',
    'oauth_device_fallback_retired', 'oauth_reset_single_confirmation',
])
def test_import_oauth_presentation_lifetime(settings_oauth_bundle, scenario):
    bindings = {'ROOT': str(ROOT), 'BUILT': str(settings_oauth_bundle), 'SCENARIO': scenario,
                'OAUTH': str(ROOT / 'frontend/src/runtime/sections/settings/oauth.js')}
    script = '\n'.join(f'const {key}={json.dumps(value)};' for key, value in bindings.items()) + HARNESS
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
