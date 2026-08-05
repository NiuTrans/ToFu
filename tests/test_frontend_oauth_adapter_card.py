"""tests/test_frontend_oauth_adapter_card.py — 订阅适配器卡片守卫（E4）。

WHY
---
The subscription-adapter card is the ONLY settings surface for the
CLIProxyAPI sidecar lifecycle on the desktop agent. Wrong badge / missing
retry / a poll chain that never tightens during bring-up (first run
downloads ~20MB, minutes) all read as "stuck" to the user. This drives the
REAL static/js/settings/oauth.js adapter block in node with a fake DOM,
asserting: empty-state guidance, per-agent badges, account counts, action
buttons, ensuring→busy-cadence, ready success line, error detail + retry,
and that the poll chain dies when the settings modal closes.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
OAUTH_JS = os.path.join(ROOT, 'static', 'js', 'settings', 'oauth.js')
NODE = shutil.which('node')

_HARNESS = r"""
const fs = require('fs');
globalThis.window = globalThis;
globalThis.addEventListener = function(){};
delete globalThis.BroadcastChannel;
globalThis.t = (k, vars) => {
  if (!vars) return k;
  let s = k;
  for (const key of Object.keys(vars)) s += '|' + key + '=' + vars[key];
  return s;
};
globalThis.escapeHtml = (s) => String(s);
globalThis.showAlert = () => {};
globalThis.debugLog = () => {};

function mkEl() {
  return {
    innerHTML: '', textContent: '', value: '',
    style: { display: 'none' },
    className: '',
    classList: { _s: new Set(), contains(c) { return this._s.has(c); },
                 add(c) { this._s.add(c); }, toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); } },
    children: [],
    appendChild(c) { this.children.push(c); },
    querySelectorAll() { return []; },
    setAttribute() {}, getAttribute() { return null; },
  };
}
const els = {};
function getEl(id) {
  if (!els[id]) els[id] = mkEl();
  return els[id];
}
const modal = getEl('settingsModal'); modal.classList.add('open');
const panel = getEl('settingsTab_oauth'); panel.classList.add('active');
globalThis._modalOpen = true;
globalThis.document = {
  getElementById: (id) => {
    if (id === 'settingsModal' && !globalThis._modalOpen) return mkEl();
    // The card and its inner containers exist only once appended to the panel.
    if ((id === 'adapterCard' || id === 'adapterRows' || id === 'adapterEmpty') &&
        panel.children.length === 0) return null;
    return getEl(id);
  },
  createElement: () => mkEl(),
};

let statusCalls = 0;
globalThis._payload = { agents: [], ensure_tasks: {} };
globalThis.Api = {
  get: (path) => { statusCalls++; return Promise.resolve(globalThis._payload); },
  post: (path, body) => { globalThis._posted = { path, body }; return Promise.resolve({ ok: true }); },
};

// Spy on the poll chain's chosen delay — deterministic cadence assertion.
globalThis._lastDelay = null;
const _origSetTimeout = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms) => { globalThis._lastDelay = ms; return _origSetTimeout(fn, ms); };

eval(fs.readFileSync(process.argv[1], 'utf8'));
_ADAPTER_POLL_IDLE_MS = 8;
_ADAPTER_POLL_BUSY_MS = 1;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function render(data) {
  getEl('adapterRows').innerHTML = '';
  const e = getEl('adapterEmpty'); e.style.display = 'none';
  _renderAdapterRows(data);
  return { rows: getEl('adapterRows'), empty: e };
}

(async () => {
  // card builds once, appended to the oauth panel
  render({ agents: [], ensure_tasks: {} });
  check('card_built', panel.children.length === 1);
  check('card_title_key', panel.children[0].innerHTML.indexOf('settings.adapterTitle') !== -1);
  render({ agents: [], ensure_tasks: {} });
  check('card_built_once', panel.children.length === 1);

  // empty state: no online agent → guidance, no rows
  let r = render({ agents: [{ agent_id: 'a1', name: 'off', online: false }], ensure_tasks: {} });
  check('empty_visible', r.empty.style.display !== 'none');
  check('empty_guidance_key', panel.children[0].innerHTML.indexOf('settings.adapterEmpty') !== -1);
  check('empty_no_rows', r.rows.innerHTML === '');

  // running agent: badge carries version+port, account count, stop button
  r = render({ agents: [{ agent_id: 'agentabcdef', name: 'mac', online: true,
      adapter: { ok: true, running: true, installed: true, version: '1.2.3', port: 8317,
                 accounts: ['x', 'y'] } }], ensure_tasks: {} });
  check('running_badge', r.rows.innerHTML.indexOf('settings.adapterBadgeRunning|version=1.2.3|port=8317') !== -1);
  check('running_accounts', r.rows.innerHTML.indexOf('settings.adapterAccounts|n=2') !== -1);
  check('running_stop_btn', r.rows.innerHTML.indexOf('adapter-stop-btn') !== -1);
  check('running_no_start', r.rows.innerHTML.indexOf('adapter-start-btn') === -1);
  check('running_not_busy', _adapterLastBusy === false);

  // not installed: start button
  r = render({ agents: [{ agent_id: 'a2', name: 'win', online: true, adapter: { installed: false, running: false } }], ensure_tasks: {} });
  check('not_installed_badge', r.rows.innerHTML.indexOf('settings.adapterBadgeNotInstalled') !== -1);
  check('not_installed_start_btn', r.rows.innerHTML.indexOf('adapter-start-btn') !== -1);

  // ensuring: progress hint + busy cadence + disabled start
  r = render({ agents: [{ agent_id: 'a3', name: 'linux', online: true, adapter: {} }],
               ensure_tasks: { a3: { state: 'ensuring', detail: 'downloading' } } });
  check('ensuring_badge', r.rows.innerHTML.indexOf('settings.adapterBadgeInstalling') !== -1);
  check('ensuring_hint', r.rows.innerHTML.indexOf('settings.adapterEnsuring') !== -1);
  check('ensuring_disabled_btn', r.rows.innerHTML.indexOf('disabled') !== -1);
  check('ensuring_busy', _adapterLastBusy === true);

  // ready: success line mentions the provider name
  r = render({ agents: [{ agent_id: 'a3', name: 'linux', online: true,
      adapter: { running: true, installed: true, version: '1.0.0', port: 8317 } }],
               ensure_tasks: { a3: { state: 'ready', models: ['m1'] } } });
  check('ready_line', r.rows.innerHTML.indexOf('settings.adapterReady|name=linux') !== -1);

  // error: detail + retry button
  r = render({ agents: [{ agent_id: 'a4', name: 'pc', online: true, adapter: {} }],
               ensure_tasks: { a4: { state: 'error', detail: 'download failed' } } });
  check('error_badge', r.rows.innerHTML.indexOf('settings.adapterBadgeError') !== -1);
  check('error_detail', r.rows.innerHTML.indexOf('download failed') !== -1);
  check('error_retry_btn', r.rows.innerHTML.indexOf('settings.adapterRetry') !== -1);

  // ensure/stop post to the right endpoints
  _adapterEnsure('agentabcdef');
  check('ensure_post', globalThis._posted.path === '/api/v1/adapter/ensure' && globalThis._posted.body.agent_id === 'agentabcdef');
  _adapterStop('agentabcdef');
  check('stop_post', globalThis._posted.path === '/api/v1/adapter/stop' && globalThis._posted.body.agent_id === 'agentabcdef');
  await sleep(20);

  // polling: busy payload → tighter cadence; modal closed → chain dies
  statusCalls = 0;
  globalThis._payload = { agents: [{ agent_id: 'a3', name: 'linux', online: true, adapter: {} }],
                          ensure_tasks: { a3: { state: 'ensuring' } } };
  _adapterTick(false);
  await sleep(15);
  const busyCalls = statusCalls;
  check('busy_cadence_fast', busyCalls >= 3);
  check('busy_delay_used', globalThis._lastDelay === 1);
  globalThis._payload = { agents: [], ensure_tasks: {} };
  statusCalls = 0;
  globalThis._lastDelay = null;
  await sleep(30);
  check('idle_cadence_slow', statusCalls >= 1);
  check('idle_delay_used', globalThis._lastDelay === 8);
  globalThis._modalOpen = false;
  panel.classList._s.delete('active');
  statusCalls = 0;
  await sleep(30);
  check('hidden_chain_dies', statusCalls <= 1);

  process.stdout.write(out.join('\n'));
})();
"""


def _run(src: str | None = None) -> str:
    if NODE is None:
        pytest.skip('node is required to execute oauth.js')
    path = OAUTH_JS
    tmp = None
    if src is not None:
        import tempfile
        tmp = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                          encoding='utf-8')
        tmp.write(src)
        tmp.close()
        path = tmp.name
    try:
        proc = subprocess.run([NODE, '-e', _HARNESS, path],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'harness failed: {proc.stderr[:800]}'
        return proc.stdout
    finally:
        if tmp is not None:
            os.unlink(tmp.name)


def test_adapter_card_states_and_polling():
    out = _run()
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'adapter-card failures:\n' + out
    assert out.count('PASS') >= 20, out


def test_NEUTER_empty_state_removed():
    """Drop the offline-agents filter → no online agent renders NO guidance
    (the user sees a blank card instead of the install prompt)."""
    src = open(OAUTH_JS, encoding='utf-8').read()
    needle = "    if (agents[i] && agents[i].online) online.push(agents[i]);"
    assert needle in src, 'NEUTER anchor missing — test stale'
    neutered = src.replace(needle, '    online.push(agents[i]);')
    out = _run(src=neutered)
    assert 'FAIL empty_visible' in out, out
    assert 'PASS card_built' in out, 'unrelated pins must stay green'


def test_NEUTER_busy_cadence_removed():
    """Break the busy flag → the ensuring poll never tightens; the user
    watches a minutes-long download through a 5s-refresh keyhole."""
    src = open(OAUTH_JS, encoding='utf-8').read()
    needle = "    if (state === 'ensuring') busy = true;"
    assert needle in src, 'NEUTER anchor missing — test stale'
    neutered = src.replace(needle, '')
    out = _run(src=neutered)
    assert 'FAIL ensuring_busy' in out, out
    assert 'FAIL busy_delay_used' in out, out
    assert 'PASS running_badge' in out, 'unrelated pins must stay green'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
