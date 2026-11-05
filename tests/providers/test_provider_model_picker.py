"""Provider picker sessions keep newer user choices across pending model adds."""
import json

import pytest

from tests._runtime_sections import ROOT
from tests.providers.test_provider_model_add import _run_harness

pytestmark = pytest.mark.unit


def _picker(tmp_path, scenario):
    setup = r'''
  const requireProject = require('node:module').createRequire(PACKAGE);
  const {JSDOM} = requireProject('jsdom');
  const dom = new JSDOM('<body><button id="trigger">Provider</button></body>');
  global.document = dom.window.document;
  function pick(key) {
    const box = document.querySelector('[data-model-key="' + key + '"]');
    box.checked = true;
    _toggleProviderModelPick(key, true);
  }
  try {
'''.replace('PACKAGE', json.dumps(str(ROOT / 'package.json')))
    return _run_harness(tmp_path, setup + scenario + r'''
  } finally {
    _closeProviderModelPicker();
    dom.window.close();
  }
''')


@pytest.mark.parametrize('action', ['different-provider', 'reopen-same-provider', 'additional-choice'])
def test_catalog_completion_keeps_newer_picker_choices(tmp_path, action):
    out = _picker(tmp_path, r'''
  let release, entered;
  const started = new Promise(resolve => {entered = resolve;});
  global._persistModelRoutingNow = async () => {
    entered(); return new Promise(resolve => {release = resolve;});
  };
  _openProviderModelPicker('p1');
  pick('meta::llama');
  const pending = _stageV2CheckedCatalogModels();
  await started;
  if (ACTION === 'different-provider') _openProviderModelPicker('p2');
  else if (ACTION === 'reopen-same-provider') _openProviderModelPicker('p1');
  pick('x::solo');
  release(null);
  await pending;
  out.checked = document.querySelector('[data-model-key="x::solo"]').checked;
  out.count = document.getElementById('stgProviderModelPickerCount').textContent;
  out.added = _stgModelRouting.offerings.filter(row => row.model && row.model.model_id === 'llama').length;
'''.replace('ACTION', json.dumps(action)))
    assert out['checked'], 'an older add must not clear a newer checkbox choice'
    assert '1' in out['count']
    assert out['added'] == 1, 'closing or changing the picker must not cancel the authorized add'


def test_custom_completion_preserves_new_input_and_focus(tmp_path):
    out = _picker(tmp_path, r'''
  let release, entered;
  const started = new Promise(resolve => {entered = resolve;});
  global._persistModelRoutingNow = async () => {
    entered(); return new Promise(resolve => {release = resolve;});
  };
  _openProviderModelPicker('p1');
  const input = document.getElementById('stgProviderModelCustomId');
  input.value = 'first-custom';
  const pending = _stageV2CustomModelFromPicker();
  await started;
  input.value = 'later-custom';
  input.dispatchEvent(new dom.window.Event('input', {bubbles: true}));
  const search = document.querySelector('.stg-v2-picker-search');
  search.focus();
  release(null);
  await pending;
  out.value = input.value;
  out.focusKept = document.activeElement === search;
  out.added = _stgModelRouting.deployments.some(row => row.wire_model_id === 'first-custom');
''')
    assert out == {'value': 'later-custom', 'focusKept': True, 'added': True}


def test_custom_completion_does_not_repaint_a_new_picker(tmp_path):
    out = _picker(tmp_path, r'''
  let release, entered;
  const started = new Promise(resolve => {entered = resolve;});
  global._persistModelRoutingNow = async () => {
    entered(); return new Promise(resolve => {release = resolve;});
  };
  _openProviderModelPicker('p1');
  document.getElementById('stgProviderModelCustomId').value = 'first-custom';
  const pending = _stageV2CustomModelFromPicker();
  await started;
  _openProviderModelPicker('p2');
  const checkbox = document.querySelector('[data-model-key="x::solo"]');
  checkbox.focus();
  release(null);
  await pending;
  out.sameNode = document.querySelector('[data-model-key="x::solo"]') === checkbox;
  out.focusKept = document.activeElement === checkbox;
''')
    assert out == {'sameNode': True, 'focusKept': True}


def test_refused_add_keeps_submitted_and_new_choices_for_retry(tmp_path):
    out = _picker(tmp_path, r'''
  let release, entered;
  const started = new Promise(resolve => {entered = resolve;});
  runtimeScope.planProviderRequestNameFaces = async () => {
    entered(); return new Promise(resolve => {release = resolve;});
  };
  _stgModelRouting.connections.push({provider_access_id: 'p1-access', connection_id: 'disabled',
    base_url: 'https://disabled.synthetic.invalid', enabled: false});
  _openProviderModelPicker('p1');
  pick('meta::llama');
  const pending = _stageV2CheckedCatalogModels();
  await started;
  pick('x::solo');
  release({llama: {base_url: 'https://disabled.synthetic.invalid', protocol: 'openai', extra_headers: {}}});
  await pending;
  out.checked = [...document.querySelectorAll('[data-model-key]:checked')].map(node => node.dataset.modelKey).sort();
  out.alerts = global.__alerts.length;
  out.added = _stgModelRouting.offerings.some(row => row.model && row.model.model_id === 'llama');
''')
    assert out == {'checked': ['meta::llama', 'x::solo'], 'alerts': 1, 'added': False}


def test_empty_search_does_not_claim_the_catalog_has_all_been_added(tmp_path):
    out = _picker(tmp_path, r'''
  _openProviderModelPicker('p1');
  _filterProviderModelPicker('unmatched-synthetic-name');
  out.message = document.querySelector('.stg-v2-picker-empty').textContent;
''')
    assert out['message'] == '没有匹配的模型。'


@pytest.mark.parametrize('event', ['backdrop', 'enter'])
def test_retired_picker_events_cannot_act_on_a_new_session(tmp_path, event):
    out = _picker(tmp_path, r'''
  _openProviderModelPicker('p1');
  const oldRoot = document.getElementById('stgProviderModelPickerOverlay');
  const oldInput = document.getElementById('stgProviderModelCustomId');
  _openProviderModelPicker('p2');
  const newRoot = document.getElementById('stgProviderModelPickerOverlay');
  document.getElementById('stgProviderModelCustomId').value = 'new-custom';
  const calls = [];
  _stageV2AddModels = async (...args) => {calls.push(args); return 1;};
  if (EVENT === 'backdrop') oldRoot.click();
  else oldInput.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: 'Enter'}));
  await new Promise(resolve => setImmediate(resolve));
  out.open = newRoot.isConnected;
  out.calls = calls.length;
'''.replace('EVENT', json.dumps(event)))
    assert out == {'open': True, 'calls': 0}


def test_failed_save_keeps_new_selection_and_its_focus(tmp_path):
    out = _picker(tmp_path, r'''
  let reject, entered;
  const started = new Promise(resolve => {entered = resolve;});
  global._persistModelRoutingNow = async () => {
    entered(); return new Promise((_, fail) => {reject = fail;});
  };
  _openProviderModelPicker('p1');
  pick('meta::llama');
  const pending = _stageV2CheckedCatalogModels();
  await started;
  pick('x::solo');
  document.querySelector('[data-model-key="x::solo"]').focus();
  reject(new Error('CAS unavailable'));
  await pending;
  out.checked = document.querySelector('[data-model-key="x::solo"]').checked;
  out.focus = document.activeElement.dataset.modelKey;
  out.staged = _stgModelRouting.offerings.some(row => row.model && row.model.model_id === 'llama');
  out.excluded = !document.querySelector('[data-model-key="meta::llama"]');
  out.error = global.__alerts[0];
''')
    assert out == {'checked': True, 'focus': 'x::solo', 'staged': True,
                   'excluded': True, 'error': '模型保存失败：CAS unavailable'}


def test_filtering_keeps_hidden_choices(tmp_path):
    out = _picker(tmp_path, r'''
  _openProviderModelPicker('p1');
  pick('meta::llama');
  _filterProviderModelPicker('solo');
  pick('x::solo');
  _filterProviderModelPicker('');
  out.checked = [...document.querySelectorAll('[data-model-key]:checked')].map(node => node.dataset.modelKey).sort();
  out.count = document.getElementById('stgProviderModelPickerCount').textContent;
''')
    assert out == {'checked': ['meta::llama', 'x::solo'], 'count': '已选择 2 个模型'}


def test_custom_completion_keeps_a_value_retyped_while_saving(tmp_path):
    out = _picker(tmp_path, r'''
  let release, entered;
  const started = new Promise(resolve => {entered = resolve;});
  global._persistModelRoutingNow = async () => {
    entered(); return new Promise(resolve => {release = resolve;});
  };
  _openProviderModelPicker('p1');
  const input = document.getElementById('stgProviderModelCustomId');
  input.value = 'custom';
  const pending = _stageV2CustomModelFromPicker();
  await started;
  for (const value of ['', 'custom']) {
    input.value = value;
    input.dispatchEvent(new dom.window.Event('input', {bubbles: true}));
  }
  release(null);
  await pending;
  out.value = input.value;
''')
    assert out['value'] == 'custom'


def test_close_and_external_removal_release_session_resources(tmp_path):
    out = _picker(tmp_path, r'''
  const observers = new Set(), listeners = new Set();
  const Observer = dom.window.MutationObserver;
  dom.window.MutationObserver = class extends Observer {
    observe(...args) { observers.add(this); return super.observe(...args); }
    disconnect() { observers.delete(this); super.disconnect(); }
  };
  const prototype = dom.window.EventTarget.prototype;
  const add = prototype.addEventListener, remove = prototype.removeEventListener;
  prototype.addEventListener = function(type, listener, options) {
    if (this.id === 'stgProviderModelCustomId' || this.id === 'stgProviderModelPickerOverlay') listeners.add(listener);
    return add.call(this, type, listener, options);
  };
  prototype.removeEventListener = function(type, listener, options) {
    listeners.delete(listener);
    return remove.call(this, type, listener, options);
  };
  for (let index = 0; index < 4; index++) {
    _openProviderModelPicker('p1');
    if (index % 2) document.getElementById('stgProviderModelPickerOverlay').remove();
    else _closeProviderModelPicker();
    await new Promise(resolve => setImmediate(resolve));
  }
  out.observers = observers.size;
  out.listeners = listeners.size;
  out.overlays = document.querySelectorAll('#stgProviderModelPickerOverlay').length;
''')
    assert out == {'observers': 0, 'listeners': 0, 'overlays': 0}
