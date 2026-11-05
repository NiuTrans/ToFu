"""Speech saves share routing CAS and preserve later settings/form edits."""
import json
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import _require_frontend_bundler

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def speech_settings_bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp('speech-settings-owner')
    entry = directory / 'entry.ts'
    entry.write_text('\n'.join([
        'export {featureRegistry as bridge} from ' + json.dumps(str(ROOT/'frontend/src/feature-registry')) + ';',
        'export {runModelRoutingMutation} from ' + json.dumps(str(ROOT/'frontend/src/features/settings/model-routing-authority')) + ';',
        'export * as speech from ' + json.dumps(str(ROOT/'frontend/src/features/settings/speech')) + ';',
        'import ' + json.dumps(str(ROOT/'frontend/src/features/settings/core-panel')) + ';',
        'import ' + json.dumps(str(ROOT/'frontend/src/features/settings/save-export')) + ';',
    ]))
    _require_frontend_bundler()
    output = directory / 'speech.cjs'
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
  <div id="settingsTab_speech"><input type="checkbox" id="settingSttEnabled" checked>
  <select id="settingSttProvider"><option value="openai">OpenAI</option></select>
  <div id="sttProviderFields"></div>
  <input id="settingSttModelOpenai" value="fixture-model">
  <input id="settingSttBaseOpenai" value="https://fixture.invalid/v1">
  <input id="settingSttKeyOpenai" value="synthetic-first"></div>
</div></body>`, {url: 'http://localhost'});
for (const name of ['window', 'document', 'localStorage', 'HTMLElement', 'HTMLInputElement',
    'HTMLButtonElement', 'HTMLSelectElement', 'HTMLTextAreaElement', 'AbortController', 'AbortSignal']) {
  global[name] = name === 'window' ? dom.window : dom.window[name];
}
const {bridge, speech, runModelRoutingMutation} = require(BUILT);
global.setTimeout = () => 1;
Object.assign(bridge, {config: {}, _featureFlags: {}, _registeredModels: [],
  _configForPersist: () => ({}), debugLog: () => {}, _serverConfig: {}});
const clone = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setImmediate(resolve));
const defer = () => {let resolve, reject; const promise = new Promise((yes, no) => {
  resolve = yes; reject = no;
}); return {promise, resolve, reject};};
const control = name => document.getElementById(name);
const key = () => control('settingSttKeyOpenai');
const model = () => control('settingSttModelOpenai');
const open = () => control('settingsModal').classList.contains('open');
let server = {contract_version: 'tofu.model-routing/v2', revision: 1,
  creators: [], models: [], providers: [{provider_id: 'other', name: 'original'}],
  provider_accesses: [{provider_access_id: 'other-access', provider_id: 'other'}],
  connections: [], credentials: [{credential_id: 'other-key', provider_access_id: 'other-access',
    kind: 'api_key', secret_reference: 'existing', key_hint: 'original'}], offerings: [], deployments: []};
function adopt() {
  bridge._stgModelRouting = clone(server);
  bridge._stgModelRoutingRevision = server.revision;
  bridge._stgModelRoutingSavedJson = JSON.stringify(server);
}
adopt(); bridge._stgPendingCredentialSecrets = {};
const calls = [], wholeWrites = [], published = [], secretWrites = [];
let reads = 0, configWrites = 0;
const response = () => ({revision: server.revision, model_routing: clone(server), reasoning_controls: []});
bridge._adoptModelRoutingResponse = value => {published.push(value); return true;};
function applyBundle(bundle) {
  for (const [field, rows, id] of [
    ['providers', [bundle.provider], 'provider_id'],
    ['provider_accesses', [bundle.provider_access], 'provider_access_id'],
    ['connections', bundle.connections, 'connection_id'], ['credentials', bundle.credentials, 'credential_id'],
    ['offerings', bundle.offerings, 'offering_id'], ['deployments', bundle.deployments, 'deployment_id'],
    ['creators', bundle.creators || [], 'creator_id'], ['models', bundle.models || [], 'model_id'],
  ]) server[field] = server[field].filter(row => !rows.some(next => next[id] === row[id])).concat(clone(rows));
  for (const row of server.credentials) if (bundle.credential_secrets?.[row.credential_id]) {
    row.secret_reference = `synthetic-ref-${server.revision}`; row.key_hint = 'synt****tail';
  }
}
const write = async (kind, bundle, revision) => {
  assert.equal(revision, server.revision, 'speech shares the actual server CAS');
  calls.push({kind, bundle: clone(bundle), revision});
  server.revision++;
  if (kind === 'delete') {
    for (const field of ['providers', 'provider_accesses', 'connections', 'credentials', 'offerings', 'deployments']) {
      server[field] = server[field].filter(row => !Object.values(row).some(value => typeof value === 'string' && value.startsWith('stt')));
    }
    return {deleted: 'stt', revision: server.revision};
  }
  applyBundle(bundle);
  return {provider: clone(bundle), revision: server.revision};
};
bridge.Api = {modelRouting: {
  get: async () => {reads++; return response();},
  replace: async (document, revision) => {
    assert.equal(revision, server.revision); wholeWrites.push(clone(document));
    server = {...clone(document), revision: revision + 1}; return response();
  },
  createProvider: (bundle, revision) => write('create', bundle, revision),
  saveProvider: (id, bundle, revision) => {assert.equal(id, 'stt'); return write('save', bundle, revision);},
  deleteProvider: (id, revision) => {assert.equal(id, 'stt'); return write('delete', {}, revision);},
  putCredentialSecret: async (id, secret, revision) => {
    assert.equal(revision, server.revision); secretWrites.push({id, secret}); server.revision++;
    server.credentials.find(row => row.credential_id === id).secret_reference = 'synthetic-replaced';
    return {credential_id: id, key_hint: 'synt****tail', revision: server.revision};
  },
}, serverConfig: {update: async () => {configWrites++; return {json: async () => ({ok: true})};}}};
function existingSpeech() {
  applyBundle(speech.buildSttProviderBundle()); adopt();
  key().value = ''; speech.populateSpeechTab();
}
function holdCreate() {
  const gate = defer();
  bridge.Api.modelRouting.createProvider = async (...args) => {
    const receipt = await write('create', ...args); await gate.promise; return receipt;
  };
  return gate;
}
async function main() {
  if (SCENARIO === 'save_during_load' || SCENARIO === 'save_after_failed_load') {
    existingSpeech();
    // The authority has loaded, but the form still displays the prior view.
    control('settingSttEnabled').checked = false;
    const loading = speech.beginSpeechTabLoad();
    if (SCENARIO === 'save_after_failed_load') loading.fail();
    await speech.persistSttProvider();
    assert.equal(calls.length, 0, 'an unavailable form cannot authorize Provider deletion');
    assert.equal(speech.hasSttChanges(), false);
    assert.equal(server.providers.some(row => row.provider_id === 'stt'), true);
  } else if (SCENARIO === 'loading_controls' || SCENARIO === 'retired_load' || SCENARIO === 'failed_load') {
    const loading = speech.beginSpeechTabLoad();
    assert.equal(control('settingSttEnabled').disabled, true);
    assert.equal(key().disabled, true);
    if (SCENARIO === 'retired_load') {
      speech.destroySpeechTab(); speech.populateSpeechTab();
      control('settingSttEnabled').checked = true; key().value = 'synthetic-new-view';
      loading.complete({});
      assert.equal(control('settingSttEnabled').checked, true);
      assert.equal(key().value, 'synthetic-new-view');
    } else if (SCENARIO === 'failed_load') {
      loading.fail(); assert.equal(control('settingSttEnabled').disabled, true);
      assert.equal(control('settingsTab_speech').hasAttribute('aria-busy'), false);
    } else {
      loading.complete({}); assert.equal(control('settingSttEnabled').disabled, false);
      assert.equal(key().disabled, false);
      assert.equal(control('settingsTab_speech').hasAttribute('aria-busy'), false);
    }
  } else if (SCENARIO === 'dirty_provider') {
    bridge._stgModelRouting.providers[0].name = 'unsaved rename';
    await bridge.saveSettings();
    assert.equal(server.providers[0].name, 'unsaved rename');
    assert.equal(server.providers.some(row => row.provider_id === 'stt'), true);
    assert.equal(wholeWrites.length, 1, 'one explicit metadata save, no redundant post-STT PUT');
    assert.equal(open(), false);
  } else if (SCENARIO === 'shared_queue' || SCENARIO === 'queued_form_edit') {
    const gate = defer();
    const prior = runModelRoutingMutation(async revision => {
      await gate.promise; assert.equal(revision, server.revision); server.revision++;
      return {revision: server.revision};
    });
    await tick(); const saving = bridge.saveSettings(); await tick();
    assert.equal(calls.length, 0);
    if (SCENARIO === 'queued_form_edit') key().value = 'synthetic-newer';
    gate.resolve(); await Promise.all([saving, prior]);
    assert.equal(calls[0].revision, 2);
    assert.equal(calls[0].bundle.credential_secrets['stt-credential'], 'synthetic-first');
    if (SCENARIO === 'queued_form_edit') {assert.equal(key().value, 'synthetic-newer'); assert.equal(open(), true);}
  } else if (SCENARIO === 'late_routing_edit') {
    const gate = holdCreate(); const saving = bridge.saveSettings(); await tick();
    const draft = bridge._stgModelRouting;
    draft.providers[0].name = 'late rename';
    bridge._stgPendingCredentialSecrets['other-key'] = 'synthetic-pending';
    gate.resolve(); await saving;
    assert.equal(bridge._stgModelRouting, draft);
    assert.equal(draft.providers[0].name, 'late rename');
    assert.equal(bridge._stgModelRoutingRevision, 1);
    assert.deepEqual(bridge._stgPendingCredentialSecrets, {'other-key': 'synthetic-pending'});
    assert.equal(wholeWrites.length, 0); assert.equal(open(), true);
    assert.equal(published.at(-1).revision, 2);
  } else if (SCENARIO === 'pending_secret') {
    bridge._stgPendingCredentialSecrets['other-key'] = 'synthetic-pending';
    await bridge.saveSettings();
    assert.deepEqual(secretWrites, [{id: 'other-key', secret: 'synthetic-pending'}]);
    assert.deepEqual(bridge._stgPendingCredentialSecrets, {});
    assert.equal(wholeWrites.length, 0); assert.equal(open(), false);
  } else if (SCENARIO === 'refresh_failure') {
    bridge.Api.modelRouting.get = async () => {throw new Error('synthetic refresh failure');};
    const result = await speech.persistSttProvider();
    assert.equal(result.refreshed, false);
    assert.equal(key().value, '', 'acknowledged plaintext must not be submitted again');
    assert.equal(calls.length, 1); assert.equal(bridge._stgModelRoutingRevision, 1);
  } else if (SCENARIO === 'invalid_receipt') {
    bridge.Api.modelRouting.createProvider = async () => null;
    await assert.rejects(speech.persistSttProvider(), /receipt|revision|response/);
    assert.equal(key().value, 'synthetic-first'); assert.equal(reads, 0);
  } else if (SCENARIO === 'missing_control') {
    existingSpeech(); control('settingSttEnabled').remove();
    await bridge.saveSettings(); assert.equal(calls.length, 0);
    assert.equal(server.providers.some(row => row.provider_id === 'stt'), true);
  } else if (SCENARIO === 'unchanged') {
    existingSpeech();
    await bridge.saveSettings();
    assert.deepEqual(calls, []); assert.deepEqual(wholeWrites, []); assert.equal(reads, 0);
    assert.equal(open(), false);
  } else if (SCENARIO === 'late_key' || SCENARIO === 'late_model') {
    const gate = holdCreate(); const saving = bridge.saveSettings(); await tick();
    if (SCENARIO === 'late_key') key().value = 'synthetic-newer';
    else model().value = 'fixture-newer-model';
    gate.resolve(); await saving;
    assert.equal(open(), true);
    if (SCENARIO === 'late_key') assert.equal(key().value, 'synthetic-newer');
    else assert.equal(key().value, '');
    await bridge.saveSettings();
    assert.equal(calls.length, 2);
    if (SCENARIO === 'late_key') assert.equal(calls[1].bundle.credential_secrets['stt-credential'], 'synthetic-newer');
    else assert.equal(calls[1].bundle.offerings[0].model.model_id, 'fixture-newer-model');
    assert.equal(key().value, ''); assert.equal(open(), false);
  } else if (SCENARIO === 'config_retry') {
    bridge.Api.serverConfig.update = async () => {throw new Error('synthetic config refusal');};
    await bridge.saveSettings(); assert.equal(open(), true); assert.equal(calls.length, 1);
    bridge.Api.serverConfig.update = async () => ({json: async () => ({ok: true})});
    await bridge.saveSettings(); assert.equal(open(), false);
    assert.equal(calls.length, 1, 'retry unrelated config cannot repeat an acknowledged STT write');
  } else if (SCENARIO === 'disabled_pending_keys' || SCENARIO === 'replaced_pending_keys') {
    existingSpeech();
    server.credentials.push({...clone(server.credentials.find(row => row.credential_id === 'stt-credential')),
      credential_id: 'stt-extra'}); adopt();
    bridge._stgPendingCredentialSecrets = {'stt-extra': 'synthetic-removed', 'other-key': 'synthetic-kept'};
    if (SCENARIO === 'disabled_pending_keys') {
      control('settingSttEnabled').checked = false;
      bridge._stgPendingCredentialSecrets['stt-credential'] = 'synthetic-removed-primary';
    } else control('settingSttBaseOpenai').value = 'https://fixture.invalid/changed';
    await speech.persistSttProvider();
    assert.deepEqual(bridge._stgPendingCredentialSecrets, SCENARIO === 'disabled_pending_keys'
      ? {'stt-extra': 'synthetic-removed', 'other-key': 'synthetic-kept', 'stt-credential': 'synthetic-removed-primary'}
      : {'other-key': 'synthetic-kept'});
  } else if (SCENARIO === 'failure_keeps_key') {
    const failed = new Error('synthetic provider refusal');
    bridge.Api.modelRouting.createProvider = async () => {throw failed;};
    await assert.rejects(speech.persistSttProvider(), error => error === failed);
    assert.equal(key().value, 'synthetic-first'); assert.equal(reads, 0);
    bridge.Api.modelRouting.createProvider = (bundle, revision) => write('create', bundle, revision);
    await speech.persistSttProvider(); assert.equal(calls.length, 1); assert.equal(key().value, '');
  } else if (SCENARIO === 'late_config_edit') {
    const gate = defer(); bridge.Api.serverConfig.update = () => gate.promise;
    const saving = bridge.saveSettings(); await tick();
    key().value = 'synthetic-after-provider-save';
    gate.resolve({json: async () => ({ok: true})}); await saving;
    assert.equal(open(), true); assert.equal(key().value, 'synthetic-after-provider-save');
  } else if (SCENARIO === 'queued_authority_gains_secret') {
    const bundle = speech.buildSttProviderBundle(), gate = defer(); key().value = '';
    const prior = runModelRoutingMutation(async revision => {
      await gate.promise; server.revision++; applyBundle(bundle); return {revision: server.revision};
    });
    await tick(); const saving = bridge.saveSettings(); await tick();
    gate.resolve(); await Promise.all([prior, saving]);
    assert.equal(calls.length, 0); assert.equal(configWrites, 1); assert.equal(open(), false);
  } else if (SCENARIO === 'disabled_unchanged') {
    existingSpeech();
    server.provider_accesses.find(row => row.provider_id === 'stt').enabled = false;
    adopt(); speech.populateSpeechTab();
    await bridge.saveSettings(); assert.equal(calls.length, 0); assert.equal(open(), false);
  } else if (SCENARIO === 'activation_queue_configuration') {
    existingSpeech();
    server.provider_accesses.find(row => row.provider_id === 'stt').enabled = false;
    adopt(); speech.populateSpeechTab();
    const gate = defer();
    const prior = runModelRoutingMutation(async revision => {
      await gate.promise;
      assert.equal(revision, server.revision);
      server.connections.find(row => row.provider_access_id === 'stt-access').base_url = 'https://fixture.invalid/newer';
      server.revision++;
      return {revision: server.revision};
    });
    await tick(); control('settingSttEnabled').checked = true;
    const saving = speech.persistSttProvider();
    gate.resolve(); await Promise.all([prior, saving]);
    assert.equal(calls.length, 0, 'a queued toggle cannot become a stale configuration replacement');
    assert.equal(wholeWrites.length, 1);
    assert.equal(server.connections.find(row => row.provider_access_id === 'stt-access').base_url,
      'https://fixture.invalid/newer');
    assert.equal(server.provider_accesses.find(row => row.provider_id === 'stt').enabled, true);
  } else if (SCENARIO === 'stored_model_option') {
    existingSpeech();
    const select = document.createElement('select'); select.id = model().id;
    select.innerHTML = '<option value="gpt-4o-transcribe">Default</option>';
    model().replaceWith(select);
    speech.populateSpeechTab();
    assert.equal(model().value, 'fixture-model', 'saved models outside presets remain selected');
    control('settingSttEnabled').checked = false; await speech.persistSttProvider();
    speech.populateSpeechTab(); control('settingSttEnabled').checked = true;
    await speech.persistSttProvider();
    assert.equal(calls.length, 0, 'restoring a saved model cannot reconstruct its bundle');
    assert.equal(wholeWrites.length, 2);
    speech.destroySpeechTab();
    assert.deepEqual(Array.from(select.options, option => option.value), ['gpt-4o-transcribe']);
  } else if (SCENARIO.startsWith('activation_')) {
    existingSpeech();
    const enabled = SCENARIO === 'activation_enable';
    const access = server.provider_accesses.find(row => row.provider_id === 'stt');
    access.enabled = !enabled;
    access.display_name = 'Owner speech access';
    server.provider_accesses.push({...clone(access), provider_access_id: 'stt-secondary', enabled: false});
    server.credentials.push({...clone(server.credentials.find(row => row.credential_id === 'stt-credential')),
      credential_id: 'stt-extra', enabled: false});
    server.offerings[0].priority = 7;
    server.deployments[0].probe_status = 'ok';
    adopt(); speech.populateSpeechTab();
    bridge._stgPendingCredentialSecrets = {'stt-extra': 'synthetic-pending', 'other-key': 'synthetic-kept'};
    const draft = bridge._stgModelRouting, original = clone(server);
    control('settingSttEnabled').checked = enabled;
    const gate = defer(), replace = bridge.Api.modelRouting.replace;
    bridge.Api.modelRouting.get = async () => {reads++; throw new Error('No redundant read after complete PUT');};
    bridge.Api.modelRouting.replace = async (document, revision) => {
      const submitted = clone(document);
      await gate.promise;
      const committed = await replace(submitted, revision);
      return SCENARIO === 'activation_invalid_response' ? {...committed, model_routing: null} : committed;
    };
    if (SCENARIO === 'activation_view_failure') {
      bridge._adoptModelRoutingResponse = () => {throw new Error('synthetic view failure');};
    }
    const saving = speech.persistSttProvider();
    await tick();
    if (SCENARIO === 'activation_failure') {
      const failed = new Error('synthetic CAS refusal');
      gate.reject(failed);
      await assert.rejects(saving, error => error === failed);
      assert.deepEqual(server, original);
      assert.equal(bridge._stgModelRouting, draft);
    } else {
      if (SCENARIO === 'activation_late_edit') draft.providers[0].name = 'later draft';
      if (SCENARIO === 'activation_reopen') {
        speech.destroySpeechTab(); speech.populateSpeechTab(); key().value = 'synthetic-reopened';
      }
      gate.resolve();
      if (SCENARIO === 'activation_invalid_response') {
        await assert.rejects(saving, /response|authority/);
      } else {
        const result = await saving;
        assert.equal(result.refreshed, !['activation_late_edit', 'activation_view_failure'].includes(SCENARIO));
      }
      const expected = clone(original); expected.revision++;
      expected.provider_accesses.find(row => row.provider_access_id === access.provider_access_id).enabled = enabled;
      assert.deepEqual(server, expected, 'activation changes only its access flag and CAS revision');
      assert.equal(wholeWrites.length, 1);
      if (SCENARIO === 'activation_late_edit') {
        assert.equal(bridge._stgModelRouting, draft);
        assert.equal(draft.providers[0].name, 'later draft');
        assert.equal(bridge._stgModelRoutingRevision, original.revision, 'later drafts retain their original CAS');
      } else if (SCENARIO === 'activation_reopen') assert.equal(key().value, 'synthetic-reopened');
      else if (SCENARIO === 'activation_round_trip') {
        speech.populateSpeechTab(); control('settingSttEnabled').checked = true;
        await speech.persistSttProvider();
        assert.deepEqual(server, {...original, revision: original.revision + 2});
        assert.equal(wholeWrites.length, 2);
      }
    }
    assert.equal(reads, 0, 'a complete mutation response needs no projection GET');
    assert.equal(calls.length, 0, 'activation cannot rebuild or delete the Provider bundle');
    assert.deepEqual(bridge._stgPendingCredentialSecrets,
      {'stt-extra': 'synthetic-pending', 'other-key': 'synthetic-kept'});
  } else if (SCENARIO === 'feature_wait_capture' || SCENARIO === 'feature_wait_reopen') {
    speech.populateSpeechTab(); control('settingSttEnabled').checked = true; key().value = 'synthetic-first';
    control('settingsModal').insertAdjacentHTML('beforeend', '<input id="settingTradingEnabled" type="checkbox" checked>');
    const gate = defer();
    bridge.Api.features = {set: async () => {await gate.promise; return {json: async () => ({ok: true})};}};
    const saving = bridge.saveSettings(); await tick();
    assert.equal(calls.length, 0, 'the earlier feature acknowledgement still owns command order');
    if (SCENARIO === 'feature_wait_reopen') {
      bridge.closeSettings(); speech.populateSpeechTab(); control('settingsModal').classList.add('open');
      control('settingSttEnabled').checked = true;
    }
    key().value = 'synthetic-newer'; gate.resolve(); await saving;
    assert.equal(calls.length, 1);
    assert.equal(calls[0].bundle.credential_secrets['stt-credential'], 'synthetic-first');
    assert.equal(key().value, 'synthetic-newer'); assert.equal(open(), true);
  } else if (SCENARIO === 'close_during_put') {
    speech.populateSpeechTab(); control('settingSttEnabled').checked = true; key().value = 'synthetic-first';
    const gate = holdCreate(); const saving = bridge.saveSettings(); await tick();
    bridge.closeSettings(); assert.equal(key().value, '');
    speech.populateSpeechTab(); control('settingsModal').classList.add('open');
    control('settingSttEnabled').checked = true; key().value = 'synthetic-reopened';
    gate.resolve(); await saving;
    assert.equal(calls.length, 1); assert.equal(key().value, 'synthetic-reopened'); assert.equal(open(), true);
  }
}
main().then(() => console.log('SPEECH_LIFECYCLE_COMPLETE')).catch(error => {
  console.error(error); process.exitCode = 1;
}).finally(() => {speech.destroySpeechTab(); dom.window.close();});
'''


@pytest.mark.parametrize('scenario', [
    'dirty_provider', 'shared_queue', 'queued_form_edit', 'late_routing_edit',
    'pending_secret', 'refresh_failure', 'invalid_receipt', 'missing_control',
    'unchanged', 'late_key', 'late_model', 'config_retry', 'close_during_put',
    'disabled_pending_keys', 'replaced_pending_keys', 'failure_keeps_key',
    'late_config_edit', 'queued_authority_gains_secret', 'disabled_unchanged',
    'loading_controls', 'retired_load', 'failed_load',
    'save_during_load', 'save_after_failed_load',
    'activation_disable', 'activation_enable', 'activation_round_trip',
    'activation_failure', 'activation_invalid_response', 'activation_late_edit',
    'activation_view_failure', 'activation_reopen',
    'activation_queue_configuration', 'stored_model_option', 'feature_wait_capture', 'feature_wait_reopen',
])
def test_speech_save_lifecycle(speech_settings_bundle, scenario):
    script = HARNESS.replace('ROOT', json.dumps(str(ROOT))).replace(
        'BUILT', json.dumps(str(speech_settings_bundle))).replace('SCENARIO', json.dumps(scenario))
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True,
                            text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'SPEECH_LIFECYCLE_COMPLETE' in result.stdout
