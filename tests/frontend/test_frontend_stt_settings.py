"""Speech settings persist through the owner-scoped model-routing v2 API."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._paper_vite import compiled_typescript


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
MODULE_TS = ROOT / 'frontend/src/features/settings/speech.ts'


@pytest.fixture(scope='module')
def speech_bundle():
    with compiled_typescript(
        MODULE_TS,
        expose_feature_registry_to_window=True,
    ) as built:
        yield built


def test_speech_provider_uses_v2_bundle_and_dedicated_secret_channel(
    speech_bundle,
):
    body = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const emptyAuthority = () => ({
  contract_version: 'tofu.model-routing/v2', revision: 4,
  creators: [], models: [], providers: [], provider_accesses: [],
  connections: [], credentials: [], offerings: [], deployments: [],
});
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!doctype html><body>' +
    '<input type="checkbox" id="settingSttEnabled" checked>' +
    '<select id="settingSttProvider"><option value="openai">openai</option>' +
      '<option value="omni">omni</option></select>' +
    '<input id="settingSttModelOpenai" value="gpt-4o-transcribe">' +
    '<input id="settingSttBaseOpenai" value="https://api.openai.com/v1">' +
    '<input id="settingSttKeyOpenai" value="sk-new-secret">' +
    '<div id="sttCardOpenai"></div><div id="sttCardGroq"></div>' +
    '<div id="sttCardOmni"></div><div id="sttCardCustom"></div>' +
    '</body>',
  targets: [process.argv[2]],
  globals: {
    _stgModelRouting: emptyAuthority(),
    _stgModelRoutingRevision: 4,
  },
});
for (const name of ['HTMLElement', 'HTMLInputElement', 'HTMLSelectElement',
                    'AbortController', 'AbortSignal']) global[name] = window[name];

const registry = window.__tofuTestFeatureRegistry;
let created = null, updated = null, replaced = null;
let serverDocument = emptyAuthority();
registry._stgModelRoutingSavedJson = JSON.stringify(registry._stgModelRouting);
function commitBundle(bundle, revision) {
  serverDocument = {contract_version: 'tofu.model-routing/v2', revision: revision + 1,
    creators: bundle.creators.length ? bundle.creators : serverDocument.creators,
    models: bundle.models.length ? bundle.models : serverDocument.models,
    providers: [bundle.provider], provider_accesses: [bundle.provider_access],
    connections: bundle.connections,
    credentials: bundle.credentials.map(row => ({...row, secret_reference: row.secret_reference || 'synthetic-stored'})),
    offerings: bundle.offerings, deployments: bundle.deployments};
  return {provider: bundle, revision: serverDocument.revision};
}
window.Api = {
  modelRouting: {
    createProvider: async (bundle, revision) => {
      created = { bundle, revision }; return commitBundle(bundle, revision);
    },
    saveProvider: async (providerId, bundle, revision) => {
      updated = { providerId, bundle, revision }; return commitBundle(bundle, revision);
    },
    replace: async (document, revision) => {
      replaced = { document, revision }; serverDocument = {...document, revision: revision + 1};
      return {model_routing: serverDocument, revision: serverDocument.revision};
    },
    deleteProvider: async () => { throw new Error('Speech disable cannot delete a Provider'); },
    get: async () => ({model_routing: serverDocument, revision: serverDocument.revision}),
  },
};

(async () => {
  try {
    await window._persistSttProvider();
    const bundle = created && created.bundle;
    check('create_uses_cas_revision', created && created.revision === 4);
    check('provider_access_is_owner_scoped',
      bundle.provider.provider_id === 'stt' && bundle.provider.scope === 'owner' &&
      bundle.provider_access.provider_access_id === 'stt-access');
    check('secret_uses_dedicated_bundle_channel',
      bundle.credential_secrets['stt-credential'] === 'sk-new-secret' &&
      bundle.credentials[0].secret_reference === '');
    check('confirmed_model_identity_is_explicit',
      bundle.offerings[0].identity_state === 'confirmed' &&
      bundle.offerings[0].model.creator_id === 'tofu-user-stt-transcription' &&
      bundle.models[0].model_id === 'gpt-4o-transcribe');
    check('deployment_preserves_wire_id',
      bundle.deployments[0].wire_model_id === 'gpt-4o-transcribe' &&
      bundle.deployments[0].probe_status === 'unprobed');

    registry._stgModelRouting = {
      contract_version: 'tofu.model-routing/v2', revision: 8,
      creators: bundle.creators, models: bundle.models,
      providers: [bundle.provider], provider_accesses: [bundle.provider_access],
      connections: bundle.connections,
      credentials: [{ ...bundle.credentials[0], kind: 'api_key',
        secret_reference: 'mrs_existing', key_hint: '…abcd' }],
      offerings: bundle.offerings, deployments: bundle.deployments,
    };
    registry._stgModelRoutingRevision = 8;
    registry._stgModelRoutingSavedJson = JSON.stringify(registry._stgModelRouting);
    serverDocument = registry._stgModelRouting;
    document.getElementById('settingSttKeyOpenai').value = '';
    document.getElementById('settingSttBaseOpenai').value = 'https://fixture.invalid/new';
    await window._persistSttProvider();
    check('update_preserves_redacted_secret_reference',
      updated && updated.providerId === 'stt' && updated.revision === 8 &&
      updated.bundle.credentials[0].secret_reference === 'mrs_existing' &&
      Object.keys(updated.bundle.credential_secrets).length === 0);

    document.getElementById('settingSttEnabled').checked = false;
    await window._persistSttProvider();
    check('disable_retains_access_and_credential', replaced && replaced.revision === 9 &&
      replaced.document.provider_accesses[0].enabled === false &&
      replaced.document.credentials[0].secret_reference === 'mrs_existing' &&
      replaced.document.deployments[0].wire_model_id === 'gpt-4o-transcribe');
  } catch (error) {
    check('harness_threw_' + String(error && error.message || error), false);
  } finally {
    report();
  }
})();
'''
    run_harness(
        target_js=speech_bundle,
        body_js=body,
        min_pass=7,
        label='stt-model-routing-v2',
    )


def test_speech_without_authority_refuses_mutation(speech_bundle):
    body = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, check, report } = setup({
  root: process.argv[3], targets: [process.argv[2]],
  globals: {_stgModelRouting: null},
});
let requests = 0;
window.Api = {modelRouting: {
  createProvider: async () => {requests++;},
  saveProvider: async () => {requests++;},
  deleteProvider: async () => {requests++;},
}};
(async () => {
  try {
    await window._persistSttProvider();
    check('missing_authority_refused', false);
  } catch (error) {
    check('missing_authority_refused', /authority/.test(error.message));
  }
  check('no_provider_write', requests === 0);
  report();
})();
'''
    run_harness(target_js=speech_bundle, body_js=body, min_pass=2,
                label='stt-authority-admission')
