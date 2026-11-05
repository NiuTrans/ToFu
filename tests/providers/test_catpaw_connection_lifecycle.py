"""Catpaw connection intent survives late reads and shared routing writes."""
import json
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import _require_frontend_bundler

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def catpaw_bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp('catpaw-settings-owner')
    entry = directory / 'entry.ts'
    entry.write_text('\n'.join([
        'export {featureRegistry as bridge} from ' + json.dumps(str(ROOT/'frontend/src/feature-registry')) + ';',
        'export {catpawSettings} from ' + json.dumps(str(ROOT/'frontend/src/features/settings/catpaw-settings')) + ';',
        'export {runModelRoutingMutation} from ' + json.dumps(str(ROOT/'frontend/src/features/settings/model-routing-authority')) + ';',
        'import ' + json.dumps(str(ROOT/'frontend/src/features/settings/core-panel')) + ';',
        'import ' + json.dumps(str(ROOT/'frontend/src/features/settings/save-export')) + ';',
    ]))
    _require_frontend_bundler()
    output = directory / 'catpaw.cjs'
    result = subprocess.run(['node', 'scripts/vite_test_bundle.mjs', str(entry),
        '--bundle', '--platform=node', '--format=cjs', f'--outfile={output}'],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return output


HARNESS = r'''
const assert = require('node:assert/strict');
const {JSDOM} = require(require('node:path').join(ROOT, 'node_modules/jsdom'));
const dom = new JSDOM(`<!doctype html><body><div id="settingsModal" class="open">
  <div id="oauthCatpawCard"><span id="oauthCatpawStatus"></span>
  <button id="oauthCatpawImportBtn"></button><button id="oauthCatpawDisconnectBtn"></button>
  <span id="oauthCatpawHint"></span></div></div></body>`, {url: 'http://localhost'});
for (const name of ['window', 'document', 'localStorage', 'HTMLElement', 'HTMLInputElement',
    'HTMLButtonElement', 'HTMLSelectElement', 'HTMLTextAreaElement', 'MutationObserver', 'AbortController', 'AbortSignal']) {
  global[name] = name === 'window' ? dom.window : dom.window[name];
}
const {bridge, catpawSettings: catpaw, runModelRoutingMutation} = require(BUILT);
const tick = () => new Promise(resolve => setImmediate(resolve));
const clone = value => JSON.parse(JSON.stringify(value));
const defer = () => {let resolve, reject; const promise = new Promise((yes, no) => {
  resolve = yes; reject = no;
}); return {promise, resolve, reject};};
const timers = new Map(); let timerId = 0;
dom.window.setTimeout = callback => {timers.set(++timerId, callback); return timerId;};
dom.window.clearTimeout = id => timers.delete(id);
const advance = async () => {
  const ready = [...timers.values()]; timers.clear(); ready.forEach(callback => callback());
  for (let i=0;i<6;i++) await tick();
};
const template = {
  providers: [{provider_id: 'catpaw', name: 'Catpaw', brand: 'catpaw'}],
  provider_accesses: [{provider_access_id: 'catpaw-access', provider_id: 'catpaw', enabled: true}],
  connections: [{connection_id: 'catpaw-connection', provider_access_id: 'catpaw-access'}],
  credentials: [{credential_id: 'catpaw-key', provider_access_id: 'catpaw-access', secret_reference: 'synthetic-stored'}],
};
let server = {contract_version: 'tofu.model-routing/v2', revision: 1,
  creators: [], models: [], providers: [], provider_accesses: [], connections: [], credentials: [],
  offerings: [], deployments: [], ...clone(template)};
function adopt() {
  bridge._stgModelRouting = clone(server); bridge._stgModelRoutingRevision = server.revision;
  bridge._stgModelRoutingSavedJson = JSON.stringify(server);
}
function absent() {
  for (const field of Object.keys(template)) server[field] = [];
  adopt();
}
adopt(); bridge._stgPendingCredentialSecrets = {};
let deletes=0, imports=0, statuses=0, logins=0, confirmations=0, available=true, renders=0;
const alerts=[], opened=[], writes=[], out={};
const response = () => ({model_routing: clone(server), revision: server.revision, reasoning_controls: []});
const status = () => ({available, connected: server.providers.some(row => row.brand === 'catpaw'),
  installed: true, model_count: 3, revision: server.revision});
bridge._adoptModelRoutingResponse = () => true;
bridge._renderModelRoutingTabs = () => {renders++;};
bridge.showConfirm = async () => {confirmations++; return true;};
bridge.showAlert = message => {alerts.push(message);};
dom.window.open = url => {opened.push(url);};
bridge.Api = {modelRouting: {
  get: async () => response(),
  replace: async (document, revision) => {
    assert.equal(revision, server.revision); writes.push('whole');
    server = {...clone(document), revision: revision+1}; return response();
  },
  deleteProvider: async (id, revision) => {
    assert.equal(id, 'catpaw'); assert.equal(revision, server.revision);
    deletes++; writes.push('delete'); server.revision++;
    for (const field of Object.keys(template)) server[field] = [];
    return {deleted: id, revision: server.revision};
  },
}, oauth: {
  catpawImportStatus: async () => {statuses++; return status();},
  catpawImportApply: async () => {
    imports++; writes.push('import'); server.revision++;
    for (const field of Object.keys(template)) server[field] = clone(template[field]);
    return {json: async () => ({provider_id: 'catpaw', connected: true, model_count: 3,
      created: true, rotated: false, revision: server.revision})};
  },
  catpawImportLogin: async () => {logins++; return {started: true, installed: true, login_url: 'https://fixture.invalid/login'};},
}};
const connectButton = () => document.getElementById('oauthCatpawImportBtn');
const disconnectButton = () => document.getElementById('oauthCatpawDisconnectBtn');
async function main() {
SCENARIO
}
main().then(() => console.log(JSON.stringify(out))).catch(error => {
  console.error(error); process.exitCode=1;
}).finally(() => {catpaw.close(); dom.window.close();});
'''


def catpaw_view(bundle, scenario):
    script = HARNESS.replace('ROOT', json.dumps(str(ROOT))).replace('BUILT', json.dumps(str(bundle)))
    result = subprocess.run(['node', '-e', script.replace('SCENARIO', scenario)],
                            cwd=ROOT, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_explicit_disconnect_cannot_auto_import_again(catpaw_bundle):
    out = catpaw_view(catpaw_bundle, r'''
  await catpaw.open(); await catpaw.disconnect(); await catpaw.refresh();
  bridge.closeSettings(); await catpaw.open();
  Object.assign(out, {deletes, imports, connected: server.providers.some(row => row.brand === 'catpaw')});
''')
    assert out == {'deletes': 1, 'imports': 0, 'connected': False}


def test_disconnect_promise_owns_its_delete_settlement(catpaw_bundle):
    out = catpaw_view(catpaw_bundle, r'''
  await catpaw.open();
  const gate=defer(), remove=bridge.Api.modelRouting.deleteProvider; let settled=false;
  bridge.Api.modelRouting.deleteProvider = async (...args) => {await gate.promise; return remove(...args);};
  const pending=catpaw.disconnect().then(() => {settled=true;});
  await tick(); out.before=settled;
  gate.resolve(); await pending; out.after=settled; out.deletes=deletes;
''')
    assert out == {'before': False, 'after': True, 'deletes': 1}


@pytest.mark.parametrize('scenario', ['initial_auto', 'coalesce', 'manual_reconnect', 'stale_status',
    'cancel_confirm', 'stale_confirm', 'close_confirm', 'delete_failure', 'delete_refresh_failure',
    'import_refresh_failure', 'shared_queue', 'dirty_draft', 'poll_success', 'poll_close',
    'poll_bound', 'late_login', 'close_import', 'reopen_import', 'read_coalesce', 'view_failure',
    'removed_card', 'invalid_receipt', 'generic_removal', 'reopen_confirmation', 'initial_delete_race', 'slow_poll_expires'])
def test_catpaw_connection_boundaries(catpaw_bundle, scenario):
    out = catpaw_view(catpaw_bundle, CASES[scenario])
    assert out == {'complete': True}


CASES = {
'slow_poll_expires': r'''
  absent(); available=false; await catpaw.open(); await catpaw.connect();
  const hint=document.getElementById('oauthCatpawHint'), waitingText=hint.textContent, gate=defer();
  bridge.Api.oauth.catpawImportStatus=()=>gate.promise;
  for(let i=0;i<65;i++) await advance();
  const expired=hint.textContent!==waitingText;
  gate.resolve({available:true,connected:false}); await tick();
  assert.equal(expired,true); assert.equal(timers.size,0); assert.equal(imports,0); out.complete=true;
''',
'initial_delete_race': r'''
  const gate=defer(), read=bridge.Api.oauth.catpawImportStatus;
  bridge.Api.oauth.catpawImportStatus=()=>gate.promise;
  const opening=catpaw.open(); await tick();
  await catpaw.providerRemoval(() => runModelRoutingMutation(
    revision => bridge.Api.modelRouting.deleteProvider('catpaw',revision)));
  bridge.Api.oauth.catpawImportStatus=read;
  gate.resolve({available:true,connected:false}); await opening;
  assert.equal(imports,0); assert.equal(deletes,1); out.complete=true;
''',
'generic_removal': r'''
  await catpaw.open();
  await runModelRoutingMutation(revision => bridge.Api.modelRouting.deleteProvider('catpaw',revision));
  bridge.closeSettings(); await catpaw.open();
  assert.equal(imports,0); assert.equal(deletes,1); out.complete=true;
''',
'reopen_confirmation': r'''
  await catpaw.open(); const gate=defer(); bridge.showConfirm=()=>gate.promise;
  const pending=catpaw.disconnect(); await tick(); bridge.closeSettings();
  let ready=false; const opening=catpaw.open().then(()=>{ready=true;});
  for(let i=0;i<6;i++) await tick();
  const readyBeforeOldConfirmation=ready;
  gate.resolve(true); await Promise.all([pending,opening]);
  assert.equal(readyBeforeOldConfirmation,true); assert.equal(deletes,0); out.complete=true;
''',
'initial_auto': r'''
  absent(); await catpaw.open(); await catpaw.refresh();
  assert.equal(imports,1); assert.equal(bridge._stgModelRoutingRevision,2);
  assert.equal(connectButton().style.display,'none'); out.complete=true;
''',
'coalesce': r'''
  absent(); available=false; await catpaw.open(); available=true;
  const gate=defer(), apply=bridge.Api.oauth.catpawImportApply;
  bridge.Api.oauth.catpawImportApply=async () => {await gate.promise; return apply();};
  const first=catpaw.connect(), second=catpaw.connect(); await tick();
  assert.equal(connectButton().disabled,true); gate.resolve(); await Promise.all([first,second]);
  assert.equal(imports,1); assert.equal(connectButton().disabled,false); out.complete=true;
''',
'manual_reconnect': r'''
  await catpaw.open(); await catpaw.disconnect(); await catpaw.refresh();
  assert.equal(imports,0); await catpaw.connect(); assert.equal(imports,1);
  assert.deepEqual(writes,['delete','import']); out.complete=true;
''',
'stale_status': r'''
  await catpaw.open(); const gate=defer(), read=bridge.Api.oauth.catpawImportStatus;
  bridge.Api.oauth.catpawImportStatus=() => gate.promise;
  const reading=catpaw.refresh(); await tick(); bridge.Api.oauth.catpawImportStatus=read;
  await catpaw.disconnect(); gate.resolve({available:true,connected:false}); await reading;
  assert.equal(imports,0); assert.equal(deletes,1); out.complete=true;
''',
'cancel_confirm': r'''
  await catpaw.open(); bridge.showConfirm=async()=>false; await catpaw.disconnect();
  assert.equal(deletes,0); assert.equal(disconnectButton().style.display,''); out.complete=true;
''',
'stale_confirm': r'''
  await catpaw.open(); const gate=defer(); bridge.showConfirm=()=>gate.promise;
  const pending=catpaw.disconnect(); await tick(); adopt(); gate.resolve(true); await pending;
  assert.equal(deletes,0); assert.equal(alerts.length,1); out.complete=true;
''',
'close_confirm': r'''
  await catpaw.open(); const gate=defer(); bridge.showConfirm=()=>gate.promise;
  const pending=catpaw.disconnect(); await tick(); bridge.closeSettings(); gate.resolve(true); await pending;
  assert.equal(deletes,0); out.complete=true;
''',
'delete_failure': r'''
  await catpaw.open(); const remove=bridge.Api.modelRouting.deleteProvider;
  bridge.Api.modelRouting.deleteProvider=async()=>{throw new Error('synthetic refusal');};
  await catpaw.disconnect(); assert.equal(alerts.length,1); assert.equal(imports,0);
  bridge.Api.modelRouting.deleteProvider=remove; await catpaw.disconnect(); assert.equal(deletes,1); out.complete=true;
''',
'delete_refresh_failure': r'''
  await catpaw.open(); bridge.Api.modelRouting.get=async()=>{throw new Error('synthetic read failure');};
  await catpaw.disconnect(); await catpaw.refresh();
  assert.equal(deletes,1); assert.equal(imports,0); assert.equal(alerts.length,1); out.complete=true;
''',
'import_refresh_failure': r'''
  absent(); bridge.Api.modelRouting.get=async()=>{throw new Error('synthetic read failure');};
  await catpaw.open(); await catpaw.refresh();
  assert.equal(imports,1); assert.equal(alerts.length,1); out.complete=true;
''',
'shared_queue': r'''
  await catpaw.open(); const gate=defer();
  const prior=runModelRoutingMutation(async revision=>{await gate.promise; assert.equal(revision,1);
    server.revision++; writes.push('prior'); return {revision:server.revision};});
  const pending=catpaw.disconnect(); await tick(); assert.equal(deletes,0);
  gate.resolve(); await Promise.all([prior,pending]); assert.deepEqual(writes,['prior','delete']); out.complete=true;
''',
'dirty_draft': r'''
  await catpaw.open(); const draft=bridge._stgModelRouting;
  draft.models.push({model_id:'pending',creator_id:'fixture'});
  bridge._stgPendingCredentialSecrets={'catpaw-key':'synthetic-removed','other-key':'synthetic-kept'};
  await catpaw.disconnect(); assert.equal(deletes,1); assert.equal(bridge._stgModelRouting,draft);
  assert.equal(bridge._stgModelRoutingRevision,1);
  assert.deepEqual(bridge._stgPendingCredentialSecrets,{'other-key':'synthetic-kept'}); out.complete=true;
''',
'poll_success': r'''
  absent(); available=false; await catpaw.open(); await catpaw.connect();
  assert.equal(logins,1); assert.equal(timers.size,1); assert.equal(opened.length,1);
  available=true; await advance(); assert.equal(imports,1); assert.equal(timers.size,0); out.complete=true;
''',
'poll_close': r'''
  absent(); available=false; await catpaw.open(); await catpaw.connect();
  const gate=defer(); bridge.Api.oauth.catpawImportStatus=()=>gate.promise;
  await advance(); bridge.closeSettings(); gate.resolve({available:true,connected:false}); await tick();
  assert.equal(imports,0); assert.equal(timers.size,0); out.complete=true;
''',
'poll_bound': r'''
  absent(); available=false; await catpaw.open(); await catpaw.connect();
  for(let i=0;i<65;i++) await advance();
  assert.equal(statuses,62); assert.equal(timers.size,0); assert.equal(imports,0); out.complete=true;
''',
'late_login': r'''
  absent(); available=false; await catpaw.open(); const gate=defer();
  bridge.Api.oauth.catpawImportLogin=()=>gate.promise;
  const pending=catpaw.connect(); await tick(); bridge.closeSettings();
  gate.resolve({started:true,installed:true,login_url:'https://fixture.invalid/late'}); await pending;
  assert.equal(opened.length,0); assert.equal(timers.size,0); out.complete=true;
''',
'close_import': r'''
  absent(); available=false; await catpaw.open(); available=true;
  const gate=defer(), apply=bridge.Api.oauth.catpawImportApply;
  bridge.Api.oauth.catpawImportApply=async()=>{await gate.promise; return apply();};
  const pending=catpaw.connect(); await tick(); bridge.closeSettings(); gate.resolve(); await pending;
  assert.equal(imports,1); assert.equal(bridge._stgModelRoutingRevision,2); assert.equal(timers.size,0); out.complete=true;
''',
'reopen_import': r'''
  absent(); available=false; await catpaw.open(); available=true;
  const gate=defer(), apply=bridge.Api.oauth.catpawImportApply;
  bridge.Api.oauth.catpawImportApply=async()=>{await gate.promise; return apply();};
  const pending=catpaw.connect(); await tick(); bridge.closeSettings(); const reopening=catpaw.open();
  gate.resolve(); await Promise.all([pending,reopening]); assert.equal(imports,1);
  assert.equal(connectButton().style.display,'none'); assert.equal(connectButton().disabled,false); out.complete=true;
''',
'read_coalesce': r'''
  await catpaw.open(); const before=statuses;
  await Promise.all([catpaw.refresh(),catpaw.refresh(),catpaw.refresh()]);
  assert.equal(statuses-before,1); out.complete=true;
''',
'view_failure': r'''
  await catpaw.open(); bridge._renderModelRoutingTabs=()=>{throw new Error('synthetic renderer fault');};
  await catpaw.disconnect(); assert.equal(deletes,1); assert.equal(alerts.length,1); assert.equal(imports,0); out.complete=true;
''',
'removed_card': r'''
  absent(); available=false; await catpaw.open(); await catpaw.connect();
  document.getElementById('oauthCatpawCard').remove(); await tick();
  assert.equal(timers.size,0); available=true; await catpaw.refresh(); assert.equal(imports,0); out.complete=true;
''',
'invalid_receipt': r'''
  absent(); available=false; await catpaw.open(); available=true;
  bridge.Api.oauth.catpawImportApply=async()=>({json:async()=>({connected:true,provider_id:'catpaw',revision:1})});
  await catpaw.connect(); assert.equal(alerts.length,1); assert.equal(bridge._stgModelRoutingRevision,1);
  assert.equal(connectButton().style.display,''); out.complete=true;
''',
}
