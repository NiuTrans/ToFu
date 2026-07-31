"""tests/test_frontend_image_reorder_cursor.py — the reposition ("move")
cursor must persist across the WHOLE reorder drag, not only when the pointer is
over a sibling chip.

The UX problem this pins: multiple attached image chips are draggable
(``draggable="true"`` + ``data-img-idx``) and reorder via the document-level
delegated handlers in ``static/js/upload.js``. During a native HTML5 drag the
OS cursor is governed by ``dataTransfer.dropEffect`` (it OVERRIDES CSS
``cursor``), and ``dropEffect='move'`` only takes hold after a ``dragover``
listener calls ``preventDefault()``. The buggy handler bailed
(``if (!hit) return;``) BEFORE ``preventDefault()`` whenever the pointer was
NOT over a sibling chip — so over the gaps between chips or the surrounding
input area the browser fell back to the no-drop / copy ("upload") cursor, and
the gesture felt like a file upload instead of a reposition.

The fix: once a reorder is in flight (``_imgDragFromIdx !== null``), the
``dragover`` handler ``preventDefault()``s + sets ``dropEffect='move'``
EVERYWHERE; the drop-target highlight still only lights the chip under the
pointer.

Because ``dragover`` now accepts the drop EVERYWHERE, the ``drop`` handler must
in turn SWALLOW the drop everywhere (``preventDefault()`` + ``stopPropagation()``
unconditionally once a reorder is in flight); otherwise releasing a chip over
the ``#userInput`` textarea (or any gap) runs the browser's native text-drop
default and leaks the ``text/plain`` index payload into the input box. Off-chip
it performs NO array move — reordering may only ever change chip order.

This suite drives the REAL shipped ``static/js/upload.js`` through jsdom:
  • a ``dragover`` OVER a sibling chip → accepted (move cursor) + highlight;
  • a ``dragover`` OFF any chip (a container gap) → STILL accepted (move
    cursor), no highlight;
  • a ``drop`` OFF any chip (on a stubbed textarea) mid-reorder → swallowed
    (``defaultPrevented``), ``pendingImages`` UNCHANGED — no text leak;
  • a ``drop`` ON a sibling chip → performs the array move.

Two NCs, each byte-reverting one guard in a COPY of the source:
  • dragover: restore the early ``if (!hit) return;`` bail → off-chip dragover
    NOT accepted, cursor stays 'copy';
  • drop: restore the early ``if (!hit) return;`` bail → off-chip drop NOT
    swallowed (native default runs → payload leak).
Shipped file untouched by both.

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
_UPLOAD_SRC = os.path.join(ROOT, 'static', 'js', 'upload.js')


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
const dom = new JSDOM(
  '<!DOCTYPE html><body><div class="image-previews" id="imagePreviews"></div>' +
  '<textarea id="userInput"></textarea></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

// ── Minimal globals upload.js reaches for at render time ──
win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.apiUrl = global.apiUrl = (u) => u;
win.debugLog = global.debugLog = () => {};
win.config = global.config = {};
win._editingMsgIdx = global._editingMsgIdx = null;
// Two attached images → two reorderable chips.
win.pendingImages = global.pendingImages = [
  { preview: 'data:image/png;base64,AAA', sizeKB: 10 },
  { preview: 'data:image/png;base64,BBB', sizeKB: 20 },
];

eval(fs.readFileSync(SRC, 'utf8'));  // upload.js — registers the document drag listeners

renderImagePreviews();  // paint the two chips into #imagePreviews

const tray = win.document.getElementById('imagePreviews');
const chips = tray.querySelectorAll('.img-preview[data-img-idx]');

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
check('two_chips_rendered', chips.length === 2);

// A synthetic HTML5 DataTransfer (jsdom has no native one).
function makeDT() {
  return { effectAllowed: 'uninitialized', dropEffect: 'none',
           _data: {}, setData(k, v) { this._data[k] = String(v); },
           getData(k) { return this._data[k] || ''; } };
}
// Dispatch a cancelable drag event on `el` with a shared dataTransfer.
function fire(type, el, dt) {
  const ev = new win.Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(ev, 'dataTransfer', { value: dt, configurable: true });
  el.dispatchEvent(ev);
  return ev;
}

// 1. Begin a reorder drag from chip 0.
const dt = makeDT();
fire('dragstart', chips[0], dt);
check('drag_started_effectAllowed_move', dt.effectAllowed === 'move');

// 2. dragover OVER the sibling chip (index 1) → accepted + highlighted.
const dtOver = makeDT();
const evOver = fire('dragover', chips[1], dtOver);
check('over_chip_accepted', evOver.defaultPrevented === true);
check('over_chip_move_cursor', dtOver.dropEffect === 'move');
check('over_chip_highlight', chips[1].classList.contains('img-drop-target'));

// 3. dragover OFF any chip — on the container gap. THE REGRESSION CASE:
//    the move cursor must persist even though there's no chip under the
//    pointer, and nothing should get the drop-target highlight.
const dtGap = makeDT();
const evGap = fire('dragover', tray, dtGap);
check('offchip_accepted', evGap.defaultPrevented === true);
check('offchip_move_cursor', dtGap.dropEffect === 'move');
check('offchip_no_highlight',
      tray.querySelectorAll('.img-preview.img-drop-target').length === 0);

// 4. drop OFF any chip — released over the #userInput textarea mid-reorder.
//    THE REGRESSION CASE: the drop must be SWALLOWED (defaultPrevented, so the
//    browser's native text-drop that would insert the text/plain index into
//    the textarea never runs) and pendingImages must be UNCHANGED (off-chip =
//    no move). We do NOT reset _imgDragFromIdx here — the drop handler owns it.
const textarea = win.document.getElementById('userInput');
const before = win.pendingImages.map((im) => im.preview).join(',');
const dtDropGap = makeDT();
const evDropGap = fire('drop', textarea, dtDropGap);
const afterGap = win.pendingImages.map((im) => im.preview).join(',');
check('offchip_drop_swallowed', evDropGap.defaultPrevented === true);
check('offchip_drop_no_move', afterGap === before);
check('offchip_drop_order_intact',
      afterGap === 'data:image/png;base64,AAA,data:image/png;base64,BBB');

// 5. drop ON a sibling chip → the array move actually happens (0 → 1).
//    Re-arm the drag (step 4's drop consumed _imgDragFromIdx).
fire('dragstart', win.document.getElementById('imagePreviews')
      .querySelectorAll('.img-preview[data-img-idx]')[0], makeDT());
const chipsNow = win.document.getElementById('imagePreviews')
      .querySelectorAll('.img-preview[data-img-idx]');
const dtDropChip = makeDT();
const evDropChip = fire('drop', chipsNow[1], dtDropChip);
const afterMove = win.pendingImages.map((im) => im.preview).join(',');
check('onchip_drop_swallowed', evDropChip.defaultPrevented === true);
check('onchip_drop_reordered',
      afterMove === 'data:image/png;base64,BBB,data:image/png;base64,AAA');

console.log(out.join('\n'));
"""


def _run(upload_src):
    harness = os.path.join(HERE, '_img_reorder_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, upload_src, ROOT],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_reorder_dragover_keeps_move_cursor_everywhere():
    """The shipped dragover handler accepts a reorder drag both OVER a sibling
    chip and OFF any chip (container gap), so the OS reposition cursor persists
    across the whole gesture; the highlight only tracks the chip under the
    pointer."""
    output = _run(_UPLOAD_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'reorder-cursor failures:\n' + output
    for marker in ('PASS two_chips_rendered', 'PASS drag_started_effectAllowed_move',
                   'PASS over_chip_accepted', 'PASS over_chip_move_cursor',
                   'PASS over_chip_highlight', 'PASS offchip_accepted',
                   'PASS offchip_move_cursor', 'PASS offchip_no_highlight',
                   'PASS offchip_drop_swallowed', 'PASS offchip_drop_no_move',
                   'PASS offchip_drop_order_intact', 'PASS onchip_drop_swallowed',
                   'PASS onchip_drop_reordered'):
        assert marker in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_offchip_accept_is_load_bearing():
    """NC: byte-revert the "accept everywhere" fix in a COPY of upload.js
    (restore the early ``if (!hit) return;`` bail before preventDefault) → an
    off-chip dragover is NOT accepted and dropEffect stays 'copy' (the
    upload-feeling regression) → the off-chip assertions flip. Shipped file
    untouched."""
    with open(_UPLOAD_SRC, encoding='utf-8') as f:
        original = f.read()
    # Anchor on the accept pair ALONE (the two lines that make a reorder drag
    # droppable anywhere): lines AROUND them (e.g. an inserted
    # _imgDragGhostMove / hit lookup) are layout noise, not the invariant.
    anchor = (
        "  e.preventDefault();  // allow drop \u2192 keeps the move cursor across the whole drag\n"
        "  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';\n")
    assert anchor in original, 'accept-everywhere anchor not found (source changed?)'
    reverted = (
        "  { const _hit = _imgChipFrom(e.target); if (!_hit) return; }  // NC: off-chip bail restored\n"
        "  e.preventDefault();  // allow drop\n"
        "  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';\n")
    patched = original.replace(anchor, reverted, 1)
    assert patched != original, 'NC patch was a no-op'
    copy_path = os.path.join(HERE, '_img_reorder_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        assert ('FAIL offchip_accepted' in output
                or 'FAIL offchip_move_cursor' in output), \
            ('NC: without the off-chip accept, an off-chip dragover must NOT be '
             'accepted and the move cursor must be lost (assertion should '
             'fail):\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_UPLOAD_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped upload.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_offchip_drop_swallow_is_load_bearing():
    """NC: byte-revert the "swallow everywhere" drop fix in a COPY of upload.js
    (restore the early ``if (!hit) return;`` bail before preventDefault) → an
    off-chip drop is NOT swallowed, so the browser's native text-drop default
    would run and leak the index into the textarea → the off-chip drop
    assertion flips. Shipped file untouched."""
    with open(_UPLOAD_SRC, encoding='utf-8') as f:
        original = f.read()
    # Anchor on the swallow pair ALONE (preventDefault + stopPropagation):
    # what follows them (ghost hide, from-idx bookkeeping, hit lookup) is
    # layout noise that shifts on unrelated insertions.
    anchor = (
        "  e.preventDefault();\n"
        "  e.stopPropagation();\n")
    assert anchor in original, 'swallow-everywhere drop anchor not found (source changed?)'
    reverted = (
        "  { const _hit = _imgChipFrom(e.target); if (!_hit) return; }  // NC: off-chip bail restored\n"
        "  e.preventDefault();\n"
        "  e.stopPropagation();\n")
    patched = original.replace(anchor, reverted, 1)
    assert patched != original, 'NC patch was a no-op'
    copy_path = os.path.join(HERE, '_img_reorder_drop_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        assert 'FAIL offchip_drop_swallowed' in output, \
            ('NC: without the off-chip swallow, an off-chip drop must NOT be '
             'defaultPrevented (native text-drop leaks the payload):\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_UPLOAD_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped upload.js must be byte-identical'
