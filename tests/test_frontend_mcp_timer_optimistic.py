"""Regression suite: settings-MCP panel + timer panel actions take visible
effect in the same task as the click (owner directive 2026-07-31, epic
pt_2bf8e5c85d8f4b2e).

WHY
---
The MCP panel held the app's LONGEST dead window: ``_mcpReconnect`` awaited
``connectOne`` (MCP cold start measured at 27-55s in JOURNAL) and then a full
repopulate before ANY visual change. ``_mcpUninstall`` / ``_mcpPurge`` sat
static after the confirm; ``_mcpQuickInstall`` logged to the debug panel and
showed nothing on the card until the poll finished. Timer panel
``_cancelTimer`` / ``_triggerTimer`` awaited the POST + a full refresh before
the row changed.

The fix rides a PENDING-MAP at the RENDER seam (not a one-off DOM patch):
``_mcpPending[serverId]`` / ``_timerPending[timerId]`` are consulted by the
catalog/row renderers, so the busy state survives any CONCURRENT repopulate
(the breaker-refresh timer fires `_populateMcpTab` mid-operation; the timer
panel has a 30s auto-refresh). Handlers set the pending state + re-render
synchronously, run the network in the background, and on failure clear the
pending state (restoring the card/row) + surface the error.

Drives the REAL settings/mcp.js and timer.js under node with controllable
server promises. Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _run_node(name: str, harness_src: str, js_rel: str, scenario: str,
              min_pass: int) -> str:
    harness = os.path.join(HERE, f'_mcp_timer_opt_{name}_{scenario}.js')
    with open(harness, 'w') as f:
        f.write(harness_src)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, js_rel), scenario],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, f'{name} ({scenario}) failures:\n' + output
    assert output.count('PASS') >= min_pass, \
        f'expected >={min_pass} PASS lines, got:\n{output}'
    return output


# ═══════════════════════════════════════════════════════════════════
# Harness M — REAL settings/mcp.js: pending state painted on the click frame,
# background completion, restore-on-failure.
# ═══════════════════════════════════════════════════════════════════
_HARNESS_MCP = r"""
const fs = require('fs');
global.window = global;
const scenario = process.argv[3];

const calls = { renders: 0, alerts: [], repopulates: 0, server: [] };
let _connRes, _connRej, _unRes, _unRej, _qiRes, _qiRej;

const _grid = { _html: '', isConnected: true, offsetParent: {},
  set innerHTML(v) { calls.renders++; this._html = v; },
  get innerHTML() { return this._html; },
  querySelectorAll() { return []; },
};
function _el() {
  return { _html: '', set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html; },
           textContent: '', style: {}, classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
           querySelectorAll() { return []; }, isConnected: true, offsetParent: {} };
}
global.document = {
  getElementById(id) { return id === 'mcpCatalogGrid' ? _grid : _el(); },
  querySelectorAll() { return []; },
  createElement() { return _el(); },
  addEventListener() {},
};
global.Icon = () => '<svg/>';
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;
global.debugLog = () => {};
global.showAlert = (m) => { calls.alerts.push(m); return Promise.resolve(); };
global.showConfirm = async () => true;
global.refreshMcpRailState = () => {};
global.Api = {
  mcp: {
    catalogList: async () => { calls.repopulates++;
      return { ok: true, json: async () => ({ catalog: _mcpCatalog }) }; },
    connectOne: (id) => { calls.server.push('connect:' + id);
      return new Promise((res, rej) => { _connRes = res; _connRej = rej; }); },
    catalogUninstall: (id, purge) => { calls.server.push('uninstall:' + id + ':' + purge);
      return new Promise((res, rej) => { _unRes = res; _unRej = rej; }); },
    catalogInstall: (id, env) => { calls.server.push('install:' + id);
      return new Promise((res, rej) => { _qiRes = res; _qiRej = rej; }); },
    catalogInstallStatus: async () => null,
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // settings/mcp.js

// Seed AFTER eval (var declarations land on global).
_mcpScope = 'all';
_mcpCatalog = [
  { id: 'srv1', name: 'Srv One', description: 'd', installed: true, connected: false,
    custom: true, category: 'Other', env_specs: [] },
  { id: 'srv2', name: 'Srv Two', description: 'd', installed: false, connected: false,
    category: 'Other', env_specs: [] },
];
_renderMcpCatalog();
const _baselineRenders = calls.renders;
const _baselineHtml = _grid.innerHTML;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  check('fns_exposed', typeof _mcpReconnect === 'function'
        && typeof _mcpUninstall === 'function' && typeof _mcpQuickInstall === 'function');
  check('baseline_idle_rendered', _baselineHtml.indexOf('mcp.statusIdle') !== -1);

  if (scenario === 'reconnect-ok' || scenario === 'reconnect-fail') {
    const p = _mcpReconnect('srv1');
    // ★ INSTANT: pending state + busy render on the CLICK frame (the 27-55s
    //   cold-start window used to show NOTHING).
    check('pending_set_instantly', (typeof _mcpPending !== 'undefined' ? _mcpPending : {})['srv1'] === 'connecting');
    check('busy_rendered_instantly', calls.renders > _baselineRenders
          && _grid.innerHTML.indexOf('mcp.connecting') !== -1);
    check('server_called', calls.server.join(',') === 'connect:srv1');
    for (let i = 0; i < 5 && typeof _connRes !== 'function'; i++) await Promise.resolve();
    if (scenario === 'reconnect-ok') {
      _connRes({ ok: true, tools_count: 3 });
      await p;
      check('pending_cleared', !(typeof _mcpPending !== 'undefined' ? _mcpPending : {})['srv1']);
      check('repopulated', calls.repopulates >= 1);
      check('no_alert', calls.alerts.length === 0);
    } else {
      _connRej(new Error('spawn npx failed'));
      await p.catch(() => {});
      check('pending_cleared_on_fail', !(typeof _mcpPending !== 'undefined' ? _mcpPending : {})['srv1']);
      check('restored_after_fail', _grid.innerHTML.indexOf('mcp.connecting') === -1
            && _grid.innerHTML.indexOf('mcp.statusIdle') !== -1);
      check('alert_shown', calls.alerts.length === 1);
    }
  } else if (scenario === 'uninstall-ok' || scenario === 'purge-ok') {
    const p = (scenario === 'uninstall-ok') ? _mcpUninstall('srv1') : _mcpPurge('srv1');
    // Confirm resolves on a microtask — flush, then pending must be set BEFORE
    // the DELETE responds.
    for (let i = 0; i < 5 && !(typeof _mcpPending !== 'undefined' ? _mcpPending : {})['srv1']; i++) await Promise.resolve();
    check('pending_set_after_confirm', (typeof _mcpPending !== 'undefined' ? _mcpPending : {})['srv1'] === 'uninstalling');
    check('busy_rendered', _grid.innerHTML.indexOf('mcp.uninstalling') !== -1);
    for (let i = 0; i < 5 && typeof _unRes !== 'function'; i++) await Promise.resolve();
    check('server_called', calls.server.length === 1
          && calls.server[0].startsWith('uninstall:srv1:'));
    _unRes({ ok: true });
    await p;
    check('pending_cleared', !(typeof _mcpPending !== 'undefined' ? _mcpPending : {})['srv1']);
    check('repopulated', calls.repopulates >= 1);
    check('no_alert', calls.alerts.length === 0);
  } else {
    // quickinstall-ok (srv2: not installed, zero required env)
    const p = _mcpQuickInstall('srv2');
    check('pending_set_instantly', (typeof _mcpPending !== 'undefined' ? _mcpPending : {})['srv2'] === 'installing');
    check('busy_rendered_instantly', _grid.innerHTML.indexOf('mcp.installing') !== -1);
    for (let i = 0; i < 5 && typeof _qiRes !== 'function'; i++) await Promise.resolve();
    check('server_called', calls.server.join(',') === 'install:srv2');
    _qiRes({ ok: true, status: 'ready', tools_count: 2 });
    await p;
    check('pending_cleared', !(typeof _mcpPending !== 'undefined' ? _mcpPending : {})['srv2']);
    check('repopulated', calls.repopulates >= 1);
    check('no_alert', calls.alerts.length === 0);
  }

  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_mcp_reconnect_paints_busy_instantly():
    _run_node('mcp', _HARNESS_MCP, os.path.join('settings', 'mcp.js'),
              'reconnect-ok', 8)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_mcp_reconnect_failure_restores_card():
    _run_node('mcp', _HARNESS_MCP, os.path.join('settings', 'mcp.js'),
              'reconnect-fail', 8)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_mcp_uninstall_paints_busy_after_confirm():
    _run_node('mcp', _HARNESS_MCP, os.path.join('settings', 'mcp.js'),
              'uninstall-ok', 7)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_mcp_purge_paints_busy_after_confirm():
    _run_node('mcp', _HARNESS_MCP, os.path.join('settings', 'mcp.js'),
              'purge-ok', 7)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_mcp_quick_install_paints_busy_instantly():
    _run_node('mcp', _HARNESS_MCP, os.path.join('settings', 'mcp.js'),
              'quickinstall-ok', 7)


# ═══════════════════════════════════════════════════════════════════
# Harness T — REAL timer.js: cancel/trigger paint the row busy on the click
# frame; restore + error on failure.
# ═══════════════════════════════════════════════════════════════════
_HARNESS_TIMER = r"""
const fs = require('fs');
global.window = global;
const scenario = process.argv[3];

const calls = { renders: 0, list: 0, server: [], errors: [] };
let _cancelRes, _cancelRej, _trigRes, _trigRej;

const _content = { _html: '',
  set innerHTML(v) { calls.renders++; this._html = v; },
  get innerHTML() { return this._html; } };
function _el() {
  return { _html: '', set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html; },
           textContent: '', style: {}, classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } } };
}
global.document = {
  getElementById(id) { return id === 'timerPanelContent' ? _content : _el(); },
  createElement() { return _el(); },
  addEventListener() {},
  body: { appendChild() {} },
};
global.setTimeout = () => 0;
global.clearTimeout = () => {};
global.setInterval = () => 0;
global.clearInterval = () => {};
global.Icon = () => '';
global.IconDot = () => '';
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;
global.debugLog = (msg, kind) => { if (kind === 'error') calls.errors.push(msg); };
global.getConvById = () => null;
global.loadConversation = () => {};
global.showToast = () => {};

const TIMERS = [
  { id: 't1', status: 'active', poll_count: 1, poll_interval: 60,
    check_instruction: 'is it done?', created_at: 1, conv_id: 'conv-1' },
];
global.Api = {
  timer: {
    list: async () => { calls.list++;
      return { ok: true, timers: JSON.parse(JSON.stringify(TIMERS)), active_count: 1 }; },
    cancel: (id) => { calls.server.push('cancel:' + id);
      return new Promise((res, rej) => { _cancelRes = res; _cancelRej = rej; }); },
    trigger: (id) => { calls.server.push('trigger:' + id);
      return new Promise((res, rej) => { _trigRes = res; _trigRej = rej; }); },
    status: async () => ({ ok: true, poll_log: [] }),
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // timer.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  check('fns_exposed', typeof _cancelTimer === 'function' && typeof _triggerTimer === 'function');
  // Seed the panel (populates the cache + baseline row with action buttons).
  await _refreshTimerPanel();
  check('baseline_row_has_buttons', _content.innerHTML.indexOf('tpi-btn-cancel') !== -1);
  const _baselineRenders = calls.renders;

  if (scenario === 'cancel-ok' || scenario === 'cancel-fail') {
    const p = _cancelTimer('t1');
    // ★ INSTANT: pending state + busy row on the CLICK frame.
    check('busy_label_painted_instantly', calls.renders > _baselineRenders
          && _content.innerHTML.indexOf('timer.cancelling') !== -1);
    check('buttons_hidden_while_pending', _content.innerHTML.indexOf('tpi-btn-cancel') === -1);
    for (let i = 0; i < 5 && typeof _cancelRes !== 'function'; i++) await Promise.resolve();
    check('server_called', calls.server.join(',') === 'cancel:t1');
    if (scenario === 'cancel-ok') {
      _cancelRes({ ok: true });
      await p;
      // ★ Busy state cleared: the fresh row shows its action buttons again.
      check('busy_cleared_after_success', _content.innerHTML.indexOf('timer.cancelling') === -1
            && _content.innerHTML.indexOf('tpi-btn-cancel') !== -1);
      check('refreshed_after', calls.list >= 2);
      check('no_error', calls.errors.length === 0);
    } else {
      _cancelRej(new Error('network down'));
      await p.catch(() => {});
      check('restored_after_fail', _content.innerHTML.indexOf('timer.cancelling') === -1
            && _content.innerHTML.indexOf('tpi-btn-cancel') !== -1);
      check('error_logged', calls.errors.length === 1);
    }
  } else {
    const p = _triggerTimer('t1');
    check('busy_label_painted_instantly', calls.renders > _baselineRenders
          && _content.innerHTML.indexOf('timer.triggering') !== -1);
    for (let i = 0; i < 5 && typeof _trigRes !== 'function'; i++) await Promise.resolve();
    check('server_called', calls.server.join(',') === 'trigger:t1');
    _trigRes({ ok: true, execution_task_id: 'task-1' });
    await p;
    check('busy_cleared_after_success', _content.innerHTML.indexOf('timer.triggering') === -1
          && _content.innerHTML.indexOf('tpi-btn-cancel') !== -1);
    check('refreshed_after', calls.list >= 2);
    check('no_error', calls.errors.length === 0);
  }

  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_timer_cancel_paints_busy_instantly():
    _run_node('timer', _HARNESS_TIMER, 'timer.js', 'cancel-ok', 8)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_timer_cancel_failure_restores_row():
    _run_node('timer', _HARNESS_TIMER, 'timer.js', 'cancel-fail', 7)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_timer_trigger_paints_busy_instantly():
    _run_node('timer', _HARNESS_TIMER, 'timer.js', 'trigger-ok', 7)
