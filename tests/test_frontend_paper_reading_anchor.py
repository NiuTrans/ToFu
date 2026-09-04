"""jsdom guard: Reading-Mode scroll-position PRESERVATION across a re-render.

Requirement: toggling the report language (EN/中) fully rebuilds the report DOM
via ``_renderFinalReport`` (``container.innerHTML=''``). Before this fix that
snapped the reader back to the TOP — a reading-flow break mid-paper. The fix
adds two pure helpers in the typed
``frontend/src/features/paper/report-runtime.ts`` owner:

  • ``_captureReadingAnchor(scroller)`` → {index, offset} : the index of the
    heading nearest the top of the viewport + that heading's pixel offset below
    the scroller top (heading ORDER is stable across languages, unlike raw
    scrollTop after a re-layout). Returns null on a fresh (unscrolled) render so
    a first paint is never perturbed.
  • ``_restoreReadingAnchor(scroller, article, anchor)`` : re-applies that anchor
    onto the rebuilt article so the reader lands where their eye was.

The harness compiles the REAL typed report runtime under jsdom, builds a
scroller with several headings, STUBS getBoundingClientRect + scroll geometry
(jsdom does no layout), captures an anchor at a mid-report heading, rebuilds the
article, and asserts the restored scrollTop lands on that heading (not 0).

Double-neuter (each on a COPY; shipped file byte-identical after):
  • NC-1: make _captureReadingAnchor always return null → restore is a no-op →
    the reader stays at top → the preservation check FAILS.
  • NC-2: make _restoreReadingAnchor a no-op → same observable failure, proving
    the RESTORE half (not just capture) is load-bearing.

DB-free; skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._paper_vite import compiled_typescript
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
REPORT_RUNTIME_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'report-runtime.ts')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# The harness models a scroller of 2000px content in a 600px viewport with four
# headings at fixed positions. jsdom does no layout, so we drive
# getBoundingClientRect from a synthetic geometry keyed off scrollTop:
#   heading i sits at absolute Y = 100 + i*400 within the content.
#   getBoundingClientRect().top (viewport-relative) = absoluteY - scrollTop.
# The scroller's own rect top is 0. This is exactly the arithmetic the helpers
# use, so a correct capture/restore round-trips to the same heading.
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));

const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document;
global.localStorage = win.localStorage; global.console = console;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { fn(); return 0; };
global.setTimeout = win.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
win.matchMedia = global.matchMedia = (q) => ({ matches:false, media:q, addEventListener(){}, removeEventListener(){} });

eval(fs.readFileSync(process.argv[2], 'utf8'));  // compiled typed report owner
const _captureReadingAnchor = win._captureReadingAnchor;
const _restoreReadingAnchor = win._restoreReadingAnchor;

const CONTENT_H = 2000, VIEW_H = 600, H0 = 100, GAP = 400;

// Build a scroller with an article + N headings. We assign each heading an
// _absY (its absolute position within the content) and synthesize layout.
function buildScroller(nHeads) {
  const scroller = document.createElement('div');
  scroller.className = 'paper-report-content';
  const article = document.createElement('article');
  article.className = 'paper-report-article';
  for (let i = 0; i < nHeads; i++) {
    const h = document.createElement(i % 2 === 0 ? 'h2' : 'h3');
    h.id = 'h' + i; h.textContent = 'Heading ' + i;
    h._absY = H0 + i * GAP;
    article.appendChild(h);
  }
  scroller.appendChild(article);
  // Scroll geometry (jsdom returns 0 for these; define our own).
  Object.defineProperty(scroller, 'scrollHeight', { value: CONTENT_H, configurable: true });
  Object.defineProperty(scroller, 'clientHeight', { value: VIEW_H, configurable: true });
  let _top = 0;
  Object.defineProperty(scroller, 'scrollTop', {
    get() { return _top; },
    set(v) { _top = Math.max(0, Math.min(v, CONTENT_H - VIEW_H)); },
    configurable: true,
  });
  scroller.getBoundingClientRect = () => ({ top: 0, left: 0, bottom: VIEW_H, right: 0, width: 0, height: VIEW_H });
  // Each heading's viewport-relative top = absY - scroller.scrollTop.
  article.querySelectorAll('h2,h3').forEach((h) => {
    h.getBoundingClientRect = () => ({ top: h._absY - scroller.scrollTop, left:0, bottom:0, right:0, width:0, height:20 });
  });
  return { scroller, article };
}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

check('capture_fn_exposed', typeof _captureReadingAnchor === 'function');
check('restore_fn_exposed', typeof _restoreReadingAnchor === 'function');

// ── Fresh (unscrolled) render → capture returns null (first paint untouched). ──
{
  const { scroller } = buildScroller(4);
  scroller.scrollTop = 0;
  const a = _captureReadingAnchor(scroller);
  check('fresh_render_anchor_null', a === null);
}

// ── Scrolled INTO heading #2 (absY=900): scroll so h2 sits ~10px ABOVE the top
//    edge (you're reading section 2). Then you're "in" heading #2 with a small
//    NEGATIVE offset (the last heading at/above the top). ──
{
  const { scroller } = buildScroller(4);
  scroller.scrollTop = 900 + 10;   // heading #2 top is at -10 in viewport
  const anchor = _captureReadingAnchor(scroller);
  check('anchor_captured_index2', anchor && anchor.index === 2);
  check('anchor_offset_near_-10', anchor && Math.abs(anchor.offset - (-10)) < 2);

  // Rebuild a fresh article (simulating the language repaint) and restore.
  const rebuilt = buildScroller(4);
  rebuilt.scroller.scrollTop = 0;   // repaint starts at top (the bug)
  _restoreReadingAnchor(rebuilt.scroller, rebuilt.article, anchor);
  // Expected: land so heading #2 (absY=900) sits at offset -10 → scrollTop ~910.
  check('restore_returns_to_heading2', Math.abs(rebuilt.scroller.scrollTop - 910) < 3);
  check('restore_not_top', rebuilt.scroller.scrollTop > 100);
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(runtime_contents: str | None = None) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_reading_anchor_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        with compiled_typescript(
            REPORT_RUNTIME_TS,
            contents=runtime_contents,
            expose_feature_registry_to_window=True,
        ) as runtime_js:
            return subprocess.run(
                ['node', harness, runtime_js, ROOT],
                capture_output=True, text=True, timeout=60,
            )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_reading_anchor_preserves_position_across_rerender():
    proc = _run()
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'reading-anchor failures:\n' + out
    assert out.count('PASS') >= 6, f'expected >=6 PASS, got:\n{out}'


def _run_neuter(patched_src: str, tag: str) -> str:
    proc = _run(runtime_contents=patched_src)
    assert proc.returncode == 0, (
        f'node crashed (typed runtime {tag}): {proc.stderr}\n{proc.stdout}'
    )
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_double_neuter_reading_anchor_is_load_bearing():
    src = open(REPORT_RUNTIME_TS, encoding='utf-8').read()

    # ── NC-1: capture always returns null → restore no-ops → stays at top. ──
    m1 = (
        'export function captureReadingAnchor('
        'scroller: HTMLElement | null): ReadingAnchor | null {\n'
        '  try {'
    )
    assert m1 in src, 'NC-1 marker not found — test is stale'
    out1 = _run_neuter(
        src.replace(m1, m1.replace('  try {', '  return null;\n  try {'), 1),
        'nc1')
    assert 'FAIL restore_returns_to_heading2' in out1 or 'FAIL anchor_captured_index2' in out1, \
        'NC-1: nulling capture did NOT break preservation — non-load-bearing:\n' + out1

    # ── NC-2: restore is a no-op → captured anchor never applied → stays at top. ──
    m2 = (
        'export function restoreReadingAnchor(\n'
        '  scroller: HTMLElement | null,\n'
        '  article: HTMLElement | null,\n'
        '  anchorValue: unknown,\n'
        '): void {\n'
        '  const anchor = normalizeReadingAnchor(anchorValue);'
    )
    assert m2 in src, 'NC-2 marker not found — test is stale'
    out2 = _run_neuter(
        src.replace(m2, m2.replace(
            '): void {\n', '): void {\n  return;\n', 1), 1),
        'nc2')
    assert 'FAIL restore_returns_to_heading2' in out2 and 'FAIL restore_not_top' in out2, \
        'NC-2: no-op restore did NOT break preservation — non-load-bearing:\n' + out2

    assert open(REPORT_RUNTIME_TS, encoding='utf-8').read() == src, (
        'typed report runtime was modified!'
    )


if __name__ == '__main__':
    test_reading_anchor_preserves_position_across_rerender()
    print('positive: PASS')
    test_double_neuter_reading_anchor_is_load_bearing()
    print('double-neuter: PASS')
    print('ALL PASSED')
