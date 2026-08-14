"""tests/test_frontend_image_optimistic_preview.py — the image attach UX fix.

Two mobile-reported problems, one root cause: images were pushed to
``pendingImages`` ONLY AFTER ``compressImage`` + upload finished. So (1) the
preview chip appeared late (slow decode of a big phone photo), and (2) tapping
send while a 2nd/3rd image was still compressing captured a ``pendingImages``
snapshot that missed it → the image was silently dropped.

The fix (static/js/upload.js): ``_handleImageDrop`` pushes an entry with an
object-URL preview IMMEDIATELY and marks it ``_status:'processing'``, then
``_processPendingImage`` fills in base64/mediaType/sizeKB in the background and
clears ``_status``. ``renderImagePreviews`` paints a darkening overlay while
processing. The send path (main_send_pipeline.js) awaits ``_waitForImageProcessing``
before snapshotting and drops any entry lacking base64/url.

This suite drives the REAL shipped ``static/js/upload.js`` through jsdom:
  • an attach shows a chip INSTANTLY with ``_status:'processing'`` + overlay,
    BEFORE the (stubbed slow) compress resolves;
  • after compress+upload resolve, the entry has base64 + url and NO
    ``_status``/``_objectUrl``, and the overlay is gone;
  • a NEUTER control removing the optimistic push proves the instant chip is
    load-bearing (no chip before compress resolves).

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_UPLOAD_SRC = runtime_section_path('upload.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const NEUTER = process.argv[4] === 'neuter';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div class="image-previews" id="imagePreviews"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

// ── Minimal globals upload.js reaches for ──
win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.apiUrl = global.apiUrl = (u) => '/base' + u;
win.debugLog = global.debugLog = () => {};
win.t = global.t = (k) => (k === 'upload.processing' ? '处理中…' : k);
win.config = global.config = {};
win._editingMsgIdx = global._editingMsgIdx = null;
win.pendingImages = global.pendingImages = [];
// URL.createObjectURL / revokeObjectURL don't exist in jsdom → stub.
let _revoked = [];
win.URL.createObjectURL = global.URL.createObjectURL = () => 'blob:mock-object-url';
win.URL.revokeObjectURL = global.URL.revokeObjectURL = (u) => { _revoked.push(u); };

let src = fs.readFileSync(SRC, 'utf8');
if (NEUTER) {
  // Remove the optimistic push so the chip only appears AFTER compress.
  src = src.replace(
    '  pendingImages.push(imgObj);\n  renderImagePreviews();\n  if (typeof _igUpdateGenButton === \'function\') _igUpdateGenButton();\n  await _processPendingImage(f, imgObj);',
    '  await _processPendingImage(f, imgObj);\n  pendingImages.push(imgObj);\n  renderImagePreviews();');
}
eval(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Stub compressImage: resolve on demand so we can inspect the in-flight
//    state BEFORE it completes (mimics a slow mobile decode). ──
let _resolveCompress;
compressImage = win.compressImage = function () {
  return new Promise((r) => { _resolveCompress = r; });
};
// Stub the upload → sets url like the real one.
uploadImageToServer = win.uploadImageToServer = async function (imgObj) {
  imgObj.url = win.apiUrl('/api/images/abc.png');
};

const fakeFile = { type: 'image/png', name: 'photo.png' };

(async () => {
  // Fire the attach but DON'T await yet — inspect the instant state.
  const p = _handleImageDrop(fakeFile);
  await Promise.resolve();  // let synchronous body run up to the first await

  const tray = win.document.getElementById('imagePreviews');
  const chipNow = tray.querySelectorAll('.img-preview').length;
  const first = win.pendingImages[0];

  if (NEUTER) {
    // With the optimistic push removed, no chip should exist yet.
    check('neuter_no_instant_chip', chipNow === 0);
  } else {
    check('instant_chip_rendered', chipNow === 1);
    check('instant_status_processing', !!first && first._status === 'processing');
    check('instant_has_object_url_preview', !!first && first.preview === 'blob:mock-object-url');
    check('instant_overlay_painted',
          tray.querySelector('.img-processing-overlay') !== null);
    check('instant_no_base64_yet', !!first && !first.base64);
  }

  // Now complete compression + let the background finish.
  _resolveCompress({ base64: 'ZZZ', mediaType: 'image/png', preview: 'data:image/png;base64,ZZZ', sizeKB: 42 });
  await p;

  const done = win.pendingImages[0];
  check('final_has_base64', !!done && done.base64 === 'ZZZ');
  check('final_has_url', !!done && done.url === '/base/api/images/abc.png');
  check('final_status_cleared', !!done && done._status === undefined);
  check('final_object_url_cleared', !!done && done._objectUrl === undefined);
  check('final_preview_is_data_url', !!done && done.preview === 'data:image/png;base64,ZZZ');
  check('final_no_overlay',
        win.document.getElementById('imagePreviews').querySelector('.img-processing-overlay') === null);

  console.log(out.join('\n'));
})();
"""


def _run(neuter=False):
    harness = os.path.join(HERE, '_img_optimistic_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        args = ['node', harness, _UPLOAD_SRC, ROOT]
        if neuter:
            args.append('neuter')
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
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
def test_optimistic_preview_lifecycle():
    """The chip appears instantly with a processing overlay, then reconciles to
    a fully-formed entry (base64 + url, no transient fields) once compress +
    upload finish."""
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'optimistic-preview failures:\n' + output
    for marker in ('PASS instant_chip_rendered', 'PASS instant_status_processing',
                   'PASS instant_has_object_url_preview', 'PASS instant_overlay_painted',
                   'PASS instant_no_base64_yet', 'PASS final_has_base64',
                   'PASS final_has_url', 'PASS final_status_cleared',
                   'PASS final_object_url_cleared', 'PASS final_preview_is_data_url',
                   'PASS final_no_overlay'):
        assert marker in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_optimistic_push_is_load_bearing():
    """NC: with the optimistic push removed (chip pushed only AFTER compress),
    no chip exists while compression is still in flight — proving the instant
    push is what makes the preview appear promptly."""
    output = _run(neuter=True)
    assert 'PASS neuter_no_instant_chip' in output, output
