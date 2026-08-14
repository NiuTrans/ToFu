"""jsdom guard for the native reader comfort-preferences owner.

The reader toolbar exposes text-size A−/A+ and a reading-width cycle. They drive
two CSS custom properties on the reader containers and persist to localStorage
so the choice survives reload and spans all papers:

  • --reader-font-scale : multiplies the base reading size (steps 0.85 … 1.3)
  • --reader-measure    : font-relative width (Narrow 60ch / Comfortable 68ch / Wide 78ch)

The logic lives in ``features/paper/reader-prefs.ts``:
  _readReaderPrefs / _persistReaderPrefs (localStorage key ``paper_reader_prefs``),
  _applyReaderPrefs (sets both custom props on #paperReportContent +
  #paperReviewContent + syncs the toolbar width label), _readerFontStep(±1),
  _readerWidthCycle().

The harness compiles and loads the TypeScript owner under jsdom, builds the two
reader containers, drives the public handlers, and asserts BOTH observable
effects: the container's inline custom property AND the persisted localStorage
key update.

DB-free; skips when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
PAPER_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'reader-prefs.ts')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));

const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div class="paper-report-content" id="paperReportContent"></div>' +
  '<div class="paper-report-content" id="paperReviewContent"></div>' +
  '<div class="paper-reader-settings">' +
  '  <button class="paper-reader-set-btn paper-reader-set-dec"></button>' +
  '  <button class="paper-reader-set-btn paper-reader-set-inc"></button>' +
  '  <button class="paper-reader-set-btn paper-reader-set-width">' +
  '    <span class="paper-reader-width-label">Comfortable</span></button>' +
  '</div>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document;
global.localStorage = win.localStorage; global.console = console;
win.t = global.t = (k) => k;   // identity i18n

eval(fs.readFileSync(process.argv[2], 'utf8'));  // compiled native preferences owner

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const rep = document.getElementById('paperReportContent');
const rev = document.getElementById('paperReviewContent');
function readPrefs() {
  try { return JSON.parse(localStorage.getItem('paper_reader_prefs') || '{}'); }
  catch (e) { return {}; }
}

check('handlers_exposed',
  typeof window._readerFontStep === 'function' &&
  typeof window._readerWidthCycle === 'function' &&
  typeof window._applyReaderPrefs === 'function');

// ── Baseline apply: defaults land on BOTH containers. ──
window._applyReaderPrefs();
check('default_scale_applied', rep.style.getPropertyValue('--reader-font-scale') === '1');
check('default_measure_applied', rep.style.getPropertyValue('--reader-measure') === '68ch');
check('applies_to_review_too', rev.style.getPropertyValue('--reader-measure') === '68ch');

// ── Font step UP: scale var grows on the container AND persists. ──
const scaleBefore = parseFloat(rep.style.getPropertyValue('--reader-font-scale'));
window._readerFontStep(1);
const scaleAfter = parseFloat(rep.style.getPropertyValue('--reader-font-scale'));
check('font_up_var_increased', scaleAfter > scaleBefore);
check('font_up_persisted', readPrefs().scaleIdx === 3);       // default 2 → 3

// ── Font step DOWN twice: goes below default, persists, and A− disables at min. ──
window._readerFontStep(-1); window._readerFontStep(-1); window._readerFontStep(-1);   // 3→2→1→0 (clamped)
check('font_down_persisted_min', readPrefs().scaleIdx === 0);
const decBtn = document.querySelector('.paper-reader-set-dec');
check('dec_disabled_at_min', decBtn.disabled === true);
// One more down does nothing (clamped).
window._readerFontStep(-1);
check('font_clamped_at_min', readPrefs().scaleIdx === 0);

// The opposite extreme is just as important: A+ must communicate that it is
// clamped instead of looking clickable forever.
for (let i = 0; i < 8; i++) window._readerFontStep(1);
const incBtn = document.querySelector('.paper-reader-set-inc');
check('inc_disabled_at_max', incBtn.disabled === true);

// ── Width cycle: measure stays in the evidence-backed 60–80ch band. ──
// Reset to a known width by reading current, then cycle once.
const wBefore = readPrefs().widthIdx == null ? 1 : readPrefs().widthIdx;
window._readerWidthCycle();
const wAfter = readPrefs().widthIdx;
check('width_cycled', wAfter === (wBefore + 1) % 3);
const measure = rep.style.getPropertyValue('--reader-measure');
check('width_measure_is_preset', measure === '60ch' || measure === '68ch' || measure === '78ch');
check('width_label_synced',
  document.querySelector('.paper-reader-width-label').textContent.indexOf('paper.readerWidth') === 0);

console.log(out.join('\n'));
process.exit(0);
"""


def _run(paper_js: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_reader_prefs_harness.js')
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
                    reason='node + jsdom dev-deps not installed')
def test_vite_reader_prefs_apply_and_persist(tmp_path):
    """The native TS owner must satisfy the same browser behavior contract."""
    esbuild = os.path.join(ROOT, 'node_modules', '.bin', 'esbuild')
    if not os.path.isfile(esbuild):
        pytest.skip('esbuild dev dependency not installed')
    built = tmp_path / 'reader-prefs.js'
    compiled = subprocess.run(
        [esbuild, PAPER_TS, '--bundle', '--format=iife',
         '--platform=browser', f'--outfile={built}'],
        capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stderr

    proc = _run(str(built))
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'Vite reader-prefs failures:\n' + out
    assert out.count('PASS') >= 11, f'expected >=11 PASS, got:\n{out}'
