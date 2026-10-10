"""jsdom guard: glossary hover-card stays INSIDE the reading column.

Symptom (fixed 2026-07-08): on a narrow reader pane (portrait tablet /
single-pane phone) the glossary hover-card (`.paper-term-card`,
`position:absolute; left:0; max-width:320px`) overflowed the RIGHT edge for a
term near the right of the column. With the reader pane now `overflow-x:hidden`
(so it can't be swiped sideways) that overflow is CLIPPED — the definition was
only half-visible and needed a drag to read.

Fix: `_positionGlossaryCard(term)` in `static/js/paper-reader.js` runs on
reveal (mouseover/focusin) and shifts the card left via a negative inline
`left` just enough to sit inside the scroller's content box (8px margin),
clamped so it never overshoots.

Harness loads the REAL shipped paper-reader.js under jsdom, stubs layout
geometry (jsdom does none), and asserts:
  • a RIGHT-edge term → card shifted left (negative inline `left`) so its right
    edge lands within the scroller (minus the 8px margin);
  • a LEFT-edge term → no shift (card already fits, `left` stays default).

Neuter (on a COPY; shipped file byte-identical after): make
`_positionGlossaryCard` a no-op → the right-edge card is NOT pulled in → the
clamp check FAILS, proving it is load-bearing.

DB-free; skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
PAPER_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'report.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# Scroller spans viewport-x [0, 800]. A term is placed at a given left edge; its
# card is 320px wide. jsdom does no layout, so we synthesize
# getBoundingClientRect + offsetWidth. The helper reads card.offsetWidth,
# term/scroller getBoundingClientRect, then sets an inline `left` offset.
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));

const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.localStorage = win.localStorage;
win.matchMedia = global.matchMedia = (q) => ({ matches:false, media:q, addEventListener(){}, removeEventListener(){} });

eval(fs.readFileSync(process.argv[2], 'utf8'));  // real paper-reader.js

const SC_LEFT = 0, SC_RIGHT = 800, CARD_W = 320, MARGIN = 8;

// Build a scroller with one term at viewport-x `termLeft` carrying a 320px card.
function buildTerm(termLeft) {
  const scroller = document.createElement('div');
  scroller.className = 'paper-report-content';
  const article = document.createElement('article');
  article.className = 'paper-report-article';
  const term = document.createElement('span');
  term.className = 'paper-term';
  const card = document.createElement('span');
  card.className = 'paper-term-card';
  term.appendChild(card);
  article.appendChild(term);
  scroller.appendChild(article);
  document.body.appendChild(scroller);

  scroller.getBoundingClientRect = () => ({ top:0, left:SC_LEFT, bottom:600, right:SC_RIGHT, width:SC_RIGHT-SC_LEFT, height:600 });
  term.getBoundingClientRect = () => ({ top:0, left:termLeft, bottom:20, right:termLeft+40, width:40, height:20 });
  Object.defineProperty(card, 'offsetWidth', { value: CARD_W, configurable: true });
  return { scroller, term, card };
}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

check('fn_exposed', typeof _positionGlossaryCard === 'function');

// ── RIGHT-edge term: left=700 → card [700,1020] would spill past 800. Must be
//    pulled in so card right edge <= 800-8=792 → left offset negative. ──
{
  const { term, card } = buildTerm(700);
  _positionGlossaryCard(term);
  const off = parseFloat(card.style.left || '0');   // inline offset vs term left
  const cardLeft = 700 + off;              // card viewport-left after shift
  const cardRight = cardLeft + CARD_W;
  check('right_edge_card_shifted_left', off < 0);
  check('right_edge_card_within_scroller', cardRight <= SC_RIGHT - MARGIN + 0.5);
  check('right_edge_card_not_past_left', cardLeft >= SC_LEFT + MARGIN - 0.5);
}

// ── LEFT-edge term: left=20 → card [20,340] fits → no shift (left stays ''). ──
{
  const { term, card } = buildTerm(20);
  _positionGlossaryCard(term);
  check('left_edge_card_no_shift', !card.style.left || card.style.left === '0px');
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(paper_js: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_glossary_clamp_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(['node', harness, paper_js, ROOT],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_glossary_card_clamped_into_column():
    proc = _run(PAPER_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'glossary-clamp failures:\n' + out
    assert out.count('PASS') >= 5, f'expected >=5 PASS, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_glossary_clamp_is_load_bearing():
    src = open(PAPER_JS, encoding='utf-8').read()
    marker = 'function _positionGlossaryCard(term) {\n  if (!term) return;'
    assert marker in src, 'NC marker not found — test is stale'
    poisoned = src.replace(
        marker,
        'function _positionGlossaryCard(term) {\n  return;\n  if (!term) return;', 1)
    tmp = os.path.join(HERE, '_paper_glossary_clamp_neuter.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(poisoned)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True, text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        assert 'FAIL right_edge_card_shifted_left' in out, \
            'no-op _positionGlossaryCard did NOT break the clamp — non-load-bearing:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    assert open(PAPER_JS, encoding='utf-8').read() == src, 'shipped file was modified!'


if __name__ == '__main__':
    test_glossary_card_clamped_into_column()
    print('positive: PASS')
    test_nc_glossary_clamp_is_load_bearing()
    print('neuter: PASS')
    print('ALL PASSED')
