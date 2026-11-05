"""Provider credential views retire plaintext and preserve current user edits."""
import json

import pytest

from tests._runtime_sections import ROOT
from tests.providers.test_provider_model_add import _run_harness

pytestmark = pytest.mark.unit


def _view(tmp_path, scenario):
    setup = r'''
  const requireProject = require('node:module').createRequire(PACKAGE);
  const {JSDOM} = requireProject('jsdom');
  const dom = new JSDOM('<body><div id="settingsModal" class="open"><div id="stgProviderList"></div><input id="other"></div></body>');
  global.document = dom.window.document;
  Object.assign(_stgModelRouting.credentials[0], {
    kind: 'api_key', secret_reference: 'synthetic-reference', key_hint: 'synt****hint',
  });
  const tick = () => new Promise(resolve => setImmediate(resolve));
  const card = () => document.querySelector('.stg-key-card[data-credential-id="cred-a"]');
  const input = () => card().querySelector('.stg-keys-input');
  const eye = () => card().querySelector('.stg-key-reveal');
  _renderProvidersTab();
  try {
'''.replace('PACKAGE', json.dumps(str(ROOT / 'package.json')))
    return _run_harness(tmp_path, setup + scenario + r'''
  } finally {
    if (typeof _destroyProviderCredentialViews === 'function') _destroyProviderCredentialViews();
    dom.window.close();
  }
''')


def test_reveal_completion_cannot_populate_a_replaced_card(tmp_path):
    out = _view(tmp_path, r'''
  let resolve;
  Api.modelRouting.revealCredentialSecret = () => new Promise(done => {resolve = done;});
  const oldInput = input();
  _toggleV2KeyReveal(eye());
  _renderProvidersTab();
  resolve({secret: 'synthetic-secret-value'});
  await tick();
  out.retiredValue = oldInput.value;
  out.retiredReadonly = oldInput.readOnly;
  out.currentValue = input().value;
''')
    assert out == {'retiredValue': 'synt****hint', 'retiredReadonly': True,
                   'currentValue': 'synt****hint'}


def test_rerender_scrubs_a_previously_revealed_card(tmp_path):
    out = _view(tmp_path, r'''
  Api.modelRouting.revealCredentialSecret = async () => ({secret: 'synthetic-secret-value'});
  _toggleV2KeyReveal(eye());
  await tick();
  const oldInput = input();
  out.wasRevealed = oldInput.value === 'synthetic-secret-value';
  _renderProvidersTab();
  out.retiredValue = oldInput.value;
  out.retainedPlaintext = oldInput.__revealedSecret || null;
''')
    assert out == {'wasRevealed': True, 'retiredValue': 'synt****hint', 'retainedPlaintext': None}


@pytest.mark.parametrize('scenario', ['newer_edit', 'old_baseline', 'pending_read', 'unrelated_draft'])
def test_acknowledged_credential_refresh_keeps_current_input_usable(tmp_path, scenario):
    out = _view(tmp_path, r'''
  const scenario = SCENARIO;
  let resolve;
  Api.modelRouting.revealCredentialSecret = () => scenario === 'pending_read'
    ? new Promise(done => {resolve = done;}) : Promise.resolve({secret: 'synthetic-original'});
  _toggleV2KeyReveal(eye());
  await tick();
  const old = _stgModelRouting;
  if (scenario !== 'pending_read') {
    input().value = 'synthetic-newer'; _onV2KeySecretEdit(input()); input().focus();
  }
  _stgModelRouting = JSON.parse(JSON.stringify(old));
  Object.assign(_stgModelRouting.credentials[0], {secret_reference: 'acknowledged-reference', key_hint: 'synt****saved'});
  _adoptProviderCredentialDraft(scenario === 'unrelated_draft' ? {} : old);
  if (scenario === 'pending_read') {
    resolve({secret: 'outdated-reveal'}); await tick();
    Object.assign(out, {value: input().value, readonly: input().readOnly, disabled: eye().disabled});
  } else {
    input().value = scenario === 'old_baseline' ? 'synthetic-original' : 'synthetic-third';
    _onV2KeySecretEdit(input());
    Object.assign(out, {pending: _stgPendingCredentialSecrets['cred-a'], readonly: input().readOnly,
      focused: document.activeElement === input(), value: input().value});
  }
'''.replace('SCENARIO', json.dumps(scenario)))
    if scenario == 'pending_read':
        assert out == {'value': 'synt****saved', 'readonly': True, 'disabled': False}
    elif scenario == 'unrelated_draft':
        assert out['pending'] == 'synthetic-newer'
        assert out['readonly'] is True
    else:
        value = 'synthetic-original' if scenario == 'old_baseline' else 'synthetic-third'
        assert out == {'pending': value, 'readonly': False, 'focused': True, 'value': value}


def test_reveal_completion_keeps_newer_focus(tmp_path):
    out = _view(tmp_path, r'''
  let resolve;
  Api.modelRouting.revealCredentialSecret = () => new Promise(done => {resolve = done;});
  eye().focus();
  _toggleV2KeyReveal(eye());
  document.getElementById('other').focus();
  resolve({secret: 'synthetic-secret-value'});
  await tick();
  out.value = input().value;
  out.focus = document.activeElement.id;
''')
    assert out == {'value': 'synthetic-secret-value', 'focus': 'other'}


def test_editing_a_revealed_staged_value_does_not_discard_the_replacement(tmp_path):
    out = _view(tmp_path, r'''
  _stgPendingCredentialSecrets['cred-a'] = 'synthetic-replacement';
  _renderProvidersTab();
  _toggleV2KeyReveal(eye());
  input().value = 'synthetic-replacement-edited';
  _onV2KeySecretEdit(input());
  input().value = 'synthetic-replacement';
  _onV2KeySecretEdit(input());
  out.staged = _stgPendingCredentialSecrets['cred-a'] || null;
  out.marked = card().classList.contains('stg-key-card--staged');
''')
    assert out == {'staged': 'synthetic-replacement', 'marked': True}


@pytest.mark.parametrize('failure', ['null', 'rejected'])
def test_failed_stats_read_can_be_retried(tmp_path, failure):
    out = _view(tmp_path, r'''
  let reads = 0;
  Api.dispatch = {keyStats: async () => {
    reads++;
    if (reads === 1) {
      if (FAILURE === 'rejected') throw new Error('synthetic read failure');
      return null;
    }
    return {key_namespace: 'owner:', providers: {'owner:p1': {'cred-a': {enabled: true, total: 7}}}};
  }};
  _loadV2KeyStats();
  await tick();
  out.failureVisible = card().querySelector('.stg-key-card-stats').textContent.includes('统计加载失败');
  _loadV2KeyStats();
  await tick();
  out.reads = reads;
  out.total = _v2KeyStatRow('p1', 'cred-a')?.total || 0;
'''.replace('FAILURE', json.dumps(failure)))
    assert out == {'reads': 2, 'total': 7, 'failureVisible': True}


def test_override_waits_for_the_authoritative_namespace(tmp_path):
    out = _view(tmp_path, r'''
  let resolve;
  const writes = [];
  Api.dispatch = {
    keyStats: () => new Promise(done => {resolve = done;}),
    keyOverride: async body => {
      writes.push(body.provider_id);
      return {row: {enabled: false, override: false, total: 3}};
    },
  };
  _loadV2KeyStats();
  _v2KeyOverride('p1', 'cred-a', false);
  await tick();
  out.writesBeforeNamespace = writes.length;
  resolve({key_namespace: 'owner:', providers: {'owner:p1': {
    'cred-a': {enabled: true, override: null, total: 2},
  }}});
  await tick();
  out.enabled = _v2KeyStatRow('p1', 'cred-a').enabled;
  out.override = _v2KeyStatRow('p1', 'cred-a').override;
  out.writes = writes;
''')
    assert out == {'enabled': False, 'override': False,
                   'writesBeforeNamespace': 0, 'writes': ['owner:p1']}


def test_override_without_namespace_evidence_refuses_the_write(tmp_path):
    out = _view(tmp_path, r'''
  let writes = 0;
  Api.dispatch = {keyStats: async () => null,
    keyOverride: async () => {writes++; return {row: {enabled: false}};}};
  _v2KeyOverride('p1', 'cred-a', false);
  await tick();
  out.writes = writes;
  out.failed = global.__toasts.some(value => value.includes('切换失败'));
''')
    assert out == {'writes': 0, 'failed': True}


def test_overrides_for_one_credential_reach_the_server_in_click_order(tmp_path):
    out = _view(tmp_path, r'''
  const bodies = [], releases = [];
  Api.dispatch = {keyStats: async () => ({key_namespace: 'owner:', providers: {}}),
    keyOverride: body => {
      bodies.push(body);
      return new Promise(resolve => {releases.push(resolve);});
    }};
  _loadV2KeyStats();
  await tick();
  _v2KeyOverride('p1', 'cred-a', false);
  _v2KeyOverride('p1', 'cred-a', true);
  await tick();
  out.firstStarted = bodies.length;
  releases[0]({row: {enabled: false, override: false}});
  await tick();
  out.secondStarted = bodies.length;
  releases[1]({row: {enabled: true, override: true}});
  await tick();
  out.targets = bodies.map(row => [row.provider_id, row.enabled]);
  out.enabled = _v2KeyStatRow('p1', 'cred-a').enabled;
''')
    assert out == {'firstStarted': 1, 'secondStarted': 2,
                   'targets': [['owner:p1', False], ['owner:p1', True]], 'enabled': True}


def test_close_scrubs_plaintext_without_discarding_a_staged_replacement(tmp_path):
    out = _view(tmp_path, r'''
  Api.modelRouting.revealCredentialSecret = async () => ({secret: 'synthetic-secret-value'});
  _toggleV2KeyReveal(eye());
  await tick();
  const control = input();
  control.value = 'synthetic-replacement';
  _onV2KeySecretEdit(control);
  const mask = control.getAttribute('data-masked');
  _destroyProviderCredentialViews();
  out.masked = control.value === mask && control.readOnly;
  out.pressed = eye().getAttribute('aria-pressed');
  out.staged = _stgPendingCredentialSecrets['cred-a'];
''')
    assert out == {'masked': True, 'pressed': 'false', 'staged': 'synthetic-replacement'}


def test_external_card_removal_scrubs_the_detached_input(tmp_path):
    out = _view(tmp_path, r'''
  Api.modelRouting.revealCredentialSecret = async () => ({secret: 'synthetic-secret-value'});
  _toggleV2KeyReveal(eye());
  await tick();
  const control = input();
  card().remove();
  await tick();
  out.value = control.value;
  out.readonly = control.readOnly;
''')
    assert out == {'value': 'synt****hint', 'readonly': True}


def test_retired_reveal_failure_does_not_notify_a_new_view(tmp_path):
    out = _view(tmp_path, r'''
  let resolve;
  Api.modelRouting.revealCredentialSecret = () => new Promise(done => {resolve = done;});
  _toggleV2KeyReveal(eye());
  _destroyProviderCredentialViews();
  _renderProvidersTab();
  resolve(null);
  await tick();
  out.toasts = global.__toasts.length;
  out.value = input().value;
  out.busy = eye().disabled;
''')
    assert out == {'toasts': 0, 'value': 'synt****hint', 'busy': False}


def test_envelope_reveal_and_edit_undo_keep_the_committed_value(tmp_path):
    out = _view(tmp_path, r'''
  Api.modelRouting.revealCredentialSecret = async () => ({secret: JSON.stringify({
    format: 'tofu.credential-secret/v1', api_key: '  synthetic-key  ',
    extra_headers: {'x-secret': 'synthetic-header'},
  })});
  _toggleV2KeyReveal(eye());
  await tick();
  out.display = input().value;
  out.serialized = card().outerHTML.includes('synthetic-key') || card().outerHTML.includes('synthetic-header');
  input().value = 'replacement';
  _onV2KeySecretEdit(input());
  input().value = 'synthetic-key';
  _onV2KeySecretEdit(input());
  out.staged = Object.hasOwn(_stgPendingCredentialSecrets, 'cred-a');
  _toggleV2KeyReveal(eye());
  out.hidden = input().value === 'synt****hint' && input().readOnly;
''')
    assert out == {'display': 'synthetic-key', 'serialized': False, 'staged': False, 'hidden': True}


def test_stats_reads_coalesce_but_a_retired_read_cannot_replace_a_newer_snapshot(tmp_path):
    out = _view(tmp_path, r'''
  const releases = [];
  Api.dispatch = {keyStats: () => new Promise(resolve => {releases.push(resolve);})};
  _loadV2KeyStats();
  _loadV2KeyStats();
  await tick();
  out.initialReads = releases.length;
  _destroyProviderCredentialViews();
  _loadV2KeyStats();
  await tick();
  out.reopenedReads = releases.length;
  releases[1]({key_namespace: 'owner:', providers: {'owner:p1': {'cred-a': {total: 9}}}});
  await tick();
  releases[0]({key_namespace: 'owner:', providers: {'owner:p1': {'cred-a': {total: 1}}}});
  await tick();
  out.total = _v2KeyStatRow('p1', 'cred-a').total;
''')
    assert out == {'initialReads': 1, 'reopenedReads': 2, 'total': 9}


def test_an_accepted_override_survives_close_while_its_namespace_is_loading(tmp_path):
    out = _view(tmp_path, r'''
  let resolve;
  const writes = [];
  Api.dispatch = {keyStats: () => new Promise(done => {resolve = done;}),
    keyOverride: async body => {writes.push(body); return {row: {enabled: false}};}};
  _v2KeyOverride('p1', 'cred-a', false);
  await tick();
  _destroyProviderCredentialViews();
  resolve({key_namespace: 'owner:', providers: {}});
  await tick();
  out.writes = writes;
  out.closedRow = _v2KeyStatRow('p1', 'cred-a');
''')
    assert out == {'writes': [{'provider_id': 'owner:p1', 'key_name': 'cred-a', 'enabled': False}], 'closedRow': None}


@pytest.mark.parametrize('replace_draft', [False, True])
def test_completed_override_survives_a_reopened_views_older_snapshot(tmp_path, replace_draft):
    out = _view(tmp_path, r'''
  let reads = 0, readReply, writeReply;
  Api.dispatch = {
    keyStats: async () => {
      reads++;
      if (reads === 1) return {key_namespace: 'owner:', providers: {}};
      return new Promise(resolve => {readReply = resolve;});
    },
    keyOverride: () => new Promise(resolve => {writeReply = resolve;}),
  };
  _loadV2KeyStats();
  await tick();
  _v2KeyOverride('p1', 'cred-a', false);
  await tick();
  _destroyProviderCredentialViews();
  if (REPLACE) _stgModelRouting = JSON.parse(JSON.stringify(_stgModelRouting));
  _loadV2KeyStats();
  await tick();
  writeReply({row: {enabled: false, override: false, total: 3}});
  await tick();
  readReply({key_namespace: 'owner:', providers: {'owner:p1': {'cred-a': {enabled: true, total: 2}}}});
  await tick();
  out.row = _v2KeyStatRow('p1', 'cred-a');
'''.replace('REPLACE', json.dumps(replace_draft)))
    assert out['row'] == {'enabled': False, 'override': False, 'total': 3}


def test_canonical_override_order_survives_a_draft_refresh(tmp_path):
    out = _view(tmp_path, r'''
  const bodies = [], releases = [];
  Api.dispatch = {keyStats: async () => ({key_namespace: 'owner:', providers: {}}),
    keyOverride: body => {bodies.push(body); return new Promise(resolve => {releases.push(resolve);});}};
  _v2KeyOverride('p1', 'cred-a', false);
  await tick();
  _stgModelRouting = JSON.parse(JSON.stringify(_stgModelRouting));
  _v2KeyOverride('p1', 'cred-a', true);
  await tick();
  out.beforeFirstAck = bodies.length;
  releases[0]({row: {enabled: false}});
  await tick();
  releases[1]({row: {enabled: true}});
  await tick();
  out.targets = bodies.map(row => row.enabled);
  out.enabled = _v2KeyStatRow('p1', 'cred-a').enabled;
''')
    assert out == {'beforeFirstAck': 1, 'targets': [False, True], 'enabled': True}


def test_independent_credentials_do_not_wait_for_each_others_override(tmp_path):
    out = _view(tmp_path, r'''
  _stgModelRouting.credentials.push({..._stgModelRouting.credentials[0], credential_id: 'cred-b', provider_access_id: 'p2-access'});
  const bodies = [], releases = [];
  Api.dispatch = {keyStats: async () => ({key_namespace: 'owner:', providers: {}}),
    keyOverride: body => {bodies.push(body); return new Promise(resolve => {releases.push(resolve);});}};
  _v2KeyOverride('p1', 'cred-a', false);
  _v2KeyOverride('p2', 'cred-b', false);
  await tick();
  out.targets = bodies.map(row => row.provider_id).sort();
  releases.forEach(resolve => resolve({row: {enabled: false}}));
  await tick();
''')
    assert out['targets'] == ['owner:p1', 'owner:p2']


def test_durable_disabled_credential_still_uses_the_settings_save_owner(tmp_path):
    out = _view(tmp_path, r'''
  _stgModelRouting.credentials[0].enabled = false;
  let writes = 0;
  Api.dispatch = {keyOverride: async () => {writes++; return null;}};
  global._setModelRoutingCollectionField = (collection, index, field, value) => {
    _stgModelRouting[collection][index][field] = value;
  };
  _onV2KeyToggle('p1', 'cred-a', true);
  await tick();
  out.durable = _stgModelRouting.credentials[0].enabled;
  out.runtimeWrites = writes;
''')
    assert out == {'durable': True, 'runtimeWrites': 0}


@pytest.mark.parametrize('prefix,has_current', [('owner:current:', True), ('owner:current:', False), ('', False)])
def test_server_namespace_wins_over_legacy_or_foreign_statistics(tmp_path, prefix, has_current):
    out = _view(tmp_path, r'''
  const prefix = PREFIX, target = prefix + 'p1', writes = [];
  const providers = {'owner:other:p1': {'cred-a': {enabled: true, total: 2}}};
  if (prefix) providers.p1 = {'cred-a': {enabled: true, total: 1}};
  if (HAS_CURRENT) providers[target] = {'cred-a': {enabled: false, total: 7}};
  Api.dispatch = {keyStats: async () => ({key_namespace: prefix, providers}),
    keyOverride: async body => {writes.push(body.provider_id); return {row: {enabled: false}};}};
  _loadV2KeyStats();
  await tick();
  out.total = _v2KeyStatRow('p1', 'cred-a')?.total || null;
  _v2KeyOverride('p1', 'cred-a', false);
  await tick();
  out.writes = writes;
'''.replace('PREFIX', json.dumps(prefix)).replace('HAS_CURRENT', json.dumps(has_current)))
    assert out == {'total': 7 if has_current else None, 'writes': [prefix + 'p1']}


def test_older_override_ack_keeps_the_newer_checkbox_intent(tmp_path):
    out = _view(tmp_path, r'''
  const releases = [];
  Api.dispatch = {keyStats: async () => ({key_namespace: 'owner:', providers: {}}),
    keyOverride: () => new Promise(resolve => {releases.push(resolve);})};
  _loadV2KeyStats();
  await tick();
  _v2KeyOverride('p1', 'cred-a', false);
  _v2KeyOverride('p1', 'cred-a', true);
  await tick();
  releases[0]({row: {enabled: false, override: false}});
  await tick();
  out.pendingChecked = card().querySelector('.stg-key-toggle input').checked;
  out.busy = card().querySelector('.stg-keystat-actions').getAttribute('aria-busy');
  releases[1]({row: {enabled: true, override: true}});
  await tick();
  out.settledChecked = card().querySelector('.stg-key-toggle input').checked;
  out.settledBusy = card().querySelector('.stg-keystat-actions').getAttribute('aria-busy');
''')
    assert out == {'pendingChecked': True, 'busy': 'true', 'settledChecked': True, 'settledBusy': 'false'}


def test_statistics_only_read_own_credential_properties(tmp_path):
    out = _view(tmp_path, r'''
  Api.dispatch = {keyStats: async () => ({key_namespace: 'owner:', providers: {'owner:p1': {}}})};
  _loadV2KeyStats();
  await tick();
  out.absent = _v2KeyStatRow('p1', 'toString') === null;
''')
    assert out['absent'] is True
