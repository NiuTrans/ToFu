"""Speech settings persist through the owner-scoped model-routing v2 API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._paper_vite import compiled_typescript


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
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

let created = null, updated = null, deleted = null;
window.Api = {
  modelRouting: {
    createProvider: async (bundle, revision) => { created = { bundle, revision }; },
    saveProvider: async (providerId, bundle, revision) => {
      updated = { providerId, bundle, revision };
    },
    deleteProvider: async (providerId, revision) => { deleted = { providerId, revision }; },
    get: async () => ({ model_routing: window._stgModelRouting,
                        revision: window._stgModelRoutingRevision }),
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
      bundle.deployments[0].probe_status === 'passed');

    window._stgModelRouting = {
      contract_version: 'tofu.model-routing/v2', revision: 8,
      creators: bundle.creators, models: bundle.models,
      providers: [bundle.provider], provider_accesses: [bundle.provider_access],
      connections: bundle.connections,
      credentials: [{ ...bundle.credentials[0], kind: 'api_key',
        secret_reference: 'mrs_existing', key_hint: '…abcd' }],
      offerings: bundle.offerings, deployments: bundle.deployments,
    };
    window._stgModelRoutingRevision = 8;
    document.getElementById('settingSttKeyOpenai').value = '';
    await window._persistSttProvider();
    check('update_preserves_redacted_secret_reference',
      updated && updated.providerId === 'stt' && updated.revision === 8 &&
      updated.bundle.credentials[0].secret_reference === 'mrs_existing' &&
      Object.keys(updated.bundle.credential_secrets).length === 0);

    document.getElementById('settingSttEnabled').checked = false;
    await window._persistSttProvider();
    check('disable_deletes_provider_access',
      deleted && deleted.providerId === 'stt' && deleted.revision === 8);
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


def test_speech_runtime_has_no_legacy_provider_array_port():
    source = MODULE_TS.read_text(encoding='utf-8')
    assert '_stgProviders' not in source
    assert 'bridge._persistSttProvider = persistSttProvider;' in source
    assert 'credential_secrets' in source

    manifest = json.loads((
        ROOT / 'frontend/src/runtime/sections/manifest.json'
    ).read_text(encoding='utf-8'))
    settings = next(
        bundle for bundle in manifest['lazyBundles']
        if bundle['name'] == 'settings-presenters'
    )
    assert {
        'name': '_persistSttProvider',
        'kind': 'function',
        'providedBy': 'feature',
    } in settings['runtimeServices']
    assert not any(
        binding['name'] == '_stgProviders'
        for binding in settings['runtimeBindings']
    )
    save = (ROOT / 'frontend/src/runtime/sections/settings/save_export.js').read_text()
    assert 'await _persistSttProvider();' in save
