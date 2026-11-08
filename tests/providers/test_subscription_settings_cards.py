"""Settings → Subscription sign-in: the Kimi Code card and coding-plan key cards.

The cards run in the real `static/settings_panels/oauth.html` markup with the
typed owners (`kimi-code-login.ts`, `subscription-plans.ts`), the shared OAuth
panel lifetime and the shared routing authority. Transport is synthetic: no
sign-in or provider request leaves the process.
"""
import json
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import _require_frontend_bundler

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SETTINGS = ROOT / 'frontend/src/features/settings'


@pytest.fixture(scope='module')
def cards_bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp('subscription-settings-cards')
    entry = directory / 'entry.ts'
    entry.write_text('\n'.join([
        'export {featureRegistry as bridge} from ' + json.dumps(str(ROOT/'frontend/src/feature-registry')) + ';',
        'export {kimiCodeLogin} from ' + json.dumps(str(SETTINGS/'kimi-code-login')) + ';',
        'export {subscriptionPlans} from ' + json.dumps(str(SETTINGS/'subscription-plans')) + ';',
        'export {captureOAuthPanel} from ' + json.dumps(str(SETTINGS/'oauth-panel-lifetime')) + ';',
        'export {beginSettingsReadSession} from ' + json.dumps(str(SETTINGS/'panel-read-session')) + ';',
        'import ' + json.dumps(str(SETTINGS/'core-panel')) + ';',
        'import ' + json.dumps(str(SETTINGS/'save-export')) + ';',
    ]))
    _require_frontend_bundler()
    output = directory / 'cards.cjs'
    result = subprocess.run(['node', 'scripts/vite_test_bundle.mjs', str(entry),
        '--bundle', '--platform=node', '--format=cjs', f'--outfile={output}'],
        cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return output


HARNESS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require(path.join(ROOT, 'node_modules/jsdom'));
const panel = fs.readFileSync(path.join(ROOT, 'static/settings_panels/oauth.html'), 'utf8');
const dom = new JSDOM(`<!doctype html><body><div id="settingsModal" class="open">${panel}</div></body>`,
  {url: 'http://localhost'});
for (const name of ['window', 'document', 'localStorage', 'HTMLElement', 'HTMLInputElement',
    'HTMLButtonElement', 'HTMLAnchorElement', 'HTMLSelectElement', 'HTMLTextAreaElement',
    'MutationObserver', 'AbortController', 'AbortSignal', 'Event', 'CustomEvent']) {
  global[name] = name === 'window' ? dom.window : dom.window[name];
}
const copied = [];
Object.defineProperty(dom.window.navigator, 'clipboard',
  {value: {writeText: async text => { copied.push(text); }}, configurable: true});
Object.defineProperty(global, 'navigator', {value: dom.window.navigator, configurable: true});
const timers = new Map(); let timerId = 0;
dom.window.setTimeout = callback => { timers.set(++timerId, {callback, repeat: false}); return timerId; };
dom.window.clearTimeout = id => { timers.delete(id); };
dom.window.setInterval = callback => { timers.set(++timerId, {callback, repeat: true}); return timerId; };
dom.window.clearInterval = id => { timers.delete(id); };
const intervals = () => [...timers.values()].filter(timer => timer.repeat).length;
const {bridge, kimiCodeLogin, subscriptionPlans, captureOAuthPanel, beginSettingsReadSession} = require(BUILT);
const tick = () => new Promise(resolve => setImmediate(resolve));
const settle = async () => { for (let i = 0; i < 12; i++) await tick(); };
const advance = async () => {
  for (const [id, timer] of [...timers]) { if (!timer.repeat) timers.delete(id); timer.callback(); }
  await settle();
};
const clone = value => JSON.parse(JSON.stringify(value));
const defer = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
const byId = id => document.getElementById(id);
const VERIFY = 'https://auth.fixture.invalid/device?user_code=WXYZ-1234';

// ── Kimi Code: the retained bridge's status read and catalogue ports ──
let kimi = {status: 'not_started', authenticated: false};
let statusReads = 0, catalogRefreshes = 0, confirmAnswer = true, confirmations = 0;
const loads = [], alerts = [], deviceLogins = [], logouts = [], out = {};
const statusPorts = {
  read: async () => { statusReads++; return {claude: {}, codex: {}, kimi: clone(kimi)}; },
  publish: data => kimiCodeLogin.render(data.kimi),
};
const ports = {
  loadStatus(fromRepoll) {
    loads.push(!!fromRepoll);
    const view = captureOAuthPanel();
    return view ? view.loadStatus(statusPorts, !!fromRepoll) : null;
  },
  refreshCatalog() { catalogRefreshes++; return Promise.resolve({}); },
};
let deviceResponse = () => ({ok: true, status: 200,
  json: async () => ({status: 'started', user_code: 'WXYZ-1234', verification_url: VERIFY})});

// ── Coding plans: committed routing authority and template catalogue ──
const PLAN_TEMPLATES = [
  {key: 'bailian_coding_plan', name: 'Bailian Coding Plan', brand: 'qwen', category: 'subscription',
   catalog_source: {url: 'https://docs.fixture.invalid/bailian'},
   subscription: {key_console_url: 'https://console.fixture.invalid/bailian'}},
  {key: 'minimax_mplan', name: 'MiniMax M Plan', brand: 'minimax', category: 'subscription',
   subscription: {key_console_url: 'javascript:alert(1)'}},
  {key: 'pay_as_you_go', name: 'Pay as you go', brand: 'openai', category: 'official'},
];
let server = {contract_version: 'tofu.model-routing/v2', revision: 1, creators: [], models: [],
  providers: [], provider_accesses: [], connections: [], credentials: [], offerings: [], deployments: []};
function adopt() {
  bridge._stgModelRouting = clone(server); bridge._stgModelRoutingRevision = server.revision;
  bridge._stgModelRoutingSavedJson = JSON.stringify(bridge._stgModelRouting);
}
function connectPlan(key) {
  const access = key + '-access';
  server.providers.push({provider_id: key, name: key, brand: 'generic'});
  server.provider_accesses.push({provider_access_id: access, provider_id: key, enabled: true});
  server.connections.push({connection_id: key + '-connection', provider_access_id: access});
  server.credentials.push({credential_id: key + '-key', provider_access_id: access,
    kind: 'api_key', secret_reference: 'synthetic-stored'});
  for (const model of ['plan-model-a', 'plan-model-b']) {
    server.offerings.push({offering_id: `${key}-${model}`, provider_access_id: access, capabilities: ['text']});
  }
}
adopt(); bridge._stgPendingCredentialSecrets = {};
const compiled = [], staged = [], deletes = [];
let stageResult = true, renders = 0;
bridge._adoptModelRoutingResponse = () => true;
bridge._renderModelRoutingTabs = () => { renders++; };
bridge.showConfirm = async () => { confirmations++; return confirmAnswer; };
bridge.showAlert = message => { alerts.push(String(message)); };
bridge.Api = {
  oauth: {
    deviceLoginPost: async provider => { deviceLogins.push(provider); return deviceResponse(); },
    logoutPost: async provider => {
      logouts.push(provider); kimi = {status: 'not_started', authenticated: false};
      return {ok: true, status: 200, json: async () => ({ok: true})};
    },
  },
  providers: {
    templates: async () => clone(PLAN_TEMPLATES),
    compileTemplate: async key => {
      compiled.push(key);
      return {provider_bundle: {provider: {provider_id: key}, connections: [], credentials: [], offerings: []}};
    },
  },
  modelRouting: {
    get: async () => ({model_routing: clone(server), revision: server.revision}),
    deleteProvider: async (id, revision) => {
      assert.equal(revision, server.revision); deletes.push(id); server.revision++;
      const accesses = new Set(server.provider_accesses.filter(row => row.provider_id === id)
        .map(row => row.provider_access_id));
      server.providers = server.providers.filter(row => row.provider_id !== id);
      for (const field of ['provider_accesses', 'connections', 'credentials', 'offerings']) {
        server[field] = server[field].filter(row => !accesses.has(row.provider_access_id));
      }
      return {deleted: id, revision: server.revision};
    },
  },
};
const planPorts = {stage: async (bundle, apiKey) => {
  staged.push([bundle.provider.provider_id, apiKey]);
  if (!stageResult) return false;
  connectPlan(bundle.provider.provider_id); server.revision++; adopt();
  return true;
}};
const card = key => document.querySelector(`#oauthPlanKeyCards [data-plan-key="${key}"]`);
async function main() {
SCENARIO
}
main().then(() => console.log(JSON.stringify(out))).catch(error => {
  console.error(error); process.exitCode = 1;
}).finally(() => { subscriptionPlans.close(); dom.window.close(); });
'''


def run_cards(bundle, scenario):
    script = HARNESS.replace('ROOT', json.dumps(str(ROOT))).replace('BUILT', json.dumps(str(bundle)))
    result = subprocess.run(['node', '-e', script.replace('SCENARIO', scenario)],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize('scenario', sorted([
    'kimi_signed_out', 'kimi_device_login_connects', 'kimi_unsafe_verification_link',
    'kimi_login_refused', 'kimi_close_during_login', 'kimi_expired_login', 'kimi_logout',
    'kimi_copy_code', 'plans_render_subscription_templates', 'plans_key_required',
    'plans_connect_stages_key', 'plans_connect_failure_keeps_key', 'plans_disconnect',
    'plans_close_retires_late_catalogue',
]))
def test_subscription_settings_cards(cards_bundle, scenario):
    assert run_cards(cards_bundle, CASES[scenario]) == {'complete': True}


CASES = {
'kimi_signed_out': r'''
  beginSettingsReadSession(); await ports.loadStatus(false);
  assert.equal(byId('oauthKimiStatus').textContent, 'settings.oauthNotLoggedIn');
  assert.equal(byId('oauthKimiLoginBtn').style.display, '');
  assert.equal(byId('oauthKimiLogoutBtn').style.display, 'none');
  assert.equal(byId('oauthKimiDevice').style.display, 'none');
  assert.ok(byId('oauthKimiIcon').childElementCount > 0);
  out.complete = true;
''',
'kimi_device_login_connects': r'''
  beginSettingsReadSession(); await ports.loadStatus(false);
  await kimiCodeLogin.start(ports);
  assert.deepEqual(deviceLogins, ['kimi']);
  assert.equal(byId('oauthKimiDevice').style.display, '');
  assert.equal(byId('oauthKimiDeviceCode').textContent, 'WXYZ-1234');
  assert.equal(byId('oauthKimiDeviceLink').href, VERIFY);
  assert.equal(byId('oauthKimiStatus').textContent, 'settings.oauthWaitingAuth');
  assert.equal(intervals(), 1);
  kimi = {status: 'waiting_callback', authenticated: false,
          device: {user_code: 'WXYZ-1234', verification_url: VERIFY}};
  await advance();
  assert.equal(intervals(), 1); assert.equal(catalogRefreshes, 0);
  assert.equal(byId('oauthKimiDevice').style.display, '');
  kimi = {status: 'completed', authenticated: true, provider_ready: true, model_count: 5, expire: 1893456000};
  await advance();
  assert.equal(intervals(), 0); assert.equal(catalogRefreshes, 1);
  assert.equal(loads[loads.length - 1], false);
  assert.equal(byId('oauthKimiDevice').style.display, 'none');
  assert.equal(byId('oauthKimiLoginBtn').style.display, 'none');
  assert.equal(byId('oauthKimiLogoutBtn').style.display, '');
  assert.match(byId('oauthKimiStatus').className, /authenticated/);
  assert.equal(byId('oauthKimiExpiry').textContent, 'settings.oauthKimiExpires');
  out.complete = true;
''',
'kimi_unsafe_verification_link': r'''
  deviceResponse = () => ({ok: true, status: 200,
    json: async () => ({status: 'started', user_code: 'CODE-0001', verification_url: 'javascript:alert(1)'})});
  beginSettingsReadSession(); const before = byId('oauthKimiDeviceLink').href;
  await kimiCodeLogin.start(ports);
  assert.equal(byId('oauthKimiDeviceCode').textContent, 'CODE-0001');
  assert.equal(byId('oauthKimiDeviceLink').href, before);
  assert.ok(before.startsWith('https://'));
  out.complete = true;
''',
'kimi_login_refused': r'''
  deviceResponse = () => ({ok: false, status: 502,
    json: async () => ({error: {message: 'synthetic refusal'}})});
  beginSettingsReadSession(); await kimiCodeLogin.start(ports);
  assert.deepEqual(alerts, ['settings.oauthLoginFailed']);
  assert.equal(byId('oauthKimiLoginBtn').disabled, false);
  assert.equal(byId('oauthKimiLoginBtn').textContent, 'settings.oauthDeviceLogin');
  assert.equal(byId('oauthKimiDevice').style.display, 'none');
  assert.equal(intervals(), 0);
  out.complete = true;
''',
'kimi_close_during_login': r'''
  beginSettingsReadSession(); const gate = defer(), respond = deviceResponse;
  bridge.Api.oauth.deviceLoginPost = async provider => {
    deviceLogins.push(provider); await gate.promise; return respond();
  };
  const pending = kimiCodeLogin.start(ports); await tick();
  bridge.closeSettings(); gate.resolve(); await pending;
  assert.deepEqual(deviceLogins, ['kimi']);
  assert.equal(byId('oauthKimiDevice').style.display, 'none');
  assert.equal(intervals(), 0); assert.deepEqual(alerts, []);
  out.complete = true;
''',
'kimi_expired_login': r'''
  kimi = {status: 'not_started', authenticated: false, auth_issue: 'refresh_invalidated'};
  beginSettingsReadSession(); await ports.loadStatus(false);
  assert.equal(byId('oauthKimiStatus').textContent, 'settings.oauthKimiSignInAgain');
  assert.match(byId('oauthKimiStatus').className, /error/);
  assert.equal(byId('oauthKimiLoginBtn').style.display, '');
  assert.equal(byId('oauthKimiLogoutBtn').style.display, 'none');
  out.complete = true;
''',
'kimi_logout': r'''
  kimi = {status: 'completed', authenticated: true, provider_ready: true, model_count: 5};
  beginSettingsReadSession(); await ports.loadStatus(false);
  assert.equal(byId('oauthKimiExpiry').textContent, 'settings.oauthConnectedAccount');
  confirmAnswer = false; await kimiCodeLogin.logout(ports);
  assert.deepEqual(logouts, []); assert.equal(byId('oauthKimiLogoutBtn').style.display, '');
  confirmAnswer = true; await kimiCodeLogin.logout(ports);
  assert.deepEqual(logouts, ['kimi']); assert.equal(confirmations, 2); assert.equal(catalogRefreshes, 1);
  assert.equal(byId('oauthKimiStatus').textContent, 'settings.oauthNotLoggedIn');
  assert.equal(byId('oauthKimiLoginBtn').style.display, '');
  assert.equal(byId('oauthKimiLogoutBtn').style.display, 'none');
  out.complete = true;
''',
'kimi_copy_code': r'''
  beginSettingsReadSession(); await kimiCodeLogin.start(ports);
  const button = byId('oauthKimiDevice').querySelector('button');
  await kimiCodeLogin.copyCode(button);
  assert.deepEqual(copied, ['WXYZ-1234']);
  assert.equal(button.textContent, 'settings.oauthCopied');
  await advance();
  assert.equal(button.textContent, 'settings.oauthCopyCode');
  out.complete = true;
''',
'plans_render_subscription_templates': r'''
  beginSettingsReadSession(); await subscriptionPlans.refresh(planPorts);
  const keys = [...document.querySelectorAll('#oauthPlanKeyCards [data-plan-key]')].map(row => row.dataset.planKey);
  assert.deepEqual(keys, ['bailian_coding_plan', 'minimax_mplan']);
  const links = [...card('bailian_coding_plan').querySelectorAll('a')];
  assert.deepEqual(links.map(anchor => anchor.href),
    ['https://console.fixture.invalid/bailian', 'https://docs.fixture.invalid/bailian']);
  assert.ok(links.every(anchor => anchor.target === '_blank' && anchor.rel === 'noopener noreferrer'));
  assert.equal(card('bailian_coding_plan').querySelector('input').type, 'password');
  assert.equal(card('bailian_coding_plan').querySelector('input').autocomplete, 'off');
  assert.equal(card('minimax_mplan').querySelectorAll('a').length, 0);
  assert.equal(card('bailian_coding_plan').querySelector('.oauth-status-badge').textContent,
    'settings.oauthNotLoggedIn');
  out.complete = true;
''',
'plans_key_required': r'''
  beginSettingsReadSession(); await subscriptionPlans.refresh(planPorts);
  card('bailian_coding_plan').querySelector('button').click(); await settle();
  assert.deepEqual(alerts, ['settings.subscriptionPlanKeyMissing']);
  assert.deepEqual(compiled, []); assert.deepEqual(staged, []);
  out.complete = true;
''',
'plans_connect_stages_key': r'''
  beginSettingsReadSession(); await subscriptionPlans.refresh(planPorts);
  const input = card('bailian_coding_plan').querySelector('input');
  input.value = '  sk-sp-synthetic-plan-key  ';
  card('bailian_coding_plan').querySelector('button').click(); await settle();
  assert.deepEqual(compiled, ['bailian_coding_plan']);
  assert.deepEqual(staged, [['bailian_coding_plan', 'sk-sp-synthetic-plan-key']]);
  assert.equal(input.value, '');
  const connected = card('bailian_coding_plan');
  assert.match(connected.querySelector('.oauth-status-badge').className, /authenticated/);
  assert.equal(connected.querySelector('input'), null);
  assert.ok(connected.querySelector('.oauth-provider-actions button'));
  assert.ok(card('minimax_mplan').querySelector('input'));
  assert.deepEqual(alerts, []);
  out.complete = true;
''',
'plans_connect_failure_keeps_key': r'''
  beginSettingsReadSession(); await subscriptionPlans.refresh(planPorts);
  bridge.Api.providers.compileTemplate = async () => { throw new Error('synthetic compile refusal'); };
  const input = card('bailian_coding_plan').querySelector('input'), button = card('bailian_coding_plan').querySelector('button');
  input.value = 'sk-sp-synthetic-plan-key';
  button.click(); await settle();
  assert.deepEqual(alerts, ['settings.subscriptionPlanConnectFailed']);
  assert.deepEqual(staged, []);
  assert.equal(input.value, 'sk-sp-synthetic-plan-key'); assert.equal(button.disabled, false);
  out.complete = true;
''',
'plans_disconnect': r'''
  connectPlan('bailian_coding_plan'); adopt();
  beginSettingsReadSession(); await subscriptionPlans.refresh(planPorts);
  assert.match(card('bailian_coding_plan').querySelector('.oauth-status-badge').className, /authenticated/);
  confirmAnswer = false;
  card('bailian_coding_plan').querySelector('.oauth-provider-actions button').click(); await settle();
  assert.deepEqual(deletes, []); assert.equal(confirmations, 1);
  confirmAnswer = true;
  card('bailian_coding_plan').querySelector('.oauth-provider-actions button').click(); await settle();
  assert.deepEqual(deletes, ['bailian_coding_plan']); assert.equal(renders, 1);
  assert.equal(bridge._stgModelRouting.providers.length, 0);
  assert.ok(card('bailian_coding_plan').querySelector('input'));
  assert.deepEqual(alerts, []);
  out.complete = true;
''',
'plans_close_retires_late_catalogue': r'''
  beginSettingsReadSession(); const gate = defer();
  bridge.Api.providers.templates = () => gate.promise;
  const pending = subscriptionPlans.refresh(planPorts); await tick();
  bridge.closeSettings(); gate.resolve(clone(PLAN_TEMPLATES)); await pending;
  assert.equal(byId('oauthPlanKeyCards').childElementCount, 0);
  out.complete = true;
''',
}
