"""tests/test_frontend_render_translation_decoupled.py — byte-identity guard for
routing chat_render.js's translation display through the canonical model
(core/translation_model.js) + the extracted indicator (ui/translation_indicator.js).

WHY
---
Decoupling steps 2+3 removed chat_render.js's inline translation logic:

  • step 2 — the `_isCritic` content-origin branch + the `showTrans` predicate
    (body selection + assistant/critic bilingual guards) now go through
    `displayContent(msg)` (pure content-origin) + `readTranslation(msg)`.
  • step 3 — the persistent "翻译中…" spinner / streaming-preview / retry-status /
    error line moved WHOLESALE into `renderTranslateIndicator(msg, idx, opts)`
    in ui/translation_indicator.js, which reads translation state via the model.

Both must be OBSERVABLY INERT: the rendered HTML must be byte-identical to the
pre-refactor render for every shape where the historic "inversion" bug bit AND
every translate-in-progress shape the indicator handles —

  • normal user (源文 escaped, NOT 译文); user with no translation,
  • VU (`_isVirtualUser`) done + toggle-off; critic (`_isEndpointReview`) done,
  • assistant done + toggle-off + no-translation,
  • pending with a streaming partial + retry-status sub-line,
  • pending plain spinner, terminal translate-error line.

STRATEGY (differential, not golden-string)
Render each shape through the REAL shipped chat_render.js (which calls the REAL
translation_indicator.js), then through a reconstructed PRE-REFACTOR copy where
BOTH the body block and the whole indicator block are string-swapped back to
their old inline form. If steps 2+3 are inert the two HTML bodies are
byte-identical. Two controls (NC_BREAK) prove the differential can DETECT a
divergence in each swapped region — otherwise "identical" would be vacuous.

Runs the real modules under jsdom; skips cleanly without node + jsdom.

EXCEPTION (2026-07 translate-error redesign): the terminal-error branch is NO
LONGER inert by design — the extracted indicator replaced the amber pill
(.translate-loading + inline color) with a borderless retry line
(.translate-retry-line) AND moved the error check ahead of the
_translateDone===false gate (the engine marks terminal errors done=true, so the
old gate hid them entirely). The OLD reconstruction's error branch below
therefore pins the POST-redesign markup as a golden string — a non-vacuous
byte-compare, since slice() captures .translate-retry-line too — while every
other branch stays a true pre-refactor reconstruction. A NEW-only pin
(new_shows_terminal_error_after_done) locks the gate-order fix the redesign
shipped for.
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
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
TR_MODEL = os.path.join(JS_DIR, 'core', 'translation_model.js')
TR_INDICATOR = os.path.join(JS_DIR, 'ui', 'translation_indicator.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# ── step-2 body block: NEW (verbatim from chat_render.js) → OLD (pre-refactor) ──
_NEW_BODY = (
    "      if (_showTrans) {\n"
    "        mdHtml = renderMarkdown(stripNoTranslateTags(_tr.text));\n"
    "      } else if (_disp.isMarkdown) {\n"
    "        mdHtml = renderMarkdown(_disp.text);\n"
    "      } else {\n"
    "        mdHtml = escapeHtml(stripNoTranslateTags(_disp.text));\n"
    "      }"
)
_OLD_BODY = (
    "      const _isCritic = isUser && (msg._isEndpointReview || msg._isVirtualUser);\n"
    "      const showTrans = (!isUser || _isCritic)\n"
    "                        && msg.translatedContent\n"
    "                        && msg._showingTranslation !== false;\n"
    "      if (showTrans) {\n"
    "        mdHtml = renderMarkdown(stripNoTranslateTags(msg.translatedContent));\n"
    "      } else if (_isCritic) {\n"
    "        mdHtml = renderMarkdown(msg.content);\n"
    "      } else if (isUser) {\n"
    "        mdHtml = escapeHtml(stripNoTranslateTags(msg.originalContent || msg.content));\n"
    "      } else {\n"
    "        mdHtml = renderMarkdown(msg.content);\n"
    "      }"
)
_NEW_ASST = "  if (!isUser && _showTrans) {\n    const _tmAsst = _tr.model"
_OLD_ASST = ("  if (!isUser && msg.translatedContent && msg._showingTranslation !== false) {\n"
             "    const _tmAsst = msg._translateModel")

# ── step-3 indicator: NEW (the component call in chat_render) → OLD (the full
#    pre-extraction inline block it replaced). Verbatim on both sides, EXCEPT
#    the terminal-error branch: that pins the 2026-07 retry-line redesign as a
#    post-redesign golden string (see docstring EXCEPTION). ──
_NEW_IND = (
    "  if (typeof renderTranslateIndicator === 'function') {\n"
    "    body += renderTranslateIndicator(msg, idx, { segTimelineRendered: _segTimelineRendered });\n"
    "  }"
)
_OLD_IND = (
    "  if ((!isUser || (isUser && (msg._isEndpointReview || msg._isVirtualUser)))\n"
    "      && !msg.translatedContent && msg._translateDone === false) {\n"
    "    const errText = msg._translateError;\n"
    "    if (errText) {\n"
    "      const _lbl = (t('translate.failed') !== 'translate.failed') ? t('translate.failed') : 'Translation failed, click to retry';\n"
    "      body += `<div class=\"translate-retry-line\" id=\"translate-loading-${idx}\" role=\"button\" tabindex=\"0\" onclick=\"event.stopPropagation();translateMessage(${idx})\" title=\"${escapeHtml(errText)}\">`\n"
    "        + `<svg class=\"trl-icon\" width=\"12\" height=\"12\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M23 4v6h-6\"/><path d=\"M20.49 15a9 9 0 1 1-2.12-9.36L23 10\"/></svg>`\n"
    "        + `<span class=\"trl-text\">${escapeHtml(_lbl)}</span></div>`;\n"
    "    } else {\n"
    "      let statusSub = '';\n"
    "      const _benignKinds = (typeof _TRANSLATE_BENIGN_STATUS_KINDS !== 'undefined')\n"
    "        ? _TRANSLATE_BENIGN_STATUS_KINDS : new Set(['started', 'in_progress']);\n"
    "      if (msg._translateStatus && !_benignKinds.has(msg._translateStatusKind || '')) {\n"
    "        const kind = msg._translateStatusKind || '';\n"
    "        const i18nKey = kind ? `translate.retry.${kind}` : '';\n"
    "        const localized = i18nKey && typeof t === 'function' ? t(i18nKey) : '';\n"
    "        const display = (localized && localized !== i18nKey) ? localized : msg._translateStatus;\n"
    "        statusSub = `<div class=\"translate-status-sub\" title=\"${escapeHtml(msg._translateStatus)}\">\u26a0 ${escapeHtml(display)}</div>`;\n"
    "      }\n"
    "      let previewSub = '';\n"
    "      if (msg._translatePartial && !_segTimelineRendered) {\n"
    "        let _pv;\n"
    "        try { _pv = renderMarkdown(stripNoTranslateTags(msg._translatePartial)); }\n"
    "        catch (e) { _pv = escapeHtml(msg._translatePartial); }\n"
    "        previewSub = `<div class=\"translate-preview\"><div class=\"md-content\">${_pv}</div><span class=\"translate-caret\"></span></div>`;\n"
    "      }\n"
    "      const _hasPreview = previewSub ? ' has-preview' : '';\n"
    "      const _segTlAttr = _segTimelineRendered ? ' data-seg-timeline=\"1\"' : '';\n"
    "      body += `<div class=\"translate-loading${_hasPreview}\" id=\"translate-loading-${idx}\"${_segTlAttr}>`\n"
    "        + `<div class=\"translate-loading-head\"><span class=\"translate-spinner\"></span>`\n"
    "        + `<span class=\"translate-loading-label\">${t('translate.translatingToCN')}</span></div>`\n"
    "        + `${statusSub}${previewSub}</div>`;\n"
    "    }\n"
    "  }"
)


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[5];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.setTimeout = win.setTimeout = () => 0;
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const _conv = { id: 'c', messages: [], activeTaskId: null };
win.activeStreams = global.activeStreams = new Map();
win.conversations = global.conversations = [_conv];
win.activeConvId = global.activeConvId = 'c';
win.getActiveConv = global.getActiveConv = () => _conv;
win.t = global.t = (k) => k;
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = () => [];
win.renderToolRoundsHTML = global.renderToolRoundsHTML = () => '';
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML = () => '';
const _noop = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderBranchZone','renderTurnCtxNote',
  'renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_prefetchConvCosts','_prefetchConvFileChanges',
  '_stampFreshness','buildTurnNav','calcCostCny','renderChat',
]) { if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; } }
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<i>u</i>';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<i>w</i>';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<i>p</i>';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<i>c</i>';
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;

// Load model + indicator in BOTH variants so the OLD inline path and the NEW
// component path are compared on equal footing.
(0, eval)(fs.readFileSync(process.argv[6], 'utf8'));  // core/translation_model.js
(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // safe_html.js
(0, eval)(fs.readFileSync(process.argv[8], 'utf8'));  // ui/translation_indicator.js

const NEW_BODY = process.env.NEW_BODY, OLD_BODY = process.env.OLD_BODY;
const NEW_ASST = process.env.NEW_ASST, OLD_ASST = process.env.OLD_ASST;
const NEW_IND = process.env.NEW_IND, OLD_IND = process.env.OLD_IND;
const MODE = process.argv[7] || '';   // '', 'nc_body', 'nc_ind'

const CHAT = fs.readFileSync(process.argv[2], 'utf8');

// Load the SHIPPED render into `renderMessage`.
(0, eval)(CHAT);
const renderNew = renderMessage;

// Reconstruct the PRE-REFACTOR render by swapping NEW→OLD blocks.
let oldSrc = CHAT;
for (const [nm, N, O] of [['body', NEW_BODY, OLD_BODY], ['asst', NEW_ASST, OLD_ASST], ['ind', NEW_IND, OLD_IND]]) {
  if (oldSrc.indexOf(N) === -1) { check('old_anchor_found__' + nm, false); }
  else { check('old_anchor_found__' + nm, true); oldSrc = oldSrc.replace(N, O); }
}
if (MODE === 'nc_body') {
  oldSrc = oldSrc.replace('mdHtml = renderMarkdown(msg.content);',
                          'mdHtml = renderMarkdown("XX" + msg.content);');
}
if (MODE === 'nc_ind') {
  // Corrupt the reconstructed OLD indicator so the pending shapes diverge.
  oldSrc = oldSrc.replace("t('translate.translatingToCN')", "'ZZ' + t('translate.translatingToCN')");
}
const renderOld = (0, eval)('(function(){ ' + oldSrc + '\n; return renderMessage; })')();

// Extract the primary .md-content body + any bilingual block + the whole
// translate indicator from the rendered HTML. The spinner/preview line is
// .translate-loading; the 2026-07 redesign renders the terminal-error line as
// .translate-retry-line (both keep the #translate-loading-<idx> id) — select
// BOTH so the error shape's identity check is non-vacuous.
function slice(html) {
  const frag = win.document.createElement('div');
  frag.innerHTML = html;
  const md = frag.querySelector('.md-content');
  const bi = Array.from(frag.querySelectorAll('.bilingual-block')).map(e => e.outerHTML).join('|');
  const ind = Array.from(frag.querySelectorAll('.translate-loading, .translate-retry-line')).map(e => e.outerHTML).join('|');
  return (md ? md.outerHTML : '<none>') + '#' + bi + '#' + ind;
}

const SHAPES = {
  user_plain:     { role:'user', _msgId:'u', content:'English for model', originalContent:'中文源文' },
  user_notrans:   { role:'user', _msgId:'u2', content:'just typed' },
  vu_done:        { role:'user', _isVirtualUser:true, _msgId:'v', content:'VU original',
                    translatedContent:'VU译文', _translatedCache:'VU译文',
                    _showingTranslation:true, _translateModel:'m1', _translateDone:true },
  critic_done:    { role:'user', _isEndpointReview:true, _msgId:'c', content:'critic original',
                    translatedContent:'评审译文', _translatedCache:'评审译文',
                    _showingTranslation:true, _translateModel:'m1', _translateDone:true },
  asst_done:      { role:'assistant', _msgId:'a', content:'assistant reply',
                    translatedContent:'助手译文', _translatedCache:'助手译文',
                    _showingTranslation:true, _translateModel:'m1', _translateDone:true },
  asst_toggleoff: { role:'assistant', _msgId:'a2', content:'assistant reply',
                    translatedContent:'助手译文', _translatedCache:'助手译文',
                    _showingTranslation:false, _translateModel:'m1', _translateDone:true },
  asst_notrans:   { role:'assistant', _msgId:'a3', content:'plain reply' },
  vu_toggleoff:   { role:'user', _isVirtualUser:true, _msgId:'v2', content:'VU original',
                    translatedContent:'VU译文', _showingTranslation:false, _translateDone:true },
  // ── indicator (step 3) shapes: mid-translation, no 译文 yet ──
  pending_spinner: { role:'assistant', _msgId:'p1', content:'body', _translateDone:false },
  pending_partial: { role:'assistant', _msgId:'p2', content:'body',
                     _translateDone:false, _translatePartial:'部分译文…' },
  pending_status:  { role:'assistant', _msgId:'p3', content:'body', _translateDone:false,
                     _translatePartial:'部分…', _translateStatus:'rate limited', _translateStatusKind:'rate_limited' },
  pending_error:   { role:'assistant', _msgId:'p4', content:'body',
                     _translateDone:false, _translateError:'boom' },
  vu_pending:      { role:'user', _isVirtualUser:true, _msgId:'p5', content:'VU body', _translateDone:false,
                     _translatePartial:'VU部分…' },
};

let anyDiff = false;
for (const [name, shape] of Object.entries(SHAPES)) {
  const a = slice(renderNew(JSON.parse(JSON.stringify(shape)), 1));
  const b = slice(renderOld(JSON.parse(JSON.stringify(shape)), 1));
  const same = (a === b);
  if (!same) anyDiff = true;
  check('identical__' + name, same);
  if (!same && MODE === '') {
    out.push('DIFF ' + name + '\n  NEW=' + a + '\n  OLD=' + b);
  }
}
check('nc_detected_a_diff', MODE === '' ? true : anyDiff);

// NEW-only behavioral pin (the redesign's gate-order fix): a terminal error on
// a done=true message — the marker the engine's _applyTranslationError sets —
// MUST still render the retry line. The old inline gate (_translateDone===false)
// hid it entirely; the OLD reconstruction cannot express this shape, so this is
// asserted on the NEW render only.
{
  const _doneErr = renderNew({ role:'assistant', _msgId:'p6', content:'body',
                               _translateDone:true, _translateError:'boom' }, 1);
  check('new_shows_terminal_error_after_done', _doneErr.indexOf('translate-retry-line') !== -1);
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(mode: str = '') -> str:
    harness = os.path.join(HERE, f'_render_decouple_harness_{mode or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    env = dict(os.environ)
    env.update({'NEW_BODY': _NEW_BODY, 'OLD_BODY': _OLD_BODY,
                'NEW_ASST': _NEW_ASST, 'OLD_ASST': _OLD_ASST,
                'NEW_IND': _NEW_IND, 'OLD_IND': _OLD_IND})
    try:
        # argv: [2]=chat_render [3]=escape_html [4]=safe_html [5]=ROOT
        #       [6]=translation_model [7]=mode [8]=translation_indicator
        proc = subprocess.run(
            ['node', harness, CHAT_RENDER, ESCAPE_HTML, SAFE_HTML, ROOT, TR_MODEL, mode, TR_INDICATOR],
            capture_output=True, text=True, timeout=60, env=env,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_render_translation_decoupled_is_byte_identical():
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL') or ln.startswith('DIFF')]
    assert not fails, 'steps 2+3 render diverged from pre-refactor:\n' + output
    assert output.count('PASS') >= 15, f'expected >=15 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_differential_harness_detects_body_divergence():
    """Control: corrupt the reconstructed OLD body path → harness MUST see a diff."""
    output = _run('nc_body')
    assert 'PASS nc_detected_a_diff' in output, (
        'nc_body did not surface a divergence — the body differential is vacuous:\n' + output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_differential_harness_detects_indicator_divergence():
    """Control: corrupt the reconstructed OLD indicator path → harness MUST see
    a diff on the pending shapes, proving the indicator differential is real."""
    output = _run('nc_ind')
    assert 'PASS nc_detected_a_diff' in output, (
        'nc_ind did not surface a divergence — the indicator differential is vacuous:\n' + output)
