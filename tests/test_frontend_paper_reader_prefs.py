"""jsdom guard: Reader COMFORT PREFERENCES (text-size + reading-width).

The reader toolbar exposes text-size A−/A+ and a reading-width cycle. They drive
two CSS custom properties on the reader containers and persist to localStorage
so the choice survives reload and spans all papers:

  • --reader-font-scale : multiplies the base reading size (steps 0.85 … 1.3)
  • --reader-measure    : text-column width (Narrow 640 / Comfortable 720 / Wide 860)

The logic lives in ``static/js/paper-reader.js``:
  _readReaderPrefs / _persistReaderPrefs (localStorage key ``paper_reader_prefs``),
  _applyReaderPrefs (sets both custom props on #paperReportContent +
  #paperReviewContent + syncs the toolbar width label), _readerFontStep(±1),
  _readerWidthCycle().

The harness loads the REAL shipped paper-reader.js under jsdom, builds the two
reader containers, drives the public handlers, and asserts BOTH observable
effects: the container's inline custom property AND the persisted localStorage
key update. Extraction-and-eval (no build step); neuter discipline on a COPY.

Neuters (each on a COPY; shipped file byte-identical after):
  • NC-apply: make _applyReaderPrefs a no-op → the container custom property
    never changes → the "var updated" check FAILS (persist still works, proving
    APPLY is the load-bearing half for the DOM effect).
  • NC-persist: make _persistReaderPrefs a no-op → after a step the localStorage
    key never advances → the "persisted" check FAILS (proving PERSIST is the
    load-bearing half for durability).

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
PAPER_JS = os.path.join(ROOT, 'static', 'js', 'paper-reader.js')


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
  '  <button class="paper-reader-set-btn"></button>' +
  '  <button class="paper-reader-set-btn paper-reader-set-width">' +
  '    <span class="paper-reader-width-label">Comfortable</span></button>' +
  '</div>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document;
global.localStorage = win.localStorage; global.console = console;
win.t = global.t = (k) => k;   // identity i18n

eval(fs.readFileSync(process.argv[2], 'utf8'));  // real paper-reader.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const rep = document.getElementById('paperReportContent');
const rev = document.getElementById('paperReviewContent');
function readPrefs() {
  try { return JSON.parse(localStorage.getItem('paper_reader_prefs') || '{}'); }
  catch (e) { return {}; }
}

check('handlers_exposed',
  typeof _readerFontStep === 'function' &&
  typeof _readerWidthCycle === 'function' &&
  typeof _applyReaderPrefs === 'function');

// ── Baseline apply: defaults land on BOTH containers. ──
_applyReaderPrefs();
check('default_scale_applied', rep.style.getPropertyValue('--reader-font-scale') === '1');
check('default_measure_applied', rep.style.getPropertyValue('--reader-measure') === '720px');
check('applies_to_review_too', rev.style.getPropertyValue('--reader-measure') === '720px');

// ── Font step UP: scale var grows on the container AND persists. ──
const scaleBefore = parseFloat(rep.style.getPropertyValue('--reader-font-scale'));
_readerFontStep(1);
const scaleAfter = parseFloat(rep.style.getPropertyValue('--reader-font-scale'));
check('font_up_var_increased', scaleAfter > scaleBefore);
check('font_up_persisted', readPrefs().scaleIdx === 3);       // default 2 → 3

// ── Font step DOWN twice: goes below default, persists, and A− disables at min. ──
_readerFontStep(-1); _readerFontStep(-1); _readerFontStep(-1);   // 3→2→1→0 (clamped)
check('font_down_persisted_min', readPrefs().scaleIdx === 0);
const decBtn = document.querySelector('.paper-reader-set-dec');
check('dec_disabled_at_min', decBtn.disabled === true);
// One more down does nothing (clamped).
_readerFontStep(-1);
check('font_clamped_at_min', readPrefs().scaleIdx === 0);

// ── Width cycle: measure var advances Comfortable(720)→Wide(860) and label syncs. ──
// Reset to a known width by reading current, then cycle once.
const wBefore = readPrefs().widthIdx == null ? 1 : readPrefs().widthIdx;
_readerWidthCycle();
const wAfter = readPrefs().widthIdx;
check('width_cycled', wAfter === (wBefore + 1) % 3);
const measure = rep.style.getPropertyValue('--reader-measure');
check('width_measure_is_preset', measure === '640px' || measure === '720px' || measure === '860px');
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
def test_reader_prefs_apply_and_persist():
    proc = _run(PAPER_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'reader-prefs failures:\n' + out
    assert out.count('PASS') >= 11, f'expected >=11 PASS, got:\n{out}'


def _run_neuter(patched_src: str, tag: str) -> str:
    tmp = os.path.join(HERE, f'_paper_reader_prefs_neuter_{tag}.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(patched_src)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True, text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid ({tag}): {chk.stderr}'
        proc = _run(tmp)
        assert proc.returncode == 0, f'node crashed ({tag}): {proc.stderr}\n{proc.stdout}'
        return proc.stdout.strip()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed')
def test_neuter_apply_and_persist_are_load_bearing():
    src = open(PAPER_JS, encoding='utf-8').read()

    # ── NC-apply: _applyReaderPrefs no-ops → container var never updates. ──
    m1 = 'function _applyReaderPrefs(prefs) {\n  prefs = prefs || _readReaderPrefs();'
    assert m1 in src, 'NC-apply marker not found — test stale'
    out1 = _run_neuter(
        src.replace(m1, 'function _applyReaderPrefs(prefs) {\n  return prefs || _readReaderPrefs();\n  prefs = prefs || _readReaderPrefs();', 1),
        'apply')
    assert 'FAIL default_scale_applied' in out1 or 'FAIL font_up_var_increased' in out1, \
        'NC-apply: no-op apply did NOT break the DOM var effect:\n' + out1

    # ── NC-persist: _persistReaderPrefs no-ops → localStorage never advances. ──
    m2 = 'function _persistReaderPrefs(prefs) {\n  try {'
    assert m2 in src, 'NC-persist marker not found — test stale'
    out2 = _run_neuter(
        src.replace(m2, 'function _persistReaderPrefs(prefs) {\n  return;\n  try {', 1),
        'persist')
    assert 'FAIL font_up_persisted' in out2, \
        'NC-persist: no-op persist did NOT break durability:\n' + out2

    assert open(PAPER_JS, encoding='utf-8').read() == src, 'shipped file was modified!'


if __name__ == '__main__':
    test_reader_prefs_apply_and_persist()
    print('positive: PASS')
    test_neuter_apply_and_persist_are_load_bearing()
    print('neuter: PASS')
    print('ALL PASSED')
