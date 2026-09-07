"""tests/test_frontend_update_restart_force.py — regression for the topbar
"Restart server" button that could NEVER actually restart.

WHY
---
The self-update restart button was a permanent no-op on this deployment. Two
compounding bugs:

  1. api.js exposed ``restart: () => post('/api/v1/update/restart', {}, ...)``
     — a FIXED empty ``{}`` body with NO parameter. So update.js calling
     ``Api.update.restart({force:true})`` had its argument SILENTLY DROPPED;
     the wire body was always ``{}`` → backend saw ``force=false``.
  2. The backend refuses with 409 ``{needsForce, runningTasks}`` whenever any
     OTHER conversation has an in-flight task. This project runs 5+ sibling
     tasks essentially always, so every click 409'd and the button never
     restarted (33 ``self_update_restart_refused`` audit rows in one day).

THE FIX
-------
  • api.js: ``restart: (payload) => post(..., payload || {}, {onError:'throw'})``
    — pass the caller's body through AND surface the 409 (onError:'throw', not
    'null') so update.js can read the guard's ``needsForce``/``runningTasks``.
  • update.js ``restartServer()``: two-stage informed restart —
      1. try WITHOUT force (scoped with our own convId so the count reflects
         only OTHER conversations),
      2. on 409+needsForce, show a themed confirm naming the running-task count;
         only on explicit consent retry with ``force:true``.
    The old blind ``{confirm:true}`` pre-flight generic confirm was removed so
    there is no double-confirm.

This harness loads the REAL shipped update.js under bare node, stubs Api /
showConfirm / DOM / timers, and drives ``restartServer()`` directly:
  • sibling tasks running → 1st call force-less → 409 → confirm(true) → 2nd
    call carries force:true.
  • user DECLINES the confirm → NO second (force) call, no restart.
  • idle server (no 409) → single force-less call is accepted, no confirm.
  • an ordinary update-check 500 still renders its error card.
  • a pre-restart update check that returns 500 after progress takes ownership
    cannot replace the restart card, even in the post-health/pre-reload window.
  • slow restart preflight paints one continuous preparation card immediately;
    an accepted request advances that same card, while an explicit preflight
    refusal restores controls/availability without entering health polling.
  • only a lost response to an approved executable request is ambiguous enough
    for bootId polling; a pre-approval network failure cannot have restarted.

DOUBLE-NEUTER (on a MUTATED copy; shipped file never touched): strip the
``e.status === 409`` branch → the confirm-then-force retry no longer happens,
so the "declined" and "accepted" force assertions FAIL — proving the branch is
load-bearing.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── globals update.js touches at load + runtime ──
global.debugLog = () => {};
global.escapeHtml = (s) => String(s);
global.showToast = () => {};
// t(): identity for most keys, but the force-confirm key must carry the %s
// placeholder so the code's .replace('%s', count) is observable (real i18n
// returns a string WITH %s; an identity stub would have nothing to substitute).
global.t = (k) => (k === 'update.restartForceConfirm'
  ? '%s other conversation(s) have running tasks — continue?'
  : k === 'update.restartCooldown' ? 'cooldown %ss left' : k);
global.activeConvId = 'my-own-conv';
// DOM: keep elements stable so the restart-progress ownership race can assert
// that a late version-check response did not replace #updateModalBody.
const domElements = {};
function domElement(id) {
  if (!domElements[id]) {
    const classes = new Set();
    const attributes = new Map();
    domElements[id] = {
      classList: {
        add(c){ classes.add(c); },
        remove(...names){ names.forEach((name) => classes.delete(name)); },
        toggle(c){ classes.has(c) ? classes.delete(c) : classes.add(c); },
        contains(c){ return classes.has(c); },
      },
      querySelector: () => null,
      setAttribute: (name, value) => attributes.set(name, String(value)),
      removeAttribute: (name) => attributes.delete(name),
      getAttribute: (name) => attributes.get(name) || null,
      insertAdjacentHTML: (_position, html) => {
        domElements[id].innerHTML += String(html);
      },
      disabled: false,
      textContent: '',
      style: {},
      innerHTML: '',
    };
  }
  return domElements[id];
}
global.document = {
  getElementById: (id) => domElement(id),
  querySelector: () => null,
  addEventListener: () => {},
};
global.requestAnimationFrame = () => 0;
global.cancelAnimationFrame = () => {};
global.setInterval = () => 0;
global.clearInterval = () => {};
// setTimeout: run nothing async (the health-poll arm is fire-and-forget here).
let timeoutDelays = [];
global.setTimeout = (_callback, delayMs) => {
  timeoutDelays.push(delayMs);
  return timeoutDelays.length;
};
global.clearTimeout = () => {};
global.addEventListener = () => {};
global._onReady = () => {};   // feature-bridge.js deferred-ready hook (Epic-E sub-9)
global.location = { reload: () => {} };

// ── Instrumented Api + showConfirm (rebound per scenario) ──
let restartCalls = [];   // each entry = the payload object passed to restart()
let confirmCalls = [];   // each entry = the message passed to showConfirm()
let toastCalls = [];     // each entry = [icon, title, body]
let decideCalls = [];    // each entry = [approvalId, approved]
let confirmReturns = true;
let firstThrows409 = true;
let pendingFirst = false;   // first restart call answers 202 {pendingApproval}
let always429 = false;      // every restart call 429s (cooldown)
let pendingUpdateChecks = [];
let deferRestart = false;
let settleRestart = null;
let rejectRestart = null;
let monitorCalls = { begin: 0, end: [] };

global.BackendAvailabilityRestartScope = {
  begin: () => { monitorCalls.begin += 1; },
  end: (backendReachable) => {
    monitorCalls.end.push(backendReachable);
  },
};

global.showToast = (icon, title, body) => { toastCalls.push([icon, title, body]); };
global.Api = {
  update: {
    check: () => new Promise((resolve, reject) => {
      pendingUpdateChecks.push({ resolve, reject });
    }),
    restart: async (payload) => {
      restartCalls.push(payload || {});
      if (always429) {
        const e = new Error('HTTP 429');
        e.status = 429;
        e.body = { ok: false, retryAfterSec: 812 };
        throw e;
      }
      if (deferRestart) {
        return await new Promise((resolve, reject) => {
          settleRestart = resolve;
          rejectRestart = reject;
        });
      }
      // Human-approval gate emulation: without an approvalId the endpoint
      // only REGISTERS a pending approval (202 shape) — nothing executes.
      if (pendingFirst && !(payload && payload.approvalId)) {
        return { ok: true, needsApproval: true,
                 pendingApproval: { id: 'tok-1', action: 'restart', status: 'pending' } };
      }
      // Emulate the backend guard: the FIRST (force-less) call 409s when
      // siblings are running; a subsequent force call succeeds.
      const forced = !!(payload && payload.force);
      if (firstThrows409 && !forced) {
        const e = new Error('HTTP 409');
        e.status = 409;
        e.body = { ok: false, needsForce: true,
                   runningTasks: [{taskId:'aaaa1111'},{taskId:'bbbb2222'},{taskId:'cccc3333'}] };
        throw e;
      }
      return { ok: true, restarting: true, forced };
    },
    decideLifecycleApproval: async (id, approved) => {
      decideCalls.push([id, approved]);
      return { ok: true };
    },
  },
  health: { info: async () => ({ ok: true, version: '9.9.9' }) },
};
global.showConfirm = async (msg) => { confirmCalls.push(msg); return confirmReturns; };
global.pushSubscribe = () => {};
global.pushUnsubscribe = () => {};

const SRC = fs.readFileSync(process.argv[2], 'utf8');
function loadModule(src) { (0, eval)(src); }

function reset(opts) {
  restartCalls = [];
  confirmCalls = [];
  toastCalls = [];
  decideCalls = [];
  pendingUpdateChecks = [];
  confirmReturns = ('confirmReturns' in opts) ? opts.confirmReturns : true;
  firstThrows409 = ('firstThrows409' in opts) ? opts.firstThrows409 : true;
  pendingFirst = ('pendingFirst' in opts) ? opts.pendingFirst : false;
  always429 = ('always429' in opts) ? opts.always429 : false;
  deferRestart = ('deferRestart' in opts) ? opts.deferRestart : false;
  settleRestart = null;
  rejectRestart = null;
  timeoutDelays = [];
  monitorCalls = { begin: 0, end: [] };
  for (const id of Object.keys(domElements)) delete domElements[id];
  // Restart state is module-level; reset it between independent scenarios.
  try { _restartActive = false; _restartDone = false; } catch (_) {}
}
const flush = () => new Promise((r) => setImmediate(r));

(async () => {
  loadModule(SRC);
  if (typeof restartServer !== 'function') {
    console.log('FAIL fn_exposed restartServer missing'); process.exit(0);
  }
  check('fn_exposed', true);

  // ══ 1. siblings running → force-less first, 409, confirm(true) → force retry ══
  {
    reset({ firstThrows409: true, confirmReturns: true });
    await restartServer();
    await flush(); await flush();
    check('s1_two_calls', restartCalls.length === 2);
    check('s1_first_no_force', restartCalls.length >= 1 && !restartCalls[0].force);
    check('s1_first_scoped_convId', restartCalls.length >= 1 && restartCalls[0].convId === 'my-own-conv');
    check('s1_confirm_shown_once', confirmCalls.length === 1);
    check('s1_confirm_names_count', confirmCalls.length === 1 && /(^|[^0-9])3([^0-9]|$)/.test(String(confirmCalls[0])));
    check('s1_second_forced', restartCalls.length === 2 && restartCalls[1].force === true);
  }

  // ══ 2. user DECLINES the confirm → NO force call, no restart ══
  {
    reset({ firstThrows409: true, confirmReturns: false });
    await restartServer();
    await flush(); await flush();
    check('s2_confirm_shown', confirmCalls.length === 1);
    check('s2_only_one_call', restartCalls.length === 1);
    check('s2_no_forced_call', !restartCalls.some(c => c.force === true));
  }

  // ══ 3. idle server (no 409) → single force-less call, no confirm ══
  {
    reset({ firstThrows409: false, confirmReturns: true });
    await restartServer();
    await flush(); await flush();
    check('s3_single_call', restartCalls.length === 1);
    check('s3_no_force', restartCalls.length === 1 && !restartCalls[0].force);
    check('s3_no_confirm', confirmCalls.length === 0);
  }

  // ══ 4. DOUBLE-NEUTER: strip the 409 branch → confirm+force retry vanishes ══
  {
    // Remove the whole `if (e && e.status === 409 ...) { ... return; }` block
    // by neutering its entry condition so the branch is never taken.
    const NEEDLE = 'if (e && e.status === 409 && e.body && e.body.needsForce) {';
    const neutered = SRC.replace(NEEDLE, 'if (false) {');
    check('neuter_patch_applied', neutered !== SRC);
    loadModule(neutered);
    reset({ firstThrows409: true, confirmReturns: true });
    await restartServer();
    await flush(); await flush();
    // With the branch gone: the 409 falls to the generic catch → NO confirm,
    // NO second (forced) call. Both load-bearing assertions from S1 now fail,
    // which is exactly what proves the branch matters.
    check('neuter_no_confirm', confirmCalls.length === 0);
    check('neuter_no_forced_retry', !restartCalls.some(c => c.force === true));
  }

  // ══ 5. approval gate: 202 pending → JS approves (the click IS the gesture) → retries with approvalId ══
  {
    reset({ pendingFirst: true });
    await restartServer();
    await flush(); await flush();
    check('s5_two_calls', restartCalls.length === 2);
    check('s5_first_no_token', restartCalls.length >= 1 && !restartCalls[0].approvalId);
    check('s5_decide_approved_once', decideCalls.length === 1 && decideCalls[0][0] === 'tok-1' && decideCalls[0][1] === true);
    check('s5_second_carries_token', restartCalls.length === 2 && restartCalls[1].approvalId === 'tok-1');
    check('s5_no_confirm', confirmCalls.length === 0);
  }

  // ══ 6. cooldown 429 → toast, no retry, no confirm, no progress ══
  {
    reset({ always429: true });
    await restartServer();
    await flush(); await flush();
    check('s6_single_call', restartCalls.length === 1);
    check('s6_no_confirm', confirmCalls.length === 0);
    check('s6_cooldown_toast', toastCalls.some(c => String(c[1]).indexOf('cooldown') !== -1 && String(c[1]).indexOf('812') !== -1));
    check('s6_no_decide', decideCalls.length === 0);
  }

  // ══ 7. NEUTER #2: strip the pendingApproval dance → token never minted ══
  {
    const NEEDLE = 'if (r && r.pendingApproval) {';
    const neutered = SRC.replace(NEEDLE, 'if (false) {');
    check('neuter2_patch_applied', neutered !== SRC);
    loadModule(neutered);
    reset({ pendingFirst: true });
    await restartServer();
    await flush(); await flush();
    // With the dance gone: the 202 is treated as success directly — NO
    // decide call, NO approvalId retry. The s5 assertions would now FAIL,
    // proving the pendingApproval branch is load-bearing.
    check('neuter2_no_decide', decideCalls.length === 0);
    check('neuter2_no_token_retry', !restartCalls.some(c => !!c.approvalId));
  }

  // ══ 8. A genuine update-check 500 remains visible outside restart ══
  {
    loadModule(SRC);
    reset({ firstThrows409: false });
    const body = domElement('updateModalBody');
    body.innerHTML = '';
    const checkRun = _runUpdateCheck();
    await flush();
    check('s8_check_started', pendingUpdateChecks.length === 1);
    const err = new Error('HTTP 500'); err.status = 500;
    pendingUpdateChecks.shift().reject(err);
    await checkRun; await flush();
    check('s8_real_500_visible', body.innerHTML.indexOf('upd-err-card') >= 0);
  }

  // ══ 9. A late 500 from the pre-restart check cannot own the modal ══
  {
    reset({ firstThrows409: false });
    const body = domElement('updateModalBody');
    body.innerHTML = '';
    const staleCheck = _runUpdateCheck();
    await flush();
    check('s9_check_started', pendingUpdateChecks.length === 1);

    await restartServer();
    await flush();
    check('s9_restart_progress_rendered', body.innerHTML.indexOf('upd-restart') >= 0);

    // Model the narrow post-health/pre-reload window: _restartActive is
    // already false, so a boolean-only guard is insufficient. The request
    // generation must still reject this older check response.
    _restartActive = false;
    _restartDone = true;
    const err = new Error('HTTP 500'); err.status = 500;
    pendingUpdateChecks.shift().reject(err);
    await staleCheck; await flush();
    check('s9_late_500_keeps_progress', body.innerHTML.indexOf('upd-restart') >= 0);
    check('s9_late_500_hidden', body.innerHTML.indexOf('upd-err-card') < 0);
  }

  // ══ 10. Slow preflight gets immediate feedback, then advances in place ══
  {
    reset({ firstThrows409: false, deferRestart: true });
    const running = restartServer({ approvalId: 'approved-10' });
    await flush(); await flush();
    const body = domElement('updateModalBody');
    const card = domElement('updRestartCard');
    check('s10_modal_open_immediately', domElement('updateModal').classList.contains('open'));
    check('s10_preparing_card_immediately', body.innerHTML.indexOf('is-preparing') >= 0 && body.innerHTML.indexOf('update.restartPreparingTitle') >= 0);
    check('s10_controls_disabled', domElement('updateRestartNowBtn').disabled && domElement('updateShutdownBtn').disabled);
    check('s10_generic_alarm_suppressed', monitorCalls.begin === 1 && monitorCalls.end.length === 0);
    check('s10_no_health_poll_before_ack', !timeoutDelays.includes(2500));
    settleRestart({ ok: true, restarting: true });
    await running; await flush();
    check('s10_same_card_advanced', domElement('updRestartCard') === card && domElement('updRestartTitle').textContent === 'update.restartTitle');
    check('s10_health_poll_deferred', timeoutDelays.includes(2500));
    check('s10_alarm_still_suppressed', monitorCalls.end.length === 0);
  }

  // ══ 11. Explicit preflight refusal ends now; it is not a fake restart ══
  {
    reset({ firstThrows409: false, deferRestart: true });
    const running = restartServer({ approvalId: 'approved-11' });
    await flush(); await flush();
    const card = domElement('updRestartCard');
    const error = new Error('Frontend artifact preparation failed.');
    error.status = 409;
    error.body = { restartPreflightFailed: true, detail: 'synthetic failure' };
    rejectRestart(error);
    await running; await flush();
    check('s11_not_started_title', domElement('updRestartTitle').textContent === 'update.restartNotStartedTitle');
    check('s11_concrete_reason', domElement('updRestartSub').textContent.indexOf('Frontend artifact preparation failed.') >= 0);
    check('s11_online_hint', domElement('updRestartPhase').textContent === 'update.restartNotStartedHint');
    check('s11_controls_restored', !domElement('updateRestartNowBtn').disabled && !domElement('updateShutdownBtn').disabled);
    check('s11_alarm_restored_online', monitorCalls.end.length === 1 && monitorCalls.end[0] === true);
    check('s11_no_health_poll', !timeoutDelays.includes(2500));
    check('s11_retry_visible', card.innerHTML.indexOf('restartServer()') >= 0);
  }

  // ══ 12. Lost response to an approved POST verifies the new boot ══
  {
    reset({ firstThrows409: false, deferRestart: true });
    const running = restartServer({ approvalId: 'approved-12' });
    await flush(); await flush();
    const error = new Error('Failed to fetch');
    error.status = 0;
    error.code = 'network';
    rejectRestart(error);
    await running; await flush();
    check('s12_advances_to_restart_verification', domElement('updRestartTitle').textContent === 'update.restartTitle');
    check('s12_health_poll_deferred', timeoutDelays.includes(2500));
    check('s12_alarm_scope_remains', monitorCalls.end.length === 0);
  }

  // ══ 13. A pre-approval network failure cannot have scheduled restart ══
  {
    reset({ firstThrows409: false, deferRestart: true });
    const running = restartServer();
    await flush(); await flush();
    const error = new Error('Failed to fetch');
    error.status = 0;
    error.code = 'network';
    rejectRestart(error);
    await running; await flush();
    check('s13_stays_not_started', domElement('updRestartTitle').textContent === 'update.restartNotStartedTitle');
    check('s13_no_health_poll', !timeoutDelays.includes(2500));
    check('s13_alarm_rechecks_backend', monitorCalls.end.length === 1 && monitorCalls.end[0] === false);
  }

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_update_restart_force_confirm_flow():
    harness = os.path.join(HERE, '_update_restart_force_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'update.js')],
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
    assert not fails, 'update restart force-flow failures:\n' + output
    assert output.count('PASS') >= 55, f'expected >=55 PASS lines, got:\n{output}'
