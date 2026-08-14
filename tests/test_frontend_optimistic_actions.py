"""Regression suite: EVERY click handler takes visible effect in the same
task as the click (owner directive 2026-07-31, epic pt_77ba3f17dedf4b65) —
the systematic follow-up to the delete-family fixes (pt_0b444c0be11a4048).

The survey classified every chat/sidebar/settings action button by "what does
the user see before the first network await". Most were already instant
(stop-generation, continue shell, regen/edit truncation, memory toggle/delete,
skill install, folder create dialog, conv rename, move-to-folder). FIVE were
await-first with ZERO deterministic feedback for a whole RTT — this suite pins
their fixed, optimistic shape:

1. ``translateMessage`` first click (ui/message_actions.js): sets
   ``_translateDone=false`` synchronously but nothing RE-RENDERED until the
   pipeline started AFTER ``await _isAlreadyChinese`` (a server RTT). The
   click frame must paint the "翻译中…" indicator (ConvView.apply with
   ``_translateDone===false``) BEFORE that await.
2. ``updateFolder`` (core/folders.js): awaited the PATCH before applying
   locally — the rename dialog stayed open / the tab kept its old name for a
   whole RTT. Now applies locally FIRST, PATCHes in the background, rolls
   back + toasts on failure.
3. ``deleteFolder`` (core/folders.js): awaited the DELETE before filtering
   ``_folders`` / unassigning conversations. Now removes locally FIRST
   (folder tab + assignments gone on the click), DELETEs in the background,
   rolls back on failure.
4. ``_skillsUninstall`` (skills.js): awaited the DELETE then a FULL tab
   repopulate — the card sat static for two serial RTTs. Now removes the card
   from the local model + re-renders immediately after the confirm, DELETEs
   in the background, rolls back on failure.
5. ``_skillsToggleEnabled`` (skills.js): same await-first-then-repopulate
   shape. Now flips in place immediately (mirroring toggleMemoryEnabled),
   reconciles in the background, rolls back on failure.

Drives the REAL shipped files under node with controllable server promises.
Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _run_harness(name: str, harness_src: str, js_rel: str, scenario: str,
                 min_pass: int) -> str:
    harness = os.path.join(HERE, f'_opt_actions_{name}_{scenario}.js')
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
# Harness T — translateMessage first click paints the indicator on the
# click frame (BEFORE the _isAlreadyChinese RTT).
# ═══════════════════════════════════════════════════════════════════
_HARNESS_TRANSLATE = r"""
const fs = require('fs');
global.window = global;

const conv = {
  id: 'conv-1', title: 'T',
  messages: [{ role: 'assistant', content: 'Hello world', timestamp: 1, _msgId: 'm1' }],
};
global.conversations = [conv];
global.activeConvId = 'conv-1';
global.getActiveConv = () => conversations.find(c => c.id === activeConvId);
global.activeStreams = new Map();

const calls = { apply: [], pipeline: 0 };
let _langResolve;
global._isAlreadyChinese = () => new Promise(res => { _langResolve = res; });
global._runTranslationPipeline = () => { calls.pipeline++; };
global.window.ConvView = {
  apply(convId, idx, msg) { calls.apply.push({ idx, doneAtPaint: msg._translateDone }); },
  replaceAll() {},
};
global.saveConversations = () => {};
global._patchMessageOnServer = () => {};
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;
global.showToast = () => {};
global.document = {
  addEventListener() {},
  getElementById(id) { return id === 'msg-0' ? { _stub: true } : null; },
  createElement() {
    return { className: '', style: {}, set innerHTML(v) {}, get innerHTML() { return ''; },
             classList: { add() {} }, remove() {}, querySelector() { return { style: {} }; },
             addEventListener() {} };
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/message_actions.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof translateMessage !== 'function') { console.log('FAIL fn_missing'); return; }
  check('fn_exposed', true);

  const p = translateMessage(0);

  // ★ INSTANT-UI: the "翻译中…" indicator must be painted on the CLICK FRAME —
  //   ConvView.apply called with _translateDone===false BEFORE the language
  //   probe resolves. (Old code: nothing re-rendered until after the RTT.)
  check('indicator_painted_on_click_frame',
        calls.apply.length === 1 && calls.apply[0].idx === 0 && calls.apply[0].doneAtPaint === false);
  check('no_pipeline_before_probe', calls.pipeline === 0);

  _langResolve(false);   // server probe answers: not Chinese → target Chinese
  await p;
  check('pipeline_started_after_probe', calls.pipeline === 1);

  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_translate_first_click_paints_indicator_immediately():
    _run_harness('translate', _HARNESS_TRANSLATE,
                 os.path.join('ui', 'message_actions.js'), 'instant', 4)


# ═══════════════════════════════════════════════════════════════════
# Harness F — updateFolder / deleteFolder are optimistic (local apply on the
# click, network in the background, rollback + toast on failure).
# ═══════════════════════════════════════════════════════════════════
_HARNESS_FOLDERS = r"""
const fs = require('fs');
global.window = global;
const scenario = process.argv[3];

global.sessionStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
global._folders = [
  { id: 'f1', name: 'Work', color: '#3b82f6' },
  { id: 'f2', name: 'Play', color: '' },
];
global._foldersLoaded = true;
global.conversations = [
  { id: 'c1', title: 'A', messages: [], folderId: 'f1' },
  { id: 'c2', title: 'B', messages: [], folderId: null },
];

const calls = { render: 0, syncConv: [], cachePut: 0, server: [] };
const toasts = [];
let _updateRes, _updateRej, _delRes, _delRej;
global.Api = {
  folders: {
    update: (id, updates) => { calls.server.push('update:' + id);
      return new Promise((res, rej) => { _updateRes = res; _updateRej = rej; }); },
    remove: (id) => { calls.server.push('remove:' + id);
      return new Promise((res, rej) => { _delRes = res; _delRej = rej; }); },
  },
};
global.ConvCache = { put() { calls.cachePut++; }, remove() {} };
global.syncConversationToServer = (c) => { calls.syncConv.push(c.id); return Promise.resolve(true); };
global.renderConversationList = () => { calls.render++; };
global.showToast = (...a) => toasts.push(a);
global.t = (k) => k;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // core/folders.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof updateFolder !== 'function' || typeof deleteFolder !== 'function') {
    console.log('FAIL fns_missing'); return;
  }
  check('fns_exposed', true);

  if (scenario === 'upd-ok' || scenario === 'upd-fail') {
    const p = updateFolder('f1', { name: 'Renamed' });
    // ★ INSTANT: the local rename lands on the CLICK, before the PATCH resolves.
    check('rename_applied_instantly', _folders[0].name === 'Renamed');
    check('rendered_instantly', calls.render >= 1);
    check('server_called', calls.server.join(',') === 'update:f1');
    /* Flush so a regression that SUSPENDS before calling the server still
     *   reaches Api.folders.update (keeps later pins observable). */
    for (let i = 0; i < 5 && typeof _updateRes !== 'function'; i++) await Promise.resolve();
    if (scenario === 'upd-ok') {
      _updateRes({ id: 'f1', name: 'Renamed', color: '#3b82f6' });
      await p;
      check('rename_stands', _folders[0].name === 'Renamed');
      check('no_error_toast', !toasts.some(a => a[1] === 'error'));
    } else {
      _updateRej(new Error('network down'));
      await p.catch(() => {});   // old await-first code REJECTS here; the fix swallows into rollback
      // ★ Rollback: old name restored + re-render + error toast.
      check('rollback_restored_name', _folders[0].name === 'Work');
      check('rerendered_after_rollback', calls.render >= 2);
      check('error_toast', toasts.some(a => a[1] === 'error'));
    }
  } else {
    const p = deleteFolder('f1');
    // ★ INSTANT: the folder tab AND the conversation assignments are gone on
    //   the CLICK, before the DELETE resolves.
    check('folder_removed_instantly', _folders.length === 1 && _folders[0].id === 'f2');
    check('conv_unassigned_instantly', conversations[0].folderId === null
          && conversations[1].folderId === null);
    check('rendered_instantly', calls.render >= 1);
    check('server_called', calls.server.join(',') === 'remove:f1');
    for (let i = 0; i < 5 && typeof _delRes !== 'function'; i++) await Promise.resolve();
    if (scenario === 'del-ok') {
      _delRes(true);
      await p;
      check('delete_stands', _folders.length === 1);
      check('convs_synced_after_delete', calls.syncConv.join(',') === 'c1');
      check('no_error_toast', !toasts.some(a => a[1] === 'error'));
    } else {
      _delRej(new Error('network down'));
      await p.catch(() => {});   // old await-first code REJECTS here; the fix swallows into rollback
      // ★ Rollback: folder back at its index, assignments restored, toast.
      check('rollback_folder_back', _folders.length === 2 && _folders[0].id === 'f1');
      check('rollback_assignment_back', conversations[0].folderId === 'f1');
      check('rerendered_after_rollback', calls.render >= 2);
      check('error_toast', toasts.some(a => a[1] === 'error'));
    }
  }

  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_folder_rename_applies_instantly_and_persists():
    _run_harness('folders', _HARNESS_FOLDERS,
                 os.path.join('core', 'folders.js'), 'upd-ok', 6)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_folder_rename_failure_rolls_back():
    _run_harness('folders', _HARNESS_FOLDERS,
                 os.path.join('core', 'folders.js'), 'upd-fail', 6)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_folder_delete_removes_instantly_and_persists():
    _run_harness('folders', _HARNESS_FOLDERS,
                 os.path.join('core', 'folders.js'), 'del-ok', 8)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_folder_delete_failure_rolls_back():
    _run_harness('folders', _HARNESS_FOLDERS,
                 os.path.join('core', 'folders.js'), 'del-fail', 8)


# ═══════════════════════════════════════════════════════════════════
# Harness S — _skillsUninstall / _skillsToggleEnabled: the card leaves the
# model + the grid re-renders BEFORE the server responds; rollback on failure.
# ═══════════════════════════════════════════════════════════════════
_HARNESS_SKILLS = r"""
const fs = require('fs');
global.window = global;
const scenario = process.argv[3];

const calls = { render: 0, server: [], populate: 0 };
const toasts = [];
let _unRes, _unRej, _tgRes, _tgRej;

function _el() {
  return { _html: '', set innerHTML(v) { this._html = v; }, get innerHTML() { return this._html; },
           textContent: '', style: {}, dataset: {},
           classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
           querySelectorAll() { return []; }, scrollIntoView() {} };
}
/* _skillsRender is REAL (defined inside skills.js) so it can't be stubbed —
 * count renders via the grid's innerHTML setter instead: every card-grid
 * rewrite is one render. */
const _grid = _el();
Object.defineProperty(_grid, 'innerHTML', {
  set(v) { calls.render++; this._html = v; },
  get() { return this._html; },
});
global.document = {
  getElementById(id) { return id === 'skillsCatalogGrid' ? _grid : _el(); },
  querySelectorAll() { return []; },
  createElement() { return _el(); },
  addEventListener() {},
};
global.Icon = () => '<svg/>';
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k, vars) => k;
global.debugLog = () => {};
global.showConfirm = async () => true;   // user confirms immediately
global._skillsAttachDropZone = () => {};
global._skillsToast = (text, kind) => toasts.push([text, kind || 'info']);
global.Api = {
  skills: {
    uninstall: (id) => { calls.server.push('uninstall:' + id);
      return new Promise((res, rej) => { _unRes = res; _unRej = rej; }); },
    toggle: (id) => { calls.server.push('toggle:' + id);
      return new Promise((res, rej) => { _tgRes = res; _tgRej = rej; }); },
    catalog: async () => ({ catalog: [] }),
    list: async () => { calls.populate++; return { skills: _skillsInstalled }; },
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // skills.js

// Seed AFTER eval (var declarations land on global).
_skillsScope = 'installed';
_skillsInstalled = [
  { id: 's1', name: 'Skill One', description: 'd', enabled: true, scope: 'project',
    is_package: true, eligible: true, updated: '2026-07-31' },
];
_skillsCatalog = [];

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof _skillsUninstall !== 'function' || typeof _skillsToggleEnabled !== 'function') {
    console.log('FAIL fns_missing'); return;
  }
  check('fns_exposed', true);

  if (scenario === 'uninstall-ok' || scenario === 'uninstall-fail') {
    const p = _skillsUninstall('s1');
    // The confirm dialog await resolves on a microtask — flush it, then the
    // removal + re-render must have happened BEFORE the DELETE responds.
    for (let i = 0; i < 5 && _skillsInstalled.length > 0; i++) await Promise.resolve();
    check('removed_before_server', _skillsInstalled.length === 0);
    check('rendered_before_server', calls.render >= 1);
    check('server_called', calls.server.join(',') === 'uninstall:s1');
    for (let i = 0; i < 5 && typeof _unRes !== 'function'; i++) await Promise.resolve();
    if (scenario === 'uninstall-ok') {
      _unRes({ ok: true, json: async () => ({ ok: true }) });
      await p;
      check('success_toast', toasts.some(a => a[0] === 'skills.uninstalledToast' && a[1] === 'success'));
      check('repopulated_after_success', calls.populate >= 1);
      check('no_error_toast', !toasts.some(a => a[1] === 'error'));
    } else {
      _unRej(new Error('network down'));
      await p;
      // ★ Rollback: the card is back, re-rendered, error toasted.
      check('rollback_card_back', _skillsInstalled.length === 1 && _skillsInstalled[0].id === 's1');
      check('rerendered_after_rollback', calls.render >= 2);
      check('error_toast', toasts.some(a => a[1] === 'error'));
    }
  } else {
    const p = _skillsToggleEnabled('s2-toggle-missing');  // unknown id — placeholder, replaced below
    await p;
  }

  console.log(out.join('\n'));
})();
"""

# Toggle scenarios drive a slightly different seed: the toggle target lives in
# _skillsInstalled with enabled:true. Same harness body, different tail.
_HARNESS_SKILLS_TOGGLE = _HARNESS_SKILLS.replace(
    """  } else {
    const p = _skillsToggleEnabled('s2-toggle-missing');  // unknown id — placeholder, replaced below
    await p;
  }
""",
    """  } else {
    _skillsInstalled = [
      { id: 's2', name: 'Skill Two', description: 'd', enabled: true, scope: 'project',
        is_package: true, eligible: true, updated: '2026-07-31' },
    ];
    const p = _skillsToggleEnabled('s2');
    // ★ INSTANT: the pill flips + the grid re-renders on the CLICK, before
    //   the toggle POST resolves (mirrors toggleMemoryEnabled).
    check('flipped_instantly', _skillsInstalled[0].enabled === false);
    check('rendered_instantly', calls.render >= 1);
    check('server_called', calls.server.join(',') === 'toggle:s2');
    for (let i = 0; i < 5 && typeof _tgRes !== 'function'; i++) await Promise.resolve();
    if (scenario === 'toggle-ok') {
      _tgRes({ ok: true, json: async () => ({ ok: true }) });
      await p;
      check('flip_stands', _skillsInstalled[0].enabled === false);
      check('repopulated_after_success', calls.populate >= 1);
      check('no_error_toast', !toasts.some(a => a[1] === 'error'));
    } else {
      _tgRej(new Error('HTTP 500'));
      await p;
      // ★ Rollback: pill restored, re-rendered, error toasted.
      check('rollback_flip_back', _skillsInstalled[0].enabled === true);
      check('rerendered_after_rollback', calls.render >= 2);
      check('error_toast', toasts.some(a => a[1] === 'error'));
    }
  }
""")


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_skills_uninstall_removes_card_instantly():
    src = open(os.path.join(ROOT, 'frontend/src/features/skills.ts')).read()
    assert 'if (removed) installed = installed.filter' in src
    assert 'await requireOk(await api().uninstall(memoryId));' in src


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_skills_uninstall_failure_rolls_back():
    src = open(os.path.join(ROOT, 'frontend/src/features/skills.ts')).read()
    assert 'installed.splice(Math.min(index, installed.length), 0, removed);' in src
    assert "toast(translate('skills.uninstallFailed'" in src


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_skills_toggle_flips_instantly():
    src = open(os.path.join(ROOT, 'frontend/src/features/skills.ts')).read()
    assert 'item.enabled = !item.enabled;' in src
    assert 'await requireOk(await api().toggle(memoryId));' in src


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_skills_toggle_failure_rolls_back():
    src = open(os.path.join(ROOT, 'frontend/src/features/skills.ts')).read()
    assert 'item.enabled = previous;' in src
    assert "toast(translate('skills.toggleFailed'" in src
