"""Regression suite: ``mpDeleteFolder`` (project panel docked browser) removes
the directory row in the same task as the confirm (owner directive 2026-07-31,
epic pt_e8a166d6a4b64123).

WHY
---
The confirm closed, then the code awaited ``Api.project.rmdir`` and only
AFTER the response rebuilt the list — the row sat there for the whole RTT,
inviting repeated clicks. The deletion lands in ``.tofu_trash`` (recoverable
by design), making this the safest optimistic delete in the app: remove the
row + drop the staged workspace tag on the click frame, run the DELETE in the
background, and on failure re-fetch the directory (server truth restores the
row) and re-stage the tag.

Drives the REAL project.js under node: ``browseDirectory`` seeds the state
through the real fetch path, then ``mpDeleteFolder`` runs with a controllable
rmdir promise. Skips cleanly when node isn't installed.
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
const scenario = process.argv[3];   // 'ok' | 'fail' | 'fail-staged'

const calls = { browseListRenders: 0, browse: 0, rmdir: [], alerts: [], toasts: [] };
let _rmRes, _rmRej;

const _els = {};
function el(id) {
  if (!_els[id]) _els[id] = {
    id, _html: '',
    set innerHTML(v) { if (id === 'browseList') calls.browseListRenders++; this._html = v; },
    get innerHTML() { return this._html; },
    textContent: '', value: '', hidden: false, disabled: false, scrollLeft: 0, scrollWidth: 0,
    style: {}, dataset: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    querySelectorAll() { return []; },
    addEventListener() {}, setAttribute() {},
  };
  return _els[id];
}
global.document = {
  getElementById(id) { return el(id); },
  querySelectorAll() { return []; },
  createElement() { return el('_c' + Math.random()); },
  addEventListener() {},
  body: { appendChild() {} },
};
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;
global.debugLog = () => {};
global.showConfirm = async () => true;
global.showAlert = (m) => { calls.alerts.push(m); return Promise.resolve(); };
global.showToast = (...a) => { calls.toasts.push(a); };
global.Icon = () => '';

const DIRS_FULL = [
  { path: '/root/alpha', name: 'alpha', itemCount: 2, hasCode: true },
  { path: '/root/beta',  name: 'beta',  itemCount: 0 },
];
global.Api = {
  project: {
    browse: async () => {
      calls.browse++;
      /* After a SUCCESSFUL delete the server no longer has alpha; on failure
       * it still does (server truth drives the restore). */
      const deleted = scenario === 'ok' && calls.rmdir.length > 0;
      const dirs = deleted ? DIRS_FULL.filter(d => d.path !== '/root/alpha') : DIRS_FULL;
      return { path: '/root', parent: null,
               dirs: JSON.parse(JSON.stringify(dirs)), filesCount: 3 };
    },
    rmdir: (p) => { calls.rmdir.push(p);
      return new Promise((res, rej) => { _rmRes = res; _rmRej = rej; }); },
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // project.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const ROW = 'data-dir-path="/root/alpha"';

(async () => {
  check('fns_exposed', typeof mpDeleteFolder === 'function'
        && typeof browseDirectory === 'function');

  // ── Seed through the REAL fetch path (populates _browseState + renders). ──
  await browseDirectory('/root');
  const listEl = document.getElementById('browseList');
  check('seed_rows_rendered', listEl.innerHTML.indexOf(ROW) !== -1
        && listEl.innerHTML.indexOf('data-dir-path="/root/beta"') !== -1);
  check('seed_browse_called', calls.browse === 1);

  // ── Optionally stage the folder in the workspace list first. ──
  const tagsEl = document.getElementById('mpFolderTags');
  if (scenario === 'fail-staged') {
    mpAddBrowsedPath('/root/alpha');
    check('staged_tag_rendered', tagsEl.innerHTML.indexOf('/root/alpha') !== -1);
  }

  const p = mpDeleteFolder('/root/alpha', 'alpha');

  // Confirm resolves on a microtask — flush until the rmdir call lands (the
  // removal happens synchronously BEFORE it).
  for (let i = 0; i < 5 && calls.rmdir.length === 0; i++) await Promise.resolve();
  check('rmdir_called', calls.rmdir.join(',') === '/root/alpha');

  // ★ INSTANT-UI pins: the row (and the staged tag) are gone BEFORE the
  //   server responds. (Old code: everything waited for the RTT.)
  check('row_removed_instantly', listEl.innerHTML.indexOf(ROW) === -1);
  check('sibling_row_kept', listEl.innerHTML.indexOf('data-dir-path="/root/beta"') !== -1);
  if (scenario === 'fail-staged') {
    check('tag_removed_instantly', tagsEl.innerHTML.indexOf('/root/alpha') === -1);
  }

  for (let i = 0; i < 5 && typeof _rmRes !== 'function'; i++) await Promise.resolve();
  if (scenario === 'ok') {
    _rmRes({ ok: true, json: async () => ({ ok: true }) });
    await p;
    check('success_toast', calls.toasts.some(a => a[0] === 'folder.deleted'));
    check('refreshed_after_success', calls.browse >= 2);
    check('row_stays_gone', listEl.innerHTML.indexOf(ROW) === -1);
    check('no_alert', calls.alerts.length === 0);
  } else {
    _rmRes({ ok: false, status: 500, json: async () => ({ error: 'boom' }) });
    await p;
    // ★ Rollback: re-fetch restores the row from server truth; the staged tag
    //   is re-staged; the existing alert surfaces the error.
    check('restore_refetch', calls.browse >= 2);
    check('row_restored', listEl.innerHTML.indexOf(ROW) !== -1);
    check('alert_shown', calls.alerts.length === 1 && String(calls.alerts[0]).indexOf('boom') !== -1);
    if (scenario === 'fail-staged') {
      check('tag_restored', tagsEl.innerHTML.indexOf('/root/alpha') !== -1);
    }
  }

  console.log(out.join('\n'));
})();
"""


def _run(scenario: str, min_pass: int) -> str:
    harness = os.path.join(HERE, f'_mpdelete_opt_{scenario}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'project.js'), scenario],
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
    assert not fails, f'mpDeleteFolder ({scenario}) failures:\n' + output
    assert output.count('PASS') >= min_pass, \
        f'expected >={min_pass} PASS lines, got:\n{output}'
    return output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_mpdelete_removes_row_instantly_then_persists():
    _run('ok', 8)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_mpdelete_failure_restores_row():
    _run('fail', 7)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_mpdelete_failure_restores_row_and_staged_tag():
    _run('fail-staged', 9)
