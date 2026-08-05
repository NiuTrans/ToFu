"""tests/test_frontend_update_background.py — regression for BACKGROUND
self-update completion + the download-finished restart notification.

WHY
---
The apply already runs server-side in a background thread, but the frontend
used to babysit it: re-opening the dialog mid-apply re-ran the version check
and painted the "checking…" spinner over the live stepper (losing all
visibility), and when the download finished with the modal closed the user
was NEVER told — the restart prompt rendered into a hidden modal. The update
now (a) replays recorded stage frames when the dialog re-opens mid-apply,
(b) renders the "restart to apply" card when re-opening after completion,
and (c) raises a clickable toast on the terminal done/failure frame while
the modal is closed ("click to restart now").

This harness loads the REAL shipped update.js under bare node, stubs the DOM
+ push + toast layers, and drives the full lifecycle: apply → stage frame →
close modal (background toast) → re-open mid-apply (stepper replayed, no
check spinner) → done frame (restart toast with onClick) → re-open (restart
card, not the check spinner).

DOUBLE-NEUTER (on mutated copies; the shipped file is untouched):
  N1 strips the background done-toast call   → the toast assertion FAILS;
  N2 strips ``_updateDoneResult = r;``       → the re-open shows no restart
                                               card. Both prove the new
                                               behaviour is load-bearing.
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
global._onReady = () => {};   // feature-loader.js's deferred-ready hook (Epic-E sub-9)
global.setTimeout = () => 1;
global.clearTimeout = () => {};
global.requestAnimationFrame = () => 0;
global.cancelAnimationFrame = () => {};
global.setInterval = () => 0;
global.clearInterval = () => {};

// ── Capture layers ──────────────────────────────────────────────────
const toasts = [];   // {icon,title,detail,dur,opts}
global.showToast = (icon, title, detail, dur, opts) => { toasts.push({ icon, title, detail, dur, opts }); };
const subs = [];     // {channel,taskId,handler}
global.pushSubscribe = (ch, id, h) => { subs.push({ ch, id, h }); };
global.pushUnsubscribe = () => {};
global.Api = { update: { apply: async () => ({ ok: true, taskId: 'task-1' }) } };

// ── Minimal DOM (extends the update-progress harness's El): classList,
//    dataset, innerHTML that materialises the stepper rows / bar fill /
//    action-area scaffold, getElementById over a registered id map. ──
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
      if (/upd-stepper/.test(v)) {
        ['fetch','pull','deps'].forEach(function (st) {
          const li = El('li','upd-step'); li._dataset.stage = st;
          li.appendChild(El('span','upd-step-dot'));
          li.appendChild(El('span','upd-step-label'));
          li.appendChild(El('span','upd-step-detail'));
          self.appendChild(li);
        });
      }
      if (/upd-step-bar-fill/.test(v)) { self.appendChild(El('div','upd-step-bar-fill')); }
      if (/id="updateActionArea"/.test(v)) { _idMap['updateActionArea'] = El('div','upd-action'); }
    },
    get innerHTML(){ return self._html || ''; },
    appendChild(c){ c._parent = self; self.children.push(c); return c; },
    remove(){ if (self._parent){ const i=self._parent.children.indexOf(self); if(i>=0) self._parent.children.splice(i,1);} },
    querySelector(sel){
      const m = sel.match(/^\.([\w-]+)\[data-stage="([\w-]+)"\]$/);
      const want = m ? m[1] : sel.replace(/^\./,'');
      const stageWanted = m ? m[2] : null;
      const walk = (node) => {
        for (const ch of node.children) {
          if (ch._classes.has(want) && (stageWanted === null || ch._dataset.stage === stageWanted)) return ch;
          const deep = walk(ch); if (deep) return deep;
        }
        return null;
      };
      return walk(self);
    },
  };
  return self;
}
_idMap['updateModal'] = El('div', 'modal open');
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
const modal = _idMap['updateModal'];
const body = _idMap['updateModalBody'];
const area = () => _idMap['updateActionArea'];

(async function main() {
  if (typeof applyUpdate !== 'function' || typeof _onUpdateDone !== 'function') {
    console.log('FAIL fns_exposed'); console.log(out.join('\n')); process.exit(0);
  }
  check('fns_exposed', true);

  // 1. Start the apply — handler registered, stepper + background hint shown.
  await applyUpdate();
  await flush();
  const sub = subs.find((s) => s.id === 'task-1');
  check('handler_registered', !!sub);
  check('stepper_rendered', area().innerHTML.indexOf('upd-stepper') >= 0);
  check('stepper_bg_hint', area().innerHTML.indexOf('update.bgHint') >= 0);

  // 2. A live stage frame — recorded for replay.
  sub.h({ taskId: 'task-1', type: 'stage', stage: 'fetch', status: 'active', detail: '1 MB / 10 MB', pct: 10 });

  // 3. Close the modal mid-apply → background-running toast, modal hidden.
  const t0 = toasts.length;
  closeUpdateModal();
  check('close_hides_modal', !modal.classList.contains('open'));
  check('bg_toast_on_close', toasts.slice(t0).some((x) => x.title === 'update.bgRunningToast'));

  // 4. Re-open mid-apply → stepper rebuilt + replayed (fetch active at 10%),
  //    NOT the "checking…" spinner that used to clobber it.
  await openUpdateDialog();
  await flush();
  check('reopen_no_check_spinner', body.innerHTML.indexOf('upd-checking-wrap') < 0);
  const fEl = _updateStageEls && _updateStageEls.fetch;
  check('reopen_replays_active', !!(fEl && fEl._classes.has('is-active')));
  const fBar = fEl && fEl.querySelector('.upd-step-bar');
  const fFill = fBar && fBar.querySelector('.upd-step-bar-fill');
  check('reopen_replays_pct', !!(fFill && parseFloat(fFill.style.width) === 10));

  // 5. Close again, then the terminal done frame lands while hidden →
  //    restart toast with a clickable onClick, result parked for re-open.
  closeUpdateModal();
  const t1 = toasts.length;
  sub.h({ taskId: 'task-1', type: 'done', ok: true, changed: true, needs_restart: true,
          new_version: '0.17.0', deps_changed: false, deps_installed: false });
  const doneToast = toasts.slice(t1).find((x) => String(x.title).indexOf('update.bgDoneTitle') === 0);
  check('bg_done_toast', !!doneToast);
  check('bg_done_toast_clickable', !!(doneToast && typeof doneToast.opts === 'object' && typeof doneToast.opts.onClick === 'function'));
  check('done_result_set', !!_updateDoneResult && _updateDoneResult.new_version === '0.17.0');

  // 6. Re-open after completion → restart card (never the check spinner).
  await openUpdateDialog();
  await flush();
  check('reopen_done_card', area().innerHTML.indexOf('updateRestartBtn') >= 0);
  check('reopen_done_no_spinner', body.innerHTML.indexOf('upd-checking-wrap') < 0);

  // 7. NEUTER 1 — strip the background done-toast → the notification is gone.
  {
    const N1 = "    showToast('✅', t('update.bgDoneTitle').replace('%s', 'v' + (r.new_version || '')),\n" +
               "      t('update.bgDoneBody'), 30000,\n" +
               "      { hint: t('update.bgDoneHint'), onClick: function () { restartServer(); } });";
    const neutered = SRC.replace(N1, '/* neutered bg-done toast */');
    check('neuter1_applied', neutered !== SRC);
    toasts.length = 0; subs.length = 0;
    loadModule(neutered);
    await applyUpdate(); await flush();
    const s2 = subs.find((s) => s.id === 'task-1');
    closeUpdateModal(); toasts.length = 0;   // drop the bg-running toast
    s2.h({ taskId: 'task-1', type: 'done', ok: true, changed: true, needs_restart: true,
           new_version: '0.17.0', deps_changed: false, deps_installed: false });
    check('neuter1_no_done_toast', !toasts.some((x) => String(x.title).indexOf('update.bgDoneTitle') === 0));
  }

  // 8. NEUTER 2 — strip ``_updateDoneResult = r;`` (success path) → re-open
  //    falls back to a fresh check; the restart card never renders.
  {
    const N2 = '  _updateDoneResult = r;\n  _renderUpdateDone(r);';
    const neutered = SRC.replace(N2, '  _renderUpdateDone(r);');
    check('neuter2_applied', neutered !== SRC);
    toasts.length = 0; subs.length = 0;
    loadModule(neutered);
    await applyUpdate(); await flush();
    const s3 = subs.find((s) => s.id === 'task-1');
    closeUpdateModal();
    s3.h({ taskId: 'task-1', type: 'done', ok: true, changed: true, needs_restart: true,
           new_version: '0.17.0', deps_changed: false, deps_installed: false });
    area().innerHTML = '';   // simulate the card not being repainted on re-open
    await openUpdateDialog(); await flush(); await flush();
    check('neuter2_no_done_card', area().innerHTML.indexOf('updateRestartBtn') < 0);
  }

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.log('FAIL harness_crash ' + (e && e.stack || e)); console.log(out.join('\n')); process.exit(0); });
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_update_background_completion_and_notify():
    harness = os.path.join(HERE, '_update_background_harness.js')
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
    assert not fails, 'update background/notify failures:\n' + output
    assert output.count('PASS') >= 16, f'expected >=16 PASS lines, got:\n{output}'
