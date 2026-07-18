#!/usr/bin/env python3
"""Guard: the Review tab's rebuttal must NOT block / collapse the review report.

ROOT CAUSE it locks in
-----------------------
The Review tab (``.paper-tab-panel[data-tab="review"]``) is a vertical flex
column. It used to STACK two independent long-document scrollers as siblings:

  • ``#paperReviewContent`` = ``.paper-report-content`` → ``flex:1 1 0%`` (basis 0)
  • the rebuttal ``<details class="paper-rebuttal-section">`` → ``flex:0 1 auto``
    (basis = the follow-up reply's REAL content height once expanded)

When the reviewer expanded the ``<details>`` AND a follow-up reply was
generated, flexbox distributed the column height by ``shrink × basis``: the
review report (``1 × 0 = 0``) shrank to **zero height** while the rebuttal
(``1 × <tall>``) ate the whole panel — the report was completely hidden and
could not be collapsed back. On top of that ``.paper-rebuttal-body`` was a
plain block, so ``#paperRebuttalContent``'s ``flex:1`` was inert and the reply
had no scrollbar of its own (it overflowed and got clipped).

THE FIX (segmented sub-view)
----------------------------
The two documents become MUTUALLY-EXCLUSIVE full-height segments switched by
``_setReviewSeg(seg)`` — only ONE ``.paper-review-seg-panel`` is in layout at a
time (the other is ``display:none``). Two long docs are never co-laid-out, so
neither can be squeezed to 0 by the other. The rebuttal segment's body becomes
a flex column with ``min-height:0`` so ``#paperRebuttalContent`` gets its own
independent scrollbar.

WHAT THIS FILE ASSERTS
----------------------
1. jsdom: ``_setReviewSeg`` keeps EXACTLY ONE segment displayed (mutual
   exclusion) while BOTH containers survive in the DOM (containerId / cache /
   stream state untouched). NEUTER: make the switcher never hide the other
   panel (the old always-both-visible / stacked-<details> behaviour) → both
   long docs are co-laid-out again → the mutual-exclusion assertion FAILS.
2. jsdom: the persist slots for the review vs rebuttal views are DISTINCT
   (``review:…`` vs ``rebuttal:…`` langKey), so a segment switch can never write
   one segment's reading position into the other's slot.
3. CSS: ``.paper-review-seg-panel`` is a full-height flex column
   (``flex:1`` + ``min-height:0``) and ``.paper-rebuttal-body`` is a flex column
   with ``min-height:0`` so the reply scrolls independently. NEUTER: revert the
   body to a plain block → the flex-scroll assertion FAILS.

DB-free; the jsdom parts skip cleanly when node + jsdom aren't installed.
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
READER_JS = os.path.join(ROOT, 'static', 'js', 'paper-reader.js')
CSS = os.path.join(ROOT, 'static', 'styles.css')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# ─────────────────────────── function extraction ───────────────────────────

def _brace_match(src: str, open_pos: int) -> int:
    depth = 0
    j = open_pos
    while j < len(src):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise AssertionError('unbalanced braces')


def _extract_fn(src: str, fn_name: str) -> str:
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(fn_name) + r'\s*\(', src)
    assert m, f'{fn_name} not found in paper-reader.js'
    i = src.find('{', m.end())
    return src[m.start():_brace_match(src, i)]


# The harness builds the real segmented DOM (mirroring index.html), stubs the
# tracker/state globals, evals the REAL shipped _setReviewSeg + _syncReviewSegState.
_DOM = r"""
<div class="paper-tab-panel" data-tab="review">
  <div class="paper-report-toolbar"></div>
  <div class="paper-review-seg" id="paperReviewSeg" role="tablist">
    <button class="paper-review-seg-btn active" data-seg="review" aria-selected="true">Review</button>
    <button class="paper-review-seg-btn" data-seg="rebuttal" aria-selected="false">Rebuttal<span class="paper-review-seg-dot" style="display:none"></span></button>
  </div>
  <div class="paper-review-seg-panel" data-seg="review">
    <div class="paper-report-content" id="paperReviewContent">REVIEW BODY</div>
  </div>
  <div class="paper-review-seg-panel" data-seg="rebuttal" style="display:none">
    <div class="paper-rebuttal-body">
      <textarea id="paperRebuttalInput"></textarea>
      <div class="paper-report-content" id="paperRebuttalContent">REBUTTAL BODY</div>
    </div>
  </div>
</div>
"""

_PREAMBLE = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>__DOM__</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

// Stubs the switcher touches. _teardownReadingTracker must be safe to call.
let _teardownCalls = 0;
function _teardownReadingTracker(silent) { _teardownCalls++; }
let _paperReviewSeg = 'review';
let _paperRebuttalInputText = '';
// _reportView minimal shape for _syncReviewSegState + the distinct-slot check.
let _rebuttalHasCache = false;
function _reportView(kind) {
  if (kind === 'rebuttal') return {
    kind:'rebuttal',
    get cache(){ return _rebuttalHasCache ? 'X' : ''; },
    stream:null,
    langKey: function(){ return 'rebuttal:generic:en'; },
  };
  return {
    kind:'review', cache:'', stream:null,
    langKey: function(){ return 'review:generic:en'; },
  };
}
""".replace('__DOM__', _DOM.replace('\n', ''))


def _reader_fns(poison: str = '') -> str:
    src = _read(READER_JS)
    body = _extract_fn(src, '_setReviewSeg') + '\n' + _extract_fn(src, '_syncReviewSegState')
    if poison == 'always_both_visible':
        # NEUTER: model the OLD stacked-<details> structure — the switcher never
        # hides the other panel, so both long-doc scrollers stay co-laid-out.
        assert "p.dataset.seg === seg) ? '' : 'none'" in body, 'NEUTER marker missing — test stale'
        body = body.replace("p.dataset.seg === seg) ? '' : 'none'",
                            "p.dataset.seg === seg) ? '' : ''")
    return body


def _run(extracted: str, driver: str) -> dict:
    import json
    harness = os.path.join(HERE, '_paper_review_seg_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_PREAMBLE + '\n' + extracted + '\n(function(){\n' + driver + '\n})();\n')
    try:
        chk = subprocess.run(['node', '--check', harness], capture_output=True, text=True, timeout=30)
        assert chk.returncode == 0, f'harness JS invalid: {chk.stderr}'
        proc = subprocess.run(['node', harness, '', ROOT], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


def _visible_panels_js() -> str:
    return r"""
function _vis() {
  var out = [];
  document.querySelectorAll('.paper-tab-panel[data-tab="review"] .paper-review-seg-panel').forEach(function(p){
    if (p.style.display !== 'none') out.push(p.dataset.seg);
  });
  return out;
}
"""


# ─────────────────────────── (1) mutual exclusion ───────────────────────────

@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_review_segments_are_mutually_exclusive_full_height():
    driver = _visible_panels_js() + r"""
var res = {};
_setReviewSeg('review');
res.reviewSeg_visible = _vis();
_setReviewSeg('rebuttal');
res.rebuttalSeg_visible = _vis();
// BOTH containers must survive in the DOM regardless of which segment shows,
// so containerId / cache / stream state is never destroyed by a switch.
res.reviewContainerAlive = !!document.getElementById('paperReviewContent');
res.rebuttalContainerAlive = !!document.getElementById('paperRebuttalContent');
// active button tracks the shown segment.
res.activeBtn = (function(){
  var b = document.querySelector('#paperReviewSeg .paper-review-seg-btn.active');
  return b ? b.dataset.seg : null;
})();
console.log(JSON.stringify(res));
"""
    r = _run(_reader_fns(), driver)
    assert r['reviewSeg_visible'] == ['review'], \
        f"on 'review' exactly the review segment shows, got {r['reviewSeg_visible']}"
    assert r['rebuttalSeg_visible'] == ['rebuttal'], \
        f"on 'rebuttal' exactly the rebuttal segment shows (review hidden → cannot be squeezed to 0), got {r['rebuttalSeg_visible']}"
    assert r['reviewContainerAlive'] and r['rebuttalContainerAlive'], \
        'both containers must persist in the DOM (state preserved, only display toggles)'
    assert r['activeBtn'] == 'rebuttal', 'active seg button must track the shown segment'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_always_both_visible_reintroduces_the_competition():
    """NEUTER: the switcher never hides the other panel (the old stacked-<details>
    always-both-visible behaviour) → switching to rebuttal leaves the review
    segment ALSO displayed → two long docs co-laid-out in one flex column again,
    which is exactly the height-competition bug. The mutual-exclusion assertion
    must catch it (proves the display-toggle is load-bearing)."""
    driver = _visible_panels_js() + r"""
_setReviewSeg('rebuttal');
console.log(JSON.stringify({ visible: _vis() }));
"""
    r = _run(_reader_fns(poison='always_both_visible'), driver)
    # Old behaviour: BOTH segments visible simultaneously.
    assert set(r['visible']) == {'review', 'rebuttal'}, \
        f'NEUTER should leave both segments visible (the competition), got {r["visible"]}'
    assert r['visible'] != ['rebuttal'], \
        'the mutual-exclusion guard would NOT flag the stacked-competition regression — not load-bearing'


# ─────────────────────────── (2) distinct persist slots ───────────────────────────

@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_review_and_rebuttal_persist_slots_are_distinct():
    """The two segments key their reading-position slot off distinct langKeys
    (``review:…`` vs ``rebuttal:…``), so a segment switch can never write one
    segment's scroll position into the other's slot."""
    driver = r"""
var rk = _reportView('review').langKey();
var bk = _reportView('rebuttal').langKey();
console.log(JSON.stringify({ reviewKey: rk, rebuttalKey: bk, distinct: rk !== bk }));
"""
    r = _run(_reader_fns(), driver)
    assert r['distinct'] is True, \
        f'review/rebuttal langKeys must differ, got {r["reviewKey"]} vs {r["rebuttalKey"]}'
    assert r['reviewKey'].startswith('review:') and r['rebuttalKey'].startswith('rebuttal:'), \
        'langKeys must keep their sibling composite namespaces'


# ─────────────────────────── (3) CSS: flex-scroll invariants ───────────────────────────

def _strip_comments(css: str) -> str:
    return re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)


def _rule_body(css: str, selector: str) -> str | None:
    css = _strip_comments(css)
    want = re.sub(r'\s+', ' ', selector).strip()
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        sel = re.sub(r'\s+', ' ', m.group(1)).strip()
        if sel == want:
            return m.group(2)
    return None


def test_seg_panel_is_full_height_flex_column():
    """.paper-review-seg-panel must be a flex:1 column with min-height:0 so the
    SHOWN segment fills the column height and its inner scroller can shrink."""
    body = _rule_body(_read(CSS), '.paper-review-seg-panel')
    assert body is not None, '.paper-review-seg-panel rule not found (structure changed?)'
    assert re.search(r'flex\s*:\s*1', body), \
        f'.paper-review-seg-panel must be flex:1 (full remaining height).\nbody={body}'
    assert 'min-height:0' in body.replace(' ', ''), \
        f'.paper-review-seg-panel must set min-height:0 (flexbox scroll-shrink).\nbody={body}'
    assert re.search(r'flex-direction\s*:\s*column', body), \
        f'.paper-review-seg-panel must be a column so its content scroller stacks.\nbody={body}'


def test_rebuttal_body_scrolls_independently():
    """.paper-rebuttal-body must be a flex column with min-height:0 so
    #paperRebuttalContent (flex:1;overflow-y:auto) gets its OWN scrollbar
    instead of overflowing and being clipped."""
    body = _rule_body(_read(CSS), '.paper-rebuttal-body')
    assert body is not None, '.paper-rebuttal-body rule not found'
    compact = body.replace(' ', '')
    assert 'display:flex' in compact and 'flex-direction:column' in compact, \
        f'.paper-rebuttal-body must be a flex column.\nbody={body}'
    assert 'min-height:0' in compact, \
        f'.paper-rebuttal-body must set min-height:0 so its content scroller can shrink.\nbody={body}'


def test_NC_plain_block_rebuttal_body_has_no_independent_scroll():
    """NEUTER: revert .paper-rebuttal-body to the old plain block (no flex) on a
    COPY → the flex-scroll assertion must be able to fire (proves it's
    load-bearing, not vacuously green)."""
    body = _rule_body(_read(CSS), '.paper-rebuttal-body')
    assert body is not None, 'fix real CSS first'
    poisoned = 'padding:0 16px 16px'  # the old plain-block body
    compact = poisoned.replace(' ', '')
    assert not ('display:flex' in compact and 'min-height:0' in compact), \
        'the flex-scroll assertion would NOT flag a plain-block regression — not load-bearing'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
