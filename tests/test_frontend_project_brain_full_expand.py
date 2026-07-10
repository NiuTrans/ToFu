"""jsdom regression: Project Brain long CONTENT expands to its FULL self.

WHY
Two cross-conversation payloads were rendering as un-expandable clipped
fragments — the "self-containment" data-loss bug:

  • BOARD TITLE: an epic title (stored full, up to 2000 chars) rendered as a
    flat `_esc(t.title)` — a long multi-sentence title was a wall of text with
    no affordance. `_boardCard` now renders it through `_clampBlock(...)`, so a
    long title collapses with a Show more/less toggle whose expandable source is
    the FULL title (never a truncated fragment).

  • ACTIVITY SUMMARY: the feed hard-truncated `summary` to 280 chars AT WRITE
    TIME, so the panel physically could not show the rest. The backend now
    preserves the untruncated text in `payload.summary_full`, and
    `buildActivityRow` renders `_clampBlock(_esc(fullText), fullText)` using
    that full text — so a long summary expands to its full self.

This drives the REAL shipped `renderBoard` + `buildActivityRow` (both exposed
on `window.ProjectBrain`) under jsdom over the real DOM fragment, and asserts:
  1. a long board title is wrapped in a `.pb-clamp` whose text is the FULL title
     and whose toggle expands it (adds `.pb-clamp-open`);
  2. a long activity summary renders the FULL `payload.summary_full` text (the
     panel is NOT limited to the 280-char display `summary`), expandable;
  3. a SHORT title/summary is plain (no needless clamp chrome).

DOUBLE-NEUTER (both in COPIES; shipped file byte-identical after):
  • NC-A: revert `_boardCard` to flat `_esc(t.title)` → the long title is no
    longer inside a `.pb-clamp` → assertion (1) fails.
  • NC-B: make `buildActivityRow` read `ev.summary` only (ignore
    `payload.summary_full`) → the rendered activity text is the 280-char
    fragment, NOT the full text → assertion (2) fails.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
_BRAIN_SRC = os.path.join(JS_DIR, 'project-brain.js')

# A long board title (well over the 240-char clamp threshold) and a long feed
# summary whose FULL text (summary_full) exceeds the 280-char display cap.
_LONG_TITLE = ('Epic B: externalize the PushHub fan-out via a Redis pub/sub '
               'substrate so a frame published on replica B for a client bound '
               'to replica A is delivered — ') * 3
_DISPLAY_SUMMARY = 'Completed: ' + ('X' * 279)          # the capped display value
_FULL_SUMMARY = 'Completed: ' + ('the full untruncated story ' * 30)  # >280


def _node_deps_available():
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_DOM = r'''<!DOCTYPE html><body>
<div class="project-brain-columns">
  <div class="project-brain-col pb-tab-panel pb-tab-panel-active" data-pb-panel="board"><div class="project-brain-col-body" id="projectBrainBoardBody"></div></div>
  <div class="project-brain-col pb-tab-panel" data-pb-panel="activity"><div class="project-brain-col-body"><div id="projectBrainActivityList"></div></div></div>
</div>
</body>'''


def _harness():
    return r'''
const fs = require('fs');
const path = require('path');
const SRC = process.argv[1];
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(DOM_PLACEHOLDER, { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
win.t = global.t = (k, f) => (f || k);
win.Icon = global.Icon = () => '<svg></svg>';
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.Api = global.Api = { project: {} };
win.activeConvId = global.activeConvId = '';

eval(fs.readFileSync(SRC, 'utf8'));
const PB = win.ProjectBrain;
const out = {};
const LONG_TITLE = LONG_TITLE_PH;
const FULL_SUMMARY = FULL_SUMMARY_PH;
const DISPLAY_SUMMARY = DISPLAY_SUMMARY_PH;

// ── Board: one long-title epic + one short-title epic. ──
PB.renderBoard({ tasks: [
  { id: 'pt_long', title: LONG_TITLE, status: 'open', depends_on: [] },
  { id: 'pt_short', title: 'short epic', status: 'open', depends_on: [] },
] });
const boardBody = win.document.getElementById('projectBrainBoardBody');
const longCard = boardBody.querySelector('.pb-board-card[data-task-id="pt_long"]');
const shortCard = boardBody.querySelector('.pb-board-card[data-task-id="pt_short"]');
const longClamp = longCard.querySelector('.pb-clamp');
out.longTitleClamped = !!longClamp;
out.longTitleTextIsFull = longClamp ? (longClamp.textContent.indexOf(LONG_TITLE.trim().slice(-40)) !== -1) : false;
out.shortTitleClamped = !!shortCard.querySelector('.pb-clamp');
// The board's clamp toggle expands.
const boardToggle = longCard.querySelector('.pb-clamp-toggle');
out.boardToggleExists = !!boardToggle;
if (boardToggle) {
  out.boardOpenBefore = longClamp.classList.contains('pb-clamp-open');
  boardToggle.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  out.boardOpenAfter = longClamp.classList.contains('pb-clamp-open');
}

// ── Activity: a long summary whose FULL text lives in payload.summary_full. ──
// Reset dedup state so ingestEvent renders.
PB._state.seen = new Set();
PB._state.maxSeq = 0;
PB.ingestEvent({ event_id: 'e1', seq: 1, kind: 'completed',
  summary: DISPLAY_SUMMARY, payload: { summary_full: FULL_SUMMARY }, ts: Date.now() },
  { backfill: true });
const actList = win.document.getElementById('projectBrainActivityList');
const summaryEl = actList.querySelector('.pb-activity-summary');
out.activityText = summaryEl ? summaryEl.textContent : '';
// The rendered text must contain the TAIL of the FULL summary (not just the
// capped display value) — i.e. the panel shows the full, not the fragment.
out.activityShowsFullTail = summaryEl
  ? (summaryEl.textContent.indexOf(FULL_SUMMARY.trim().slice(-40)) !== -1) : false;
out.activityClamped = !!(summaryEl && summaryEl.querySelector('.pb-clamp'));

// Short summary (no summary_full) → no clamp chrome.
PB.ingestEvent({ event_id: 'e2', seq: 2, kind: 'note',
  summary: 'a short pulse', payload: {}, ts: Date.now() }, { backfill: true });
const rows = actList.querySelectorAll('.pb-activity-row');
let shortRowClamped = null;
for (const r of rows) {
  const s = r.querySelector('.pb-activity-summary');
  if (s && s.textContent.indexOf('a short pulse') !== -1) shortRowClamped = !!s.querySelector('.pb-clamp');
}
out.shortSummaryClamped = shortRowClamped;

console.log('__RESULT__' + JSON.stringify(out));
'''.replace('DOM_PLACEHOLDER', json.dumps(_DOM)) \
   .replace('LONG_TITLE_PH', json.dumps(_LONG_TITLE)) \
   .replace('FULL_SUMMARY_PH', json.dumps(_FULL_SUMMARY)) \
   .replace('DISPLAY_SUMMARY_PH', json.dumps(_DISPLAY_SUMMARY))


def _run(src):
    proc = subprocess.run(
        ['node', '-e', _harness(), src, ROOT],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed: {proc.stderr or proc.stdout}')
    for line in proc.stdout.splitlines():
        if line.startswith('__RESULT__'):
            return json.loads(line[len('__RESULT__'):])
    raise AssertionError(f'no result line: {proc.stdout}\n{proc.stderr}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_long_content_expands_to_full():
    out = _run(_BRAIN_SRC)
    # Board title: long → clamped, full text present, expandable; short → plain.
    assert out['longTitleClamped'] is True, out
    assert out['longTitleTextIsFull'] is True, \
        f'the clamp must carry the FULL board title, not a fragment: {out}'
    assert out['boardToggleExists'] is True, out
    assert out['boardOpenBefore'] is False and out['boardOpenAfter'] is True, \
        f'board title toggle must expand the clamp: {out}'
    assert out['shortTitleClamped'] is False, \
        f'a short title must not get clamp chrome: {out}'
    # Activity summary: renders the FULL payload.summary_full (not the 280 cap).
    assert out['activityShowsFullTail'] is True, \
        f'activity row must show the FULL summary tail, not the clipped one: {out}'
    assert out['activityClamped'] is True, out
    assert out['shortSummaryClamped'] is False, \
        f'a short summary must not get clamp chrome: {out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_A_board_title_flat_render_is_load_bearing(tmp_path):
    """NC-A: revert _boardCard to a flat _esc(t.title) → the long title is no
    longer inside a .pb-clamp. Shipped file byte-identical after."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("    var titleHtml = _clampBlock(_esc(t.title), t.title || '');\n"
              "    return '<div class=\"pb-board-card pb-board-' + _esc(t.status) + '\" data-task-id=\"' +\n"
              "      _esc(t.id) + '\">' +\n"
              "      '<div class=\"pb-board-title\">' + titleHtml + '</div>' +")
    assert anchor in original, 'board title clamp anchor not found'
    patched = original.replace(
        anchor,
        ("    var titleHtml = _esc(t.title);  // NC-A (flat, no clamp)\n"
         "    return '<div class=\"pb-board-card pb-board-' + _esc(t.status) + '\" data-task-id=\"' +\n"
         "      _esc(t.id) + '\">' +\n"
         "      '<div class=\"pb-board-title\">' + titleHtml + '</div>' +"),
        1)
    assert patched != original, 'NC-A patch did not apply'
    src = os.path.join(tmp_path, 'brain-nc-a.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(src)
    assert out['longTitleClamped'] is False, \
        f'NC-A: with flat render the long title must NOT be clamped: {out}'
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_B_summary_ignores_full_is_load_bearing(tmp_path):
    """NC-B: make buildActivityRow use ev.summary ONLY (ignore summary_full) →
    the rendered activity text is the 280-char fragment, not the full text.
    Shipped file byte-identical after."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("    var fullText = (ev.payload && ev.payload.summary_full) "
              "|| ev.summary || kindLabel;")
    assert anchor in original, 'activity summary fullText anchor not found'
    patched = original.replace(
        anchor,
        "    var fullText = ev.summary || kindLabel;  // NC-B (ignore summary_full)",
        1)
    assert patched != original, 'NC-B patch did not apply'
    src = os.path.join(tmp_path, 'brain-nc-b.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(src)
    assert out['activityShowsFullTail'] is False, \
        f'NC-B: ignoring summary_full must drop the full tail: {out}'
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
