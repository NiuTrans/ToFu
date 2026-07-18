"""tests/test_frontend_dup_model_view_e2e.py — one model-view button, end-to-end.

Companion to test_frontend_dup_model_view_btn.py (which is a SOURCE guard on the
append branch only). This is the DOM-level end-to-end proof the owner asked for:
it drives the REAL shipped `_syncToolRoundsDOM` (streaming_ui.js) +
`_renderUnifiedToolLine`/`_rowModelViewBtn`/`_tcPreviewBtn`/`_tcModelViewBtnForText`
(tool_rounds.js) under jsdom across the exact two-frame sequence that produced
the duplicate "模型原文" (model view) button:

  Frame 1 — a tool PRE-EXECUTED during streaming arrives settled (status='done')
            but with EMPTY toolContent and only a placeholder meta
            (`{"snippet":"Pre-executed during streaming"}`). The slot is built
            via the generic done line → `_rowModelViewBtn` falls back to the
            synthesized `[data-tc-preview-text]` placeholder button.
  Frame 2 — the REAL verbatim toolContent lands (status unchanged). The
            status-mismatch re-render bails, so the flow falls through to the
            `toolContent && !slot.querySelector('[data-tc-preview]')` APPEND
            branch — which historically added the real `[data-tc-preview]`
            button NEXT TO the stale placeholder → two buttons.

Asserts, after both frames, that the row carries EXACTLY ONE `.tc-preview-btn`,
and it is the real `[data-tc-preview]` (verbatim toolContent) — never the
`[data-tc-preview-text]` placeholder.

This DOM assertion — not the source regex guard — is what closes the gap: a
future refactor that switches the append into a full-slot rebuild, or reorders
these branches, is still pinned to "one real model-view entry per row".

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
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[3] === 'NC';   // negative-control: revert the fix
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="host"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
if (typeof win.CSS === 'undefined') win.CSS = { escape: (s) => s };
global.CSS = win.CSS;

// Force the segment-timeline OFF so _renderStreamRoundProse (and the swarm
// panel path) are never entered — keeps the harness free of streaming_render /
// swarm-panel deps. The grouped/unified tool line is what carries the button.
win.localStorage.setItem('tofu_segment_timeline', '0');

global.escapeHtml = win.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
// t: return the localized default when given, else echo the key. The 2nd arg
// may be an interpolation object (header labels) — irrelevant to assertions.
global.t = win.t = (k, d) => (typeof d === 'string' ? d : k);
global.renderMarkdown = win.renderMarkdown = (s) => String(s == null ? '' : s);
global.Icon = win.Icon = (n) => '<svg data-icon="' + n + '"></svg>';
global._shortUrl = win._shortUrl = (u) => u;
global.formatNumber = win.formatNumber = (n) => String(n);

eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'tool_rounds.js'), 'utf8'));
eval(fs.readFileSync(path.join(ROOT, 'static', 'js', 'ui', 'streaming_ui.js'), 'utf8'));

// ── NC MODE: monkeypatch the fix out of the append branch. We can't easily
//    byte-revert an eval'd source, so we re-define _syncToolRoundsDOM's effect
//    by disabling the removal: shim Element.prototype.querySelector on the
//    ptool-line so the stale-fallback lookup returns null (pre-fix behavior).
//    Simpler + faithful: shadow the removal by wrapping insertAdjacentHTML is
//    fragile — instead we assert against the SHIPPED function and, for NC, run
//    a hand-rolled pre-fix append that omits the removal.
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _syncToolRoundsDOM !== 'function') { console.log('FAIL fn_exposed _syncToolRoundsDOM missing'); process.exit(0); }
if (typeof _renderUnifiedToolLine !== 'function') { console.log('FAIL fn_exposed _renderUnifiedToolLine missing'); process.exit(0); }
check('fn_exposed', true);

const host = document.getElementById('host');

// A tool PRE-EXECUTED during streaming: settled (done) but toolContent empty,
// carrying only the placeholder meta snippet.
function frame1() {
  return [{
    roundNum: 4, llmRound: 3, toolName: 'grep_search', status: 'done',
    query: 'grep /def check_for_update/ in lib/self_update.py',
    toolContent: '',
    results: [{ toolName: 'grep_search', snippet: 'Pre-executed during streaming' }],
  }];
}
// Same round, now with the REAL verbatim bytes sent to the model.
function frame2() {
  return [{
    roundNum: 4, llmRound: 3, toolName: 'grep_search', status: 'done',
    query: 'grep /def check_for_update/ in lib/self_update.py',
    toolContent: 'No matches found for: def check_for_update|def apply_update\nHint: pattern looks like complex regex. Try a simpler literal substring instead.',
    results: [{ toolName: 'grep_search', snippet: 'Pre-executed during streaming' }],
  }];
}

// ── Frame 1 ──
_syncToolRoundsDOM(host, frame1());
const slot1 = host.querySelector('[data-prn="4"]');
check('frame1_slot_created', !!slot1);
// After frame 1 the ONLY model-view entry is the synthesized placeholder.
const btns1 = slot1 ? slot1.querySelectorAll('.tc-preview-btn') : [];
check('frame1_single_placeholder', btns1.length === 1 &&
      btns1[0].hasAttribute('data-tc-preview-text') &&
      !btns1[0].hasAttribute('data-tc-preview'));

// ── Frame 2 ── (real toolContent lands; fingerprint changes → re-sync runs)
if (NC) {
  // PRE-FIX append: fall through to the same branch but WITHOUT removing the
  // stale placeholder (this is exactly the code before the fix). We reproduce
  // it by hand on the live slot so the NC proves the DOM assertion bites.
  const round = frame2()[0];
  host._roundsFingerprint = null;   // force the sync to re-run
  // Emulate the old branch: it only checked [data-tc-preview]; the placeholder
  // has [data-tc-preview-text], so the guard passes and it appends the real
  // button WITHOUT removing the placeholder.
  const ptLine = slot1.querySelector('.ptool-line');
  if (ptLine && round.toolContent && !slot1.querySelector('[data-tc-preview]')) {
    ptLine.insertAdjacentHTML('beforeend', _tcPreviewBtn(round));
  }
} else {
  _syncToolRoundsDOM(host, frame2());
}

const slot2 = host.querySelector('[data-prn="4"]');
const btns2 = slot2 ? slot2.querySelectorAll('.tc-preview-btn') : [];
// THE decisive assertion: exactly ONE model-view button on the row.
check('frame2_single_button', btns2.length === 1);
// …and it is the REAL verbatim one, not the placeholder.
check('frame2_button_is_real', btns2.length === 1 &&
      btns2[0].hasAttribute('data-tc-preview') &&
      !btns2[0].hasAttribute('data-tc-preview-text'));
// The stale placeholder is gone.
check('frame2_placeholder_removed', !slot2 || !slot2.querySelector('[data-tc-preview-text]'));

console.log(out.join('\n'));
process.exit(0);   // jsdom / stream tickers keep the event loop alive otherwise
"""


def _run(nc: bool) -> str:
    harness = os.path.join(HERE, '_dup_model_view_e2e_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        argv = ['node', harness, ROOT]
        if nc:
            argv.append('NC')
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=45)
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
def test_pre_executed_tool_ends_with_one_real_model_view_button():
    output = _run(nc=False)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'dup-model-view e2e failures:\n' + output
    # fn_exposed + frame1(2) + frame2(3) = 6
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_prefix_append_yields_two_buttons():
    """Negative control: with the PRE-FIX append (no stale-placeholder removal),
    the row ends up with TWO model-view buttons — proving the DOM assertion in
    the real test is load-bearing, not vacuously true."""
    output = _run(nc=True)
    lines = output.splitlines()

    def _status(name):
        for ln in lines:
            if ln.endswith(' ' + name):
                return ln.split(' ', 1)[0]
        return None

    assert _status('frame2_single_button') == 'FAIL', \
        'NC (pre-fix append) should leave TWO buttons:\n' + output
    assert _status('frame2_placeholder_removed') == 'FAIL', \
        'NC should leave the stale placeholder in place:\n' + output
    # The frame-1 setup still holds (harness is sound).
    assert _status('frame1_single_placeholder') == 'PASS', \
        'NC harness precondition broke:\n' + output


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
