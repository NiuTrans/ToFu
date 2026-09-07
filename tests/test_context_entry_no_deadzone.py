"""Context-compaction entry point must be reachable at EVERY viewport.

ROOT CAUSE this guards (2026-07-14): the desktop context sphere
(`.ctx-health-bar`) is the click target that opens the manual-compaction
popover, but it is `display:none` on narrow viewports. Its ONLY mobile
replacement is the "Context" section inside the "···" bottom sheet
(`#mobileContextSection` → `#mobileCompactNow`). The sheet is opened solely by
`.mobile-more-btn`, which is revealed in TWO media blocks:

  * `@media(max-width:768px)`                                  (phone)
  * `@media(min-width:769px) and (max-width:1024px) and (pointer:coarse)` (tablet)

The sphere USED to hide under a blanket `@media(max-width:900px)`. That created
a DEAD ZONE: **769–900px with a FINE pointer** (a narrowed desktop window / a
small fine-pointer monitor) — the sphere was hidden there, but the "···" button
is NOT revealed (it needs ≤768px OR pointer:coarse), so that band had ZERO
reachable "compact now" entry. The fix realigns the sphere's hide rule to
mirror the mobile-more-btn reveal conditions exactly, so the sphere stays the
entry on fine-pointer narrow windows and hands off to the sheet only where the
sheet is actually reachable.

Two guards:
  1. CSS invariant (no node/jsdom needed): for every (width, pointer) sample in
     0..1200px × {fine,coarse}, at least ONE compaction entry is reachable —
     the sphere (shown when NOT hidden) OR the sheet (reachable only when
     .mobile-more-btn is revealed). Asserts the historical dead-zone sample
     (800px, fine) is now covered.
  2. jsdom render/wire proof: the `#mobileContextSection` markup renders inside
     `#mobileSheet`, `updateMobileContext()` reflects live usage + the busy
     guard, and `_mobileCompactNow()` delegates to `window.runManualCompaction`.
     A NEUTER (drop the runManualCompaction delegation) proves the wiring
     assertion is load-bearing.

DB-free; the jsdom part skips when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')
RUNTIME_JS = os.path.join(ROOT, 'frontend', 'src', 'runtime', 'app-runtime.js')
INDEX_HTML = os.path.join(ROOT, 'index.html')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# ═══════════════════════════════════════════════════════════════════════
# Part 1 — CSS breakpoint dead-zone guard (pure Python, always runs)
# ═══════════════════════════════════════════════════════════════════════

def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _runtime_section(name: str) -> str:
    source = _read(RUNTIME_JS)
    marker = f'/* ===== migrated source: {name} ===== */'
    start = source.index(marker)
    next_marker = source.find('/* ===== migrated source:', start + len(marker))
    return source[start:next_marker if next_marker >= 0 else len(source)]


def _iter_media_blocks(css: str):
    """Yield (condition_text, body_text) for every top-level @media block."""
    i = 0
    n = len(css)
    while True:
        m = css.find('@media', i)
        if m < 0:
            return
        brace = css.find('{', m)
        if brace < 0:
            return
        cond = css[m + len('@media'):brace].strip()
        # Walk to the matching close brace (media blocks can nest one level).
        depth = 0
        j = brace
        while j < n:
            c = css[j]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        yield cond, css[brace + 1:j]
        i = j + 1


def _matches(cond: str, width: int, coarse: bool) -> bool:
    """Evaluate a (simplified) @media condition for a (width, pointer) sample.

    Supports the comma-separated OR of AND-clauses actually used in this file:
    max-width / min-width / pointer:coarse / pointer:fine / orientation /
    max-height. Orientation/height features we can't sample here are treated as
    satisfiable (we only care that width+pointer gating lines up)."""
    for clause in cond.split(','):
        clause = clause.strip()
        if not clause:
            continue
        ok = True
        for feat in re.findall(r'\(([^)]*)\)', clause):
            feat = feat.replace(' ', '')
            if feat.startswith('max-width:'):
                px = int(re.sub(r'\D', '', feat))
                ok = ok and (width <= px)
            elif feat.startswith('min-width:'):
                px = int(re.sub(r'\D', '', feat))
                ok = ok and (width >= px)
            elif feat == 'pointer:coarse':
                ok = ok and coarse
            elif feat == 'pointer:fine':
                ok = ok and (not coarse)
            # orientation / max-height / min-height: not sampled → assume OK
        if ok:
            return True
    return False


def _sphere_hidden(css: str, width: int, coarse: bool) -> bool:
    """True if a media block that matches this sample hides .ctx-health-bar."""
    for cond, body in _iter_media_blocks(css):
        if '.ctx-health-bar' in body and 'display:none' in body.replace(' ', ''):
            if _matches(cond, width, coarse):
                return True
    return False


def _sheet_reachable(css: str, width: int, coarse: bool) -> bool:
    """True if a matching media block reveals .mobile-more-btn (display:flex)."""
    for cond, body in _iter_media_blocks(css):
        # The reveal rule sets .mobile-more-btn{display:flex...}. The base
        # top-level rule (outside any @media) is display:none, so only a
        # media-block reveal counts.
        if re.search(r'\.mobile-more-btn\s*\{[^}]*display:\s*flex', body):
            if _matches(cond, width, coarse):
                return True
    return False


def test_no_compaction_entry_deadzone():
    """At every (width, pointer) sample, at least ONE entry is reachable."""
    css = _read(CSS)
    holes = []
    for width in range(320, 1201, 10):
        for coarse in (False, True):
            sphere = not _sphere_hidden(css, width, coarse)
            sheet = _sheet_reachable(css, width, coarse)
            if not (sphere or sheet):
                holes.append((width, 'coarse' if coarse else 'fine'))
    assert not holes, (
        'compaction-entry DEAD ZONE — no reachable entry at these '
        '(width, pointer) samples:\n' + '\n'.join(f'  {w}px {p}' for w, p in holes))


def test_historical_deadzone_sample_is_covered():
    """The specific bug: 800px fine-pointer had no entry. It must now."""
    css = _read(CSS)
    # sphere shown OR sheet reachable
    covered = (not _sphere_hidden(css, 800, coarse=False)) or _sheet_reachable(css, 800, coarse=False)
    assert covered, (
        '769–900px fine-pointer dead zone regressed: at 800px/fine the context '
        'sphere is hidden AND the "···" sheet is not reachable → no "compact '
        'now" entry. Realign the .ctx-health-bar hide rule to the '
        '.mobile-more-btn reveal breakpoints (styles.css ~20417).')


def test_sphere_hidden_where_sheet_takes_over():
    """Sanity: on a phone (≤768) and a coarse tablet the sheet IS the entry
    and the sphere is correctly hidden (we didn't just delete the hide rule)."""
    css = _read(CSS)
    assert _sphere_hidden(css, 400, coarse=True), 'sphere should be hidden on phones'
    assert _sheet_reachable(css, 400, coarse=True), 'sheet should be reachable on phones'
    assert _sphere_hidden(css, 1000, coarse=True), 'sphere should be hidden on coarse tablets'
    assert _sheet_reachable(css, 1000, coarse=True), 'sheet should be reachable on coarse tablets'


def test_index_html_has_context_section():
    """The sheet markup the CSS/JS guard relies on must exist in index.html."""
    html = _read(INDEX_HTML)
    assert 'id="mobileContextSection"' in html
    assert 'id="mobileCompactNow"' in html
    assert '_mobileCompactNow()' in html
    assert 'id="mobileCompactHistory"' in html


# ═══════════════════════════════════════════════════════════════════════
# Part 2 — jsdom render/wire proof (skips without node + jsdom)
# ═══════════════════════════════════════════════════════════════════════

# Minimal #mobileSheet Context section markup, mirroring index.html. The test
# asserts the SHIPPED index.html carries the same ids (test_index_html_* above),
# so this inline copy is only the render substrate for the wiring proof.
_SHEET_HTML = (
    '<div class="mobile-bottom-sheet" id="mobileSheet">'
    '  <div class="mobile-sheet-section" id="mobileContextSection">'
    '    <div class="mobile-sheet-item" id="mobileCompactNow" onclick="_mobileCompactNow()">'
    '      <span class="mobile-sheet-item-desc" id="mobileCompactDesc">d</span>'
    '    </div>'
    '    <div class="mobile-sheet-item" id="mobileCompactHistory">h</div>'
    '  </div>'
    '</div>'
    '<div class="mobile-bottom-sheet-backdrop" id="mobileSheetBackdrop"></div>'
)

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div class="chat-wrapper"></div>SHEET_HTML</body>',
                      { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.runtimeScope = win.runtimeScope = win;
global.localStorage = win.localStorage;
global.console = console;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => setTimeout(fn, 0);

// i18n stub that interpolates {pct} so the usage label test is meaningful.
win.t = global.t = (k, vars) => {
  if (k === 'mobile.compactUsage' && vars) return String(vars.pct) + '% used';
  if (k === 'compactNow.busy') return 'busy';
  if (k === 'mobile.compactDesc') return 'compact';
  return k;
};

// ── Fixtures the two files read at runtime ──
let ACTIVE = 'conv-1';
let CONV = { id: 'conv-1', model: 'm' };
let TURN_PROJECTIONS = [];
let COMPACTION_HISTORY = [];
let TURN_BUSY = false;
global.activeConvId = win.activeConvId = ACTIVE;
global.getConvById = win.getConvById = (id) => (id === CONV.id ? CONV : null);
global.getActiveConv = win.getActiveConv = () => CONV;
global.activeStreams = win.activeStreams = new Map();
global.convIsBusy = win.convIsBusy = () => TURN_BUSY;
global.getCompactionHistory = win.getCompactionHistory = () => COMPACTION_HISTORY;
global.ConversationTurnRead = win.ConversationTurnRead = {
  ordered() {
    return TURN_PROJECTIONS.map((projection, index) => ({
      turnId: 'turn-' + index,
      actor: projection.role === 'user' ? 'human' : projection.role,
      projection,
      updatedAt: projection.timestamp || 0,
    }));
  },
  state() {
    const liveRoundUsageByTurn = {};
    TURN_PROJECTIONS.forEach((projection, index) => {
      if (projection._liveLastRoundUsage) {
        liveRoundUsageByTurn['turn-' + index] = projection._liveLastRoundUsage;
      }
    });
    return { liveRoundUsageByTurn };
  },
};
win._contextPolicy = {
  default_limit: 200000,
  per_model: { m: { window: 200000, source: 'test', exact: true } },
  output_reserve: 0, compaction_reserve: 0,
  summary_trigger_ratio: 0.9, min_usable_ratio: 0.5
};

// main_folders_mobile.js runs load-time IIFEs (initMobileLayout, resize/gesture
// wiring) that reference viewport helpers defined in sibling files. Stub the
// few they touch at import time so the eval doesn't crash — the functions under
// test (updateMobileContext / _mobileCompactNow) don't use them.
global.isDrawerViewport = win.isDrawerViewport = () => false;
global.isMobileViewport = win.isMobileViewport = () => false;
global.isTabletDrawerViewport = win.isTabletDrawerViewport = () => false;
global._scheduleReflow = win._scheduleReflow = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // context-bar.js (exports contextUsageSummary + runManualCompaction)
eval(fs.readFileSync(process.argv[4], 'utf8'));  // main_folders_mobile.js (updateMobileContext + _mobileCompactNow)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  // The Context section markup exists inside the sheet.
  const sec = document.getElementById('mobileContextSection');
  check('section_in_sheet', !!sec && sec.closest('#mobileSheet') !== null);
  check('summary_exposed', typeof window.contextUsageSummary === 'function');
  check('update_fn_exposed', typeof window.updateMobileContext === 'function');
  check('compact_fn_exposed', typeof window._mobileCompactNow === 'function');

  // ── (a) usage % surfaces in the desc when there IS usage ──
  TURN_PROJECTIONS = [{ role: 'assistant', _liveLastRoundUsage: { tokensIn: 100000 } }];
  const expectedPct = window.contextUsageSummary().pct;
  updateMobileContext();
  const desc = document.getElementById('mobileCompactDesc');
  check('desc_shows_pct', expectedPct != null &&
        desc.textContent.includes(String(expectedPct) + '% used'));
  check('not_disabled_when_idle',
        !document.getElementById('mobileCompactNow').classList.contains('disabled'));

  // ── (b) busy guard: a live task disables "compact now" ──
  TURN_BUSY = true;
  updateMobileContext();
  check('disabled_when_busy',
        document.getElementById('mobileCompactNow').classList.contains('disabled'));
  check('desc_shows_busy', desc.textContent === 'busy');
  TURN_BUSY = false;

  // ── (c) history item hidden without snapshots, shown with them ──
  TURN_PROJECTIONS = [{ role: 'assistant', _liveLastRoundUsage: { tokensIn: 100000 } }];
  COMPACTION_HISTORY = [];
  updateMobileContext();
  check('history_hidden_no_snapshots',
        document.getElementById('mobileCompactHistory').style.display === 'none');
  COMPACTION_HISTORY = [{ archiveId: 7 }];
  updateMobileContext();
  check('history_shown_with_snapshots',
        document.getElementById('mobileCompactHistory').style.display !== 'none');

  // ── (d) _mobileCompactNow delegates to runManualCompaction with the conv id ──
  let delegated = null;
  window.runManualCompaction = (cid) => { delegated = cid; };
  _mobileCompactNow();
  check('compact_delegates', delegated === 'conv-1');

  // ── (e) disabled item does NOT delegate ──
  document.getElementById('mobileCompactNow').classList.add('disabled');
  delegated = null;
  _mobileCompactNow();
  check('disabled_blocks_delegate', delegated === null);
  document.getElementById('mobileCompactNow').classList.remove('disabled');

  console.log(out.join('\n'));
  process.exit(0);
})();
""".replace('SHEET_HTML', _SHEET_HTML)


def _run(ctx_source: str, mobile_source: str) -> subprocess.CompletedProcess:
    # Unique per invocation: the two jsdom tests in this module are eligible
    # for different xdist workers.  A fixed tests/_ctx_deadzone_harness.js let
    # one worker overwrite/unlink the file while the other's Node process was
    # loading it, producing suite-only hangs/timeouts on FUSE.
    fd, harness = tempfile.mkstemp(
        prefix='_ctx_deadzone_harness_', suffix='.js', dir=HERE)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    source_paths = []
    try:
        for prefix, source in (('_ctx_runtime_', ctx_source),
                               ('_mobile_runtime_', mobile_source)):
            source_fd, source_path = tempfile.mkstemp(
                prefix=prefix, suffix='.js', dir=HERE)
            with os.fdopen(source_fd, 'w', encoding='utf-8') as f:
                f.write(source)
            source_paths.append(source_path)
        return subprocess.run(
            ['node', harness, source_paths[0], ROOT, source_paths[1]],
                              capture_output=True, text=True, timeout=60)
    finally:
        for path in (harness, *source_paths):
            try:
                os.remove(path)
            except OSError:
                pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_mobile_context_section_renders_and_wires():
    proc = _run(_runtime_section('context-bar.js'),
                _runtime_section('main/main_folders_mobile.js'))
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'mobile context-section failures:\n' + out
    assert out.count('PASS') >= 12, f'expected >=12 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_neuter_delegation_is_load_bearing():
    """Drop the runManualCompaction delegation in _mobileCompactNow → the
    'compact_delegates' check must FAIL, proving the wiring assertion has teeth."""
    src = _runtime_section('main/main_folders_mobile.js')
    marker = 'runtimeScope.runManualCompaction(cid);'
    assert marker in src, 'NC marker not found — test stale vs main_folders_mobile.js'
    patched = src.replace(marker, '/* neutered */ void cid;', 1)
    fd, tmp = tempfile.mkstemp(
        prefix='_mobile_folders_neuter_', suffix='.js', dir=HERE)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(patched)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True, text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run(_runtime_section('context-bar.js'), patched)
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{proc.stdout}'
        assert 'FAIL compact_delegates' in proc.stdout, \
            'NC: neutering the delegation did NOT break compact_delegates:\n' + proc.stdout
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    assert _runtime_section('main/main_folders_mobile.js') == src, \
        'shipped file was modified!'


if __name__ == '__main__':
    test_no_compaction_entry_deadzone()
    test_historical_deadzone_sample_is_covered()
    test_sphere_hidden_where_sheet_takes_over()
    test_index_html_has_context_section()
    print('CSS guards: PASS')
    if _node_deps_available():
        test_mobile_context_section_renders_and_wires()
        test_neuter_delegation_is_load_bearing()
        print('jsdom guards: PASS')
    print('ALL PASSED')
