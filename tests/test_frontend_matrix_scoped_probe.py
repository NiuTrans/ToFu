#!/usr/bin/env python3
"""tests/test_frontend_matrix_scoped_probe.py — scoped probe UI + panel fit.

Drives the REAL shipped ``static/js/settings/providers/access_matrix.js``
under node (same harness pattern as tests/test_probe_nonchat_skip.py) and
pins the scoped-probe contract (2026-07-25):

  * RENDER — every key header carries a column probe button dispatching
    ``{key_idxs:[ki]}``; every model row (root AND alias) carries a row
    button dispatching ``{model_ids:[id]}``; a cell without a verdict shows
    a hover probe button; a cell WITH a verdict turns its pip into the
    re-probe trigger (exact (key, id) pair in the onclick).
  * STATE — while a scoped probe runs, the scoped cell/row/column renders
    the spinning 'probing' state and other scope buttons are disabled;
    the scope is cleared when a terminal snapshot is ingested.
  * DISPATCH — ``_runMatrixProbe(provIdx, false, only)`` posts the scope to
    the backend (``body.only``), does NOT set force (a scoped probe is a
    refresh, not a reset), and KEEPS the locally-known verdict cells so the
    grid merges instead of blanking.
  * PANEL FIT — ``_fitMatrixPanelWidth`` toggles .stg-matrix-wide on the
    settings panel iff an open matrix's scroll container overflows AT THE
    PANEL'S DEFAULT WIDTH. A re-fit while the panel is already wide (the
    probe-resume re-render after ``_toggleMatrixView``, the 1.5s probe
    poll, a tab switch) must KEEP the panel wide — measuring at the
    widened width used to read "no overflow" and shrink the panel right
    back (the owner-reported expand→narrow flicker, 2026-07-26).

NEUTER proofs: deleting ``if (only) body.only = only;`` from a COPY of the
file flips the dispatch assertions red (the scope buttons would silently
launch full-grid probes); deleting the ``classList.remove`` that forces
default-width measurement revives the flicker assertion.

Also pins the CSS affordances this feature relies on (settings.css):
always-visible matrix scrollbar, scroll-shadow layers, panel-wide rule.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
ACCESS_MATRIX_JS = runtime_section_path('settings/providers/access_matrix.js')
SETTINGS_CSS = os.path.join(ROOT, 'static', 'settings.css')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
var _resizeHandlers = [];
global.addEventListener = function (ev, fn) {       // resize hook registered at eval
  if (ev === 'resize') _resizeHandlers.push(fn);
};
global.document = {
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  // The 1.5s probe-poll tick probes for an open settings page; null =
  // 'closed' makes it delete its own timer instead of crashing the tail.
  getElementById: function () { return null; },
};
global.t = function (k) { return k; };
global.escapeHtml = function (s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
};
global.showToast = function () {};
global._renderProvidersTab = function () {};
global._stgProviders = [];
global._maskApiKey = function (k) { return k ? '…' + k.slice(-4) : ''; };
global._detectBrand = function () { return ''; };
global._brandSvg = function () { return ''; };

// Api stub: capture probe-start bodies; status polls return 'none'.
var _postedBodies = [];
global.Api = {
  providers: {
    probeCellsStart: function (body) {
      _postedBodies.push(body);
      return { then: function () { return { catch: function () {} }; } };
    },
    probeCellsStatus: function () {
      return { then: function () { return { catch: function () {} }; } };
    },
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL access_matrix.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

_stgProviders[0] = {
  id: 'p',
  api_keys: ['sk-aaa', 'sk-bbb'],
  models: [
    { model_id: 'm1', aliases: ['m1-a'], capabilities: ['text'] },
    { model_id: 'm2', capabilities: ['text'] },
  ],
};
_stgMatrixProbeAttached[0] = true;   // skip the resume-from-disk timer

// ── RENDER: column / row / cell triggers ──────────────────────────────
const matrixHtml = _renderAccessMatrix(0);
check('col_button_present', matrixHtml.indexOf('stg-mx-zap col') >= 0);
check('col_button_dispatches_key_scope',
      matrixHtml.indexOf('_probeMatrixScope(0,{key_idxs:[1]})') >= 0);
check('row_button_root_dispatches_model_scope',
      matrixHtml.indexOf('_probeMatrixScope(0,{model_ids:[&quot;m1&quot;]})') >= 0);
check('row_button_alias_dispatches_alias_scope',
      matrixHtml.indexOf('_probeMatrixScope(0,{model_ids:[&quot;m1-a&quot;]})') >= 0);
check('row_buttons_rendered_per_row',
      (matrixHtml.match(/stg-mx-zap row/g) || []).length === 3);

// Cell without a verdict → hover probe button for the exact pair.
const m1 = _stgProviders[0].models[0];
const cellHtml = _renderMatrixCell(0, 0, 1, m1, 'm1', false);
check('cell_button_present', cellHtml.indexOf('stg-mx-zap cell') >= 0);
check('cell_button_dispatches_exact_pair',
      cellHtml.indexOf('_probeMatrixScope(0,{key_idxs:[1],model_ids:[&quot;m1&quot;]})') >= 0);

// Cell WITH a verdict → the pip itself becomes the re-probe trigger.
_stgMatrixProbe[0] = {
  status: 'done',
  cells: { '0::m1': { key_idx: 0, model_id: 'm1', root_model_id: 'm1',
                      status: 'ok', detail: 'HTTP 200', recommend_disable: false } },
  summary: { ok: 1, disable: 0 }, total: 1, done_count: 1,
};
const pipCell = _renderMatrixCell(0, 0, 0, m1, 'm1', false);
check('pip_is_clickable_reprobe', pipCell.indexOf('stg-mx-probe-pip ok clickable') >= 0);
check('pip_dispatches_exact_pair',
      pipCell.indexOf('_probeMatrixScope(0,{key_idxs:[0],model_ids:[&quot;m1&quot;]})') >= 0);
check('no_separate_cell_button_when_pip', pipCell.indexOf('stg-mx-zap cell') < 0);

// ── STATE: running scope → spinner on the target, others disabled ──────
_stgMatrixProbeScope[0] = { key_idxs: [0], model_ids: ['m1'] };
_stgMatrixProbe[0].status = 'running';
const spinning = _renderMatrixCell(0, 0, 0, m1, 'm1', false);
check('scoped_cell_spins', spinning.indexOf('stg-mx-zap cell probing') >= 0);
check('spinning_cell_hides_stale_pip', spinning.indexOf('probe-pip ok clickable') < 0);
const otherCell = _renderMatrixCell(0, 0, 1, m1, 'm1', false);
check('other_cell_button_disabled_while_running', otherCell.indexOf('disabled') >= 0);
check('scopeCovers_cell_exact', _scopeCovers(0, 'cell', 0, 'm1') === true);
check('scopeCovers_cell_wrong_key', _scopeCovers(0, 'cell', 1, 'm1') === false);
check('scopeCovers_row_vs_cell_scope', _scopeCovers(0, 'row', null, 'm1') === false);

// Terminal ingest clears the scope.
_ingestProbeSnapshot(0, { status: 'done', cells: {}, summary: { ok: 0, disable: 0 } });
check('terminal_ingest_clears_scope', !_stgMatrixProbeScope[0]);

// ── DISPATCH: scoped start posts `only`, keeps local cells, no force ──
_postedBodies.length = 0;
_stgMatrixProbe[0] = {
  status: 'done',
  cells: { '0::m1': { key_idx: 0, model_id: 'm1', root_model_id: 'm1',
                      status: 'ok', detail: 'HTTP 200', recommend_disable: false } },
  summary: { ok: 1, disable: 0 }, total: 1, done_count: 1,
};
_runMatrixProbe(0, false, { key_idxs: [1] });
check('scoped_dispatch_posts_only',
      _postedBodies.length === 1 &&
      JSON.stringify(_postedBodies[0].only) === '{"key_idxs":[1]}');
check('scoped_dispatch_never_forces', _postedBodies[0].force === false);
check('scoped_start_keeps_local_verdicts',
      _stgMatrixProbe[0].cells['0::m1'] && _stgMatrixProbe[0].cells['0::m1'].status === 'ok');
check('scoped_start_records_scope',
      _stgMatrixProbeScope[0] && _stgMatrixProbeScope[0].key_idxs[0] === 1);
check('scoped_start_status_running', _stgMatrixProbe[0].status === 'running');

// Full retest still forces + clears cells (regression pin).
_postedBodies.length = 0;
_stgMatrixProbe[0].status = 'done';
_runMatrixProbe(0, true);
check('full_retest_still_forces',
      _postedBodies.length === 1 && _postedBodies[0].force === true &&
      !_postedBodies[0].only);
check('full_retest_clears_cells',
      Object.keys(_stgMatrixProbe[0].cells).length === 0);

// A second probe click while running is refused (backend is single-task).
_postedBodies.length = 0;
_probeMatrixScope(0, { model_ids: ['m2'] });
check('concurrent_scope_refused', _postedBodies.length === 0);

// ── PANEL FIT: verdict measured at the DEFAULT width (anti-flicker) ────
_stgMatrixProbe[0].status = 'done';
// Coupled fake: the scroll container's clientWidth tracks the panel class
// (narrow 400 / wide 1180) and scrollWidth = max(content, clientWidth) —
// the coupling a real browser has. Content 1000 fits the wide panel but
// overflows the narrow one: exactly the 3-column flicker shape.
const NARROW_W = 400, WIDE_W = 1180;
const ops = [];
const fakePanel = { _wide: false, style: {} };
Object.defineProperty(fakePanel.style, 'transition', {
  get: function () { return this._t || ''; },
  set: function (v) { this._t = v; ops.push(v === 'none' ? 't:suspend' : 't:restore'); },
});
fakePanel.classList = {
  contains: function (cls) { return cls === 'stg-matrix-wide' && fakePanel._wide; },
  remove: function (cls) { if (cls === 'stg-matrix-wide') { fakePanel._wide = false; ops.push('remove'); } },
  toggle: function (cls, on) { if (cls === 'stg-matrix-wide') { fakePanel._wide = !!on; ops.push('toggle:' + !!on); } },
};
Object.defineProperty(fakePanel, 'offsetWidth', {
  get: function () { ops.push('reflow'); return fakePanel._wide ? WIDE_W : NARROW_W; },
});
// Element factory. In the real browser matrix content ONLY changes through a
// full `_renderProvidersTab` rebuild, which returns a brand-new scroll element
// — so a content change is modelled here as a NEW element, and a settled
// re-fit (no re-render) keeps the SAME one. That identity is the fit's only
// truthful content signal: scrollWidth saturates to the panel width once wide.
function makeScroll(contentW) {
  const el = { _contentW: contentW };
  Object.defineProperty(el, 'clientWidth', {
    get: function () { return fakePanel._wide ? WIDE_W : NARROW_W; },
  });
  Object.defineProperty(el, 'scrollWidth', {
    get: function () { return Math.max(el._contentW, fakePanel._wide ? WIDE_W : NARROW_W); },
  });
  return el;
}
let fakeScrolls = [];
global.document.querySelector = function (sel) {
  if (sel === '.modal.settings-panel') return fakePanel;
  // The resize handler's deferred re-fit gates on this compound selector —
  // truthy exactly when a matrix is open. Mirror it from the scroll list.
  if (sel === '.modal.settings-panel .stg-matrix-scroll') return fakeScrolls[0] || null;
  return null;
};
global.document.querySelectorAll = function (sel) {
  return sel === '.stg-matrix-scroll' ? fakeScrolls : [];
};

// 1) First open (panel narrow, content overflows) → widen. The transition
//    is restored BEFORE the class change so the single widen still animates.
fakeScrolls = [makeScroll(1000)];
ops.length = 0;
_fitMatrixPanelWidth();
check('overflowing_matrix_widens_panel', fakePanel._wide === true);
check('widen_edge_ops',
      ops.join('|') === 't:suspend|remove|t:restore|toggle:true');

// 2) Settled re-fit (SAME element, no re-render) — a true no-op. The owner's
//    screenshot shows a SETTLED grid with no probe in flight, yet the panel
//    still oscillates: the driver is re-entry, not the probe poll. The fit
//    must become a fixpoint that costs ZERO DOM writes once settled, so EVERY
//    periodic caller (probe poll, tab switch, the resize echo) goes quiet.
ops.length = 0;
_fitMatrixPanelWidth();
check('settled_refit_writes_nothing', ops.length === 0);
check('settled_refit_keeps_wide', fakePanel._wide === true);
const fitBefore = window.__fitCount, workBefore = window.__fitWork;
_fitMatrixPanelWidth();
check('settled_refit_counted_but_no_work',
      window.__fitCount === fitBefore + 1 && window.__fitWork === workBefore);

// 2b) Content re-render while ALREADY wide (NEW element, still overflowing):
//     the full path runs and must KEEP the panel wide — measuring at the
//     widened width used to read "no overflow" and shrink the panel right
//     back (the expand→narrow flicker). Verdict comes from the DEFAULT width,
//     applied with the transition suspended so nothing re-animates.
fakeScrolls = [makeScroll(1000)];
ops.length = 0;
_fitMatrixPanelWidth();
check('refit_while_wide_stays_wide', fakePanel._wide === true);
check('stay_wide_ops',
      ops.join('|') === 't:suspend|remove|toggle:true|reflow|t:restore');
// The re-added class must be COMMITTED (forced reflow) while the transition
// is still suspended. Restoring the transition first makes the engine animate
// from the DEFAULT width the measurement reflow just committed — and since the
// 1.5s probe poll re-fits forever, that became a continuous narrow<->wide
// sweep (owner-reported "keeps narrowing and widening in a loop", 2026-07-26).
check('stay_wide_commits_before_transition_restored',
      ops.indexOf('reflow') >= 0 &&
      ops.indexOf('reflow') < ops.indexOf('t:restore'));

// 3) Content shrinks to fit the narrow panel (NEW element) → panel unwidens.
//    Proves the memo does NOT freeze the panel at a stale wide verdict.
fakeScrolls = [makeScroll(300)];
ops.length = 0;
_fitMatrixPanelWidth();
check('fitting_matrix_unwidens_panel', fakePanel._wide === false);
check('unwiden_ops',
      ops.join('|') === 't:suspend|remove|toggle:false|reflow|t:restore');

// 4) A hidden matrix (zero layout box) never widens the panel.
fakeScrolls = [{ clientWidth: 0, scrollWidth: 2000 }];
_fitMatrixPanelWidth();
check('hidden_matrix_never_widens', fakePanel._wide === false);

// ── RESIZE SELF-FEED: our own width change must not bounce back ───────
check('resize_handler_registered', _resizeHandlers.length === 1);
fakeScrolls = [makeScroll(1000)];
_fitMatrixPanelWidth();                       // widen; guard now held
check('selffeed_precondition_wide', fakePanel._wide === true);
const echoBefore = window.__resizeEchoDropped || 0;
_resizeHandlers[0]();                         // the scrollbar-toggle echo
check('resize_echo_dropped_while_applying',
      (window.__resizeEchoDropped || 0) === echoBefore + 1);
check('resize_events_counted', window.__resizeCount >= 1);

// ── LATE RESIZE ECHO: the 250ms applying-flag only guards echoes delivered
//    promptly. Event-loop jank (this project logs 5-10s LoopWatch stalls)
//    can deliver our own scrollbar-toggle echo AFTER the flag expires —
//    then the idempotence memo is the ONLY thing between the echo and
//    another mutation cycle. Pin that terminator with real timers. ──
function _sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function() {
  // A) Late echo, unchanged inputs (same elements, same vw): the debounced
  //    re-fit MUST actually run (proves the flag is no longer doing the
  //    work) and MUST be absorbed by the memo — zero DOM writes, fitWork
  //    flat.
  await _sleep(350);                             // guard from the widen above expired
  const fitA = window.__fitCount, workA = window.__fitWork;
  ops.length = 0;
  _resizeHandlers[0]();                          // late echo, inputs unchanged
  await _sleep(250);                             // 180ms debounce has fired
  check('late_echo_reroutes_into_fit',
        window.__fitCount === fitA + 1);
  check('late_echo_absorbed_by_memo',
        window.__fitWork === workA && ops.length === 0);
  check('late_echo_keeps_wide', fakePanel._wide === true);

  // B) Late echo carrying a CHANGED innerWidth (a root-scrollbar toggle
  //    shifts the viewport ~15px, so the memo's vw leg misses): exactly ONE
  //    full fit runs, it MUST take the suspended path (never animate), and
  //    the layout settles — the NEXT echo at the same vw is a memo no-op.
  //    A vw-miss that re-animated would re-open the loop this test kills.
  global.innerWidth = 1300;
  const workB = window.__fitWork;
  ops.length = 0;
  _resizeHandlers[0]();                          // late echo, vw changed
  await _sleep(250);
  check('late_echo_vw_miss_runs_one_fit',
        window.__fitWork === workB + 1);
  check('late_echo_vw_miss_takes_suspended_path',
        ops.join('|') === 't:suspend|remove|toggle:true|reflow|t:restore');
  check('late_echo_vw_miss_keeps_wide', fakePanel._wide === true);

  await _sleep(350);                             // guard from the vw-miss fit expired
  const workC = window.__fitWork;
  ops.length = 0;
  _resizeHandlers[0]();                          // second late echo, same vw
  await _sleep(250);
  check('late_echo_terminates',
        window.__fitWork === workC && ops.length === 0);
  check('late_echo_terminates_keeps_wide', fakePanel._wide === true);

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run_harness(matrix_js: str) -> str:
    harness = os.path.join(HERE, '_matrix_scoped_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, matrix_js],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
class ScopedProbeFrontendTest(unittest.TestCase):

    def test_scoped_probe_render_state_dispatch_panelfit(self):
        output = _run_harness(ACCESS_MATRIX_JS)
        fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
        self.assertEqual(fails, [], 'scoped-probe frontend failures:\n' + output)
        self.assertGreaterEqual(output.count('PASS'), 30,
                                'expected >=30 PASS lines, got:\n' + output)

    def test_neuter_scope_dispatch_is_load_bearing(self):
        """NEUTER: drop `if (only) body.only = only;` from a COPY → the scope
        buttons silently launch full-grid probes → dispatch checks go red."""
        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            src = f.read()
        anchor = '  if (only) body.only = only;\n'
        self.assertIn(anchor, src, 'dispatch anchor drifted — update the neuter')
        neutered = src.replace(anchor, '', 1)
        self.assertNotEqual(neutered, src)

        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, 'access_matrix_neutered.js')
            with open(copy, 'w', encoding='utf-8') as f:
                f.write(neutered)
            output = _run_harness(copy)
        self.assertIn('FAIL scoped_dispatch_posts_only', output,
                      'NEUTER did not bite: scope still posted without the only line.\n'
                      + output)

        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            self.assertEqual(f.read(), src, 'harness mutated the shipped access_matrix.js')

    def test_neuter_width_commit_before_transition_restore_is_load_bearing(self):
        """NEUTER: drop the ``void panel.offsetWidth`` that commits the final
        width while the transition is still suspended → the engine animates
        from the DEFAULT width the measurement reflow committed, and because
        the 1.5s probe poll re-fits forever the panel sweeps narrow↔wide
        continuously (owner-reported oscillation, 2026-07-26)."""
        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            src = f.read()
        anchor = '    void panel.offsetWidth;\n'
        self.assertIn(anchor, src, 'width-commit anchor drifted — update the neuter')
        neutered = src.replace(anchor, '', 1)
        self.assertNotEqual(neutered, src)

        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, 'access_matrix_neutered.js')
            with open(copy, 'w', encoding='utf-8') as f:
                f.write(neutered)
            output = _run_harness(copy)
        self.assertIn('FAIL stay_wide_commits_before_transition_restored', output,
                      'NEUTER did not bite: width still committed without the reflow.\n'
                      + output)

        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            self.assertEqual(f.read(), src, 'harness mutated the shipped access_matrix.js')

    def test_neuter_default_width_measurement_is_load_bearing(self):
        """NEUTER: drop the ``classList.remove('stg-matrix-wide')`` that forces
        the overflow verdict to be measured at the panel's DEFAULT width from
        a COPY → a re-fit with the panel already wide reads "no overflow" at
        the widened width and shrinks the panel back — the owner-reported
        expand→narrow flicker returns."""
        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            src = f.read()
        anchor = "  panel.classList.remove('stg-matrix-wide');\n"
        self.assertIn(anchor, src, 'default-width measurement anchor drifted — update the neuter')
        neutered = src.replace(anchor, '', 1)
        self.assertNotEqual(neutered, src)

        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, 'access_matrix_neutered.js')
            with open(copy, 'w', encoding='utf-8') as f:
                f.write(neutered)
            output = _run_harness(copy)
        self.assertIn('FAIL refit_while_wide_stays_wide', output,
                      'NEUTER did not bite: panel stayed wide without the default-width measurement.\n'
                      + output)

        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            self.assertEqual(f.read(), src, 'harness mutated the shipped access_matrix.js')


    def test_neuter_idempotence_gate_is_load_bearing(self):
        """NEUTER: drop the ``if (_mxFitUnchanged(...)) return;`` gate from a
        COPY → a settled re-fit and every late resize echo re-run the full
        mutation path (DOM writes on each poll/echo), so the memo-absorption
        checks go red."""
        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            src = f.read()
        anchor = '  if (_mxFitUnchanged(scrolls, vw, wasWide)) return;\n'
        self.assertIn(anchor, src, 'idempotence-gate anchor drifted — update the neuter')
        neutered = src.replace(anchor, '', 1)
        self.assertNotEqual(neutered, src)

        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, 'access_matrix_neutered.js')
            with open(copy, 'w', encoding='utf-8') as f:
                f.write(neutered)
            output = _run_harness(copy)
        self.assertIn('FAIL late_echo_absorbed_by_memo', output,
                      'NEUTER did not bite: late echo still absorbed without the gate.\n'
                      + output)

        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            self.assertEqual(f.read(), src, 'harness mutated the shipped access_matrix.js')


class ScopedProbeCssTest(unittest.TestCase):
    """Static pins for the affordances: visible scrollbar, scroll shadows,
    panel auto-widen, zap placements, model-id ellipsis cap."""

    @classmethod
    def setUpClass(cls):
        with open(SETTINGS_CSS, encoding='utf-8') as f:
            cls.css = f.read()

    def test_always_visible_scrollbar_rules(self):
        self.assertIn('.stg-matrix-scroll::-webkit-scrollbar', self.css)
        self.assertIn('scrollbar-width: thin', self.css)

    def test_scroll_shadow_layers(self):
        self.assertIn('background-attachment: local, local, scroll, scroll', self.css)

    def test_panel_wide_rule_desktop_only(self):
        self.assertIn('.settings-panel.stg-matrix-wide', self.css)
        self.assertIn('@media (min-width: 769px)', self.css)

    def test_zap_placements_and_pip_clickable(self):
        for sel in ('.stg-mx-zap.col', '.stg-mx-zap.row', '.stg-mx-zap.cell',
                    '.stg-mx-zap.probing', '.stg-mx-probe-pip.clickable'):
            self.assertIn(sel, self.css, sel + ' missing from settings.css')

    def test_model_id_ellipsis_cap(self):
        self.assertIn('.stg-mx-mid.alias-id', self.css)

    def _rule_body(self, selector):
        """Extract the declaration block of one exact selector."""
        import re
        m = re.search(re.escape(selector) + r'\s*\{([^{}]*)\}', self.css)
        self.assertIsNotNone(m, selector + ' rule missing from settings.css')
        return m.group(1)

    def test_col_zap_has_own_lane(self):
        """The column zap must sit in a RESERVED lane, never on top of the
        full-width key-name input: the keyhead pads ~26px on the right
        (mirroring the model cell's 28px row-zap lane) and the zap is
        vertically centered there with the same recipe as .stg-mx-zap.row.
        Regression pin for the owner-reported overlap (zap covered the
        input's right half → mis-clicks launched a whole-column probe)."""
        keyhead = self._rule_body('.stg-mx-keyhead')
        self.assertIn('padding: 6px 26px 6px 8px !important', keyhead,
                      'keyhead lost its reserved right lane for the col zap')
        col = self._rule_body('.stg-mx-zap.col')
        self.assertIn('top: 50%', col)
        self.assertIn('margin-top: -9px', col)
        self.assertIn('right: 4px', col)
        row = self._rule_body('.stg-mx-zap.row')
        self.assertIn('top: 50%', row, 'row/col zap centering recipes drifted apart')


if __name__ == '__main__':
    unittest.main()
