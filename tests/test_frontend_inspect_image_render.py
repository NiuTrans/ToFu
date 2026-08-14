"""tests/test_frontend_inspect_image_render.py — inspect_image tool-panel render.

Two bugs are pinned here (both surfaced by "can't enlarge the thumbnail /
'cropped' isn't translated"):

1. ENLARGE (bundling regression). The image thumbnails rendered by
   ui/tool_rounds.js (read_files / inspect_image / browser_screenshot) call
   ``_openImageFullscreen(this.src)`` via inline onclick. That helper used to
   live in image-gen.js, which was MOVED to the DEFERRED feature bundle
   (loaded only on first entry into Image-Gen mode). So the onclick pointed at
   an undefined function until Image-Gen mode was opened — clicking "enlarge"
   silently did nothing. The fix moves ``_openImageFullscreen`` /
   ``_downloadGenImage`` into the CORE module ui/image_fullscreen.js. This test
   asserts the helper is defined by that core file and that the thumbnail
   onclick wires to it.

2. i18n. The inspect_image ops chip rendered the backend's English op string
   ("cropped, zoom 2×") with a hardcoded English ``title="Applied transform"``.
   ``_localizeInspectOps`` now translates both the chip body and its tooltip at
   render time. This test drives it through the REAL _renderUnifiedToolLine with
   a zh ``t`` stub and asserts the translated tokens appear.

3. PREVIEW (missing whitelist entry). browser_preview_page (server-side rendered
   page screenshot) attaches meta.imageDataUris exactly like browser_screenshot,
   but the inline-image whitelist in _renderReadImagesBlock omitted it — the
   round degraded to the generic badge-only line with no clickable thumbnail.
   Pinned by driving a browser_preview_page round through the same renderer.

Loads the REAL shipped ui/tool_rounds.js + ui/image_fullscreen.js under jsdom.
Double-neuters prove each fix is load-bearing. Skips cleanly without node/jsdom.
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
_TR_SRC = os.path.join(JS_DIR, 'ui', 'tool_rounds.js')
_IF_SRC = os.path.join(JS_DIR, 'ui', 'image_fullscreen.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# A minimal zh i18n stub covering ONLY the inspect.* keys under test; any other
# key falls back to the provided default (mirrors t()'s real fallback).
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const TR = process.argv[2];
const IF = process.argv[4];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const ZH = {
  'inspect.opsTitle': '已应用的图像变换',
  'inspect.cropped': '已裁剪',
  'inspect.zoom': '放大 {factor}',
  'inspect.opsSep': '、',
  'inspect.fullFrame': '完整画面',
};
global.t = (k, d) => (ZH[k] || d || k);
global.renderMarkdown = (s) => 'MD-DUMP:' + String(s);
global.Icon = (n) => '<svg data-icon="' + n + '"></svg>';
global._shortUrl = (u) => u;
global.formatNumber = (n) => String(n);

eval(fs.readFileSync(IF, 'utf8'));   // ui/image_fullscreen.js — defines the helper
eval(fs.readFileSync(TR, 'utf8'));   // ui/tool_rounds.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// The shared helper must be defined by the CORE image_fullscreen.js.
check('helper_defined', typeof window._openImageFullscreen === 'function' ||
                        typeof _openImageFullscreen === 'function');
check('download_helper_defined', typeof window._downloadGenImage === 'function' ||
                                 typeof _downloadGenImage === 'function');

// inspect_image round with a data-URI thumbnail + a "cropped, zoom 2×" op.
const round = {
  status: 'done', toolName: 'inspect_image', query: 'inspect_image',
  toolContent: 'Inspected view.', toolRounds: [],
  results: [{
    source: 'Project', badge: 'cropped, zoom 2×', inspectOps: 'cropped, zoom 2×',
    imageDataUris: [{ uri: 'data:image/png;base64,AAAA', format: 'png', filename: 'diagram.png' }],
  }],
};
const html = _renderUnifiedToolLine(round, false);

// (1) enlarge wiring: the thumbnail onclick calls _openImageFullscreen.
check('onclick_wires_fullscreen', html.includes('_openImageFullscreen(this.src)'));
check('thumb_rendered', html.includes('data:image/png;base64,AAAA'));

// (2) i18n: chip body + tooltip are translated (zh), NOT raw English.
check('chip_localized_cropped', html.includes('已裁剪'));
check('chip_localized_zoom', html.includes('放大 2×'));
check('chip_title_localized', html.includes('已应用的图像变换'));
check('chip_no_hardcoded_title', !html.includes('title="Applied transform"'));

// (3) browser_preview_page — server-side rendered page screenshot. The backend
// attaches imageDataUris exactly like browser_screenshot; the inline-image
// whitelist must include it or the round degrades to the badge-only generic
// line (no clickable thumbnail).
const previewRound = {
  status: 'done', toolName: 'browser_preview_page',
  query: 'Render page preview: debug/_preview.html',
  toolContent: 'Page preview rendered (jpeg)', toolRounds: [],
  results: [{
    source: 'Browser', badge: 'captured',
    imageDataUris: [{ uri: 'data:image/jpeg;base64,BBBB', format: 'jpeg', filename: 'screenshot.jpeg' }],
  }],
};
const previewHtml = _renderUnifiedToolLine(previewRound, false);
check('preview_thumb_rendered', previewHtml.includes('data:image/jpeg;base64,BBBB'));
check('preview_onclick_fullscreen', previewHtml.includes('_openImageFullscreen(this.src)'));
check('preview_badge_captured', previewHtml.includes('captured'));

console.log(out.join('\n'));
// jsdom keeps the event loop alive (its fake timers/raF handles are never
// released) — without an explicit exit node hangs past the subprocess
// timeout even when every check has already printed.
process.exit(0);
"""


def _run(tr_path, if_path):
    harness = os.path.join(HERE, '_inspect_render_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, tr_path, ROOT, if_path],
            capture_output=True, text=True, timeout=240)   # jsdom require alone is ~80s on FUSE
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
def test_inspect_image_render_and_localize():
    output = _run(_TR_SRC, _IF_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'inspect_image render failures:\n' + output
    for must in (
        'PASS helper_defined', 'PASS download_helper_defined',
        'PASS onclick_wires_fullscreen', 'PASS thumb_rendered',
        'PASS chip_localized_cropped', 'PASS chip_localized_zoom',
        'PASS chip_title_localized', 'PASS chip_no_hardcoded_title',
        'PASS preview_thumb_rendered', 'PASS preview_onclick_fullscreen',
        'PASS preview_badge_captured',
    ):
        assert must in output, output


def _nc(src_path, anchor, replacement, must_fail, must_still_pass):
    """Patch a COPY of src_path, run, assert target checks flip to FAIL while a
    control stays PASS, then assert the shipped file is byte-identical."""
    with open(src_path, encoding='utf-8') as f:
        original = f.read()
    assert anchor in original, f'NC anchor not found: {anchor[:70]!r}'
    patched = original.replace(anchor, replacement, 1)
    assert patched != original, 'NC replacement was a no-op'
    copy_path = src_path + '.nc_copy.js'
    tr = copy_path if src_path == _TR_SRC else _TR_SRC
    ifile = copy_path if src_path == _IF_SRC else _IF_SRC
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(tr, ifile)
        for m in must_fail:
            assert ('FAIL ' + m) in output, \
                f'NC: expected {m} to FAIL:\n{output}'
        for m in must_still_pass:
            assert ('PASS ' + m) in output, \
                f'NC must be surgical — {m} should still PASS:\n{output}'
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(src_path, encoding='utf-8') as f:
        assert f.read() == original, f'shipped {os.path.basename(src_path)} must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_localize_ops_is_load_bearing():
    """Revert the chip to the raw English inspectOps + hardcoded title → the zh
    tokens disappear and the hardcoded English title reappears, while the
    enlarge wiring (separate concern) still works."""
    _nc(
        _TR_SRC,
        anchor='? `<span class="ptool-badge rf-inspect-chip" title="${escapeHtml(_localizeInspectOps(t, meta.inspectOps, "title"))}">${escapeHtml(_localizeInspectOps(t, meta.inspectOps))}</span>`',
        replacement='? `<span class="ptool-badge rf-inspect-chip" title="Applied transform">${escapeHtml(meta.inspectOps)}</span>`',
        must_fail=['chip_localized_cropped', 'chip_localized_zoom',
                   'chip_title_localized', 'chip_no_hardcoded_title'],
        must_still_pass=['onclick_wires_fullscreen', 'helper_defined'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_preview_whitelist_is_load_bearing():
    """Drop browser_preview_page from the inline-image whitelist → the preview
    round degrades to the badge-only generic line (the reported bug), while the
    inspect_image entry (separate concern) still renders its thumbnail."""
    _nc(
        _TR_SRC,
        anchor='round.toolName === "browser_screenshot" || round.toolName === "browser_preview_page") &&',
        replacement='round.toolName === "browser_screenshot") &&',
        must_fail=['preview_thumb_rendered', 'preview_onclick_fullscreen'],
        must_still_pass=['thumb_rendered', 'chip_localized_cropped'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_fullscreen_helper_is_load_bearing():
    """Neuter the helper definition in image_fullscreen.js → _openImageFullscreen
    is no longer defined, while the localized chip (separate concern) still
    renders. Proves the core helper is what makes 'enlarge' work."""
    _nc(
        _IF_SRC,
        anchor='function _openImageFullscreen(src) {',
        replacement='function _openImageFullscreen_DISABLED(src) {',
        must_fail=['helper_defined'],
        must_still_pass=['chip_localized_cropped', 'onclick_wires_fullscreen'],
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
