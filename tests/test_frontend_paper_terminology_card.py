"""jsdom guard: the terminology self-containment card is rendered to the READER.

The backend attaches ``meta.terminologyAudit`` to a report when the glossary has
a gap (a term used in the body with no glossary row, or a glossary definition
leaning on an undefined sibling term). That payload was dying in the DB — the
frontend had NO handler for it, so the reader saw nothing. This test loads the
REAL shipped ``static/js/paper/report.js`` under jsdom and drives
``_renderFinalReport`` with a report meta carrying a ``terminologyAudit``:

  • a gappy meta → a ``.paper-terminology-audit`` card appears in the article,
    listing the missing term (with the section it appears in) and the dangling
    glossary reference (with the term whose definition leans on it);
  • a clean meta (no terminologyAudit) → NO card;
  • an empty audit object ({missing:[], dangling:[]}) → NO card (defensive).

Load-bearing negative control (on a COPY; shipped file byte-identical after):
  • neuter the render call in ``_renderFinalReport`` → the gappy meta no longer
    produces the card → the positive check FAILS.

DB-free; skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
REPORT_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'report.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="c"></div></body>',
                      { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.localStorage = win.localStorage;
global.console = console;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
// t(): the dangling reason is built via t('paper.termDanglingReason',
// {term, referencedTerm}); the dangling term (RM) appears ONLY inside that
// interpolated template (the <code> element carries referencedTerm=DPO). The
// KEY STRING itself has no {term} placeholder, so the stub must map the key to
// the real i18n template (mirroring static/js/i18n.js) THEN interpolate — else
// 'RM' never renders.
const _TMPL = {
  'paper.termDanglingReason': 'the glossary definition of “{term}” references undefined “{referencedTerm}”',
};
win.t = global.t = (k, p) => {
  let s = (k in _TMPL) ? _TMPL[k] : k;
  if (p) for (const kk in p) s = s.replace(new RegExp('\\{' + kk + '\\}', 'g'), p[kk]);
  return s;
};
// renderMarkdown must exist and non-trivially transform so _renderFinalReport
// takes the real (non-<pre>) path where the audit cards are prepended.
win.renderMarkdown = global.renderMarkdown = (s) => '<p>' + escapeHtml(s || '') + '</p>';
global._i18nLang = win._i18nLang = 'en';

eval(fs.readFileSync(process.argv[2], 'utf8'));  // real, shipped paper/report.js

// Stub the layout/enrichment helpers _renderFinalReport calls that need real
// layout or unrelated subsystems (jsdom has no layout). The functions under
// test — _renderTerminologyAuditCard + the prepend in _renderFinalReport — run
// for real.
_decorateCallouts = () => {};
_frameFigures = () => {};
_decorateZoomableImages = () => {};
_decorateGlossaryTerms = () => {};
_extractGlossary = () => ({});
_indexHeadings = () => [];
_buildReportTOC = () => '';
_buildReadingTimeBar = () => null;
_wireReportScrollSpy = () => {};
_wireReadingTimeTracking = () => {};
_captureReadingAnchor = () => null;
_loadReadingPosition = () => null;
_restoreReadingAnchor = () => {};
_persistReadingPosition = () => {};
_syncReportToolbar = () => {};
_renderReportFinishTag = () => '';
if (typeof _reportView === 'function') {
  // Give _renderFinalReport a minimal view when meta is passed explicitly.
}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const GAPPY_META = { model: 'm', terminologyAudit: {
  glossaryCount: 3,
  counts: { missing: 1, dangling: 1 },
  missing: [{ term: 'SFT', section: 'Method', evidence: 'begins with an SFT stage' }],
  dangling: [{ term: 'RM', referencedTerm: 'DPO', definition: 'trained via the DPO objective' }],
}};
const CLEAN_META = { model: 'm' };
const EMPTY_META = { model: 'm', terminologyAudit: {
  glossaryCount: 4, counts: { missing: 0, dangling: 0 }, missing: [], dangling: [] } };

const view = (typeof _reportView === 'function') ? _reportView('report') : { meta: null };

// ── (1) gappy meta → card rendered with both gaps ──
const c = document.getElementById('c');
_renderFinalReport(c, 'BODY TEXT', GAPPY_META, view);
const card = c.querySelector('.paper-terminology-audit');
check('card_present_for_gappy_meta', !!card);
const cardHtml = card ? card.innerHTML : '';
check('card_lists_missing_term', cardHtml.indexOf('SFT') !== -1);
check('card_names_missing_section', cardHtml.indexOf('Method') !== -1);
check('card_lists_dangling_ref', cardHtml.indexOf('DPO') !== -1);
check('card_names_dangling_owner', cardHtml.indexOf('RM') !== -1);
check('card_has_role_note', card && card.getAttribute('role') === 'note');

// ── (2) clean meta → no card ──
const c2 = document.getElementById('c');
_renderFinalReport(c2, 'BODY TEXT', CLEAN_META, view);
check('no_card_for_clean_meta', c2.querySelector('.paper-terminology-audit') === null);

// ── (3) empty audit object → no card (defensive) ──
_renderFinalReport(c2, 'BODY TEXT', EMPTY_META, view);
check('no_card_for_empty_audit', c2.querySelector('.paper-terminology-audit') === null);

console.log(out.join('\n'));
process.exit(0);
"""


def _write_harness() -> str:
    harness = os.path.join(HERE, '_paper_terminology_card_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    return harness


def _run(report_js: str) -> subprocess.CompletedProcess:
    harness = _write_harness()
    try:
        return subprocess.run(['node', harness, report_js, ROOT],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_terminology_card_rendered_to_reader():
    proc = _run(REPORT_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'terminology-card render failures:\n' + out
    assert out.count('PASS') >= 8, f'expected >=8 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_render_call_is_load_bearing():
    """Neuter the terminologyAudit prepend in _renderFinalReport → the gappy
    meta no longer produces the card → the positive check FAILS. Proves the
    wiring (not just the builder) is what surfaces the gap to the reader."""
    src = open(REPORT_JS, encoding='utf-8').read()
    marker = (
        "  if (meta && meta.terminologyAudit) {\n"
        "    var termHtml = _renderTerminologyAuditCard(meta.terminologyAudit);\n"
        "    if (termHtml) article.insertAdjacentHTML('afterbegin', termHtml);\n"
        "  }\n"
    )
    assert marker in src, 'terminology prepend marker not found — test stale'
    patched = src.replace(marker, '', 1)
    tmp = os.path.join(HERE, '_paper_terminology_card_neuter.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(patched)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True, text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        assert 'FAIL card_present_for_gappy_meta' in out, \
            'NC: removing the prepend did NOT stop the card from rendering:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    assert open(REPORT_JS, encoding='utf-8').read() == src, 'shipped file was modified!'


if __name__ == '__main__':
    test_terminology_card_rendered_to_reader()
    print('positive: PASS')
    test_render_call_is_load_bearing()
    print('neuter: PASS')
    print('ALL PASSED')
