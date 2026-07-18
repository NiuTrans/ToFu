"""jsdom guard: Reading-Mode Report/Review default model tracks the frontend preset.

Behaviour (2026-07-02): when the user has NOT explicitly picked a model in the
Report/Review model dropdown, ``_populatePaperReportModelDropdown`` seeds the
view's default from the model selected in the frontend toolbar preset
(``config.model``, then the configured ``serverModel``) — so Reading Mode and
Review Mode stay consistent with the rest of the app instead of silently
landing on the first model in the list. It falls back to the first visible chat
model only when that preset is not among the available chat models.

CONTRACT — "default at open", intentionally (not accidentally):
  • The seed fires ONCE, only when ``!view.model`` (nothing chosen yet). It is a
    DEFAULT, evaluated when the picker is first populated.
  • An explicit paper-picker choice (``view.model`` already set) is NEVER
    overwritten by the seed — the user's per-view selection wins.
  • Because the seed only fires while ``!view.model``, switching the toolbar
    model LATER does not retro-actively change a paper view that has already
    seeded. That is the intended semantics: it is a default at open, and an
    explicit paper-picker choice should still win. (Pinned by
    ``test_explicit_pick_is_not_overwritten`` below.)

The regression risk is that a future refactor reverts the seeding block back to
``_selectPaperReportModel(chatModels[0].model_id, view)`` — the old
first-model default — and nothing catches it. This guard pins the behaviour.

The harness loads the REAL shipped ``static/js/paper-reader.js`` under jsdom and
drives ``_populatePaperReportModelDropdown`` for both the Report and Review
views against a fixed ``_registeredModels`` list, asserting which model the seed
lands on under three regimes (preset available / preset missing / explicit prior
pick). DB-free by construction: no ``server.app``, no Postgres/SQLite bootstrap
(respects the bare-CI rule).

Negative-control (automated, source-level): a second test patches a COPY of
paper-reader.js reverting the seeding block to the old first-model default and
asserts the harness then FAILS the preset-seed checks. The shipped file is never
modified.

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
PAPER_JS = os.path.join(JS_DIR, 'paper', 'report.js')
# _reportView + the _paperReportModel/_paperReviewModel module state live in
# the (still-shipped) paper-reader.js; report.js only carries the seed logic.
PAPER_READER_DEPS = os.path.join(JS_DIR, 'paper-reader.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
// The model pickers for both views must coexist (dropdown + label elements).
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="paperReportModelDropdown"></div><div id="paperReportModelLabel"></div>' +
  '<div id="paperReviewModelDropdown"></div><div id="paperReviewModelLabel"></div>' +
  '</body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.localStorage = win.localStorage;
global.console = console;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.t = global.t = (k) => k;
// Permissive Api so any load-time paper bootstrap doesn't throw (unused here).
global.Api = win.Api = { paper: {
  libraryList: async () => ({ ok: true, papers: [] }),
}};

// _reportView + _paperReportModel/_paperReviewModel state come from the
// still-shipped paper-reader.js (deps, argv[4]); the seed logic under test is
// in paper/report.js (target, argv[2]). Eval deps first (best-effort — its
// load-time bootstrap is stubbed/permissive), then the target.
try { eval(fs.readFileSync(process.argv[4], 'utf8')); } catch (e) {
  console.error('deps eval warning: ' + (e && e.message));
}
eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/report.js (real, shipped, target)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Fixed, capability-clean model list. 'm-preset' is deliberately NOT first, so
// seeding to it proves the preset was consulted (not chatModels[0]).
global._registeredModels = win._registeredModels = [
  { model_id: 'm-first',  capabilities: [] },
  { model_id: 'm-preset', capabilities: [] },
  { model_id: 'm-other',  capabilities: [] },
];
global._hiddenModels = win._hiddenModels = new Set();

(async () => {
  // Let any eval-time async init settle.
  for (let i = 0; i < 10; i++) { await new Promise(r => setTimeout(r, 0)); }

  const reportView = _reportView('report');
  const reviewView = _reportView('review');

  // ══ 1) config.model is an available chat model → seed from it (both views) ══
  global.config = win.config = { model: 'm-preset' };
  global.serverModel = win.serverModel = '';
  _paperReportModel = '';
  _paperReviewModel = '';
  _populatePaperReportModelDropdown(reportView);
  _populatePaperReportModelDropdown(reviewView);
  check('report_seed_from_preset', _paperReportModel === 'm-preset');
  check('report_seed_not_first',   _paperReportModel !== 'm-first');   // discriminator
  check('review_seed_from_preset', _paperReviewModel === 'm-preset');

  // ══ 1b) config absent but serverModel available → seed from serverModel ══
  global.config = win.config = {};                 // no .model
  global.serverModel = win.serverModel = 'm-preset';
  _paperReportModel = '';
  _populatePaperReportModelDropdown(reportView);
  check('report_seed_from_servermodel', _paperReportModel === 'm-preset');

  // ══ 2) preset NOT among available models → fall back to first chat model ══
  global.config = win.config = { model: 'm-missing' };
  global.serverModel = win.serverModel = '';       // nothing else to seed from
  _paperReportModel = '';
  _populatePaperReportModelDropdown(reportView);
  check('report_fallback_first', _paperReportModel === 'm-first');

  // ══ 3) explicit prior pick is NOT overwritten by the seed (both views) ══
  //     This also pins the "default at open" contract: the seed only fires
  //     while !view.model, so a later toolbar switch never clobbers a choice.
  global.config = win.config = { model: 'm-preset' };
  _paperReportModel = 'm-other';                    // user already picked
  _paperReviewModel = 'm-other';
  _populatePaperReportModelDropdown(reportView);
  _populatePaperReportModelDropdown(reviewView);
  check('report_explicit_pick_preserved', _paperReportModel === 'm-other');
  check('review_explicit_pick_preserved', _paperReviewModel === 'm-other');

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run_harness(paper_js: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_model_default_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, paper_js, ROOT, PAPER_READER_DEPS],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_paper_model_default_seeds_from_frontend_preset():
    proc = _run_harness(PAPER_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'paper model-default failures:\n' + out
    assert out.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_source_level_negative_control_first_model_default_reintroduces_bug():
    """Revert the seeding block to the old first-model default and prove FAIL.

    We patch a COPY of paper-reader.js so ``_populatePaperReportModelDropdown``
    seeds ``chatModels[0]`` again (the pre-fix default) instead of the frontend
    preset. The harness must then FAIL the preset-seed checks. The shipped file
    is untouched (md5-verified below).
    """
    src = open(PAPER_JS, encoding='utf-8').read()

    marker = (
        "  if (!view.model && chatModels.length > 0) {\n"
        "    var availableIds = {};\n"
        "    for (var ci = 0; ci < chatModels.length; ci++) availableIds[chatModels[ci].model_id] = true;\n"
        "    var preset = (typeof config !== 'undefined' && config && config.model)\n"
        "      ? config.model\n"
        "      : ((typeof serverModel !== 'undefined' && serverModel) ? serverModel : '');\n"
        "    var seed = (preset && availableIds[preset]) ? preset : chatModels[0].model_id;\n"
        "    _selectPaperReportModel(seed, view);\n"
        "  }"
    )
    assert marker in src, 'fix marker not found — test is stale, update the marker'
    broken = src.replace(
        marker,
        "  if (!view.model && chatModels.length > 0) {\n"
        "    _selectPaperReportModel(chatModels[0].model_id, view);\n"
        "  }",
        1,
    )
    assert broken != src, 'negative-control patch was a no-op'

    tmp = os.path.join(HERE, '_paper_reader_model_default_revert.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True, text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run_harness(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        # Reverting to the first-model default: the preset ('m-preset', not
        # first) is ignored and the seed lands on chatModels[0] ('m-first') →
        # these checks MUST flip to FAIL.
        assert 'FAIL report_seed_from_preset' in out, \
            'reverting the seed block did NOT reintroduce the first-model default — guard is non-load-bearing:\n' + out
        assert 'FAIL review_seed_from_preset' in out, \
            'reverting the seed block did NOT affect the review view — guard is non-load-bearing:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    assert open(PAPER_JS, encoding='utf-8').read() == src, 'shipped file was modified!'


if __name__ == '__main__':
    test_paper_model_default_seeds_from_frontend_preset()
    print('positive: PASS')
    test_source_level_negative_control_first_model_default_reintroduces_bug()
    print('negative-control: PASS')
    print('ALL PASSED')
