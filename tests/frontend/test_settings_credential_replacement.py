"""Credential replacement settles per key through the common routing owner."""
import json
import subprocess
from pathlib import Path

import pytest

from tests.frontend.test_settings_save_feedback import settings_bundle as settings_bundle

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]

HARNESS = r'''
const assert = require('node:assert/strict');
global.window = globalThis;
global.localStorage = {getItem: () => null, setItem: () => {}};
global.HTMLInputElement = class {value = ''; checked = false;};
global.HTMLSelectElement = class extends HTMLInputElement {};
global.HTMLTextAreaElement = class extends HTMLInputElement {};
global.HTMLButtonElement = class {disabled = false;};
global.setTimeout = () => 1;
let closed = 0, configWrites = 0;
const hint = {textContent: ''}, button = new HTMLButtonElement();
const elements = {settingsSaveBtn: button, settingsStatusHint: hint,
  settingsModal: {classList: {remove: () => closed++}}};
global.document = {getElementById: id => elements[id] || null, documentElement: {lang: 'en'}};
const {bridge, t, runModelRoutingMutation} = require(BUILT);
Object.assign(bridge, {config: {}, _featureFlags: {}, _registeredModels: [],
  _configForPersist: () => ({}), debugLog: () => {}, _serverConfig: {}});
const clone = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setImmediate(resolve));
const defer = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
};
let server = {contract_version: 'tofu.model-routing/v2', revision: 1, models: [],
  credentials: ['a', 'b'].map(credential_id => ({credential_id,
    secret_reference: `ref-${credential_id}-1`, key_hint: 'old'}))};
bridge._stgModelRouting = clone(server);
bridge._stgModelRoutingRevision = 1;
bridge._stgModelRoutingSavedJson = JSON.stringify(server);
bridge._stgPendingCredentialSecrets = {a: 'synthetic-first'};
const writes = [], reads = [], published = [];
bridge._adoptModelRoutingResponse = response => {published.push(response); return true;};
const response = () => ({revision: server.revision, model_routing: clone(server)});
const put = async (id, secret, revision) => {
  writes.push({id, secret, revision});
  assert.equal(revision, server.revision, 'every write uses server CAS');
  server.revision++;
  const row = server.credentials.find(row => row.credential_id === id);
  row.secret_reference = `ref-${id}-${server.revision}`;
  row.key_hint = `hint-${server.revision}`;
  return {credential_id: id, key_hint: row.key_hint, revision: server.revision};
};
bridge.Api = {
  modelRouting: {
    get: async () => {reads.push(server.revision); return response();},
    replace: async (draft, revision) => {
      assert.equal(revision, server.revision);
      server = {...clone(draft), revision: revision + 1};
      return response();
    },
    putCredentialSecret: put,
  },
  serverConfig: {update: async () => {configWrites++; return {json: async () => ({ok: true})};}},
};
function expectKept(refreshed = true) {
  assert.equal(closed, 0, 'new editor work or an unrefreshed receipt keeps Settings open');
  assert.equal(button.disabled, false);
  assert.equal(hint.textContent, refreshed
    ? `${t('settings.saved')} · ${t('context.unsaved')}` : t('settings.providerSavedDraftKept'));
}
async function lateEdit(phase, edit, refreshed = true) {
  const gate = defer();
  if (phase === 'put') bridge.Api.modelRouting.putCredentialSecret = async (...args) => {
    const receipt = await put(...args); await gate.promise; return receipt;
  };
  else bridge.Api.modelRouting.get = async () => {reads.push(server.revision); await gate.promise; return response();};
  const saving = bridge.saveSettings(); await tick();
  edit(); gate.resolve(); await saving;
  expectKept(refreshed);
}
async function main() {
  if (SCENARIO === 'late_value' || SCENARIO === 'late_key' || SCENARIO === 'late_refresh') {
    const id = SCENARIO === 'late_key' ? 'b' : 'a';
    await lateEdit(SCENARIO === 'late_refresh' ? 'get' : 'put', () => {
      bridge._stgPendingCredentialSecrets[id] = 'synthetic-newer';
    });
    assert.deepEqual(bridge._stgPendingCredentialSecrets, {[id]: 'synthetic-newer'});
    assert.deepEqual(writes, [{id: 'a', secret: 'synthetic-first', revision: 1}]);
    assert.equal(bridge._stgModelRoutingRevision, 2);
    bridge.Api.modelRouting.putCredentialSecret = put;
    await bridge.saveSettings();
    assert.equal(writes.length, 2);
    assert.deepEqual(writes[1], {id, secret: 'synthetic-newer', revision: 2});
    assert.deepEqual(bridge._stgPendingCredentialSecrets, {});
    assert.equal(closed, 1);
  } else if (SCENARIO === 'partial_failure' || SCENARIO === 'partial_failure_refresh') {
    bridge._stgPendingCredentialSecrets.b = 'synthetic-second';
    bridge.Api.modelRouting.putCredentialSecret = async (...args) => {
      if (args[0] === 'b') throw new Error('second credential denied');
      return put(...args);
    };
    if (SCENARIO === 'partial_failure_refresh') bridge.Api.modelRouting.get = async () => {
      throw new Error('refresh unavailable');
    };
    await bridge.saveSettings();
    assert.deepEqual(bridge._stgPendingCredentialSecrets, {b: 'synthetic-second'});
    assert.equal(bridge._stgModelRoutingRevision, SCENARIO === 'partial_failure' ? 2 : 1);
    assert.equal(configWrites, 0);
    assert.equal(closed, 0); assert.match(hint.textContent, /second credential denied/);
    if (SCENARIO === 'partial_failure_refresh') return;
    bridge.Api.modelRouting.putCredentialSecret = put;
    await bridge.saveSettings();
    assert.deepEqual(writes.map(row => [row.id, row.revision]), [['a', 1], ['b', 2]]);
    assert.equal(closed, 1);
  } else if (SCENARIO.startsWith('invalid_')) {
    bridge.Api.modelRouting.putCredentialSecret = async () => ({
      invalid_null: null, invalid_revision: {credential_id: 'a', key_hint: 'x', revision: 1},
      invalid_identity: {credential_id: 'b', key_hint: 'x', revision: 2},
    })[SCENARIO];
    await bridge.saveSettings();
    assert.deepEqual(bridge._stgPendingCredentialSecrets, {a: 'synthetic-first'});
    assert.equal(configWrites, 0); assert.equal(closed, 0);
    assert.match(hint.textContent, /保存失败/);
  } else if (SCENARIO === 'view_failure') {
    let faults = 0;
    bridge._adoptProviderCredentialDraft = () => {faults++; throw new Error('view failure');};
    await bridge.saveSettings(); expectKept(false);
    assert.equal(faults, 1); assert.equal(writes.length, 1);
    assert.deepEqual(bridge._stgPendingCredentialSecrets, {});
    assert.equal(bridge._stgModelRoutingRevision, 2);
  } else if (SCENARIO === 'refresh_failure' || SCENARIO === 'stale_refresh') {
    const original = bridge._stgModelRouting;
    bridge.Api.modelRouting.get = async () => {
      if (SCENARIO === 'refresh_failure') throw new Error('refresh unavailable');
      return {revision: 1, model_routing: original};
    };
    await bridge.saveSettings(); expectKept(false);
    assert.deepEqual(bridge._stgPendingCredentialSecrets, {}, 'acknowledged secret must not be retried');
    assert.equal(bridge._stgModelRouting, original);
    assert.equal(bridge._stgModelRoutingRevision, 1, 'partial receipt does not rebase stale document');
    assert.equal(writes.length, 1);
  } else if (SCENARIO === 'dirty_during_put' || SCENARIO === 'dirty_during_get') {
    const draft = bridge._stgModelRouting;
    await lateEdit(SCENARIO === 'dirty_during_put' ? 'put' : 'get', () => {draft.models.push({model_id: 'late-model'});}, false);
    assert.equal(bridge._stgModelRouting, draft);
    assert.deepEqual(draft.models, [{model_id: 'late-model'}]);
    assert.equal(bridge._stgModelRoutingRevision, 1);
    assert.deepEqual(bridge._stgPendingCredentialSecrets, {});
    assert.equal(published.at(-1).revision, 2, 'composer gets full committed projection');
  } else if (SCENARIO === 'replaced_draft') {
    bridge._stgPendingCredentialSecrets.b = 'synthetic-second';
    let newer;
    await lateEdit('put', () => {
      newer = bridge._stgModelRouting = clone(bridge._stgModelRouting);
      bridge._stgPendingCredentialSecrets = {a: 'synthetic-first', b: 'synthetic-second'};
    }, false);
    assert.equal(bridge._stgModelRouting, newer);
    assert.deepEqual(bridge._stgPendingCredentialSecrets, {a: 'synthetic-first', b: 'synthetic-second'});
    assert.equal(writes.length, 1, 'retired editor cannot submit the rest of its batch');
    assert.equal(bridge._stgModelRoutingRevision, 1);
  } else if (SCENARIO === 'changed_unsent_key') {
    bridge._stgPendingCredentialSecrets.b = 'synthetic-second';
    await lateEdit('put', () => {bridge._stgPendingCredentialSecrets.b = 'synthetic-newer';});
    assert.deepEqual(writes.map(row => row.id), ['a']);
    assert.deepEqual(bridge._stgPendingCredentialSecrets, {b: 'synthetic-newer'});
  } else if (SCENARIO === 'replaced_pending_map') {
    bridge._stgPendingCredentialSecrets.b = 'synthetic-second';
    const newer = {...bridge._stgPendingCredentialSecrets};
    await lateEdit('put', () => {bridge._stgPendingCredentialSecrets = newer;});
    assert.equal(bridge._stgPendingCredentialSecrets, newer);
    assert.deepEqual(newer, {a: 'synthetic-first', b: 'synthetic-second'});
    assert.equal(writes.length, 1);
  } else if (SCENARIO === 'late_config_edit') {
    const gate = defer();
    bridge.Api.serverConfig.update = () => gate.promise;
    const saving = bridge.saveSettings(); await tick();
    bridge._stgPendingCredentialSecrets.b = 'synthetic-late';
    gate.resolve({json: async () => ({ok: true})}); await saving;
    expectKept(); assert.deepEqual(bridge._stgPendingCredentialSecrets, {b: 'synthetic-late'});
  } else if (SCENARIO === 'shared_queue') {
    const gate = defer(); let providerWrites = 0;
    bridge.Api.modelRouting.putCredentialSecret = async (...args) => {
      const receipt = await put(...args); await gate.promise; return receipt;
    };
    const saving = bridge.saveSettings(); await tick();
    const mutation = runModelRoutingMutation(async revision => {
      providerWrites++; assert.equal(revision, server.revision);
      server.revision++; return {revision: server.revision};
    });
    await tick(); assert.equal(providerWrites, 0, 'Provider waits for credential batch and projection');
    gate.resolve(); await Promise.all([saving, mutation]);
    assert.equal(providerWrites, 1); assert.equal(server.revision, 3);
  } else if (SCENARIO === 'before_secret_save') {
    const gate = defer();
    bridge._stgModelRouting.models.push({model_id: 'staged'});
    bridge.Api.modelRouting.replace = async (draft, revision) => {
      await gate.promise; server = {...clone(draft), revision: revision + 1}; return response();
    };
    const saving = bridge.saveSettings(); await tick();
    assert.equal(writes.length, 0);
    gate.resolve(); await saving;
    assert.deepEqual(writes.map(row => row.revision), [2]);
    assert.equal(bridge._stgModelRoutingRevision, 3); assert.equal(closed, 1);
  } else if (SCENARIO === 'closed_during_put') {
    const gate = defer();
    bridge.Api.modelRouting.putCredentialSecret = async (...args) => {
      const receipt = await put(...args); await gate.promise; return receipt;
    };
    const saving = bridge.saveSettings(); await tick(); bridge.closeSettings();
    gate.resolve(); await saving;
    assert.equal(writes.length, 1); assert.deepEqual(bridge._stgPendingCredentialSecrets, {});
    assert.equal(server.revision, 2);
  }
}
main().then(() => console.log('CREDENTIAL_REPLACEMENT_COMPLETE')).catch(error => {console.error(error); process.exitCode = 1;});
'''


@pytest.mark.parametrize('scenario', [
    'late_value', 'late_key', 'late_refresh', 'partial_failure', 'partial_failure_refresh',
    'invalid_null', 'invalid_revision', 'invalid_identity',
    'refresh_failure', 'stale_refresh', 'dirty_during_put', 'dirty_during_get', 'replaced_draft',
    'changed_unsent_key', 'replaced_pending_map', 'late_config_edit', 'view_failure',
    'shared_queue', 'before_secret_save', 'closed_during_put',
])
def test_settings_credential_replacement(settings_bundle, scenario):
    script = HARNESS.replace('BUILT', json.dumps(str(settings_bundle))).replace(
        'SCENARIO', json.dumps(scenario))
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'CREDENTIAL_REPLACEMENT_COMPLETE' in result.stdout
