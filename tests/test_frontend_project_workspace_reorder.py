"""tests/test_frontend_project_workspace_reorder.py — Workspace drag-to-reorder.

WHY
---
The Project Co-Pilot modal's WORKSPACE card lists the attached roots. Its ORDER
IS SEMANTIC: ``_mpFolders[0]`` is the PRIMARY root — it carries the star + the
``root`` badge and is sent to the backend as the primary in
``setPaths(folders, readOnly)``, the rest becoming extra roots. Until now the
order was fixed at whatever the server returned and could not be changed from
the UI at all, so "make this folder the root" was impossible without removing
and re-adding every other folder.

Two guarantees are asserted here against the REAL shipped
``static/js/project.js`` under jsdom:

  • ROOT AT TOP — ``_syncFoldersFromState()`` always seeds ``_mpFolders[0]``
    with ``projectState.path`` (the primary), extras after, and
    ``_mpRenderTags()`` paints the star + ``root`` badge on the TOP row only.
  • DRAG TO REORDER — dragging a row onto another row's top/bottom half moves
    it there (``_mpReorder`` + the delegated dragstart/dragover/drop wiring on
    the stable ``#mpFolderTags`` container), and dropping a row at position 0
    PROMOTES it to primary (badge follows). Buttons inside a row never start a
    drag, and a drop marks/clears the insertion caret.

jsdom reports a zero-size ``getBoundingClientRect`` for every element, which
would collapse the top-half/bottom-half decision, so the harness installs a
synthetic rect keyed on ``data-mp-idx`` (row *i* spans y=[50i, 50i+50)).

Triple-neuter proves the reorder math, the drop-index compensation and the
top-row primary marking are each load-bearing. Skips cleanly without
node/jsdom.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
_PROJECT_SRC = os.path.join(JS_DIR, 'project.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));

const dom = new JSDOM(`<!DOCTYPE html><body>
  <div id="projectModal">
    <span id="mpFolderCount"></span>
    <span id="pmMobileCount"></span>
    <div class="mp-folder-list" id="mpFolderTags"></div>
  </div>
</body>`, { url: 'http://localhost/' });
const { window } = dom;
global.window = window; global.document = window.document;
global.navigator = window.navigator;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.getConvById = () => null;
global.getActiveConv = () => null;

// jsdom has no layout: give each workspace row a synthetic 50px band so the
// top-half / bottom-half hover decision is exercised for real.
window.Element.prototype.getBoundingClientRect = function () {
  const raw = this.dataset ? this.dataset.mpIdx : undefined;
  const i = parseInt(raw, 10);
  if (Number.isInteger(i)) {
    return { top: i * 50, bottom: i * 50 + 50, height: 50,
             left: 0, right: 300, width: 300, x: 0, y: i * 50 };
  }
  return { top: 0, bottom: 0, height: 0, left: 0, right: 0, width: 0, x: 0, y: 0 };
};

let src = fs.readFileSync(SRC, 'utf8');
// _mpFolders / _mpReadOnly / projectState are top-level `let`s (module scope,
// unreachable from this outer scope) — append an in-scope bridge.
src += '\n;globalThis.__seed = (arr, ro) => { _mpFolders = arr.slice(); _mpReadOnly = new Set(ro || []); };'
     + '\n;globalThis.__folders = () => _mpFolders.slice();'
     + '\n;globalThis.__seedState = (st) => { projectState = st; };';
(0, eval)(src);

const out = [];
function check(name, cond, got) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : ' :: got=' + JSON.stringify(got)));
}

const list = document.getElementById('mpFolderTags');
const A = '/home/u/auto-motion', B = '/home/u/chatui', C = '/home/u/tofu-search';

function rows() { return [...list.querySelectorAll('.mp-row[data-mp-idx]')]; }
function rowAt(i) { return list.querySelector('.mp-row[data-mp-idx="' + i + '"]'); }
function names() { return rows().map(r => r.querySelector('.mp-row-name').textContent); }

/** Fire a delegated drag event on `el` at viewport y=`clientY`. */
function fire(el, type, clientY, dt) {
  const ev = new window.Event(type, { bubbles: true, cancelable: true });
  ev.clientY = clientY;
  ev.clientX = 10;
  ev.dataTransfer = dt || { setData(){}, getData(){ return ''; }, effectAllowed: '', dropEffect: '' };
  el.dispatchEvent(ev);
  return ev;
}
/** Full drag gesture: grab row `from`, hover row `over` (half), release. */
function drag(from, over, half) {
  const y = over * 50 + (half === 'top' ? 10 : 40);
  const dt = { setData(){}, getData(){ return ''; }, effectAllowed: '', dropEffect: '' };
  fire(rowAt(from), 'dragstart', from * 50 + 25, dt);
  fire(rowAt(over), 'dragover', y, dt);
  fire(rowAt(over), 'drop', y, dt);
}

// ══ Scenario A: ROOT AT TOP by default ══
// projectState carries the primary + two extras; the sync must put the PRIMARY
// at index 0 regardless of how the extras are ordered by the server.
globalThis.__seedState({ path: B, readOnly: false,
                         extraRoots: [{ path: A, readOnly: false },
                                      { path: C, readOnly: true }] });
_syncFoldersFromState();
check('sync_root_first', globalThis.__folders()[0] === B, globalThis.__folders());
check('sync_keeps_extras', globalThis.__folders().length === 3, globalThis.__folders());
_mpRenderTags();
check('render_top_is_primary', rowAt(0).classList.contains('mp-row-primary'));
check('render_only_one_primary',
  list.querySelectorAll('.mp-row-primary').length === 1);
const _topBadge = rowAt(0).querySelector('.mp-row-badge');
check('render_root_badge_on_top',
  !!_topBadge && _topBadge.textContent.trim() === 'root',
  _topBadge && _topBadge.textContent);
check('render_no_root_badge_below',
  rowAt(1).textContent.indexOf('root') === -1, rowAt(1).textContent);
check('readonly_flag_preserved', rowAt(2).classList.contains('mp-row-readonly'));

// ══ Scenario B: rows are drag-enabled and carry a grip ══
_attachMpReorder();
check('rows_draggable', rows().every(r => r.getAttribute('draggable') === 'true'));
check('rows_indexed', rows().map(r => r.dataset.mpIdx).join(',') === '0,1,2');
check('grip_present', rows().every(r => !!r.querySelector('.mp-row-grip')));
check('buttons_not_draggable',
  [...list.querySelectorAll('.mp-row button')].every(b => b.getAttribute('draggable') === 'false'));

// ══ Scenario C: drag the LAST row onto the TOP half of row 0 → promoted ══
globalThis.__seed([A, B, C]);
_mpRenderTags();
drag(2, 0, 'top');
check('drag_to_top_reorders', globalThis.__folders().join('|') === [C, A, B].join('|'),
      globalThis.__folders());
check('drag_to_top_promotes_primary', rowAt(0).classList.contains('mp-row-primary') &&
      rowAt(0).querySelector('.mp-row-name').textContent === 'tofu-search',
      names());
check('drag_clears_drop_marks',
  list.querySelectorAll('.mp-drop-before, .mp-drop-after').length === 0);
check('drag_clears_dragging_class',
  list.querySelectorAll('.mp-row-dragging').length === 0);

// ══ Scenario D: DOWNWARD move — index compensation ══
// Grab row 0 and release on the BOTTOM half of row 2 → it must land LAST.
globalThis.__seed([A, B, C]);
_mpRenderTags();
drag(0, 2, 'bottom');
check('drag_down_lands_last', globalThis.__folders().join('|') === [B, C, A].join('|'),
      globalThis.__folders());

// Same gesture into the MIDDLE slot: row 0 released on the bottom half of
// row 1 must land at index 1 — the clamp cannot rescue a missing shift
// compensation here (it would overshoot to the end instead).
globalThis.__seed([A, B, C]);
_mpRenderTags();
drag(0, 1, 'bottom');
check('drag_down_lands_middle', globalThis.__folders().join('|') === [B, A, C].join('|'),
      globalThis.__folders());

// ══ Scenario E: releasing on the TOP half of the row just below is a no-op ══
globalThis.__seed([A, B, C]);
_mpRenderTags();
drag(0, 1, 'top');
check('drag_noop_same_slot', globalThis.__folders().join('|') === [A, B, C].join('|'),
      globalThis.__folders());

// ══ Scenario F: the hover caret marks the target edge while dragging ══
globalThis.__seed([A, B, C]);
_mpRenderTags();
const dt = { setData(){}, getData(){ return ''; }, effectAllowed: '', dropEffect: '' };
fire(rowAt(2), 'dragstart', 125, dt);
fire(rowAt(0), 'dragover', 10, dt);
check('caret_before_on_top_half', rowAt(0).classList.contains('mp-drop-before'));
fire(rowAt(1), 'dragover', 90, dt);
check('caret_after_on_bottom_half', rowAt(1).classList.contains('mp-drop-after'));
check('caret_single_target',
  list.querySelectorAll('.mp-drop-before, .mp-drop-after').length === 1);
check('dragover_sets_move_cursor', dt.dropEffect === 'move', dt.dropEffect);
fire(rowAt(2), 'dragend', 125, dt);
check('dragend_clears_caret',
  list.querySelectorAll('.mp-drop-before, .mp-drop-after').length === 0);

// ══ Scenario G: a drag started ON A BUTTON is refused (click semantics kept) ══
globalThis.__seed([A, B, C]);
_mpRenderTags();
const removeBtn = rowAt(1).querySelector('.mp-row-remove');
const started = fire(removeBtn, 'dragstart', 75, dt);
check('button_dragstart_prevented', started.defaultPrevented === true);
check('button_dragstart_no_dragging_class',
  list.querySelectorAll('.mp-row-dragging').length === 0);
// …and a subsequent drop does nothing because no drag is in flight.
fire(rowAt(0), 'drop', 10, dt);
check('button_drag_leaves_order', globalThis.__folders().join('|') === [A, B, C].join('|'),
      globalThis.__folders());

// ══ Scenario H: a FILE drag (no reorder in flight) is not swallowed ══
globalThis.__seed([A, B, C]);
_mpRenderTags();
const fileOver = fire(rowAt(0), 'dragover', 10, dt);
check('file_drag_not_hijacked', fileOver.defaultPrevented === false);

// ══ Scenario I: touch reorder from the grip ══
globalThis.__seed([A, B, C]);
_mpRenderTags();
function touch(el, type, y, listName) {
  const ev = new window.Event(type, { bubbles: true, cancelable: true });
  const pt = { clientX: 10, clientY: y };
  ev[listName] = [pt];
  el.dispatchEvent(ev);
  return ev;
}
// elementFromPoint has no layout in jsdom → resolve via the synthetic bands.
document.elementFromPoint = (x, y) => rowAt(Math.min(2, Math.floor(y / 50))) || null;
touch(rowAt(2).querySelector('.mp-row-grip'), 'touchstart', 125, 'touches');
touch(rowAt(2), 'touchmove', 10, 'touches');
check('touch_caret_marks', rowAt(0).classList.contains('mp-drop-before'));
touch(rowAt(2), 'touchend', 10, 'changedTouches');
check('touch_reorders', globalThis.__folders().join('|') === [C, A, B].join('|'),
      globalThis.__folders());

console.log(out.join('\n'));
"""


def _run(src_path):
    harness = os.path.join(HERE, '_ws_reorder_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, src_path, ROOT],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


_EXPECTED = (
    'sync_root_first', 'sync_keeps_extras',
    'render_top_is_primary', 'render_only_one_primary',
    'render_root_badge_on_top', 'render_no_root_badge_below',
    'readonly_flag_preserved',
    'rows_draggable', 'rows_indexed', 'grip_present', 'buttons_not_draggable',
    'drag_to_top_reorders', 'drag_to_top_promotes_primary',
    'drag_clears_drop_marks', 'drag_clears_dragging_class',
    'drag_down_lands_last', 'drag_down_lands_middle', 'drag_noop_same_slot',
    'caret_before_on_top_half', 'caret_after_on_bottom_half',
    'caret_single_target', 'dragover_sets_move_cursor', 'dragend_clears_caret',
    'button_dragstart_prevented', 'button_dragstart_no_dragging_class',
    'button_drag_leaves_order', 'file_drag_not_hijacked',
    'touch_caret_marks', 'touch_reorders',
)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_workspace_reorder_and_root_at_top():
    output = _run(_PROJECT_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'workspace-reorder failures:\n' + output
    for must in _EXPECTED:
        assert ('PASS ' + must) in output, output


def _nc(anchor, replacement, must_fail, must_still_pass):
    """Patch a COPY of project.js, run, assert the target checks flip to FAIL
    while controls stay PASS, then confirm the shipped file is untouched."""
    with open(_PROJECT_SRC, encoding='utf-8') as f:
        original = f.read()
    assert anchor in original, f'NC anchor not found: {anchor[:70]!r}'
    patched = original.replace(anchor, replacement, 1)
    assert patched != original, 'NC replacement was a no-op'
    # Suffix with this module's name so sibling NC suites that copy the SAME
    # shipped source can never collide under xdist.
    copy_path = _PROJECT_SRC + '.wsreorder.nc_copy.js'
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        for m in must_fail:
            assert ('FAIL ' + m) in output, f'NC: expected {m} to FAIL:\n{output}'
        for m in must_still_pass:
            assert ('PASS ' + m) in output, \
                f'NC must be surgical — {m} should still PASS:\n{output}'
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_PROJECT_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_reorder_move_is_load_bearing():
    """Neuter the splice-move in _mpReorder (repaint only) → every reorder
    check FAILs while the render / root-at-top checks still PASS."""
    _nc(
        anchor='  const moved = _mpFolders.splice(from, 1)[0];\n  _mpFolders.splice(dest, 0, moved);',
        replacement='  void dest;',
        must_fail=['drag_to_top_reorders', 'drag_down_lands_last', 'touch_reorders'],
        must_still_pass=['sync_root_first', 'render_top_is_primary',
                         'caret_before_on_top_half'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_drop_index_compensation_is_load_bearing():
    """Drop the `from < to` shift compensation → a downward move into a MIDDLE
    slot overshoots by one, while upward moves (unaffected by the shift) still
    PASS. The move-to-tail case is deliberately NOT asserted here: the index
    clamp coincidentally lands it correctly, so it cannot detect the bug."""
    _nc(
        anchor='  if (from < to) to -= 1;   // removing `from` first shifts everything after it',
        replacement='  // compensation removed',
        must_fail=['drag_down_lands_middle'],
        must_still_pass=['drag_to_top_reorders', 'touch_reorders'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_top_row_is_primary_is_load_bearing():
    """Mark the LAST row primary instead of the first → the root-at-top checks
    FAIL while the reorder mechanics still PASS."""
    _nc(
        anchor='    const isPrimary = i === 0;',
        replacement='    const isPrimary = i === _mpFolders.length - 1;',
        must_fail=['render_top_is_primary', 'render_root_badge_on_top'],
        must_still_pass=['drag_to_top_reorders', 'drag_down_lands_last'],
    )


def test_modal_open_wires_the_reorder_listeners():
    """Static: openProjectModal() must arm the delegated reorder listeners —
    otherwise the rows render draggable but nothing listens."""
    src = open(_PROJECT_SRC, encoding='utf-8').read()
    m = re.search(r'function openProjectModal\(\) \{(.*?)\n\}', src, re.S)
    assert m, 'openProjectModal() not found in project.js'
    assert '_attachMpReorder();' in m.group(1), \
        'openProjectModal must call _attachMpReorder() to wire drag-to-reorder'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
