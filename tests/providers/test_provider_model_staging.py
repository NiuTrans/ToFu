"""Model-add batches publish a complete draft or leave the current draft intact."""
import json

import pytest

from tests.providers.test_provider_model_add import _run_harness

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('kind', ['catalog-first', 'catalog-later', 'custom-later'])
def test_disabled_face_rejects_entire_batch_without_draft_edits(tmp_path, kind):
    out = _run_harness(tmp_path, r'''
  const kind = KIND;
  _stgModelRouting.connections.push({connection_id: 'disabled-face',
    provider_access_id: 'p1-access', base_url: 'https://disabled.synthetic.invalid/v1',
    protocol: 'openai', gateway_namespace: 'disabled', enabled: false});
  runtimeScope.planProviderRequestNameFaces = async () => ({
    llama: {base_url: 'https://disabled.synthetic.invalid/v1', protocol: 'openai', face: 'disabled'},
    solo: {base_url: 'https://new.synthetic.invalid/v1', protocol: 'openai', face: 'new',
      extra_headers: {'x-selector': 'required'}},
  });
  const before = JSON.stringify(_stgModelRouting);
  const connection = _stgModelRouting.connections[0];
  const credential = _stgModelRouting.credentials[0];
  let saves = 0;
  global._persistModelRoutingNow = async () => {saves++;};
  const models = kind === 'custom-later' ? [] : (kind === 'catalog-first' ? ['llama'] : ['solo', 'llama'])
    .map(id => _stgModelRouting.models.find(model => model.model_id === id));
  try { await _stageV2AddModels('p1', models, kind === 'custom-later' ? ['solo', 'llama'] : []); }
  catch (error) {out.error = String(error.message);}
  out.unchanged = JSON.stringify(_stgModelRouting) === before;
  out.sameRows = _stgModelRouting.connections[0] === connection && _stgModelRouting.credentials[0] === credential;
  out.saves = saves;
'''.replace('KIND', json.dumps(kind)))
    assert out['error']
    assert out['unchanged'], 'a refused batch must not leave offerings, connections or grants'
    assert out['sameRows'], 'failure must preserve editor row identities'
    assert out['saves'] == 0


def test_concurrent_model_add_rechecks_wire_names_after_face_lookup(tmp_path):
    out = _run_harness(tmp_path, r'''
  let release, entered;
  const started = new Promise(resolve => {entered = resolve;});
  let calls = 0, saves = 0;
  runtimeScope.planProviderRequestNameFaces = async () => {
    if (++calls === 1) {entered(); return new Promise(resolve => {release = resolve;});}
    return {};
  };
  global._persistModelRoutingNow = async () => {saves++;};
  const model = _stgModelRouting.models[0];
  const first = _stageV2AddModels('p1', [model], []);
  await started;
  out.second = await _stageV2AddModels('p1', [model], []);
  release({});
  out.first = await first;
  out.offerings = _stgModelRouting.offerings.filter(row => row.model && row.model.model_id === 'llama').length;
  out.deployments = _stgModelRouting.deployments.filter(row => row.wire_model_id === 'llama').length;
  out.saves = saves;
''')
    assert out == {'second': 1, 'first': 0, 'offerings': 1, 'deployments': 1, 'saves': 1}


def test_provider_removed_during_face_lookup_is_not_recreated(tmp_path):
    out = _run_harness(tmp_path, r'''
  let release, entered;
  const started = new Promise(resolve => {entered = resolve;});
  runtimeScope.planProviderRequestNameFaces = async () => {
    entered(); return new Promise(resolve => {release = resolve;});
  };
  let saves = 0;
  global._persistModelRoutingNow = async () => {saves++;};
  const pending = _stageV2AddModels('p1', [_stgModelRouting.models[0]], []);
  await started;
  _stgModelRouting.providers.splice(0, 1);
  _stgModelRouting.provider_accesses.splice(0, 1);
  const current = JSON.stringify(_stgModelRouting);
  release({});
  out.added = await pending;
  out.unchanged = JSON.stringify(_stgModelRouting) === current;
  out.saves = saves;
''')
    assert out == {'added': 0, 'unchanged': True, 'saves': 0}


def test_face_lookup_uses_current_credentials_and_preserves_retired_row(tmp_path):
    out = _run_harness(tmp_path, r'''
  let release, entered;
  const started = new Promise(resolve => {entered = resolve;});
  runtimeScope.planProviderRequestNameFaces = async () => {
    entered(); return new Promise(resolve => {release = resolve;});
  };
  const oldCredential = _stgModelRouting.credentials[0];
  const before = JSON.stringify(oldCredential);
  const pending = _stageV2AddModels('p1', [_stgModelRouting.models[0]], []);
  await started;
  const current = JSON.parse(before);
  current.authorization.models = [{creator_id: 'meta', model_id: 'esm', excluded_wire_model_ids: ['denied']}];
  _stgModelRouting.credentials[0] = current;
  release({llama: {base_url: 'https://new.synthetic.invalid/v1', protocol: 'openai', face: 'new'}});
  out.added = await pending;
  out.oldUnchanged = JSON.stringify(oldCredential) === before;
  out.sameCurrent = _stgModelRouting.credentials[0] === current;
  out.grants = current.authorization.models;
  out.connectionGranted = current.authorization.connection_ids.includes(
    _stgModelRouting.deployments.find(row => row.wire_model_id === 'llama').connection_id);
''')
    assert out['added'] == 1
    assert out['oldUnchanged'] and out['sameCurrent']
    assert out['grants'] == [
        {'creator_id': 'meta', 'model_id': 'esm', 'excluded_wire_model_ids': ['denied']},
        {'creator_id': 'meta', 'model_id': 'llama'},
    ]
    assert out['connectionGranted']


@pytest.mark.parametrize('phase', ['names', 'faces'])
def test_changed_connection_inputs_refuse_stale_template_plan(tmp_path, phase):
    out = _run_harness(tmp_path, r'''
  let release, entered;
  const started = new Promise(resolve => {entered = resolve;});
  runtimeScope[PHASE === 'names' ? 'planProviderRequestNames' : 'planProviderRequestNameFaces'] = async () => {
    entered(); return new Promise(resolve => {release = resolve;});
  };
  let saves = 0;
  global._persistModelRoutingNow = async () => {saves++;};
  const pending = _stageV2AddModels('p1', [_stgModelRouting.models[0]], []);
  await started;
  _stgModelRouting.connections[0].base_url = 'https://changed.synthetic.invalid/v1';
  const current = JSON.stringify(_stgModelRouting);
  release({});
  try { await pending; } catch (error) {out.error = String(error.message);}
  out.unchanged = JSON.stringify(_stgModelRouting) === current;
  out.saves = saves;
'''.replace('PHASE', json.dumps(phase)))
    assert out.get('error'), 'changed planner inputs need an actionable refusal'
    assert out['unchanged'] and out['saves'] == 0


def test_replaced_draft_during_capability_read_cancels_original_model_add(tmp_path):
    out = _run_harness(tmp_path, r'''
  let release;
  Api.modelRouting.get = () => new Promise(resolve => {release = resolve;});
  let saves = 0;
  global._persistModelRoutingNow = async () => {saves++;};
  const pending = _stageV2AddModels('p1', [_stgModelRouting.models[0]], []);
  _stgModelRouting = global.freshRouting();
  _stgModelRouting.providers[0].name = 'Reloaded';
  const current = JSON.stringify(_stgModelRouting);
  release({metadata_inheritance_supported: true});
  out.added = await pending;
  out.unchanged = JSON.stringify(_stgModelRouting) === current;
  out.saves = saves;
''')
    assert out == {'added': 0, 'unchanged': True, 'saves': 0}


@pytest.mark.parametrize('edit', ['remove', 'replace-metadata'])
def test_model_add_reads_current_catalog_after_face_lookup(tmp_path, edit):
    out = _run_harness(tmp_path, r'''
  let release, entered;
  const started = new Promise(resolve => {entered = resolve;});
  runtimeScope.planProviderRequestNameFaces = async () => {
    entered(); return new Promise(resolve => {release = resolve;});
  };
  const pending = _stageV2AddModels('p1', [_stgModelRouting.models[0]], []);
  await started;
  if (EDIT === 'remove') _stgModelRouting.models.splice(0, 1);
  else _stgModelRouting.models[0] = {..._stgModelRouting.models[0], capabilities: ['image'], context_window: 777};
  const current = JSON.stringify(_stgModelRouting);
  release({});
  try {out.added = await pending;} catch (error) {out.error = String(error.message);}
  out.unchanged = JSON.stringify(_stgModelRouting) === current;
  const added = _stgModelRouting.offerings.find(row => row.model && row.model.model_id === 'llama');
  if (added) out.facts = [added.capabilities, added.context_window];
'''.replace('EDIT', json.dumps(edit)))
    if edit == 'remove':
        assert out.get('error') and out['unchanged']
        assert 'facts' not in out
    else:
        assert out['added'] == 1
        assert out['facts'] == [['image'], 777]
