"""MCP catalog/update/tool reads belong to one Settings opening."""
import json
import subprocess

import pytest

from tests.frontend.test_settings_import_oauth_lifecycle import ROOT, settings_oauth_bundle as settings_oauth_bundle

pytestmark = pytest.mark.unit


HARNESS = r'''
const assert=require('node:assert/strict');
const {JSDOM}=require(require('node:path').join(ROOT,'node_modules/jsdom'));
const dom=new JSDOM('<!doctype html><body><div id="settingsModal" class="open">'
 +'<div id="settingsTab_mcp" class="active"><div id="mcpCatalogGrid"></div></div>'
 +'<div id="settingsTab_preferences"><div id="prefsList"></div><div id="prefsMemoryList"></div></div><div id="toolsInvBody"></div>'
 +'<span id="settingsStatusHint"></span><button id="settingsSaveBtn"></button></div></body>',{url:'http://localhost'});
for(const name of ['window','document','localStorage','HTMLElement','HTMLInputElement','HTMLSelectElement',
 'HTMLButtonElement','HTMLTextAreaElement','AbortController','AbortSignal'])global[name]=name==='window'?dom.window:dom.window[name];
const built=require(BUILT),{bridge,beginSettingsReadSession}=built;
const tick=async()=>{for(let i=0;i<4;i++)await new Promise(done=>setImmediate(done));};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const requests={catalog:[],updates:[],tools:[]},painted=[],summaries=[];
const read=(kind,options)=>{const next=deferred();requests[kind].push({...next,options});return next.promise;};
global.t=key=>key;global.escapeHtml=value=>String(value);global.Icon=()=>'';global.debugLog=()=>{};
global.showAlert=async()=>{};global.showConfirm=async()=>true;
bridge._registeredModels=[];bridge.config={};bridge._featureFlags={};
const grid=document.getElementById('mcpCatalogGrid'),tab=document.getElementById('settingsTab_mcp');
Object.defineProperty(grid,'offsetParent',{get:()=>tab.classList.contains('active')&&document.getElementById('settingsModal').classList.contains('open')?tab:null});
const timers=new Map();let nextId=0;
window.setInterval=global.setInterval=(fn,ms)=>{timers.set(++nextId,{fn,ms,kind:'interval'});return nextId;};
window.setTimeout=global.setTimeout=(fn,ms)=>{timers.set(++nextId,{fn,ms,kind:'timeout'});return nextId;};
window.clearInterval=global.clearInterval=window.clearTimeout=global.clearTimeout=id=>timers.delete(id);
global.Api={mcp:{catalogList:options=>read('catalog',options),updatesCheck:options=>read('updates',options),
 toolsListForServer:(id,options)=>read('tools',options)}};
global.runtimeScope={applyMcpToolSummary:data=>summaries.push(data)};
global.captureMcpSettingsReads=built.captureMcpSettingsReads;
global.notifyMcpSettingsVisibility=built.notifyMcpSettingsVisibility;
global.refreshMcpSettingsAfterWrite=built.refreshMcpSettingsAfterWrite;
global.createMcpCommands=built.createMcpCommands;
global.createMcpToolSelection=built.createMcpToolSelection;
require('node:vm').runInThisContext(require('node:fs').readFileSync(MCP,'utf8'),{filename:MCP});
_renderMcpCategoryBar=()=>{};_renderMcpInstalled=()=>{};_mcpUpdateToolCount=()=>{};
_renderMcpCatalog=()=>painted.push(_mcpCatalog.map(row=>row.id));
const start=()=>{const session=beginSettingsReadSession();session.completeLoading(true);return session;};
let session=start();
const retire=()=>bridge.closeSettings();
const reopen=()=>{retire();document.getElementById('settingsModal').classList.add('open');session=start();};
const response=data=>({ok:true,json:async()=>data});
const catalog=id=>response({catalog:[{id,installed:true}],mcp_tool_summary:{total:id}});
async function main(){
 if(SCENARIO==='accepted_summary_survives_retired_read'){
  Api.mcp.connectOne=async()=>({ok:true});const command=_mcpReconnect('server');await tick();
  reopen();_populateMcpTab();await tick();retire();
  requests.catalog[1].resolve(catalog('retired-read'));await tick();
  requests.catalog[0].resolve(catalog('committed'));await command;await tick();
  assert.deepEqual(summaries,[{total:'committed'}],'a later cancelled read cannot suppress an acknowledged command summary');
 }else if(SCENARIO==='accepted_summary_order'){
  Api.mcp.connectOne=async()=>({ok:true});const first=_mcpReconnect('first');await tick();
  const second=_mcpReconnect('second');await tick();
  requests.catalog[1].resolve(catalog('newer'));await second;await tick();
  requests.catalog[0].resolve(catalog('older'));await first;await tick();
  assert.deepEqual(summaries,[{total:'newer'}],'an older completion cannot replace a published newer summary');
 }else if(SCENARIO==='accepted_refresh_coalesced'){
  Api.mcp.connectOne=async()=>({ok:true});const command=_mcpReconnect('server');await tick();
  _populateMcpTab();_populateMcpTab();await tick();
  assert.equal(requests.catalog.length,1,'normal refresh joins the post-command read rather than duplicating it');
  retire();requests.catalog[0].resolve(catalog('committed'));await command;await tick();
  assert.deepEqual(summaries,[{total:'committed'}]);
 }else if(SCENARIO==='expanded_tools_refresh'){
  _mcpToolsOpen.server=true;_mcpToolsCache.server=[{name:'old'}];
  Api.mcp.connectOne=async()=>({ok:true});const command=_mcpReconnect('server');await tick();
  requests.catalog[0].resolve(response({catalog:[{id:'server',connected:true}]}));await command;await tick();
  assert.equal(requests.tools.length,1,'a committed reconnect refreshes the expanded tool list');
  requests.tools[0].resolve(response({tools:[{name:'new'}]}));await tick();
  assert.equal(_mcpToolsCache.server[0].name,'new');
 }else if(SCENARIO==='preferences_followup_retired'){
  const request=deferred();let memoryReads=0;
  bridge.Api={userContext:{get:()=>request.promise}};bridge.refreshMemoryList=()=>{memoryReads++;};
  bridge.switchSettingsTab('preferences');await tick();retire();request.resolve({items:[]});await tick();
  assert.equal(memoryReads,0,'a retired context read cannot begin a new memory request');
 }else if(SCENARIO==='tools_inventory_retired'){
  const request=deferred();bridge.Api={tools:{inventory:()=>request.promise}};
  bridge.switchSettingsTab('tools');await tick();retire();document.getElementById('toolsInvBody').textContent='retired view';
  request.resolve({groups:[],totals:{tools:0}});await tick();assert.equal(document.getElementById('toolsInvBody').textContent,'retired view');
 }else if(SCENARIO==='accepted_command_closed'){
  const command=deferred();Api.mcp.connectOne=()=>command.promise;
  const connecting=_mcpReconnect('server');await tick();retire();const before=painted.length;
  command.resolve({ok:true,tools_count:3});await tick();
  assert.equal(requests.catalog.length,1,'an accepted command still refreshes the committed tool summary after closing');
  requests.catalog[0].resolve(catalog('committed'));await connecting;await tick();
  assert.deepEqual(summaries,[{total:'committed'}]);assert.equal(painted.length,before,'accepted command refresh does not paint a retired editor');
 }else if(SCENARIO==='catalog_coalesced'){
  _populateMcpTab();_populateMcpTab();await tick();assert.equal(requests.catalog.length,1);
  requests.catalog[0].resolve(catalog('current'));await tick();assert.equal(requests.updates.length,1);
 }else if(SCENARIO==='catalog_retired'){
  _populateMcpTab();await tick();reopen();_populateMcpTab();await tick();
  requests.catalog[1].resolve(catalog('new'));await tick();requests.catalog[0].resolve(catalog('old'));await tick();
  assert.equal(_mcpCatalog[0].id,'new');assert.deepEqual(summaries,[{total:'new'}]);
  assert.equal(requests.catalog[0].options.signal.aborted,true);
 }else if(SCENARIO==='catalog_error_retired'){
  _populateMcpTab();await tick();reopen();grid.textContent='new view';
  requests.catalog[0].reject(new Error('old failure'));await tick();assert.equal(grid.textContent,'new view');
 }else if(SCENARIO==='updates_retired'){
  _populateMcpTab();await tick();requests.catalog[0].resolve(catalog('old'));await tick();
  reopen();_populateMcpTab();await tick();requests.catalog[1].resolve(catalog('new'));await tick();
  assert.equal(requests.updates.length,2,'an old update flight cannot block the new opening');
  requests.updates[1].resolve(response({updates:{new:{latest:'2'}}}));await tick();
  requests.updates[0].resolve(response({updates:{old:{latest:'1'}}}));await tick();
  assert.deepEqual(_mcpUpdates,{new:{latest:'2'}});
 }else if(SCENARIO==='tools_coalesced'){
  _mcpLoadTools('server');_mcpLoadTools('server');await tick();assert.equal(requests.tools.length,1);
  requests.tools[0].resolve(response({tools:[{name:'tool',enabled:true}]}));await tick();assert.equal(_mcpToolsCache.server.length,1);
 }else if(SCENARIO==='tools_retired'){
  _mcpLoadTools('server');await tick();reopen();_mcpToolsCache.server=[{name:'new'}];
  requests.tools[0].resolve(response({tools:[{name:'old'}]}));await tick();assert.equal(_mcpToolsCache.server[0].name,'new');
 }else if(SCENARIO==='breaker_close'||SCENARIO==='breaker_hide'){
  _mcpCatalog=[{id:'server',installed:true,connected:false,breaker:{retry_in:4}}];
  _mcpScheduleBreakerRefresh();assert.equal(timers.size,2);
  if(SCENARIO==='breaker_close')retire();
  else {tab.classList.remove('active');if(typeof notifyMcpSettingsVisibility==='function')notifyMcpSettingsVisibility();}
  assert.equal(timers.size,0,'closing or leaving MCP releases both breaker observers immediately');
 }
 retire();dom.window.close();console.log('MCP read scenario completed');
}
main().catch(error=>{console.error(error);process.exitCode=1;dom.window.close();});
'''


@pytest.mark.parametrize('scenario', ['catalog_coalesced', 'catalog_retired', 'catalog_error_retired',
    'updates_retired', 'tools_coalesced', 'tools_retired', 'breaker_close', 'breaker_hide', 'accepted_command_closed', 'preferences_followup_retired', 'tools_inventory_retired', 'accepted_refresh_coalesced', 'expanded_tools_refresh', 'accepted_summary_survives_retired_read', 'accepted_summary_order'])
def test_settings_mcp_read_lifetime(settings_oauth_bundle, scenario):
    bindings = {'ROOT': str(ROOT), 'BUILT': str(settings_oauth_bundle), 'SCENARIO': scenario,
                'MCP': str(ROOT / 'frontend/src/runtime/sections/settings/mcp.js')}
    script = '\n'.join(f'const {key}={json.dumps(value)};' for key, value in bindings.items()) + HARNESS
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'MCP read scenario completed' in result.stdout, 'Node exited with the scenario still unsettled'
