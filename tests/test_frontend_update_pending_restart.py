#!/usr/bin/env python3
"""tests/test_frontend_update_pending_restart.py — regression for the
RELOAD-ROBUST update completion path (the main scenario of the feature).

WHY
---
The download outlives the page: 5-15 minutes in, the user reloads or closes
the tab. The server now persists the apply outcome and projects it through
/update/check (``pending_restart`` / ``apply_in_progress``); the frontend
must, on ANY fresh page load:
  1. toast the "download finished — restart now?" offer from a bare boot
     check (and only ONCE per version), parking the result so the dialog
     renders the restart card instead of the apply button;
  2. re-attach the push subscription of a still-running download so its
     terminal done frame still toasts without the user opening the dialog.

This harness loads the REAL shipped update.js under bare node, stubs the DOM
+ Api + toast + push layers, and drives both flows. NEUTER: deleting the
boot-check pending_restart branch makes assertion (1) fail — proving the
branch is load-bearing.
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


_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

global.debugLog = () => {};
global.escapeHtml = (s) => String(s);
global.t = (k) => k;
global.addEventListener = () => {};
global._onReady = () => {};   // feature-bridge.js's deferred-ready hook (Epic-E sub-9)
global.setTimeout = () => 1;
global.clearTimeout = () => {};
global.requestAnimationFrame = () => 0;
global.cancelAnimationFrame = () => {};
global.setInterval = () => 0;
global.clearInterval = () => {};

const toasts = [];
global.showToast = (icon, title, detail, dur, opts) => { toasts.push({ icon, title, detail, dur, opts }); };
const subs = [];
global.pushSubscribe = (ch, id, h) => { subs.push({ ch, id, h }); };
global.pushUnsubscribe = () => {};
global.Api = { update: { check: async () => global.__CHECK_PAYLOAD } };

const _idMap = {};
function El(tag, cls) {
  const self = {
    tag: tag || 'div', _className: cls || '', _classes: new Set((cls||'').split(' ').filter(Boolean)),
    children: [], style: {}, textContent: '', _dataset: {},
    set className(v){ self._className = v; self._classes = new Set(String(v||'').split(' ').filter(Boolean)); },
    get className(){ return self._className; },
    classList: {
      add(c){ self._classes.add(c); }, remove(){ for (const c of arguments) self._classes.delete(c); },
      toggle(c){ self._classes.has(c) ? self._classes.delete(c) : self._classes.add(c); },
      contains(c){ return self._classes.has(c); },
    },
    set innerHTML(v){
      self._html = v; self.children = [];
      if (/id="updateActionArea"/.test(v)) { _idMap['updateActionArea'] = El('div','upd-action'); }
    },
    get innerHTML(){ return self._html || ''; },
    appendChild(c){ c._parent = self; self.children.push(c); return c; },
    remove(){},
    querySelector(){ return null; },
  };
  return self;
}
_idMap['updateModal'] = El('div', 'modal');
_idMap['updateModalBody'] = El('div');
_idMap['updateActionArea'] = El('div', 'upd-action');
global.document = {
  createElement: (t) => El(t),
  getElementById: (id) => _idMap[id] || null,
};

const SRC = fs.readFileSync(process.argv[2], 'utf8');
function loadModule(src){ (0, eval)(src); }
loadModule(SRC);

const flush = () => new Promise((r) => setImmediate(r));

(async function main() {
  if (typeof _updateBootCheck !== 'function' || typeof _doneResultFromPending !== 'function') {
    console.log('FAIL fns_exposed'); console.log(out.join('\n')); process.exit(0);
  }
  check('fns_exposed', true);

  // ── Flow 1: fresh page load AFTER the download finished server-side ──
  global.__CHECK_PAYLOAD = {
    ok: true, current: '0.16.0', latest: '0.17.0', update_available: true,
    pending_restart: { new_version: '0.17.0', old_version: '0.16.0',
                       method: 'tarball', finished_at: 1, changed: true,
                       deps_changed: false, deps_installed: false },
  };
  await _updateBootCheck(); await flush();
  const bootToast = toasts.find((x) => String(x.title).indexOf('update.bgDoneTitle') === 0);
  check('boot_toasts_restart_offer', !!bootToast);
  check('boot_toast_clickable_restart', !!(bootToast && bootToast.opts && typeof bootToast.opts.onClick === 'function'));
  check('boot_parks_done_result', !!(_updateDoneResult && _updateDoneResult.new_version === '0.17.0'));

  // Once per version: a second boot check (badge re-render) must NOT re-toast.
  toasts.length = 0;
  await _updateBootCheck(); await flush();
  check('boot_toast_once_per_version', !toasts.some((x) => String(x.title).indexOf('update.bgDoneTitle') === 0));

  // The dialog then renders the RESTART card, never the apply button again.
  await openUpdateDialog(); await flush();
  check('dialog_restart_card', _idMap['updateActionArea'].innerHTML.indexOf('updateRestartBtn') >= 0);
  check('dialog_no_reapply', _idMap['updateActionArea'].innerHTML.indexOf('updateApplyBtn') < 0);

  // ── Flow 2: fresh page load WHILE the download still runs ──
  toasts.length = 0; subs.length = 0;
  loadModule(SRC);   // reset module state (fresh page)
  _idMap['updateModal'].classList.remove('open');   // fresh page: dialog closed
  global.__CHECK_PAYLOAD = {
    ok: true, current: '0.16.0', latest: '0.17.0', update_available: true,
    apply_in_progress: { task_id: 'bg-1', started_at: 1, old_version: '0.16.0' },
  };
  await _updateBootCheck(); await flush();
  const sub = subs.find((s) => s.id === 'bg-1');
  check('boot_reattaches_subscription', !!sub);
  // The still-running download's terminal frame toasts even with no dialog open.
  sub.h({ taskId: 'bg-1', type: 'done', ok: true, changed: true, needs_restart: true,
          new_version: '0.17.0', deps_changed: false, deps_installed: false });
  check('reattached_done_toasts', toasts.some((x) => String(x.title).indexOf('update.bgDoneTitle') === 0));

  // ── NEUTER: delete the boot-check pending_restart branch → flow 1 dies ──
  {
    const NEEDLE = "    if (r.pending_restart && r.pending_restart.new_version) {\n" +
                   "      _updateDoneResult = _doneResultFromPending(r.pending_restart);";
    const neutered = SRC.replace(NEEDLE, '    if (false) {');
    check('neuter_applied', neutered !== SRC);
    toasts.length = 0; subs.length = 0;
    loadModule(neutered);
    global.__CHECK_PAYLOAD = {
      ok: true, current: '0.16.0', latest: '0.17.0', update_available: true,
      pending_restart: { new_version: '0.17.0', old_version: '0.16.0',
                         method: 'tarball', finished_at: 1, changed: true,
                         deps_changed: false, deps_installed: false },
    };
    await _updateBootCheck(); await flush();
    check('neuter_no_boot_toast', !toasts.some((x) => String(x.title).indexOf('update.bgDoneTitle') === 0));
    check('neuter_no_done_result', !_updateDoneResult);
  }

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.log('FAIL harness_crash ' + (e && e.stack || e)); console.log(out.join('\n')); process.exit(0); });
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_update_pending_restart_reload_path():
    harness = os.path.join(HERE, '_update_pending_restart_harness.js')
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
    fails = [l for l in output.splitlines() if l.startswith('FAIL')]
    assert not fails, 'pending-restart reload-path failures:\n' + output
    assert output.count('PASS') >= 12, f'expected >=12 PASS lines, got:\n{output}'
