"""Timer actions reconcile a post-commit snapshot despite older reads in flight."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('action', ['cancel', 'trigger'])
@pytest.mark.parametrize('stale_failure', [False, True])
def test_timer_mutation_waits_for_fresh_snapshot(action, stale_failure):
    setup = r'''
const assert = require('node:assert/strict');
const content = {innerHTML: ''};
global.runtimeScope = {};
global.document = {
  visibilityState: 'visible', addEventListener() {},
  getElementById(id) { return id === 'timerPanelContent' ? content : null; },
};
global.setTimeout = global.setInterval = () => 1;
global.Icon = global.IconDot = () => '';
global.escapeHtml = value => String(value ?? '');
global.t = key => key;
global.debugLog = () => {};
const requests = [];
const row = {id: 'tmr_ui', status: 'active', poll_count: 1,
  poll_interval: 60, check_instruction: 'Check readiness'};
const snapshot = status => ({ok: true, active_count: status === 'active' ? 1 : 0,
  timers: [{...row, status}]});
global.Api = {timer: {
  list() { return new Promise((resolve, reject) => requests.push({resolve, reject})); },
  cancel: async () => ({ok: true}),
  trigger: async () => ({ok: true, execution_task_id: 'task_ui'}),
}};
async function drain() { for (let i = 0; i < 30; i++) await Promise.resolve(); }
'''
    driver = r'''
(async () => {
  const initial = _refreshTimerPanel();
  await drain();
  requests[0].resolve(snapshot('active'));
  await initial;
  const staleRead = _refreshTimerPanel();
  await drain();
  const mutation = ACTION === 'cancel' ? _cancelTimer(row.id) : _triggerTimer(row.id);
  await drain();
  if (STALE_FAILURE) requests[1].reject(new Error('old read failed'));
  else requests[1].resolve(snapshot('active'));
  await staleRead;
  await drain();
  assert.equal(requests.length, 3, 'mutation must obtain a post-commit snapshot');
  assert.ok(content.innerHTML.includes('tpi-pending'), 'stale read cannot restore actions');
  const status = ACTION === 'cancel' ? 'cancelled' : 'triggered';
  requests[2].resolve(snapshot(status));
  await mutation;
  assert.ok(content.innerHTML.includes('timer-status-' + status));
  assert.ok(!content.innerHTML.includes('tpi-pending'));
  assert.ok(!content.innerHTML.includes('tpi-btn-cancel'));
  process.stdout.write(JSON.stringify({status}));
})().catch(error => { console.error(error); process.exit(1); });
'''.replace('ACTION', json.dumps(action)).replace('STALE_FAILURE', json.dumps(stale_failure))
    source = (ROOT / 'frontend/src/runtime/sections/timer.js').read_text()
    result = subprocess.run(['node'], input=setup + source + driver,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] in {'cancelled', 'triggered'}


def test_pending_row_replacement_keeps_click_inside_timer_panel():
    setup = r'''
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<body><div id="timerBadge"><div id="timerPanel" class="visible"><div id="timerPanelContent"></div></div></div></body>');
global.document = dom.window.document;
global.runtimeScope = {};
global.setTimeout = global.setInterval = () => 1;
global.Icon = global.IconDot = () => '';
global.escapeHtml = value => String(value ?? '');
global.t = key => key;
global.Api = {timer: {cancel: () => new Promise(() => {})}};
'''
    driver = r'''
_timerPanelOpen = true;
_timerLastTimers = [{id: 'tmr_click', status: 'active', poll_count: 0,
  poll_interval: 60, check_instruction: 'Verify completion'}];
_renderTimerList(_timerLastTimers);
const badge = document.getElementById('timerBadge');
const panel = document.getElementById('timerPanel');
badge.addEventListener('click', toggleTimerPanel);
const button = panel.querySelector('.tpi-btn-cancel');
button.addEventListener('click', () => _cancelTimer('tmr_click'));
button.click();
assert.equal(button.isConnected, false, 'pending repaint removes the click target');
assert.ok(panel.classList.contains('visible'), 'internal click must keep panel open');
assert.ok(panel.querySelector('.tpi-pending'));
document.body.dispatchEvent(new dom.window.MouseEvent('click', {bubbles: true}));
assert.ok(!panel.classList.contains('visible'), 'outside click must still close panel');
dom.window.close();
'''
    source = (ROOT / 'frontend/src/runtime/sections/timer.js').read_text()
    result = subprocess.run(['node'], input=setup + source + driver,
                            text=True, capture_output=True, timeout=20, cwd=ROOT)
    assert result.returncode == 0, result.stderr
