"""MCP commands retain captured input, committed effects and original feedback."""
import json
import subprocess

import pytest

from tests.frontend.test_settings_import_oauth_lifecycle import ROOT, settings_oauth_bundle as settings_oauth_bundle

pytestmark = pytest.mark.unit

HARNESS = r'''
const assert=require('node:assert/strict'),fs=require('node:fs');
const {JSDOM}=require(require('node:path').join(ROOT,'node_modules/jsdom'));
const dom=new JSDOM('<!doctype html><body><div id="settingsModal" class="open">'
 +fs.readFileSync(ROOT+'/static/settings_panels/mcp.html','utf8')
 +'<button id="settingsSaveBtn"></button><span id="settingsStatusHint"></span></div></body>',{url:'http://localhost'});
const shell=JSDOM.fragment(fs.readFileSync(ROOT+'/index.html','utf8'));
for(const id of ['mcpInstallOverlay','mcpAddOverlay'])dom.window.document.body.appendChild(shell.querySelector('#'+id));
for(const name of ['window','document','localStorage','HTMLElement','HTMLInputElement','HTMLSelectElement',
 'HTMLButtonElement','HTMLTextAreaElement','AbortController','AbortSignal'])global[name]=name==='window'?dom.window:dom.window[name];
const built=require(BUILT),{bridge,beginSettingsReadSession}=built;
const tick=async()=>{for(let i=0;i<4;i++)await new Promise(done=>setImmediate(done));};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const byId=id=>document.getElementById(id),requests=[],alerts=[],summaries=[],refreshed=[];
const timers=new Map();let timerId=0,now=0;
window.setTimeout=global.setTimeout=(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;};
window.clearTimeout=global.clearTimeout=id=>timers.delete(id);
Date.now=()=>now;
const fire=async(ms)=>{const timer=[...timers].find(([,value])=>value.ms===ms);if(timer){timers.delete(timer[0]);now+=ms;timer[1].fn();await tick();}};
const request=(kind,...args)=>{const pending=deferred();requests.push({...pending,kind,args});return pending.promise;};
const requestsFor=kind=>requests.filter(r=>r.kind===kind);
global.Api={mcp:{catalogInstall:(...args)=>request('install',...args),catalogInstallStatus:(...args)=>request('status',...args),
 catalogUninstall:(...args)=>request('uninstall',...args),connectOne:(...args)=>request('connect',...args),
 connectAll:(...args)=>request('connectAll',...args),serverCreate:(...args)=>request('create',...args),
 updateApply:(...args)=>request('update',...args),serverToolsSet:(...args)=>request('tools',...args)}};
global.t=(key,values)=>key+(values?JSON.stringify(values):'');
global.Icon=()=>'';global.escapeHtml=value=>String(value??'');global.debugLog=()=>{};
global.showAlert=message=>{alerts.push(message);return Promise.resolve();};
let confirm=deferred(),confirmCount=0;
global.showConfirm=()=>{confirmCount++;return confirm.promise;};
global.runtimeScope={applyMcpToolSummary:data=>summaries.push(data)};
for(const name of ['captureMcpSettingsReads','refreshMcpSettingsAfterWrite','createMcpCommands','createMcpToolSelection'])
 if(built[name])global[name]=built[name];
bridge.config={};bridge._featureFlags={};bridge._registeredModels=[];
let session=beginSettingsReadSession();session.completeLoading(true);
require('node:vm').runInThisContext(fs.readFileSync(ROOT+'/frontend/src/runtime/sections/settings/mcp.js','utf8'));
_renderMcpCatalog=()=>{};_renderMcpInstalled=()=>{};_renderMcpCategoryBar=()=>{};_mcpUpdateToolCount=()=>{};
_mcpRefreshAfterWrite=async()=>{refreshed.push(true);};
_mcpCatalog=['alpha','beta'].map(id=>({id,name:id,env_specs:[],installed:true,connected:true}));
const retire=()=>bridge.closeSettings();
const reopen=()=>{retire();byId('settingsModal').classList.add('open');session=beginSettingsReadSession();session.completeLoading(true);};
const toolsResponse=total=>({ok:true,json:async()=>({mcp_tool_summary:{total}})});
const add=()=>{_mcpOpenAddModal();byId('mcpNewName').value='custom';byId('mcpNewCommand').value='synthetic-command';};
async function main(){
 if(SCENARIO==='retired_dialog_cannot_open'){
  _mcpOpenInstallModal('alpha');retire();_mcpOpenInstallModal('beta');
  assert.equal(byId('mcpInstallOverlay').style.display,'none','a retired Settings action cannot expose a dialog without an owner');
  _mcpOpenAddModal();assert.equal(byId('mcpAddOverlay').style.display,'none');
 }else if(SCENARIO==='install_poll_deadline'){
  let reads=0;Api.mcp.catalogInstallStatus=async()=>{reads++;return null;};
  _mcpOpenInstallModal('alpha');const operation=_mcpDoInstall();await tick();
  requestsFor('install')[0].resolve({ok:true,status:'installing'});await tick();
  for(let i=0;i<144;i++)await fire(2500);
  await operation;assert.equal(reads,144);assert.equal(timers.size,0);
  assert.match(byId('mcpInstallStatus').textContent,/mcp.installTimeout/);assert.equal(refreshed.length,0);
 }else if(SCENARIO==='pending_operation_identity'){
  const one=_mcpReconnect('alpha'),two=_mcpApplyUpdate('alpha');await tick();
  assert.equal(_mcpPending.alpha,'updating');requestsFor('connect')[0].resolve({ok:true});await one;
  assert.equal(_mcpPending.alpha,'updating','an earlier completion cannot clear a later pending operation');
  requestsFor('update')[0].resolve({ok:true,updated:true,version:'synthetic'});await two;
  assert.equal(_mcpPending.alpha,undefined);assert.equal(refreshed.length,2);
 }else if(SCENARIO==='tool_projection_failure'){
  _mcpToolsCache.alpha=[{name:'one',enabled:true},{name:'two',enabled:true}];
  let paints=0;_mcpUpdateToolCount=()=>{if(++paints===1)throw new Error('synthetic projection failure');};
  const one=_mcpToggleTool('alpha','one',false),two=_mcpToggleTool('alpha','two',false);await tick();
  requestsFor('tools')[0].resolve(toolsResponse(1));await tick();
  assert.equal(requestsFor('tools').length,2,'a display failure cannot strand an already accepted later intent');
  requestsFor('tools')[1].resolve(toolsResponse(0));await Promise.all([one,two]);
  assert.deepEqual(_mcpCatalog[0].disabled_tools,['one','two']);
 }else if(SCENARIO==='install_success_controls'){
  _mcpOpenInstallModal('alpha');byId('mcpInstallFields').innerHTML='<input class="mcp-env-input" data-key="TOKEN" value="before">';
  const operation=_mcpDoInstall();await tick();requestsFor('install')[0].resolve({ok:true});await operation;
  assert.equal(byId('mcpInstallBtn').disabled,true,'unchanged success stays inert until its scheduled close');
  const input=byId('mcpInstallFields').querySelector('input');input.value='after';input.dispatchEvent(new window.Event('input',{bubbles:true}));
  assert.equal(byId('mcpInstallBtn').disabled,false,'later edits allow an explicit new submission');
  await fire(1200);assert.equal(byId('mcpInstallOverlay').style.display,'flex');
 }else if(SCENARIO==='install_settles_closed'){
  _mcpOpenInstallModal('alpha');const operation=_mcpDoInstall();await tick();retire();
  requestsFor('install')[0].resolve({ok:true,status:'installing'});await tick();await fire(2500);
  assert.equal(requestsFor('status')[0].args[0],'alpha');
  requestsFor('status')[0].resolve({json:async()=>({ok:true,status:'ready'})});await operation;
  assert.equal(refreshed.length,1);assert.equal(byId('mcpInstallOverlay').style.display,'none');assert.equal(timers.size,0);
 }else if(SCENARIO==='manual_reopen_payload'){
  add();byId('mcpNewEnv').value='TOKEN=original';const one=_mcpSaveServer(),two=_mcpSaveServer();await tick();
  assert.equal(requestsFor('create').length,1);_mcpCloseAddModal();add();byId('mcpNewName').value='later';byId('mcpNewEnv').value='TOKEN=later';
  requestsFor('create')[0].resolve({ok:true});await tick();assert.equal(requestsFor('connect')[0].args[0],'custom');
  requestsFor('connect')[0].resolve({ok:false,error:'old failure'});await Promise.all([one,two]);
  assert.equal(byId('mcpAddStatus').style.display,'none');assert.equal(byId('mcpNewEnv').value,'TOKEN=later');
  assert.deepEqual(requestsFor('create')[0].args[0].env,{TOKEN:'original'});assert.equal(refreshed.length,1);
 }else if(SCENARIO==='connect_all_retired'){
  const operation=_mcpConnectAll();await tick();assert.equal(byId('mcpConnectAllBtn').disabled,true);
  reopen();assert.equal(byId('mcpConnectAllBtn').disabled,false);
  requestsFor('connectAll')[0].resolve({ok:true,total_tools:4,servers:{alpha:{},beta:{}}});await operation;
  assert.equal(refreshed.length,1);assert.deepEqual(alerts,[]);
 }else if(SCENARIO==='tools_independent_servers'){
  _mcpToolsCache.alpha=[{name:'one',enabled:true}];_mcpToolsCache.beta=[{name:'two',enabled:true}];
  const one=_mcpToggleTool('alpha','one',false),two=_mcpToggleTool('beta','two',false);await tick();
  assert.deepEqual(requestsFor('tools').map(r=>r.args[0]),['alpha','beta']);
  requestsFor('tools')[1].resolve(toolsResponse(1));requestsFor('tools')[0].resolve(toolsResponse(0));await Promise.all([one,two]);
  assert.deepEqual(summaries.map(s=>s.total).sort(),[0,1],'each acknowledged replacement publishes its response summary to the context rail');
 }else if(SCENARIO==='install_captures_target'){
  _mcpOpenInstallModal('alpha');const operation=_mcpDoInstall();await tick();_mcpOpenInstallModal('beta');
  requestsFor('install')[0].resolve({ok:true,status:'installing'});await tick();await fire(2500);
  assert.equal(requestsFor('status')[0].args[0],'alpha','poll the accepted target, not the later dialog target');
  requestsFor('status')[0].resolve({json:async()=>({ok:true,status:'ready',tools_count:3})});await operation;
  assert.equal(byId('mcpInstallStatus').style.display,'none','old progress must not show on the new dialog');
  assert.equal(refreshed.length,1,'accepted installation refreshes global tool state');
 }else if(SCENARIO==='install_delayed_close'){
  _mcpOpenInstallModal('alpha');const operation=_mcpDoInstall();await tick();
  requestsFor('install')[0].resolve({ok:true,status:'ready'});await operation;
  _mcpOpenInstallModal('beta');await fire(1200);
  assert.equal(byId('mcpInstallOverlay').style.display,'flex','old success cannot close a replacement dialog');
  assert.equal(refreshed.length,1);
 }else if(SCENARIO==='install_duplicate'){
  _mcpOpenInstallModal('alpha');const one=_mcpDoInstall(),two=_mcpDoInstall();await tick();
  assert.equal(requestsFor('install').length,1,'one dialog submission joins the accepted command');
  requestsFor('install')[0].resolve({ok:true});await Promise.all([one,two]);
 }else if(SCENARIO==='install_later_edit'){
  _mcpOpenInstallModal('alpha');byId('mcpInstallFields').innerHTML='<input class="mcp-env-input" data-key="TOKEN" value="original">';
  const operation=_mcpDoInstall();await tick();const input=byId('mcpInstallFields').querySelector('input');
  input.value='later';input.dispatchEvent(new window.Event('input',{bubbles:true}));
  requestsFor('install')[0].resolve({ok:true});await operation;await fire(1200);
  assert.equal(byId('mcpInstallOverlay').style.display,'flex');assert.equal(input.value,'later');
  assert.deepEqual(requestsFor('install')[0].args,['alpha',{TOKEN:'original'}]);
  assert.equal(byId('mcpInstallBtn').disabled,false);
 }else if(SCENARIO==='close_retires_dialog'){
  _mcpOpenInstallModal('alpha');byId('mcpInstallFields').innerHTML='<input class="mcp-env-input" value="synthetic-secret">';
  const input=byId('mcpInstallFields').querySelector('input');retire();
  assert.equal(byId('mcpInstallOverlay').style.display,'none');assert.equal(input.value,'');
 }else if(SCENARIO==='quick_failure_retired'){
  const operation=_mcpQuickInstall('alpha');await tick();reopen();_mcpOpenInstallModal('beta');
  requestsFor('install')[0].reject(new Error('old failure'));await operation;
  assert.equal(byId('mcpInstallTitle').textContent,'beta');assert.deepEqual(alerts,[]);
 }else if(SCENARIO==='uninstall_confirmation_retired'||SCENARIO==='purge_confirmation_retired'){
  const operation=SCENARIO.startsWith('purge')?_mcpPurge('alpha'):_mcpUninstall('alpha');
  reopen();confirm.resolve(true);await tick();assert.equal(requestsFor('uninstall').length,0,'retired confirmation cannot authorize a write');await operation;
 }else if(SCENARIO==='duplicate_confirmation'){
  const one=_mcpUninstall('alpha'),two=_mcpUninstall('alpha');
  assert.equal(confirmCount,1);confirm.resolve(false);await Promise.all([one,two]);
 }else if(SCENARIO==='reconnect_error_retired'){
  const operation=_mcpReconnect('alpha');await tick();reopen();
  requestsFor('connect')[0].resolve({ok:false,error:'old failure'});await operation;assert.deepEqual(alerts,[]);
 }else if(SCENARIO==='manual_connect_failure'){
  add();const operation=_mcpSaveServer();await tick();requestsFor('create')[0].resolve({ok:true});await tick();
  requestsFor('connect')[0].resolve({ok:false,error:'synthetic unavailable'});await operation;
  assert.equal(byId('mcpAddStatus').className,'mcp-install-status error');
  assert.match(byId('mcpAddStatus').textContent,/synthetic unavailable/);assert.equal(refreshed.length,1,'creation remains committed even when connecting fails');
 }else if(SCENARIO==='manual_delayed_close'){
  add();const operation=_mcpSaveServer();await tick();requestsFor('create')[0].resolve({ok:true});await tick();
  requestsFor('connect')[0].resolve({ok:true});await operation;add();byId('mcpNewName').value='later';await fire(900);
  assert.equal(byId('mcpAddOverlay').style.display,'flex');assert.equal(byId('mcpNewName').value,'later');assert.equal(refreshed.length,1);
 }else if(SCENARIO==='tool_failure_rebase'){
  _mcpToolsCache.alpha=[{name:'one',enabled:true},{name:'two',enabled:true}];
  const one=_mcpToggleTool('alpha','one',false),two=_mcpToggleTool('alpha','two',false);await tick();
  assert.equal(requestsFor('tools').length,1,'full replacements for one server settle in intent order');
  requestsFor('tools')[0].reject(new Error('first failed'));await tick();
  assert.deepEqual(requestsFor('tools')[1].args,['alpha',['two']],'later independent edit rebases on the last acknowledgement');
  requestsFor('tools')[1].resolve(toolsResponse(1));await Promise.all([one,two]);
  assert.deepEqual(_mcpToolsCache.alpha.map(row=>row.enabled),[true,false]);
  assert.deepEqual(_mcpCatalog[0].disabled_tools,['two']);
 }else if(SCENARIO==='tool_rollback_latest_intent'){
  _mcpToolsCache.alpha=[{name:'one',enabled:true},{name:'two',enabled:true}];
  const one=_mcpSetAllTools('alpha',false),two=_mcpToggleTool('alpha','one',true);await tick();
  requestsFor('tools')[0].resolve(toolsResponse(0));await tick();
  requestsFor('tools')[1].reject(new Error('second failed'));await Promise.all([one,two]);
  assert.deepEqual(_mcpToolsCache.alpha.map(row=>row.enabled),[false,false]);
  assert.deepEqual(_mcpCatalog[0].disabled_tools,['one','two']);
 }
 retire();dom.window.close();console.log('MCP command scenario completed');
}
main().catch(error=>{console.error(error);process.exitCode=1;dom.window.close();});
'''


@pytest.mark.parametrize('scenario', [
    'install_captures_target', 'install_delayed_close', 'install_duplicate',
    'install_later_edit', 'close_retires_dialog', 'quick_failure_retired',
    'uninstall_confirmation_retired', 'purge_confirmation_retired', 'duplicate_confirmation',
    'reconnect_error_retired', 'manual_connect_failure', 'manual_delayed_close',
    'tool_failure_rebase', 'tool_rollback_latest_intent',
    'tool_projection_failure', 'install_success_controls', 'install_settles_closed',
    'manual_reopen_payload', 'connect_all_retired', 'tools_independent_servers',
    'retired_dialog_cannot_open', 'install_poll_deadline', 'pending_operation_identity',
])
def test_mcp_command_lifetime(settings_oauth_bundle, scenario):
    bindings = {'ROOT': str(ROOT), 'BUILT': str(settings_oauth_bundle), 'SCENARIO': scenario}
    script = '\n'.join(f'const {key}={json.dumps(value)};' for key, value in bindings.items()) + HARNESS
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'MCP command scenario completed' in result.stdout, 'Node exited with the scenario still unsettled'
