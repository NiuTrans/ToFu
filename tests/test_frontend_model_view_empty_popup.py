"""tests/test_frontend_model_view_empty_popup.py — the "模型原文 popup shows
only a single bar" fix, driven end-to-end under jsdom against the REAL shipped
`openTextPreview` (upload_preview.js) + `_tcModelViewBtnForText` (tool_rounds.js).

Root cause: an inject row (autopilot / sub-agent / peer / steer — the COMMON
case in this project, which carries no `toolContent`) resolves its verbatim
text via `_injectVerbatimText(previews)`, which returns "" when previews are
empty. That "" was stored verbatim in the model-text registry and handed to
`openTextPreview`, whose `<pre>` then rendered empty → the flex panel collapsed
to just its header, i.e. the "single bar" popup the owner saw.

Row-agnostic two-layer fix (per owner directive):
  L1 (`openTextPreview`)  — empty/whitespace body → render a localized
                            `tool.noContent` placeholder, so the modal can
                            NEVER collapse to a header-only bar regardless of
                            which caller passed empty text.
  L2 (`_tcModelViewBtnForText`) — never park an empty registry entry; fall back
                            to the same `tool.noContent` sentinel at the source.

Asserts:
  • L1: openTextPreview('', '', '') yields a NON-empty `.preview-text-body`
        containing the placeholder text (the panel is not a bare header).
  • L2: a button built from empty text registers a NON-empty entry.
Two NEUTERs (revert each layer) must make the respective assertion FAIL, so
neither guard is vacuous.

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


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const MODE = process.argv[3] || '';   // '', 'NC_L1', 'NC_L2'
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div class="modal-overlay" id="previewModal"></div>' +
  '<div class="preview-body" id="previewBody"></div>' +
  '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
if (typeof win.CSS === 'undefined') win.CSS = { escape: (s) => s };
global.CSS = win.CSS;
win.localStorage.setItem('tofu_segment_timeline', '0');

global.escapeHtml = win.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
// t: return the localized default when given, else echo the key.
global.t = win.t = (k, d) => (typeof d === 'string' ? d : k);
global.renderMarkdown = win.renderMarkdown = (s) => String(s == null ? '' : s);
global.Icon = win.Icon = (n) => '<svg data-icon="' + n + '"></svg>';

eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'tool_rounds.js'), 'utf8'));
eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'upload_preview.js'), 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof openTextPreview !== 'function') { console.log('FAIL fn_exposed openTextPreview missing'); process.exit(0); }
if (typeof _tcModelViewBtnForText !== 'function') { console.log('FAIL fn_exposed _tcModelViewBtnForText missing'); process.exit(0); }
check('fn_exposed', true);

// ── Layer 1: openTextPreview with EMPTY text must NOT collapse to a header. ──
// NC_L1 reproduces the pre-fix behaviour: escape the raw (empty) text directly.
function openTextPreview_prefix(title, meta, text) {
  document.getElementById('previewBody').innerHTML =
    '<div class="preview-text-panel"><div class="preview-text-header">' +
    '<span class="preview-text-title">' + escapeHtml(title) + '</span>' +
    '<span class="preview-text-meta">' + escapeHtml(meta) + '</span></div>' +
    '<pre class="preview-text-body">' + escapeHtml(text) + '</pre></div>';
  document.getElementById('previewModal').classList.add('open');
}
(MODE === 'NC_L1' ? openTextPreview_prefix : openTextPreview)('T', 'meta', '');
const bodyEl = document.querySelector('.preview-text-body');
check('l1_body_present', !!bodyEl);
// THE decisive L1 assertion: the <pre> is non-empty (placeholder visible), so
// the panel is not a bare single-bar header.
const bodyText = bodyEl ? bodyEl.textContent.trim() : '';
check('l1_body_nonempty', bodyText.length > 0);

// ── Layer 2: a button built from empty text must register NON-empty text. ──
const reg = win._tcModelTextRegistry;
let btnHtml;
if (MODE === 'NC_L2') {
  // Pre-fix source behaviour: store the raw empty string verbatim.
  const id = 'tcmt_nc';
  reg.set(id, { text: String('' == null ? '' : ''), title: 'x' });
  btnHtml = 'data-tc-preview-text="' + id + '"';
} else {
  btnHtml = _tcModelViewBtnForText({ roundNum: 9 }, '');
}
// Pull the id the button references and inspect the registry entry.
const m = btnHtml.match(/data-tc-preview-text="([^"]+)"/);
check('l2_btn_has_id', !!m);
const entry = m ? reg.get(m[1]) : null;
check('l2_entry_present', !!entry);
// THE decisive L2 assertion: registered text is non-empty (sentinel, not "").
check('l2_entry_nonempty', !!(entry && String(entry.text).trim().length > 0));

console.log(out.join('\n'));
process.exit(0);
"""


def _run(mode: str = '') -> str:
    harness = os.path.join(HERE, '_model_view_empty_popup_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        argv = ['node', harness, ROOT]
        if mode:
            argv.append(mode)
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=45)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


def _status(output: str, name: str):
    for ln in output.splitlines():
        if ln.endswith(' ' + name):
            return ln.split(' ', 1)[0]
    return None


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_empty_model_view_renders_placeholder_not_single_bar():
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'empty-popup fix failures:\n' + output
    assert _status(output, 'l1_body_nonempty') == 'PASS', output
    assert _status(output, 'l2_entry_nonempty') == 'PASS', output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_layer1_empty_body_collapses():
    """NEUTER L1: pre-fix openTextPreview escapes the empty text directly →
    the <pre> is empty (the single-bar popup). Proves L1 is load-bearing."""
    output = _run('NC_L1')
    assert _status(output, 'l1_body_nonempty') == 'FAIL', \
        'NC_L1 should leave an empty popup body:\n' + output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_layer2_empty_registry_entry():
    """NEUTER L2: pre-fix source stored the raw "" in the registry. Proves the
    source-layer sentinel is load-bearing."""
    output = _run('NC_L2')
    assert _status(output, 'l2_entry_nonempty') == 'FAIL', \
        'NC_L2 should register an empty entry:\n' + output


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
