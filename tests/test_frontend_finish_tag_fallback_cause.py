#!/usr/bin/env python3
"""tests/test_frontend_finish_tag_fallback_cause.py — the SETTLED fallback
finish-tag must render WHY the fallback happened as VISIBLE text.

The reported bug (owner, 2026-07-28): a settled turn that fell back from
claude-opus-5 to kimi-k3 rendered only

    回退 → kimi-k3

while the actual cause — ``upstream_error: API HTTP 502: <html>…openresty…``
— was locked inside the tag's ``title`` attribute. A tooltip is unreachable
on touch and easy to miss on desktop, so in practice the cause was invisible.
The live streaming banner had the identical defect; it was fixed first, which
is why the formatting now lives in ONE place.

What this pins:

  1. Visible cause: renderFinishInfo emits a ``.fb-cause`` tag whose TEXT
     carries the localized kind label + the distilled detail.
  2. Distillation: a whole upstream HTML error page reads as its human
     signal ('502 Bad Gateway · openresty'), not as markup source, and the
     injected markup is inert (no live <h1>/<center> nodes).
  3. Verbatim preservation: nothing is lost — the full cause stays in title.
  4. Pass-through: a PLAIN reason (rate limit / timeout — the common case) is
     never reworded, and a lone '<' is not mistaken for markup.
  5. Single formatter: the typed Surface footer delegates to finish_info.js,
     which consumes core/error_envelope.js's fallbackCauseParts; no surface
     may carry a private copy of the stripping logic.
  6. Graceful degradation: finish_info.js evaled WITHOUT error_envelope.js
     (the dev / isolated-harness context test_frontend_finish_tag_fallback.py
     already protects) must not throw.

NEUTER discipline:
  * test_nc_visible_cause_tag_is_load_bearing — drop the .fb-cause push from a
    COPY of finish_info.js (keeping the title, i.e. the exact pre-fix
    behaviour) → the visible-cause assertions MUST flip red. Proves they are
    satisfied by rendered markup, not by the title that always had the cause.
  * test_nc_distillation_is_load_bearing — make distillFallbackDetail a
    pass-through in a COPY of error_envelope.js → the raw-markup assertion
    MUST flip red, and it must flip red for the SETTLED tag, proving
    finish_info really routes through the shared formatter.

Run standalone:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_frontend_finish_tag_fallback_cause.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

from tests._runtime_sections import native_module_path, runtime_sections_dir

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS = runtime_sections_dir()
FINISH_INFO = os.path.join(JS, 'ui', 'finish_info.js')
ERROR_ENVELOPE = os.path.join(JS, 'core', 'error_envelope.js')
TYPED_ERRORS = native_module_path(
    '.native/api-errors.js', os.path.join(ROOT, 'frontend/src/api/errors.ts'))

# The owner's verbatim payload — a bare openresty 502 whose whole HTML body
# arrives as the fallback reason.
REASON_502 = (
    'upstream_error: API HTTP 502: <html>\n'
    '<head><title>502 Bad Gateway</title></head>\n'
    '<body>\n<center><h1>502 Bad Gateway</h1></center>\n'
    '<hr><center>openresty</center>\n</body>\n</html>'
)


def _node_deps_available() -> bool:
    return (bool(shutil.which('node'))
            and os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom')))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const TYPED_ERRORS = process.argv[3];
const ERROR_ENVELOPE = process.argv[4];   // real or NEUTERED copy
const FINISH_INFO = process.argv[5];      // real or NEUTERED copy
const REASON_502 = process.argv[6];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));

const dom = new JSDOM('<!DOCTYPE html><body><div id="h"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.localStorage = win.localStorage;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

global.escapeHtml = win.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');
// t() echoes the key (with params substituted) so label CHOICE is assertable.
global.t = win.t = (k, o) => {
  o = o || {};
  let s = k;
  for (const kk in o) s = s.split('{' + kk + '}').join(o[kk]);
  return s;
};
global.Icon = win.Icon = (name) => '<ICON:' + name + '>';
global.formatCny = win.formatCny = (n) => '¥' + Number(n || 0).toFixed(4);
global.calcCostCny = win.calcCostCny = () => 0;
global.calcCost = win.calcCost = () => 0;
// Minimal _i18n so _envResolveI18n can resolve a keyed kind chip.
global._i18n = win._i18n = {
  'err.k.upstream_error.chip': { zh: '上游故障', en: 'Upstream error' },
  'err.k.ratelimit.chip': { zh: '限流', en: 'Rate limited' },
};
global._i18nLang = win._i18nLang = 'en';

eval(fs.readFileSync(TYPED_ERRORS, 'utf8'));     // api/errors.ts owner
global.normalizeTypedErrorEnvelope = global.normalizeErrorEnvelope;
global.isTypedErrorEnvelope = global.isErrorEnvelope;
eval(fs.readFileSync(ERROR_ENVELOPE, 'utf8'));   // core/error_envelope.js
eval(fs.readFileSync(FINISH_INFO, 'utf8'));      // ui/finish_info.js

const h = document.getElementById('h');
function render(msg) {
  h.innerHTML = renderFinishInfo(msg);
  return h;
}
// A settled fallback turn: finishReason present ⇒ the finish bar renders.
const settled = (reason, kind) => ({
  role: 'assistant', content: 'answer', _turnStatus: 'completed',
  finishReason: 'stop', toolRounds: [],
  fallbackModel: 'kimi-k3', fallbackFrom: 'claude-opus-5',
  fallbackReason: reason, fallbackKind: kind,
});

// ── 1. Visible cause on the owner's exact payload ──
render(settled(REASON_502, 'upstream_error'));
const causeEl = h.querySelector('.fb-cause');
check('settled_bar_renders', h.querySelector('.message-finish') !== null);
check('settled_names_fallback_model', h.textContent.indexOf('kimi-k3') !== -1);
check('cause_tag_exists', causeEl !== null);
check('cause_is_visible_text_not_only_title',
  !!causeEl && causeEl.textContent.indexOf('502') !== -1);
check('cause_has_kind_label',
  !!h.querySelector('.fb-cause-kind') &&
  h.querySelector('.fb-cause-kind').textContent === 'Upstream error');
const detEl = h.querySelector('.fb-cause-detail');
check('cause_detail_exists', detEl !== null);
// ── 2. Distillation + inertness ──
check('cause_distills_html_page_to_signal',
  !!detEl && detEl.textContent.indexOf('502 Bad Gateway') !== -1 &&
  detEl.textContent.indexOf('openresty') !== -1);
check('cause_carries_no_markup_source',
  !!detEl && detEl.textContent.indexOf('<') === -1 &&
  detEl.textContent.indexOf('>') === -1);
check('cause_markup_is_inert',
  h.querySelector('h1') === null && h.querySelector('center') === null);
check('cause_dedupes_title_and_h1',
  !!detEl && detEl.textContent.split('502 Bad Gateway').length - 1 === 1);
check('cause_drops_duplicate_kind_prefix',
  !!detEl && detEl.textContent.indexOf('upstream_error:') === -1);
check('cause_keeps_status_prefix',
  !!detEl && detEl.textContent.indexOf('API HTTP 502') !== -1);
check('cause_detail_is_bounded',
  !!detEl && detEl.textContent.length <= 161);
// ── 3. Verbatim preserved in title ──
check('verbatim_cause_kept_in_title',
  !!causeEl && (causeEl.getAttribute('title') || '').indexOf('openresty') !== -1);
// ── 4. Plain reasons pass through untouched ──
render(settled('ratelimit: 429 Too Many Requests (retry in 30s)', 'ratelimit'));
const plainDet = h.querySelector('.fb-cause-detail');
check('plain_reason_verbatim',
  !!plainDet && plainDet.textContent === '429 Too Many Requests (retry in 30s)');
check('plain_reason_kind_label',
  !!h.querySelector('.fb-cause-kind') &&
  h.querySelector('.fb-cause-kind').textContent === 'Rate limited');
render(settled('ratelimit: budget < 100 tokens remaining', 'ratelimit'));
check('lone_angle_bracket_not_markup',
  !!h.querySelector('.fb-cause-detail') &&
  h.querySelector('.fb-cause-detail').textContent === 'budget < 100 tokens remaining');
// Unknown kind → raw kind, never blank.
render(settled('weird_kind: something odd', 'weird_kind'));
check('unknown_kind_label_is_raw_kind',
  !!h.querySelector('.fb-cause-kind') &&
  h.querySelector('.fb-cause-kind').textContent === 'weird_kind');
// ── 5. No fallback → no cause tag at all ──
h.innerHTML = renderFinishInfo(
  { role: 'assistant', content: 'x', _turnStatus: 'completed',
    finishReason: 'stop', toolRounds: [] });
check('no_cause_tag_without_fallback', h.querySelector('.fb-cause') === null);
// A fallback with NO reason at all must not emit an empty cause chip.
h.innerHTML = renderFinishInfo({ role: 'assistant', content: 'x',
  _turnStatus: 'completed', finishReason: 'stop', toolRounds: [],
  fallbackModel: 'kimi-k3', fallbackFrom: 'claude-opus-5' });
check('no_empty_cause_tag_without_reason', h.querySelector('.fb-cause') === null);

console.log(out.join('\n'));
"""


def _run(error_envelope: str = ERROR_ENVELOPE,
         finish_info: str = FINISH_INFO) -> str:
    harness = os.path.join(HERE, '_finish_cause_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, TYPED_ERRORS, error_envelope,
             finish_info, REASON_502],
            capture_output=True, text=True, timeout=90)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_settled_finish_tag_renders_fallback_cause_visibly():
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'settled fallback-cause failures:\n' + output
    assert output.count('PASS') >= 20, f'expected >=20 PASS, got:\n{output}'
    print(output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_visible_cause_tag_is_load_bearing(tmp_path):
    """NEUTER: drop the .fb-cause push from a COPY of finish_info.js — i.e.
    restore the exact pre-fix behaviour where the cause lived ONLY in the
    title. The visible-cause assertions MUST flip red."""
    src = open(FINISH_INFO).read()
    m = re.search(r'    if \(_fb\.hasCause\) \{\n.*?\n    \}\n', src, re.S)
    assert m, 'fb-cause push block not found — source-scan guard stale'
    copy = tmp_path / 'finish_info.js'
    copy.write_text(src[:m.start()] + src[m.end():])
    output = _run(finish_info=str(copy))
    assert 'FAIL cause_tag_exists' in output, (
        'neutered cause tag must fail cause_tag_exists:\n' + output)
    assert 'FAIL cause_is_visible_text_not_only_title' in output, (
        'neutered cause tag must fail the visible-text assertion:\n' + output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_distillation_is_load_bearing(tmp_path):
    """NEUTER: make distillFallbackDetail a pass-through in a COPY of
    error_envelope.js. The SETTLED tag's raw-markup assertion MUST flip red —
    which also proves finish_info.js really routes through the shared
    formatter instead of a private copy."""
    src = open(ERROR_ENVELOPE).read()
    m = re.search(r'function distillFallbackDetail\(detail\) \{\n', src)
    assert m, 'distillFallbackDetail not found — source-scan guard stale'
    copy = tmp_path / 'error_envelope.js'
    copy.write_text(src[:m.end()] + '  return detail;\n' + src[m.end():])
    output = _run(error_envelope=str(copy))
    assert 'FAIL cause_carries_no_markup_source' in output, (
        'neutered distiller must fail cause_carries_no_markup_source '
        '(if it stayed green, finish_info is NOT using the shared '
        'formatter):\n' + output)


def test_single_cause_formatter_no_duplicate_stripping():
    """Charter #2: ONE normalization throat. The cause formatter lives in
    core/error_envelope.js; neither renderer may carry a private copy of the
    stripping/label logic, or the two surfaces drift."""
    env = open(ERROR_ENVELOPE).read()
    for sym in ('function distillFallbackDetail',
                'function fallbackKindLabel',
                'function fallbackCauseParts'):
        assert sym in env, f'core/error_envelope.js must define {sym}'
    assert 'runtimeScope.fallbackCauseParts' in env, (
        'fallbackCauseParts must be exported for cross-file use')

    fi = open(FINISH_INFO).read()
    assert 'fallbackCauseParts(msg)' in fi, (
        'finish_info.js must consume the shared formatter')
    assert '<[^>]*>' not in fi, (
        'finish_info.js contains a private tag-stripping regex — cause '
        'formatting belongs in core/error_envelope.js only')

    css = open(os.path.join(ROOT, 'static', 'styles.css')).read()
    for sel in ('.fb-cause', '.fb-cause-kind', '.fb-cause-detail'):
        assert sel in css, f'styles.css must style {sel}'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_finish_info_survives_without_error_envelope():
    """finish_info.js is required to degrade gracefully when evaled WITHOUT
    its bundle siblings (see test_frontend_finish_tag_fallback.py). A settled
    fallback message must therefore not throw when fallbackCauseParts is
    absent — it renders the bar without the cause chip rather than dying."""
    harness = os.path.join(HERE, '_finish_cause_solo_harness.js')
    with open(harness, 'w') as f:
        f.write(r"""
const fs = require('fs');
global.window = {}; global.document = {};
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');
global.t = (k, o) => { o = o || {}; let s = k;
  for (const kk in o) s = s.split('{' + kk + '}').join(o[kk]); return s; };
global.Icon = (n) => '<ICON:' + n + '>';
global.formatCny = (n) => '' + n;
global.calcCostCny = () => 0; global.calcCost = () => 0;
eval(fs.readFileSync(process.argv[2], 'utf8'));   // finish_info.js ALONE
const html = renderFinishInfo({ role: 'assistant', content: 'x',
  _turnStatus: 'completed', finishReason: 'stop', toolRounds: [],
  fallbackModel: 'kimi-k3',
  fallbackFrom: 'claude-opus-5', fallbackReason: 'upstream_error: boom',
  fallbackKind: 'upstream_error' });
console.log(html.indexOf('kimi-k3') !== -1 ? 'PASS solo_render_ok'
                                           : 'FAIL solo_render_ok');
""")
    try:
        proc = subprocess.run(['node', harness, FINISH_INFO],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, (
        'finish_info.js threw when evaled without error_envelope.js '
        f'(a settled fallback must not kill the finish bar):\n{proc.stderr}')
    assert 'PASS solo_render_ok' in proc.stdout, proc.stdout


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
