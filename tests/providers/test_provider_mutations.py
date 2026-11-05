"""Immediate Provider commands keep confirmation, submission and draft ownership."""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.providers.test_provider_credentials_view import _view

pytestmark = pytest.mark.unit


def _mutation_view(tmp_path, scenario):
    return _view(tmp_path, r'''
  global._loadModelRoutingAuthority = async () => _stgModelRouting;
  global._renderModelRoutingTabs = () => _renderProvidersTab();
  const newInput = () => document.querySelector('.stg-key-card--new input');
  const newCard = () => document.querySelector('.stg-key-card--new');
  const reply = bundle => ({provider: bundle, revision: 2});
''' + scenario)


def test_failed_new_credential_submission_is_retryable_without_taking_focus(tmp_path):
    out = _mutation_view(tmp_path, r'''
  let reject;
  Api.modelRouting.saveProvider = () => new Promise((_, no) => {reject = no;});
  _startNewV2ApiKey('p1');
  const input = newInput();
  input.value = 'synthetic-new-key';
  const submitting = _getProviderKeyEditor().submit(input);
  await tick();
  document.getElementById('other').focus();
  reject(new Error('synthetic refusal'));
  await submitting;
  out.disabled = input.disabled;
  out.value = input.value;
  out.focus = document.activeElement.id;
  out.alerts = __alerts.length;
''')
    assert out == {'disabled': False, 'value': 'synthetic-new-key',
                   'focus': 'other', 'alerts': 1}


def test_enter_and_blur_share_one_accepted_credential_submission(tmp_path):
    out = _mutation_view(tmp_path, r'''
  const writes = [], releases = [];
  Api.modelRouting.saveProvider = (id, bundle) => {
    writes.push(bundle); return new Promise(resolve => {releases.push(() => resolve(reply(bundle)));});
  };
  _startNewV2ApiKey('p1');
  const input = newInput(), card = newCard();
  input.value = 'synthetic-new-key';
  input.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
  input.dispatchEvent(new dom.window.FocusEvent('blur'));
  await tick();
  out.writes = writes.length;
  releases.forEach(release => release());
  await tick();
''')
    assert out['writes'] == 1


@pytest.mark.parametrize('retirement', ['cancel', 'close', 'rerender'])
def test_retiring_new_credential_input_scrubs_plaintext(tmp_path, retirement):
    out = _mutation_view(tmp_path, r'''
  _startNewV2ApiKey('p1');
  const input = newInput();
  input.value = 'synthetic-new-key';
  if (RETIREMENT === 'cancel') newCard().querySelector('.stg-keys-btn').click();
  if (RETIREMENT === 'close') _destroyProviderCredentialViews();
  if (RETIREMENT === 'rerender') _renderProvidersTab();
  await tick();
  out.value = input.value;
'''.replace('RETIREMENT', repr(retirement)))
    assert out['value'] == ''


@pytest.mark.parametrize('command', ['credential', 'provider'])
def test_confirmation_cannot_mutate_a_replaced_provider_draft(tmp_path, command):
    out = _mutation_view(tmp_path, r'''
  let confirm;
  const writes = [];
  global.showConfirm = () => new Promise(resolve => {confirm = resolve;});
  Api.modelRouting.saveProvider = async (id, bundle, revision) => {
    writes.push({id, revision}); return reply(bundle);
  };
  Api.modelRouting.deleteProvider = async (id, revision) => {
    writes.push({id, revision}); return {deleted: id, revision: revision + 1};
  };
  const pending = COMMAND === 'credential' ? _deleteV2Credential('p1', 0) : _deleteModelRoutingProvider('p1');
  _stgModelRouting = freshRouting();
  _stgModelRoutingRevision = 9;
  confirm(true);
  await pending;
  out.writes = writes;
  out.toasts = __toasts;
  out.alerts = __alerts.length;
'''.replace('COMMAND', repr(command)))
    assert out['writes'] == []
    assert out['toasts'] == []
    assert out['alerts'] == 1


def test_absent_provider_delete_receipt_is_not_reported_as_success(tmp_path):
    out = _mutation_view(tmp_path, r'''
  Api.modelRouting.deleteProvider = async () => null;
  await _deleteModelRoutingProvider('p1');
  out.toasts = __toasts;
  out.alerts = __alerts.length;
''')
    assert out == {'toasts': [], 'alerts': 1}


def _authority_view(tmp_path, scenario):
    authority = native_module_path('.native/provider-routing-authority.js',
        ROOT / 'frontend/src/features/settings/model-routing-authority.ts')
    return _view(tmp_path, r'''
  eval(fs.readFileSync(AUTHORITY, 'utf8'));
  for (const name of ['_stgModelRouting', '_stgModelRoutingRevision', '_stgModelRoutingSavedJson',
    '_stgRoutingPersistChain', '_stgModelRoutingLoadPromise', '_stgModelRoutingLoadError',
    '_stgPendingCredentialSecrets', 'Api']) {
    Object.defineProperty(featureRegistry, name, {configurable: true,
      get: () => global[name], set: value => {global[name] = value;}});
  }
  global._loadModelRoutingAuthority = loadModelRoutingAuthority;
  global._persistModelRoutingNow = persistModelRoutingNow;
  global._runModelRoutingMutation = global.runModelRoutingMutation;
  global._stgModelRoutingSavedJson = JSON.stringify(_stgModelRouting);
  const clone = value => JSON.parse(JSON.stringify(value));
  const initial = _stgModelRouting;
  const server = clone(initial);
  let serverRevision = 1;
  const writes = [];
  Api.modelRouting.get = async () => ({model_routing: clone(server), revision: serverRevision});
  Api.modelRouting.saveProvider = async (id, bundle, revision) => {
    writes.push({id, revision});
    server.credentials = clone(bundle.credentials);
    serverRevision++;
    return {provider: clone(bundle), revision: serverRevision};
  };
'''.replace('AUTHORITY', json.dumps(authority)) + scenario)


def test_provider_mutation_joins_the_shared_routing_write_chain(tmp_path):
    out = _authority_view(tmp_path, r'''
  let release;
  _stgRoutingPersistChain = new Promise(resolve => {release = resolve;});
  const pending = _saveNewProviderCredential(_modelRoutingProviderContext('p1'), 'synthetic-new-key');
  await tick();
  out.before = writes.length;
  release();
  await pending;
  out.after = writes.length;
  out.revision = _stgModelRoutingRevision;
''')
    assert out == {'before': 0, 'after': 1, 'revision': 2}


@pytest.mark.parametrize('when', ['before', 'during'])
def test_provider_commit_preserves_unrelated_unsaved_draft_and_its_cas(tmp_path, when):
    out = _authority_view(tmp_path, r'''
  const write = Api.modelRouting.saveProvider;
  let release;
  Api.modelRouting.saveProvider = (...args) => new Promise(resolve => {
    release = () => resolve(write(...args));
  });
  if (WHEN === 'before') initial.provider_accesses[1].display_name = 'newer local edit';
  const pending = _saveNewProviderCredential(_modelRoutingProviderContext('p1'), 'synthetic-new-key');
  await tick();
  if (WHEN === 'during') initial.provider_accesses[1].display_name = 'newer local edit';
  release();
  await pending;
  out.sameDraft = _stgModelRouting === initial;
  out.name = _stgModelRouting.provider_accesses[1].display_name;
  out.revision = _stgModelRoutingRevision;
  out.committed = server.credentials.length;
'''.replace('WHEN', repr(when)))
    assert out == {'sameDraft': True, 'name': 'newer local edit', 'revision': 1, 'committed': 2}


def test_refresh_failure_does_not_discard_the_draft_or_reject_an_acknowledged_add(tmp_path):
    out = _authority_view(tmp_path, r'''
  Api.modelRouting.get = async () => {throw new Error('synthetic refresh outage');};
  let rejected = false;
  try {await _saveNewProviderCredential(_modelRoutingProviderContext('p1'), 'synthetic-new-key');}
  catch {rejected = true;}
  out.rejected = rejected;
  out.sameDraft = _stgModelRouting === initial;
  out.revision = _stgModelRoutingRevision;
  out.committed = server.credentials.length;
''')
    assert out == {'rejected': False, 'sameDraft': True, 'revision': 1, 'committed': 2}


def test_provider_refresh_keeps_unrelated_secret_replacements(tmp_path):
    out = _authority_view(tmp_path, r'''
  _stgPendingCredentialSecrets['cred-a'] = 'synthetic-staged-replacement';
  await _saveNewProviderCredential(_modelRoutingProviderContext('p1'), 'synthetic-new-key');
  out.pending = _stgPendingCredentialSecrets;
  out.committed = _stgModelRouting.credentials.length;
''')
    assert out == {'pending': {'cred-a': 'synthetic-staged-replacement'}, 'committed': 2}


def test_cancel_pointer_does_not_commit_via_the_blur_it_causes(tmp_path):
    out = _mutation_view(tmp_path, r'''
  let writes = 0;
  Api.modelRouting.saveProvider = async (id, bundle) => {writes++; return reply(bundle);};
  _startNewV2ApiKey('p1');
  const input = newInput(), cancel = newCard().querySelector('.stg-keys-btn');
  input.value = 'synthetic-new-key';
  cancel.dispatchEvent(new dom.window.MouseEvent('pointerdown', {bubbles: true, cancelable: true}));
  input.dispatchEvent(new dom.window.FocusEvent('blur', {relatedTarget: cancel}));
  cancel.click();
  await tick();
  out.writes = writes;
  out.value = input.value;
''')
    assert out == {'writes': 0, 'value': ''}


def test_older_accepted_add_keeps_a_reopened_input_and_its_next_submission(tmp_path):
    out = _authority_view(tmp_path, r'''
  const write = Api.modelRouting.saveProvider;
  let release;
  Api.modelRouting.saveProvider = (...args) => new Promise(resolve => {
    release = () => resolve(write(...args));
  });
  _startNewV2ApiKey('p1');
  const first = document.querySelector('.stg-key-card--new input');
  first.value = 'synthetic-first-key';
  const pending = _getProviderKeyEditor().submit(first);
  await tick();
  _destroyProviderCredentialViews();
  _startNewV2ApiKey('p1');
  const next = document.querySelector('.stg-key-card--new input');
  next.value = 'synthetic-next-key';
  release();
  await pending;
  out.connected = next.isConnected;
  out.value = next.value;
  Api.modelRouting.saveProvider = write;
  await _getProviderKeyEditor().submit(next);
  out.revisions = writes.map(row => row.revision);
  out.count = server.credentials.length;
''')
    assert out == {'connected': True, 'value': 'synthetic-next-key',
                   'revisions': [1, 2], 'count': 3}


def test_credential_confirmation_keeps_its_original_identifier(tmp_path):
    out = _authority_view(tmp_path, r'''
  let confirm;
  global.showConfirm = () => new Promise(resolve => {confirm = resolve;});
  const pending = _deleteV2Credential('p1', 0);
  initial.credentials[0].credential_id = 'replacement-id';
  confirm(true);
  await pending;
  out.writes = writes.length;
  out.alerts = __alerts.length;
''')
    assert out == {'writes': 0, 'alerts': 1}


def test_credential_delete_only_retires_its_own_pending_replacement(tmp_path):
    out = _authority_view(tmp_path, r'''
  const second = {...clone(initial.credentials[0]), credential_id: 'cred-b'};
  initial.credentials.push(second);
  server.credentials.push(clone(second));
  _stgModelRoutingSavedJson = JSON.stringify(initial);
  _stgPendingCredentialSecrets = {'cred-a': 'synthetic-deleted', 'cred-b': 'synthetic-kept'};
  await _deleteV2Credential('p1', 0);
  out.credentials = _stgModelRouting.credentials.map(row => row.credential_id);
  out.pending = _stgPendingCredentialSecrets;
  out.revision = _stgModelRoutingRevision;
  out.alerts = __alerts;
''')
    assert out == {'credentials': ['cred-b'], 'pending': {'cred-b': 'synthetic-kept'},
                   'revision': 2, 'alerts': []}


def test_rejected_provider_write_does_not_poison_the_shared_chain(tmp_path):
    out = _authority_view(tmp_path, r'''
  const write = Api.modelRouting.saveProvider;
  Api.modelRouting.saveProvider = async () => {throw new Error('synthetic first refusal');};
  try {await _saveNewProviderCredential(_modelRoutingProviderContext('p1'), 'synthetic-first');}
  catch { /* The adapter already reports the failed authorized command. */ }
  Api.modelRouting.saveProvider = write;
  await _saveNewProviderCredential(_modelRoutingProviderContext('p1'), 'synthetic-next');
  out.revision = _stgModelRoutingRevision;
  out.writes = writes.length;
  out.credentials = server.credentials.length;
''')
    assert out == {'revision': 2, 'writes': 1, 'credentials': 2}


def test_queued_provider_write_refuses_a_changed_access_identity(tmp_path):
    out = _authority_view(tmp_path, r'''
  let release;
  _stgRoutingPersistChain = new Promise(resolve => {release = resolve;});
  const pending = _saveNewProviderCredential(_modelRoutingProviderContext('p1'), 'synthetic-new');
  initial.provider_accesses[0].provider_access_id = 'replacement-access';
  release();
  let rejected = false;
  try {await pending;} catch {rejected = true;}
  out.rejected = rejected;
  out.writes = writes.length;
''')
    assert out == {'rejected': True, 'writes': 0}


def test_new_add_after_an_acknowledged_add_uses_the_adopted_revision(tmp_path):
    out = _authority_view(tmp_path, r'''
  const context = _modelRoutingProviderContext('p1');
  const first = _saveNewProviderCredential(context, 'synthetic-one');
  const second = _saveNewProviderCredential(context, 'synthetic-two');
  await Promise.all([first, second]);
  out.revisions = writes.map(row => row.revision);
  out.credentials = _stgModelRouting.credentials.length;
''')
    assert out == {'revisions': [1, 2], 'credentials': 3}


def test_render_failure_cannot_reject_an_acknowledged_add(tmp_path):
    out = _authority_view(tmp_path, r'''
  let faults = 0;
  document.getElementById('stgProviderList').querySelectorAll = () => {
    faults++; throw new Error('synthetic render failure');
  };
  let rejected = false;
  try {await _saveNewProviderCredential(_modelRoutingProviderContext('p1'), 'synthetic-new');}
  catch {rejected = true;}
  out.rejected = rejected;
  out.writes = writes.length;
  out.revision = _stgModelRoutingRevision;
  out.faults = faults;
''')
    assert out == {'rejected': False, 'writes': 1, 'revision': 2, 'faults': 1}


def test_delayed_error_focus_cannot_resubmit_an_unchanged_failed_key(tmp_path):
    out = _mutation_view(tmp_path, r'''
  let writes = 0, showError;
  global.showAlert = message => {__alerts.push(message); showError = () => document.getElementById('other').focus();};
  Api.modelRouting.saveProvider = async () => {writes++; throw new Error('synthetic refusal');};
  _startNewV2ApiKey('p1');
  const input = newInput();
  input.value = 'synthetic-new-key';
  await _getProviderKeyEditor().submit(input);
  showError();
  await tick();
  out.writes = writes;
  out.disabled = input.disabled;
  // Enter remains an explicit retry even when the failed text is unchanged.
  await _getProviderKeyEditor().submit(input);
  out.afterRetry = writes;
''')
    assert out == {'writes': 1, 'disabled': False, 'afterRetry': 2}


def test_another_dialog_taking_focus_is_not_an_implicit_key_submission(tmp_path):
    out = _mutation_view(tmp_path, r'''
  let writes = 0;
  Api.modelRouting.saveProvider = async (id, bundle) => {writes++; return reply(bundle);};
  _startNewV2ApiKey('p1');
  const input = newInput();
  input.value = 'synthetic-new-key';
  const dialog = document.createElement('div');
  dialog.setAttribute('role', 'alertdialog');
  dialog.innerHTML = '<button>OK</button>';
  document.body.append(dialog);
  dialog.querySelector('button').focus();
  await tick();
  out.writes = writes;
  out.value = input.value;
''')
    assert out == {'writes': 0, 'value': 'synthetic-new-key'}


def _zh(key):
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text(encoding='utf-8'))
    return locale[key]


def _listed_import_view(tmp_path, scenario):
    return _authority_view(tmp_path, r'''
  global._renderModelRoutingTabs = () => _renderProvidersTab();
  _openProviderModelPicker('p1');
  const loadAll = () => document.getElementById('stgProviderListedImport');
''' + scenario)


def test_load_all_joins_the_write_chain_and_refreshes_the_projection(tmp_path):
    out = _listed_import_view(tmp_path, r'''
  const imports = [];
  let finish;
  Api.modelRouting.importListedModels = providerId => {
    imports.push(providerId);
    return new Promise(resolve => {finish = () => {
      server.offerings.push({offering_id: 'off-listed', provider_access_id: 'p1-access',
        identity_state: 'pending_identity', pending_model_id: 'relay/listed', enabled: true,
        stale: false, capabilities: ['text'], context_window: 1000, priority: 1});
      server.deployments.push({deployment_id: 'dep-listed', offering_id: 'off-listed',
        connection_id: 'p1-conn', wire_model_id: 'relay/listed', identity_confidence: 'pending',
        probe_status: 'unprobed', priority: 1});
      serverRevision++;
      resolve({provider_id: providerId, listed_model_count: 2, added_model_count: 1,
               revision: serverRevision});
    };});
  };
  let release;
  _stgRoutingPersistChain = new Promise(resolve => {release = resolve;});
  const pending = _importListedProviderModels();
  await tick();
  out.queuedBehindWrite = imports.length === 0;
  out.disabledWhileLoading = loadAll().disabled;
  release();
  while (!finish) await tick();
  finish();
  await pending;
  out.imports = imports;
  out.enabledAfter = !loadAll().disabled;
  out.revision = _stgModelRoutingRevision;
  out.listedWire = _stgModelRouting.deployments.some(row => row.wire_model_id === 'relay/listed');
  out.toasts = __toasts;
  out.alerts = __alerts.length;
''')
    assert out == {
        'queuedBehindWrite': True, 'disabledWhileLoading': True, 'imports': ['p1'],
        'enabledAfter': True, 'revision': 2, 'listedWire': True,
        'toasts': [_zh('settings.listedModelsAdded')], 'alerts': 0,
    }


def test_load_all_with_nothing_new_is_not_a_failed_write(tmp_path):
    out = _listed_import_view(tmp_path, r'''
  Api.modelRouting.importListedModels = async providerId => ({
    provider_id: providerId, listed_model_count: 1, added_model_count: 0, revision: serverRevision});
  await _importListedProviderModels();
  out.revision = _stgModelRoutingRevision;
  out.toasts = __toasts;
  out.alerts = __alerts.length;
''')
    assert out == {'revision': 1, 'toasts': [_zh('settings.listedModelsUpToDate')], 'alerts': 0}


def test_failed_load_all_is_reported_and_retryable(tmp_path):
    out = _listed_import_view(tmp_path, r'''
  Api.modelRouting.importListedModels = async () => {throw new Error('synthetic listing failure');};
  await _importListedProviderModels();
  out.alerts = __alerts;
  out.enabled = !loadAll().disabled;
  out.revision = _stgModelRoutingRevision;
''')
    assert out == {'alerts': [_zh('settings.listedModelsFailed')], 'enabled': True, 'revision': 1}


def _creation_view(tmp_path, scenario):
    return _authority_view(tmp_path, r'''
  global._renderModelRoutingTabs = () => _renderProvidersTab();
  const scrolled = [];
  dom.window.Element.prototype.scrollIntoView = function() {scrolled.push(this.dataset.providerId);};
  let onCommit = () => {};
  Api.modelRouting.createProvider = async (draft, revision) => {
    if (revision !== serverRevision) throw new Error('model-routing revision changed');
    server.providers.push(clone(draft.provider));
    server.provider_accesses.push(clone(draft.provider_access));
    server.connections.push(...clone(draft.connections));
    server.credentials.push(...clone(draft.credentials));
    serverRevision++;
    onCommit();
    return {provider: clone(draft), revision: serverRevision};
  };
  const relayBundle = () => ({
    provider: {provider_id: 'p3', name: 'Relay', scope: 'owner', brand: 'generic'},
    provider_access: {provider_access_id: 'p3-access', provider_id: 'p3', display_name: 'Relay',
      enabled: true, quota_policy: {}},
    connections: [{connection_id: 'p3-conn', provider_access_id: 'p3-access',
      base_url: 'https://relay3.example/v1', protocol: 'openai', enabled: true, priority: 0,
      extra_headers: {}}],
    credentials: [{credential_id: 'p3-cred', provider_access_id: 'p3-access', kind: 'api_key',
      secret_reference: '', key_hint: '', enabled: true,
      authorization: {connection_ids: ['p3-conn'], models: []}, quota_policy: {}}],
    offerings: [], deployments: [], creators: [], models: []});
  const cards = () => Array.from(document.querySelectorAll('.stg-provider-card-v2'))
    .map(card => card.dataset.providerId + (card.open ? ':open' : ''));
''' + scenario)


@pytest.mark.parametrize('listing, notice', [
    ({'listed': 1347, 'imported': 512}, 'settings.providerModelsRetained'),
    ({'listed': 3, 'imported': 3}, 'providerCatalog.addedWithKey'),
])
def test_partly_imported_listing_says_how_to_load_the_rest(tmp_path, listing, notice):
    out = _creation_view(tmp_path, r'''
  out.staged = await _stageModelRoutingProviderBundle(relayBundle(), 'synthetic-key', LISTING);
  out.toasts = __toasts;
  out.alerts = __alerts;
'''.replace('LISTING', json.dumps(listing)))
    assert out == {'staged': True, 'toasts': [_zh(notice)], 'alerts': []}


@pytest.mark.parametrize('first', ['hint', 'command'])
def test_added_provider_card_appears_while_the_commit_wake_hint_read_is_in_flight(tmp_path, first):
    # The repository publishes model_routing.changed inside its commit, before
    # the endpoint returns, so the real live sync already reads when the
    # receipt arrives. Either read may finish first.
    sync = native_module_path('.native/model-routing-sync.js',
        ROOT / 'frontend/src/features/settings/model-routing-sync.ts')
    out = _creation_view(tmp_path, r'''
  const hints = [];
  global.pushSubscribe = (channel, taskId, handler) => {hints.push(handler);};
  eval(fs.readFileSync(SYNC, 'utf8'));
  onCommit = () => hints.forEach(handler =>
    handler({type: 'model_routing.changed', userId: 1, rev: serverRevision}));
  const reads = [];
  Api.modelRouting.get = () => {
    const response = {model_routing: clone(server), revision: serverRevision};
    return new Promise(resolve => {reads.push(() => resolve(response));});
  };
  let settled = false;
  const staging = _stageModelRoutingProviderBundle(relayBundle(), '')
    .finally(() => {settled = true;});
  while (!settled) {
    await tick();
    const release = FIRST === 'hint' ? reads.shift() : reads.pop();
    if (release) release();
  }
  out.staged = await staging;
  while (reads.length) {reads.shift()(); await tick();}
  out.hinted = hints.length;
  out.cards = cards();
  out.scrolled = scrolled;
  out.revision = _stgModelRoutingRevision;
  out.toasts = __toasts;
  out.alerts = __alerts;
'''.replace('SYNC', json.dumps(sync)).replace('FIRST', repr(first)))
    assert out == {'staged': True, 'hinted': 1, 'cards': ['p1', 'p2', 'p3:open'],
                   'scrolled': ['p3'], 'revision': 2,
                   'toasts': [_zh('providerCatalog.addedWithoutKey')], 'alerts': []}


def test_provider_add_keeps_unrelated_unsaved_draft_and_pending_replacements(tmp_path):
    out = _creation_view(tmp_path, r'''
  initial.provider_accesses[1].display_name = 'newer local edit';
  _stgPendingCredentialSecrets['cred-a'] = 'synthetic-staged-replacement';
  out.staged = await _stageModelRoutingProviderBundle(relayBundle(), '');
  out.sameDraft = _stgModelRouting === initial;
  out.name = _stgModelRouting.provider_accesses[1].display_name;
  out.pending = _stgPendingCredentialSecrets;
  out.revision = _stgModelRoutingRevision;
  out.committed = server.providers.map(row => row.provider_id);
  out.alerts = __alerts;
''')
    assert out == {'staged': True, 'sameDraft': True, 'name': 'newer local edit',
                   'pending': {'cred-a': 'synthetic-staged-replacement'}, 'revision': 1,
                   'committed': ['p1', 'p2', 'p3'],
                   'alerts': [_zh('settings.providerSavedDraftKept')]}


def test_provider_add_joins_the_shared_routing_write_chain(tmp_path):
    out = _creation_view(tmp_path, r'''
  let release;
  _stgRoutingPersistChain = new Promise(resolve => {release = resolve;});
  const pending = _stageModelRoutingProviderBundle(relayBundle(), '');
  await tick();
  out.before = server.providers.length;
  release();
  out.staged = await pending;
  out.after = server.providers.length;
  out.cards = cards();
''')
    assert out == {'before': 2, 'staged': True, 'after': 3, 'cards': ['p1', 'p2', 'p3:open']}
