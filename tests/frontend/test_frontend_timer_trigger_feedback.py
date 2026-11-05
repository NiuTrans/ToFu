"""The timer panel's check and force-deliver buttons must always answer the click.

Owner report: clicking 触发 produced no visible reaction — the row flashed
触发中… and silently rolled back, because a busy conversation lane made the
delivery fail and the handler only wrote to the debug panel. The delivery lane
(started / injected into the running turn / queued behind it) and an
unactionable failure are all things the operator must be told about.

Owner report (2026-11-02): 触发 was described as running one check but
delivered without checking. "Check now" is the default action and names an
unmet condition with its reason; forced delivery stays a separate button.

Drives the REAL timer.js under node with a controllable transport.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
JS_DIR = runtime_sections_dir()

_HARNESS = r"""
const fs = require('fs');
global.window = global;
const scenario = process.argv[3];

const calls = { toasts: [] };
let _triggerImpl = async () => ({ ok: true, execution_task_id: 'task-1' });
let _checkImpl = async () => ({ ok: true, ready: false, decision: 'wait', reason: '' });

function _el() {
  return { _html: '', set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html; },
           textContent: '', style: {}, classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } } };
}
const panel = _el();
global.document = {
  getElementById(id) { return id === 'timerPanelContent' ? panel : _el(); },
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
global.t = (k, vars) => (vars && vars.reason ? `${k}|${vars.reason}` : k);
global.debugLog = () => {};
global.getConvById = () => null;
global.loadConversation = () => {};
global.showToast = (msg, type) => { calls.toasts.push({ msg, type }); };

const ROWS = [{ id: 't1', status: 'active', poll_count: 1, poll_interval: 60,
                check_instruction: 'is it done?', created_at: 1, conv_id: 'conv-1',
                last_poll_decision: '' }];
global.Api = {
  timer: {
    list: async () => ({ ok: true, timers: JSON.parse(JSON.stringify(ROWS)), active_count: 1 }),
    cancel: async () => ({ ok: true }),
    trigger: (id) => _triggerImpl(id),
    check: (id) => _checkImpl(id),
    status: async () => ({ ok: true, poll_log: [] }),
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // timer.js

class ApiError extends Error {
  constructor(message, options) {
    super(message);
    Object.assign(this, options || {});
  }
}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const lastToast = () => calls.toasts[calls.toasts.length - 1] || {};

(async () => {
  check('fn_exposed', typeof _triggerTimer === 'function' && typeof _checkTimer === 'function');
  await _refreshTimerPanel();

  if (scenario === 'buttons') {
    // Checking is the default action; forced delivery is separate and later.
    const html = panel.innerHTML;
    const checkAt = html.indexOf('tpi-btn-check');
    const forceAt = html.indexOf('tpi-btn-trigger');
    check('check_button_rendered', checkAt !== -1 && html.indexOf("_checkTimer('t1')") !== -1);
    check('force_button_rendered', forceAt !== -1 && html.indexOf("_triggerTimer('t1')") !== -1);
    check('check_precedes_force', checkAt !== -1 && checkAt < forceAt);
    check('check_hint', html.indexOf('timer.checkHint') !== -1);
    let release;
    _checkImpl = () => new Promise((resolve) => { release = resolve; });
    const pending = _checkTimer('t1');
    check('checking_label_painted', panel.innerHTML.indexOf('timer.checking') !== -1
          && panel.innerHTML.indexOf('tpi-btn-check') === -1);
    release({ ok: true, ready: false, decision: 'wait', reason: 'later' });
    await pending;
    check('buttons_restored', panel.innerHTML.indexOf('tpi-btn-check') !== -1);
  } else if (scenario === 'check_not_ready') {
    _checkImpl = async () => ({ ok: true, ready: false, decision: 'wait',
                                reason: 'predicate no match (exit=1)' });
    await _checkTimer('t1');
    check('names_reason', lastToast().msg === 'timer.checkNotReadyToast|predicate no match (exit=1)');
    check('not_an_error', lastToast().type === 'info');
    check('row_still_actionable', panel.innerHTML.indexOf('tpi-btn-check') !== -1
          && panel.innerHTML.indexOf('tpi-pending') === -1);
    check('only_one_toast', calls.toasts.length === 1);
  } else if (scenario === 'check_ready') {
    _checkImpl = async () => {
      ROWS[0].status = 'triggered';
      ROWS[0].last_poll_decision = 'steered';
      return { ok: true, ready: true, decision: 'ready', delivery: 'steered' };
    };
    await _checkTimer('t1');
    check('announces_delivery_lane', lastToast().msg === 'timer.deliverySteeredToast');
    check('success_type', lastToast().type === 'success');
    check('panel_refreshed_state', panel.innerHTML.indexOf('timer-status-triggered') !== -1);
  } else if (scenario === 'check_error') {
    _checkImpl = async () => ({ ok: true, ready: false, decision: 'error',
                                reason: 'predicate errored (exit=127)' });
    await _checkTimer('t1');
    check('announces_check_error', lastToast().msg === 'timer.checkErrorToast|predicate errored (exit=127)');
    check('error_type', lastToast().type === 'error');
  } else if (scenario === 'check_retry') {
    // Ready, but the delivery lane was unavailable: the watcher delivers later.
    _checkImpl = async () => {
      throw new ApiError('Target conversation is busy', {
        status: 409, problem: { code: 'timer_delivery_retry' },
      });
    };
    await _checkTimer('t1');
    check('announces_retry', lastToast().msg === 'timer.triggerRetryToast');
    check('retry_is_not_an_error', lastToast().type !== 'error');
    check('row_restored', panel.innerHTML.indexOf('tpi-btn-check') !== -1
          && panel.innerHTML.indexOf('tpi-pending') === -1);
  } else if (scenario === 'check_failed') {
    _checkImpl = async () => { throw new ApiError('boom', { status: 500 }); };
    await _checkTimer('t1');
    check('announces_failure', lastToast().msg === 'timer.checkFailedToast');
    check('failure_type', lastToast().type === 'error');
    check('busy_label_cleared', panel.innerHTML.indexOf('tpi-pending') === -1);
  } else if (scenario === 'steered') {
    // The row settles as 已触发 with an injection receipt; the click announces it.
    _triggerImpl = async () => {
      ROWS[0].status = 'triggered';
      ROWS[0].last_poll_decision = 'steered';
      return { ok: true };
    };
    await _triggerTimer('t1');
    check('announces_injection', lastToast().msg === 'timer.deliverySteeredToast');
    check('success_type', lastToast().type === 'success');
    check('panel_refreshed_state', panel.innerHTML.indexOf('timer-status-triggered') !== -1);
    check('busy_label_cleared', panel.innerHTML.indexOf('tpi-pending') === -1);
    check('only_one_toast', calls.toasts.length === 1);
  } else if (scenario === 'queued') {
    _triggerImpl = async () => {
      ROWS[0].status = 'triggered';
      ROWS[0].last_poll_decision = 'queued';
      return { ok: true };
    };
    await _triggerTimer('t1');
    check('announces_queue', lastToast().msg === 'timer.deliveryQueuedToast');
    check('success_type', lastToast().type === 'success');
  } else if (scenario === 'retry') {
    // 409: nothing was lost, the watcher still owns the obligation.
    _triggerImpl = async () => {
      throw new ApiError('Target conversation is busy', {
        status: 409, problem: { code: 'timer_delivery_retry' },
      });
    };
    await _triggerTimer('t1');
    check('announces_retry', lastToast().msg === 'timer.triggerRetryToast');
    check('retry_is_not_an_error', lastToast().type !== 'error');
    check('row_restored', ROWS[0].status === 'active'
          && panel.innerHTML.indexOf('tpi-btn-trigger') !== -1
          && panel.innerHTML.indexOf('tpi-pending') === -1);
  } else if (scenario === 'inactive') {
    _triggerImpl = async () => {
      throw new ApiError('Timer is no longer active', {
        status: 409, envelope: { kind: 'timer_inactive' },
      });
    };
    await _triggerTimer('t1');
    check('announces_inactive', lastToast().msg === 'timer.triggerClosedToast');
  } else {
    // An unrecognized failure must still be announced, not swallowed.
    _triggerImpl = async () => { throw new ApiError('boom', { status: 500 }); };
    await _triggerTimer('t1');
    check('announces_failure', lastToast().msg === 'timer.triggerFailedToast');
    check('failure_type', lastToast().type === 'error');
    check('busy_label_cleared', panel.innerHTML.indexOf('tpi-pending') === -1);
  }

  console.log(out.join('\n'));
})();
"""


def _run(scenario: str, min_pass: int) -> None:
    harness = os.path.join(HERE, f'_timer_trigger_feedback_{scenario}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'timer.js'), scenario],
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
    assert not fails, f'{scenario} failures:\n{output}'
    assert output.count('PASS') >= min_pass, \
        f'expected >={min_pass} PASS lines, got:\n{output}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_check_is_the_default_action_beside_forced_delivery():
    _run('buttons', 7)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_check_names_an_unmet_condition_and_its_reason():
    _run('check_not_ready', 5)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_check_that_passes_announces_the_delivery_lane():
    _run('check_ready', 4)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_check_error_is_announced_as_an_error():
    _run('check_error', 3)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_check_with_busy_lane_reports_retry():
    _run('check_retry', 4)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_failed_check_request_is_announced():
    _run('check_failed', 4)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_trigger_announces_injection_into_running_turn():
    _run('steered', 5)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_trigger_announces_queued_delivery():
    _run('queued', 2)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_busy_lane_reports_retry_not_failure():
    _run('retry', 4)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_inactive_timer_reports_closure():
    _run('inactive', 1)


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_unknown_failure_is_announced():
    _run('failed', 3)
