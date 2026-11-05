"""Committed responses reach the composer without replacing newer editor work."""
import json
import subprocess
from pathlib import Path

import pytest

from tests.frontend.test_settings_save_feedback import settings_bundle as settings_bundle

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('scenario', [
    'read_publish', 'persist_publish', 'read_after_save', 'failed_read_after_save',
    'read_during_edit', 'persist_during_edit', 'sync_read_failure', 'persist_failure',
    'subscription_refresh_during_edit', 'mutation_refresh_after_hint_adoption',
    'mutation_refresh_during_edit',
])
def test_settings_routing_response_lifecycle(settings_bundle, scenario):
    harness = r'''
const assert = require('node:assert/strict');
global.window = globalThis;
global.localStorage = {getItem: () => null, setItem: () => {}};
global.document = {getElementById: () => null, documentElement: {lang: 'en'}};
const {bridge} = require(BUILT);
bridge.debugLog = () => {};
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
};
const tick = () => new Promise(resolve => setImmediate(resolve));
const clone = value => JSON.parse(JSON.stringify(value));
const original = {contract_version: 'tofu.model-routing/v2', revision: 1, models: []};
const response = revision => ({revision,
  model_routing: {...clone(original), revision, models: [{model_id: `model-${revision}`}]},
  reasoning_controls: [{depths: ['default', 'low', `effort-${revision}`]}]});
const published = [], writes = [];
let reads = 0;
bridge._adoptModelRoutingResponse = value => {published.push(value); return true;};
bridge._stgModelRouting = clone(original);
bridge._stgModelRoutingSavedJson = JSON.stringify(original);
bridge._stgModelRoutingRevision = 1;
bridge._stgPendingCredentialSecrets = {};
bridge.Api = {modelRouting: {
  get: async () => {reads++; return response(2);},
  replace: async (document, revision) => {writes.push({document: clone(document), revision}); return response(2);},
}};

async function run() {
  if (SCENARIO === 'read_publish') {
    const value = response(2);
    bridge.Api.modelRouting.get = async () => {reads++; return value;};
    await bridge._loadModelRoutingAuthority();
    assert.deepEqual(published, [value]);
    assert.equal(published[0], value, 'forward complete response, including controls');
    assert.equal(reads, 1);
    assert.deepEqual(bridge._stgModelRouting, value.model_routing);
  } else if (SCENARIO === 'persist_publish') {
    bridge._stgModelRouting.models.push({model_id: 'staged'});
    await bridge._persistModelRoutingNow();
    await bridge._persistModelRoutingNow();
    assert.deepEqual(published, [response(2)]);
    assert.equal(writes.length, 1, 'unchanged draft does not create a second commit');
    assert.equal(reads, 0, 'the successful PUT already returned the projection');
    assert.equal(bridge._stgModelRoutingSavedJson, JSON.stringify(response(2).model_routing));
  } else if (SCENARIO === 'read_after_save' || SCENARIO === 'failed_read_after_save') {
    const gate = deferred();
    bridge.Api.modelRouting.get = () => gate.promise;
    const loading = bridge._loadModelRoutingAuthority();
    bridge._stgModelRouting.models.push({model_id: 'staged'});
    await bridge._persistModelRoutingNow();
    if (SCENARIO === 'read_after_save') gate.resolve({revision: 1, model_routing: original});
    else gate.reject(new Error('old read failed'));
    await loading;
    assert.deepEqual(bridge._stgModelRouting, response(2).model_routing);
    assert.equal(bridge._stgModelRoutingRevision, 2);
    assert.deepEqual(published, [response(2)]);
  } else if (SCENARIO === 'read_during_edit') {
    const gate = deferred();
    bridge.Api.modelRouting.get = () => gate.promise;
    const loading = bridge._loadModelRoutingAuthority();
    const draft = bridge._stgModelRouting;
    draft.models.push({model_id: 'unsaved'});
    bridge._stgPendingCredentialSecrets = {'test-key': 'synthetic-secret'};
    gate.resolve(response(2));
    await loading;
    assert.deepEqual(published, [response(2)], 'composer observes committed authority');
    assert.equal(bridge._stgModelRouting, draft, 'editor retains its separate unsaved work');
    assert.deepEqual(draft.models, [{model_id: 'unsaved'}]);
    assert.equal(bridge._stgModelRoutingRevision, 1, 'draft keeps the CAS revision it edited');
    assert.deepEqual(bridge._stgPendingCredentialSecrets, {'test-key': 'synthetic-secret'});
  } else if (SCENARIO === 'subscription_refresh_during_edit') {
    const gate = deferred(), rendered = [];
    bridge.Api.modelRouting.get = () => gate.promise;
    bridge.Api.serverConfig = {get: async () => ({})};
    bridge._adoptModelRoutingResponse = value => {
      published.push(value); rendered.push(value.model_routing.models);
    };
    bridge._modelRoutingDropdownModels = document => document.models;
    bridge._populateModelDropdown = models => rendered.push(models);
    const refreshing = bridge._refreshSubscriptionModelCatalog();
    bridge._stgModelRouting.models.push({model_id: 'unsaved'});
    gate.resolve(response(2));
    await refreshing;
    assert.deepEqual(rendered, [response(2).model_routing.models],
      'subscription refresh must not repaint the composer from the editor draft');
    assert.deepEqual(bridge._stgModelRouting.models, [{model_id: 'unsaved'}]);
  } else if (SCENARIO === 'sync_read_failure') {
    bridge._stgModelRouting = null;
    bridge.Api.modelRouting.get = () => {throw new Error('synchronous admission failure');};
    assert.equal(await bridge._loadModelRoutingAuthority(), null);
    assert.equal(bridge._stgModelRoutingLoadPromise, null);
    bridge.Api.modelRouting.get = async () => response(2);
    await bridge._loadModelRoutingAuthority();
    assert.deepEqual(published, [response(2)]);
    assert.equal(bridge._stgModelRoutingLoadError, '');
  } else if (SCENARIO === 'persist_failure') {
    bridge._stgModelRouting.models.push({model_id: 'unsaved'});
    const failed = new Error('owner CAS conflict');
    bridge.Api.modelRouting.replace = async () => {throw failed;};
    await assert.rejects(bridge._persistModelRoutingNow(), error => error === failed);
    assert.deepEqual(published, []);
    assert.equal(bridge._stgModelRoutingSavedJson, JSON.stringify(original));
    assert.deepEqual(bridge._stgModelRouting.models, [{model_id: 'unsaved'}]);
    bridge.Api.modelRouting.replace = async () => response(2);
    await bridge._persistModelRoutingNow();
    assert.deepEqual(published, [response(2)]);
  } else if (SCENARIO === 'mutation_refresh_after_hint_adoption') {
    // The commit's wake hint starts a live-sync read before the receipt arrives.
    const {runModelRoutingMutation} = require(BUILT);
    const hintRead = deferred(), commandRead = deferred(), pending = [hintRead, commandRead];
    bridge.Api.modelRouting.get = () => {reads++; return pending.shift().promise;};
    let hinted;
    const mutation = runModelRoutingMutation(async () => {
      hinted = bridge._loadModelRoutingAuthority(true);
      return {revision: 2};
    });
    await tick();
    assert.equal(reads, 2, 'the command still reads its own projection');
    hintRead.resolve(response(2));
    assert.deepEqual(await hinted, response(2).model_routing);
    commandRead.resolve(response(2));
    assert.deepEqual(await mutation, {refreshed: true},
      'an editor already holding the acknowledged revision is refreshed');
    assert.equal(bridge._stgModelRoutingRevision, 2);
    assert.deepEqual(bridge._stgModelRouting, response(2).model_routing);
  } else if (SCENARIO === 'mutation_refresh_during_edit') {
    const {runModelRoutingMutation} = require(BUILT);
    const draft = bridge._stgModelRouting;
    const mutation = runModelRoutingMutation(async () => {
      draft.models.push({model_id: 'unsaved'});
      return {revision: 2};
    });
    assert.deepEqual(await mutation, {refreshed: false}, 'a kept draft is not reported as refreshed');
    assert.equal(bridge._stgModelRouting, draft);
    assert.equal(bridge._stgModelRoutingRevision, 1);
  } else if (SCENARIO === 'persist_during_edit') {
    const gate = deferred();
    bridge.Api.modelRouting.replace = async (document, revision) => {
      writes.push({document: clone(document), revision});
      return writes.length === 1 ? gate.promise : response(3);
    };
    bridge._stgModelRouting.models.push({model_id: 'first'});
    const saving = bridge._persistModelRoutingNow();
    await tick();
    bridge._stgModelRouting.models.push({model_id: 'late'});
    gate.resolve(response(2));
    await saving;
    assert.deepEqual(writes.map(write => write.revision), [1, 2]);
    assert.deepEqual(writes[1].document.models, [{model_id: 'first'}, {model_id: 'late'}]);
    assert.deepEqual(published, [response(2), response(3)]);
    assert.deepEqual(bridge._stgModelRouting, response(3).model_routing);
  }
  console.log('ROUTING_PROJECTION_COMPLETE');
}
run().catch(error => {console.error(error); process.exitCode = 1;});
'''
    harness = harness.replace('BUILT', json.dumps(str(settings_bundle))).replace(
        'SCENARIO', json.dumps(scenario))
    result = subprocess.run(['node', '-e', harness], cwd=ROOT, capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ROUTING_PROJECTION_COMPLETE' in result.stdout
