#!/usr/bin/env python3
"""jsdom + static guards for the Reading-Mode model picker's label + ordering.

Label defects this pins (both user-reported as "the model name is cut off /
not fully shown" in the Reading-Mode toolbar):

  * The chosen model's short name must SURVIVE ``_applyI18n()``. The markup
    ships ``data-i18n="paper.reportSelectModel"`` on the label span for the
    initial placeholder; ``_applyI18n()`` walks every ``[data-i18n]`` and
    overwrites textContent. It runs on boot AND on every language toggle, so
    leaving the attribute in place silently replaced the model name with
    "Select model" — the one piece of state the button exists to show.
  * A long id is ellipsized by CSS, so the FULL id must stay recoverable via
    the button's tooltip (and the static ``data-i18n-title`` must be dropped
    for the same clobber reason).

Static guard: the CSS max-width must fit the longest real model short-name.

Ordering guards: the report/review dropdowns read the same ``_registeredModels``
as the toolbar picker, so both axes (provider sections, in-section models)
must sort through the SAME shared comparator (``_compareModelsByDisplayName``
from settings/branding.js) — pinned by driving the real populate function
with the real comparator chain, plus two NEUTER probes and a stale-bundle
degraded-mode complement.

Skips cleanly when node + jsdom aren't installed.
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
REPORT_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'report.js')
STYLES_CSS = os.path.join(ROOT, 'static', 'styles.css')
INDEX_HTML = os.path.join(ROOT, 'index.html')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


#: The label span + button markup mirrors index.html (both report and review
#: pickers ship the same data-i18n placeholder pair).
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(`<!DOCTYPE html><body>
  <div class="paper-report-model-picker">
    <button class="paper-report-model-btn"
            data-i18n-title="paper.reportSelectModelTitle"
            title="Select model for report generation">
      <span id="paperReportModelLabel" data-i18n="paper.reportSelectModel">Select model</span>
    </button>
    <div class="paper-report-model-dropdown" id="paperReportModelDropdown"></div>
  </div>
</body>`, { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;

const T_MAP = {
  'paper.reportSelectModel': 'SELECT_MODEL_PLACEHOLDER',
  'paper.reportSelectModelTitle': 'PICK_A_MODEL_TITLE',
};
win.t = global.t = (k) => T_MAP[k] || k;
global._modelShortName = win._modelShortName = (id) =>
  String(id || '').replace(/^(aws\.|vertex\.)/, '').split('/').pop();

/* The real _applyI18n contract (static/js/i18n.js): walk every [data-i18n]
   and overwrite textContent; same for [data-i18n-title] → title. */
function _applyI18n() {
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    const k = el.getAttribute('data-i18n');
    if (k) el.textContent = t(k);
  });
  document.querySelectorAll('[data-i18n-title]').forEach((el) => {
    const k = el.getAttribute('data-i18n-title');
    if (k) el.title = t(k);
  });
}

/* Minimal view-context stand-in — the shipped _reportView lives in
   paper-reader.js; the picker only reads these three fields. */
let VIEW_MODEL = '';
global._reportView = win._reportView = () => ({
  modelDropdownId: 'paperReportModelDropdown',
  modelLabelId: 'paperReportModelLabel',
  get model() { return VIEW_MODEL; },
  set model(v) { VIEW_MODEL = v; },
});
global._registeredModels = win._registeredModels = [];
global._hiddenModels = win._hiddenModels = new Set();

/* Load ONLY the picker functions out of the real shipped report.js — the file
   is a 2700-line module whose top level touches many other globals. Slicing by
   the function boundary keeps this a test of the REAL code, not a copy. */
const src = fs.readFileSync(path.join(ROOT, 'static/js/paper/report.js'), 'utf8');
const start = src.indexOf('function _selectPaperReportModel(');
if (start < 0) { console.log('FAIL harness_cannot_find_function'); process.exit(0); }
const end = src.indexOf('\nfunction _togglePaperReportModelDropdown', start);
eval(src.slice(start, end > 0 ? end : undefined));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const LONG_ID = 'yuju-claude-opus-5-evaDaily';
const label = document.getElementById('paperReportModelLabel');
const btn = label.closest('.paper-report-model-btn');

// Baseline: the placeholder is i18n-driven before any choice is made.
_applyI18n();
check('placeholder_is_i18n_driven',
  label.textContent === 'SELECT_MODEL_PLACEHOLDER');

// Choose a model → the label shows its short name.
_selectPaperReportModel(LONG_ID);
check('label_shows_model', label.textContent === LONG_ID);
check('full_id_in_button_tooltip', btn.title === LONG_ID);

// ★ The regression: a language toggle (or boot) re-runs _applyI18n().
_applyI18n();
check('model_name_survives_i18n_reapply', label.textContent === LONG_ID);
check('tooltip_survives_i18n_reapply', btn.title === LONG_ID);
// Run it a few more times — idempotent, never drifts back to the placeholder.
_applyI18n(); _applyI18n();
check('model_name_survives_repeated_i18n', label.textContent === LONG_ID);

// Empty model (no chat models available) keeps the button usable.
_selectPaperReportModel('');
check('empty_model_falls_back_to_placeholder',
  label.textContent === 'SELECT_MODEL_PLACEHOLDER');

console.log(out.join('\n'));
process.exit(0);
"""


def _run_harness(src_override: str | None = None, extra_argv=None) -> str:
    harness = os.path.join(HERE, '_model_label_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(src_override if src_override is not None else _HARNESS)
    try:
        proc = subprocess.run(['node', harness, '-', ROOT] + list(extra_argv or ()),
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_model_label_survives_language_toggle():
    out = _run_harness()
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'model-picker label failures:\n' + out
    assert out.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_data_i18n_removal_is_loadbearing():
    """NEUTER: drop the ``removeAttribute('data-i18n')`` line from a COPY of
    report.js → the next _applyI18n() overwrites the model name with the
    placeholder and the survival probe flips FAIL. Proves the attribute
    removal (not the textContent write) is what makes the choice stick."""
    src = open(REPORT_JS, encoding='utf-8').read()
    marker = "      label.removeAttribute('data-i18n');"
    assert marker in src, 'data-i18n removal marker not found — test is stale'
    broken = src.replace(marker, '      /* NEUTERED */', 1)
    assert broken != src

    tmp_js = os.path.join(HERE, '_report_no_i18n_removal.js')
    with open(tmp_js, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp_js], capture_output=True,
                             text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        patched_harness = _HARNESS.replace(
            "path.join(ROOT, 'static/js/paper/report.js')",
            repr(tmp_js).replace("'", '"'))
        out = _run_harness(patched_harness)
        assert 'FAIL model_name_survives_i18n_reapply' in out, \
            'removing the data-i18n cleanup did NOT flip the probe:\n' + out
    finally:
        try:
            os.remove(tmp_js)
        except OSError:
            pass
    assert open(REPORT_JS, encoding='utf-8').read() == src, 'shipped file modified!'


# ═══ Ordering guards — the dropdown must sort through the SAME comparator
# ═══ the toolbar picker uses (_compareModelsByDisplayName), on BOTH axes.

#: Drives the REAL _populatePaperReportModelDropdown (spliced out of the
#: shipped report.js) against a scrambled two-provider fixture, with the REAL
#: comparator chain spliced out of settings/branding.js — nothing hand-copied,
#: so "paper picker order == toolbar picker order" is pinned by construction.
#: argv 'no-branding' simulates a stale bundle missing branding.js: the
#: dropdown must degrade to arrival order, never die empty.
_ORDER_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const NO_BRANDING = process.argv[4] === 'no-branding';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(`<!DOCTYPE html><body>
  <div class="paper-report-model-picker">
    <button class="paper-report-model-btn">
      <span id="paperReportModelLabel">Select model</span>
    </button>
    <div class="paper-report-model-dropdown" id="paperReportModelDropdown"></div>
  </div>
</body>`, { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
win.t = global.t = (k) => k;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

/* The REAL comparator chain (_modelShortName → _modelDisplaySortKey →
   _MODEL_NAME_COLLATOR → _compareModelsByDisplayName), spliced at runtime
   from settings/branding.js — the same single source of truth the toolbar
   picker sorts with. */
if (!NO_BRANDING) {
  const bsrc = fs.readFileSync(path.join(ROOT, 'static/js/settings/branding.js'), 'utf8');
  const bStart = bsrc.indexOf('function _modelShortName(');
  const bEnd = bsrc.indexOf('/* Sort a list of {model_id} entries', bStart);
  if (bStart < 0 || bEnd < 0) { console.log('FAIL harness_cannot_find_comparator_chain'); process.exit(0); }
  eval(bsrc.slice(bStart, bEnd));
}

/* Fixture: arrival order is scrambled on BOTH axes, and one model's friendly
   pricing name ("Alpha One") sorts OPPOSITE to its raw id ("gw/z-ultra") —
   so display-name order, raw-id order and arrival order are three different
   sequences and the assertions can tell them apart. */
global._modelPricingCache = win._modelPricingCache = {
  'gw/z-ultra': { name: 'Alpha One' },
  'gw/claude-opus-5': { name: 'Claude Opus 5' },
  'zz/gemini-flash': { name: 'Gemini Flash' },
};
global._registeredModels = win._registeredModels = [
  { model_id: 'zz/gemini-flash',  provider_id: 'p2', provider_name: 'Zebra Provider' },
  { model_id: 'gw/claude-opus-5', provider_id: 'p1', provider_name: 'Acme Provider' },
  { model_id: 'gw/z-ultra',       provider_id: 'p1', provider_name: 'Acme Provider' },
];
global._hiddenModels = win._hiddenModels = new Set();

let VIEW_MODEL = 'gw/claude-opus-5';
global._reportView = win._reportView = () => ({
  modelDropdownId: 'paperReportModelDropdown',
  modelLabelId: 'paperReportModelLabel',
  get model() { return VIEW_MODEL; },
  set model(v) { VIEW_MODEL = v; },
});

/* Load the REAL picker functions out of the shipped report.js. */
const src = fs.readFileSync(path.join(ROOT, 'static/js/paper/report.js'), 'utf8');
const start = src.indexOf('function _populatePaperReportModelDropdown(');
if (start < 0) { console.log('FAIL harness_cannot_find_populate'); process.exit(0); }
const end = src.indexOf('\nfunction _togglePaperReportModelDropdown', start);
eval(src.slice(start, end > 0 ? end : undefined));

_populatePaperReportModelDropdown();

const dropdown = document.getElementById('paperReportModelDropdown');
const sections = Array.from(dropdown.querySelectorAll('.paper-report-model-dropdown-section'))
  .map((el) => el.textContent);
const items = Array.from(dropdown.querySelectorAll('.paper-report-model-dropdown-item'))
  .map((el) => el.title);

if (NO_BRANDING) {
  /* Degraded mode: no comparator on a stale bundle — the dropdown must still
     render every model (arrival order), never strand an empty list. This is
     the complement probe: deleting the _canSort guard and calling the
     comparator unguarded throws here and flips this FAIL. */
  check('dropdown_still_renders_without_comparator', items.length === 3);
} else {
  check('sections_follow_provider_display_name',
    JSON.stringify(sections) === JSON.stringify(['Acme Provider', 'Zebra Provider']));
  check('items_follow_display_name_not_raw_id',
    JSON.stringify(items) === JSON.stringify(['gw/z-ultra', 'gw/claude-opus-5', 'zz/gemini-flash']));
  const firstItem = dropdown.querySelector('.paper-report-model-dropdown-item');
  check('rendered_label_is_the_friendly_name',
    !!(firstItem && firstItem.textContent === 'Alpha One'));
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run_order_harness_patched(marker: str, replacement: str) -> str:
    """Run the order harness against a COPY of report.js with ``marker``
    replaced — the NEUTER driver. Asserts the patch actually lands (per the
    'a NEUTER that hits nothing is indistinguishable from a valid guard'
    discipline) and that the shipped file is untouched afterwards."""
    src = open(REPORT_JS, encoding='utf-8').read()
    assert marker in src, 'NEUTER marker not found in report.js — test is stale'
    broken = src.replace(marker, replacement, 1)
    assert broken != src
    tmp_js = os.path.join(HERE, '_report_order_neutered.js')
    with open(tmp_js, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp_js], capture_output=True,
                             text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        patched_harness = _ORDER_HARNESS.replace(
            "path.join(ROOT, 'static/js/paper/report.js')",
            repr(tmp_js).replace("'", '"'))
        return _run_harness(patched_harness)
    finally:
        try:
            os.remove(tmp_js)
        except OSError:
            pass
        assert open(REPORT_JS, encoding='utf-8').read() == src, \
            'shipped file modified!'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_dropdown_orders_by_display_name():
    """The paper report/review pickers read the same _registeredModels as the
    toolbar picker, so they must render the same ORDER: provider sections by
    provider name, in-section models by display name — never raw arrival
    order and never raw model_id order."""
    out = _run_harness(_ORDER_HARNESS)
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'paper model-picker ordering failures:\n' + out
    assert out.count('PASS') >= 3, f'expected >=3 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_dropdown_renders_without_branding_js():
    """Complement: on a stale bundle missing the comparator, the dropdown
    must degrade to arrival order — not throw and strand an empty list."""
    out = _run_harness(_ORDER_HARNESS, extra_argv=['no-branding'])
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'degraded-mode ordering failures:\n' + out
    assert 'PASS dropdown_still_renders_without_comparator' in out, out


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_item_sort_is_loadbearing():
    """Remove the in-section model sort → items revert to arrival order and
    the display-name probe flips FAIL."""
    out = _run_order_harness_patched(
        '    if (_canSort) group.models.sort(_compareModelsByDisplayName);',
        '    /* NEUTERED item sort */')
    assert 'FAIL items_follow_display_name_not_raw_id' in out, \
        'removing the item sort did NOT flip the order probe:\n' + out


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_section_sort_is_loadbearing():
    """Remove the provider-section sort → sections revert to arrival order
    and the section probe flips FAIL."""
    out = _run_order_harness_patched(
        "    pids.sort(function(x, y) {\n"
        "      var nx = String((grouped[x] && grouped[x].name) || x);\n"
        "      var ny = String((grouped[y] && grouped[y].name) || y);\n"
        "      return _compareModelsByDisplayName(nx, ny);\n"
        "    });",
        '    /* NEUTERED section sort */')
    assert 'FAIL sections_follow_provider_display_name' in out, \
        'removing the section sort did NOT flip the section probe:\n' + out


# ═══ Static guards (no node required) ═══
# ═══ Static guards (no node required) ═══

def test_model_btn_width_fits_longest_model_name():
    """The button must be wide enough for the longest real model short-name.

    Measured from the shipped provider config: ids reach 30 characters
    (e.g. ``gemini-3.1-flash-image-preview``). At the button's 11px semibold
    font ~30 chars needs roughly 200px of text box plus ~46px of chrome
    (two SVG icons + gaps + padding), so the cap must clear ~250px. The old
    180px cut ``yuju-claude-opus-5-e…`` off at 21 chars, which is the
    reported symptom.
    """
    css = open(STYLES_CSS, encoding='utf-8').read()
    m = re.search(r'\.paper-report-model-btn\{[^}]*?max-width:(\d+)px', css)
    assert m, '.paper-report-model-btn max-width rule not found'
    assert int(m.group(1)) >= 250, (
        f'model button max-width is {m.group(1)}px — too narrow for a 30-char '
        'model id; long names get ellipsized in the toolbar')


def test_model_btn_label_span_still_ellipsizes():
    """Widening is not a licence to overflow: the span must still clip so a
    pathological id can never push the toolbar sideways."""
    css = open(STYLES_CSS, encoding='utf-8').read()
    m = re.search(r'\.paper-report-model-btn span\{([^}]*)\}', css)
    assert m, '.paper-report-model-btn span rule not found'
    body = m.group(1)
    for prop in ('overflow:hidden', 'text-overflow:ellipsis', 'white-space:nowrap'):
        assert prop in body, f'{prop} missing from the label span rule'


def test_both_pickers_ship_the_same_placeholder_markup():
    """Report AND review pickers must both carry the i18n placeholder — the
    JS clobber fix is shared, so a divergence here would silently mean one
    tab's label is not covered by it."""
    html = open(INDEX_HTML, encoding='utf-8').read()
    for label_id in ('paperReportModelLabel', 'paperReviewModelLabel'):
        m = re.search(r'<span id="' + label_id + r'"([^>]*)>', html)
        assert m, f'{label_id} span not found in index.html'
        assert 'data-i18n="paper.reportSelectModel"' in m.group(1), (
            f'{label_id} lost its data-i18n placeholder — the initial '
            '"Select model" text would no longer localize')


def test_static_js_syntax():
    if not shutil.which('node'):
        pytest.skip('node not installed')
    proc = subprocess.run(['node', '--check', REPORT_JS],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f'report.js syntax: {proc.stderr}'


if __name__ == '__main__':
    import sys
    test_model_btn_width_fits_longest_model_name()
    test_model_btn_label_span_still_ellipsizes()
    test_both_pickers_ship_the_same_placeholder_markup()
    if _node_deps_available():
        test_static_js_syntax()
        test_model_label_survives_language_toggle()
        test_NEUTER_data_i18n_removal_is_loadbearing()
        test_dropdown_orders_by_display_name()
        test_dropdown_renders_without_branding_js()
        test_NEUTER_item_sort_is_loadbearing()
        test_NEUTER_section_sort_is_loadbearing()
    else:
        print('SKIP jsdom cases — node + jsdom not available')
    print('ALL PASSED')
    sys.exit(0)
