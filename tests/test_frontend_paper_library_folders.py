"""jsdom test for the Reading-mode paper-library FOLDERS feature.

Loads the REAL shipped ``static/js/paper/library.js`` under jsdom and drives the
folder render / assign / drill-in paths directly against stubbed ``Api`` +
minimal globals, asserting:

  * the grouped view renders a folder header (with the member count) plus the
    unfiled papers below it;
  * each paper row carries a "move to folder" <select> whose current option
    reflects the paper's ``folderId``;
  * ``_assignPaperFolder`` mutates the entry + calls the library upsert with the
    new ``folderId`` (so the assignment persists);
  * drilling into a folder (``_setActivePaperFolder``) shows ONLY that folder's
    papers, and ``null`` returns to the grouped view;
  * deleting a folder unfiles its members (client-side) and drops the folder.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
LIBRARY_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'library.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<span id="paperLibCount"></span>' +
  '<div id="paperLibraryList"></div>' +
  '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
win.t = global.t = (k, vars) => k;
global.escapeHtml = win.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
global.paperMode = win.paperMode = true;
// upsert spy — records the body so we can assert folderId persistence.
let upserts = [];
global.Api = win.Api = {
  paper: {
    libraryUpsert: (id, body) => { upserts.push({ id, body }); return Promise.resolve({ ok: true }); },
    libraryDelete: () => Promise.resolve({ ok: true }),
  },
  paperFolders: {
    list: () => Promise.resolve([]),
    create: () => Promise.resolve(null),
    update: () => Promise.resolve(null),
    remove: () => Promise.resolve(true),
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/library.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Seed state directly (bypass the network load).
_paperFolders = [{ id: 'pf_a', name: 'Diffusion', color: '', collapsed: false, order: 0, createdAt: 1 }];
_paperLibrary = [
  { id: 'p1', title: 'Paper One', folderId: 'pf_a', createdAt: Date.now(), pageCount: 3, _persisted: true },
  { id: 'p2', title: 'Paper Two', folderId: '',     createdAt: Date.now(), pageCount: 5, _persisted: true },
];
_activePaperId = 'p1';

(async () => {
  // ── 1. Grouped view: folder header + count + both a foldered and unfiled row.
  _renderPaperLibrary();
  const listEl = document.getElementById('paperLibraryList');
  const html = listEl.innerHTML;
  check('folder_group_rendered', !!listEl.querySelector('.paper-folder-group[data-folder="pf_a"]'));
  check('folder_name_shown', html.indexOf('Diffusion') !== -1);
  const cnt = listEl.querySelector('.paper-folder-count');
  check('folder_count_is_1', !!cnt && cnt.textContent.trim() === '1');
  check('new_folder_bar_present', !!listEl.querySelector('.paper-folder-new-btn'));
  check('both_papers_rendered', listEl.querySelectorAll('.paper-lib-item').length === 2);
  // p1's move-select current value = pf_a; p2's = '' (unfiled)
  const sels = listEl.querySelectorAll('.paper-lib-item-folder');
  check('move_select_per_row', sels.length === 2);

  // ── 2. Assign p2 into pf_a → entry mutated + upsert called with folderId.
  upserts = [];
  _assignPaperFolder('p2', 'pf_a');
  const p2 = _paperLibrary.find(p => p.id === 'p2');
  check('assign_mutated_entry', p2.folderId === 'pf_a');
  check('assign_persisted', upserts.length === 1 && upserts[0].id === 'p2'
        && upserts[0].body.folderId === 'pf_a');
  // Re-render: folder now has 2 members, no unfiled rows outside a group.
  _renderPaperLibrary();
  check('folder_count_now_2', document.querySelector('.paper-folder-count').textContent.trim() === '2');

  // ── 3. Drill into the folder → only its members, plus a back crumb.
  _setActivePaperFolder('pf_a');
  const l2 = document.getElementById('paperLibraryList');
  check('folder_view_crumb', !!l2.querySelector('.paper-folder-crumb'));
  check('folder_view_two_items', l2.querySelectorAll('.paper-lib-item').length === 2);
  check('folder_view_no_group', !l2.querySelector('.paper-folder-group'));
  // Back to grouped view.
  _setActivePaperFolder(null);
  check('back_to_grouped', !!document.querySelector('.paper-folder-group'));

  // ── 4. Unassign p1 (move to no folder) → folder count drops to 1.
  upserts = [];
  _assignPaperFolder('p1', '');
  check('unassign_empty_folderId', _paperLibrary.find(p => p.id==='p1').folderId === '');
  check('unassign_persisted_empty', upserts.length === 1 && upserts[0].body.folderId === '');

  // ── 5. Delete the folder → members unfiled, folder gone.
  await _deletePaperFolder('pf_a');
  check('folder_deleted', _paperFolders.length === 0);
  check('members_unfiled', _paperLibrary.every(p => (p.folderId || '') === ''));
  _renderPaperLibrary();
  check('no_group_after_delete', !document.querySelector('.paper-folder-group'));

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(js_path: str):
    harness = os.path.join(HERE, '_paper_folders_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, js_path, ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return [ln for ln in output.splitlines() if ln.startswith(('PASS ', 'FAIL '))]


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_paper_library_folders_render_assign_and_view():
    lines = _run(LIBRARY_JS)
    fails = [ln for ln in lines if ln.startswith('FAIL')]
    assert not fails, 'paper library folder failures:\n' + '\n'.join(lines)
    assert len(lines) >= 15, 'expected >=15 result lines, got:\n' + '\n'.join(lines)
