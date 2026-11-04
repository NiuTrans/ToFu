"""tests/frontend/test_frontend_project_workspace_reorder.py — Workspace root order.

WHY
---
The Project Co-Pilot modal's WORKSPACE card lists the attached roots. Its ORDER
IS SEMANTIC: ``_mpFolders[0]`` is the PRIMARY root — it carries the star + the
``root`` badge and is sent to the backend as the primary in
``setPaths(folders, readOnly)``, the rest becoming extra roots. Moving a folder
to the top is therefore the "make this folder the root" gesture.

The rows, drag/touch/keyboard gestures and drop-index math belong to the shared
rail (``frontend/src/features/project/workspace-rail.test.ts``). The Co-Pilot
host owns the ordered draft, so this suite drives the REAL composed
``project.js`` + rail under jsdom and asserts what lives in the host:

  • ROOT AT TOP — ``_syncFoldersFromState()`` always seeds ``_mpFolders[0]``
    with ``projectState.path`` (the primary), extras after in server order,
    read-only flags preserved.
  • MOVE PORT — ``_mpReorder(from, to)`` lands the entry at final index ``to``
    (clamped), refuses invalid or same-slot moves, repaints the rail, and is
    the port the rail's grip keyboard reorder calls.

A neuter of the splice-move proves the reorder checks are load-bearing.
Skips cleanly without node/jsdom.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')
_PROJECT_SRC = os.path.join(JS_DIR, 'project.js')


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));

const dom = new JSDOM(`<!DOCTYPE html><body>
  <div id="projectModal">
    <span id="pmMobileCount"></span>
    <div class="pm-body"></div>
  </div>
</body>`, { url: 'http://localhost/' });
const { window } = dom;
global.window = window; global.document = window.document;
global.navigator = window.navigator;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');

let src = fs.readFileSync(SRC, 'utf8');
// _mpFolders / _mpReadOnly / projectState are top-level `let`s (module scope,
// unreachable from this outer scope) — append an in-scope bridge.
src += '\n;globalThis.__seed = (arr, ro) => { _mpFolders = arr.slice(); _mpReadOnly = new Set(ro || []); };'
     + '\n;globalThis.__folders = () => _mpFolders.slice();'
     + '\n;globalThis.__readOnly = () => [..._mpReadOnly];'
     + '\n;globalThis.__seedState = (st) => { projectState = st; };';
(0, eval)(src);

const out = [];
function check(name, cond, got) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : ' :: got=' + JSON.stringify(got)));
}

const A = '/home/u/auto-motion', B = '/home/u/chatui', C = '/home/u/tofu-search';
const order = () => globalThis.__folders().join('|');
const railRows = () => [...document.querySelectorAll(
  '#projectModal .pm-rail .mp-folder-list > li.mp-row[data-path]')];

// ══ Scenario A: ROOT AT TOP ══
// projectState carries the primary + two extras; the sync must put the PRIMARY
// at index 0 regardless of how the extras are ordered by the server.
globalThis.__seedState({ path: B, readOnly: false,
                         extraRoots: [{ path: A, readOnly: false },
                                      { path: C, readOnly: true }] });
_syncFoldersFromState();
check('sync_root_first', globalThis.__folders()[0] === B, globalThis.__folders());
check('sync_keeps_extras', order() === [B, A, C].join('|'), globalThis.__folders());
check('readonly_flag_preserved',
  globalThis.__readOnly().join('|') === C, globalThis.__readOnly());

// ══ Scenario B: _mpReorder(from, to) — `to` is the FINAL index ══
globalThis.__seed([A, B, C]);
_mpRenderTags();
const promoted = _mpReorder(2, 0);
check('reorder_up_to_top', promoted === true && order() === [C, A, B].join('|'),
      [promoted, globalThis.__folders()]);
check('reorder_repaints_rail',
  railRows().map(r => r.dataset.path).join('|') === [C, A, B].join('|'),
  railRows().map(r => r.dataset.path));

globalThis.__seed([A, B, C]);
_mpReorder(0, 2);
check('reorder_down_to_last', order() === [B, C, A].join('|'), globalThis.__folders());

globalThis.__seed([A, B, C]);
_mpReorder(0, 1);
check('reorder_down_to_middle', order() === [B, A, C].join('|'), globalThis.__folders());

globalThis.__seed([A, B, C]);
const clamped = _mpReorder(0, 99);
check('reorder_clamps_target', clamped === true && order() === [B, C, A].join('|'),
      [clamped, globalThis.__folders()]);

globalThis.__seed([A, B, C]);
const sameSlot = _mpReorder(1, 1);
check('reorder_same_slot_is_noop', sameSlot === false && order() === [A, B, C].join('|'),
      [sameSlot, globalThis.__folders()]);

const refused = [_mpReorder(3, 0), _mpReorder(-1, 0), _mpReorder(0.5, 1), _mpReorder('1', 0)];
check('reorder_rejects_invalid',
  refused.every(r => r === false) && order() === [A, B, C].join('|'),
  [refused, globalThis.__folders()]);

// ══ Scenario C: the rail's move port is _mpReorder ══
globalThis.__seed([A, B, C]);
_mpRenderTags();
const grip = railRows().find(r => r.dataset.path === C).querySelector('.mp-row-grip');
grip.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true, cancelable: true }));
check('rail_grip_key_moves_draft', order() === [A, C, B].join('|'), globalThis.__folders());

console.log(out.join('\n'));
"""


def _run(src_path, tmp_path):
    harness = tmp_path / '_ws_reorder_harness.js'
    harness.write_text(_HARNESS, encoding='utf-8')
    proc = subprocess.run(
        ['node', str(harness), src_path, ROOT],
        capture_output=True, text=True, timeout=60)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


_EXPECTED = (
    'sync_root_first', 'sync_keeps_extras', 'readonly_flag_preserved',
    'reorder_up_to_top', 'reorder_repaints_rail', 'reorder_down_to_last',
    'reorder_down_to_middle', 'reorder_clamps_target',
    'reorder_same_slot_is_noop', 'reorder_rejects_invalid',
    'rail_grip_key_moves_draft',
)


def test_workspace_reorder_and_root_at_top(tmp_path):
    output = _run(_PROJECT_SRC, tmp_path)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'workspace-reorder failures:\n' + output
    for must in _EXPECTED:
        assert ('PASS ' + must) in output, output


def _nc(tmp_path, anchor, replacement, must_fail, must_still_pass):
    """Patch a COPY of project.js, run, assert the target checks flip to FAIL
    while controls stay PASS, then confirm the shipped file is untouched."""
    with open(_PROJECT_SRC, encoding='utf-8') as f:
        original = f.read()
    assert anchor in original, f'NC anchor not found: {anchor[:70]!r}'
    patched = original.replace(anchor, replacement, 1)
    assert patched != original, 'NC replacement was a no-op'
    copy_path = tmp_path / 'project.wsreorder.nc_copy.js'
    copy_path.write_text(patched, encoding='utf-8')
    output = _run(str(copy_path), tmp_path)
    for m in must_fail:
        assert ('FAIL ' + m) in output, f'NC: expected {m} to FAIL:\n{output}'
    for m in must_still_pass:
        assert ('PASS ' + m) in output, \
            f'NC must be surgical — {m} should still PASS:\n{output}'
    with open(_PROJECT_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project.js must be byte-identical'


def test_NC_reorder_move_is_load_bearing(tmp_path):
    """Neuter the splice-move in _mpReorder (repaint only) → every reorder
    check FAILs while the root-at-top and refusal checks still PASS."""
    _nc(
        tmp_path,
        anchor='  const moved = _mpFolders.splice(from, 1)[0];\n  _mpFolders.splice(dest, 0, moved);',
        replacement='  void dest;',
        must_fail=['reorder_up_to_top', 'reorder_repaints_rail',
                   'reorder_down_to_middle', 'rail_grip_key_moves_draft'],
        must_still_pass=['sync_root_first', 'reorder_same_slot_is_noop',
                         'reorder_rejects_invalid'],
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
