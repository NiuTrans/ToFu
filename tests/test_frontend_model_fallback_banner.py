#!/usr/bin/env python3
"""Frontend: model-fallback EARLY in-bubble banner.

Companion to tests/test_model_fallback_early_notify.py (backend). Pins the
frontend half of the contract:

  1. SSE dispatch: a ``model_fallback`` event stamps the SAME field names the
     settled message carries (fallbackModel/From/Reason/Kind) onto the live
     assistantMsg — so the streaming banner and the settled finish-tag agree.
  2. Cold reload: the ``state`` snapshot sidecar adoption re-stamps the
     fields (backend folds them in build_fresh_state_snapshot).
  3. Streaming render: updateStreamingUI paints a persistent in-bubble
     ``.fallback-banner`` into a dedicated ``data-zone="fallback"`` at the
     TOP of the bubble — NOT a toast — keyed on data-fb-key so unchanged
     frames don't rewrite the DOM.
  4. Frame-arg plumbing: _streamFrameArg / stream_lifecycle payloads forward
     the fields (synthetic payloads enumerate fields explicitly; an
     unforwarded field would never reach updateStreamingUI).
  5. Source-scan guards: dispatch branch, handler, i18n keys, CSS class.

NEUTER discipline:
  * test_nc_banner_zone_render_is_load_bearing — delete the fb-zone render
    block from a COPY of streaming_ui.js → the banner never paints (Part 3
    assertions flip red).
  * test_nc_dispatch_branch_is_load_bearing — delete the model_fallback
    dispatch branch from a COPY of sse_pipeline.js → no fields are stamped.

Run directly:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_frontend_model_fallback_banner.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS = os.path.join(ROOT, 'static', 'js')


def _node_deps_available() -> bool:
    return (bool(shutil.which('node'))
            and os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom')))


# ═══════════════════════════════════════════════════════════════════════════
# Part A + B (jsdom): banner render + SSE dispatch
# ═══════════════════════════════════════════════════════════════════════════
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const STREAMING_UI = process.argv[2];          // real or NEUTERED copy
const SSE_PIPELINE = process.argv[4];          // real or NEUTERED copy
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' +
  '<div id="chatInner"></div><div id="streaming-body"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.setTimeout = win.setTimeout = (fn) => 0;
global.clearTimeout = win.clearTimeout = () => {};
global.setInterval = win.setInterval = () => 0;
global.clearInterval = win.clearInterval = () => {};
if (typeof global.requestAnimationFrame !== 'function') {
  global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;
}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Shared stubs ──
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');
win.t = global.t = (key, params) => {
  let s = key;
  if (params) for (const k in params) s = s.replace('{' + k + '}', params[k]);
  return s;
};
win.Icon = global.Icon = (name, size) => '<ICON:' + name + '>';
win.renderMarkdown = global.renderMarkdown = (s) => s;
win.isNearBottom = global.isNearBottom = () => false;
win.scrollToBottom = global.scrollToBottom = () => {};
// Property-only builders from tool_rounds.js / finish_info.js (not loaded).
win.renderMcpLoginHintHtml = global.renderMcpLoginHintHtml = () => '';
win.renderTurnProvenanceHtml = global.renderTurnProvenanceHtml = () => '';
win.renderPreferenceLearnedHtml = global.renderPreferenceLearnedHtml = () => '';
win._buildSwarmInboxChipsHTML = global._buildSwarmInboxChipsHTML = () => '';
win._fcFingerprint = global._fcFingerprint = () => 'fp';
win._extractFileChangesFromRoundsAsync =
  global._extractFileChangesFromRoundsAsync = () => Promise.resolve([]);

/* State globals must EXIST before streaming_ui.js is even eval'd: current
 * updateStreamingUI reads `conversations`/`activeConvId` on every repaint
 * (the stalled-card block) — a `let` declared LATER in this script is a TDZ
 * binding, and `typeof x` on a TDZ binding throws. In the bundle,
 * core/conversations.js loads before ui/*, so declare first, then eval. */
let conversations = [];
let activeConvId = null;

eval(fs.readFileSync(STREAMING_UI, 'utf8'));   // ui/streaming_ui.js

// ── Part A: banner zone render ──
const body = document.getElementById('streaming-body');
check('builder_empty_without_fallbackModel',
  renderModelFallbackBannerHtml({ content: 'x' }) === '');
const _bHtml = renderModelFallbackBannerHtml({
  fallbackModel: 'aws.fb', fallbackFrom: 'aws.primary',
  fallbackReason: 'upstream_5xx: boom', fallbackKind: 'upstream_5xx' });
check('builder_has_banner_class', _bHtml.indexOf('fallback-banner') !== -1);
check('builder_shows_both_models',
  _bHtml.indexOf('aws.primary') !== -1 && _bHtml.indexOf('aws.fb') !== -1);
check('builder_tip_carries_reason', _bHtml.indexOf('boom') !== -1);
check('builder_escapes_html',
  renderModelFallbackBannerHtml({ fallbackModel: '<b>x</b>',
    fallbackFrom: 'a<script>' }).indexOf('<script>') === -1);

updateStreamingUI({ content: 'partial answer', thinking: '', toolRounds: [],
  fallbackModel: 'aws.fb', fallbackFrom: 'aws.primary',
  fallbackReason: 'upstream_5xx: boom', fallbackKind: 'upstream_5xx' });
const fbZone = body.querySelector('[data-zone="fallback"]');
check('fallback_zone_exists', !!fbZone);
check('fallback_zone_is_first_zone',
  body.firstElementChild === fbZone);
check('banner_painted_in_bubble',
  !!fbZone && fbZone.querySelector('.fallback-banner') !== null);
check('banner_names_both_models',
  !!fbZone && fbZone.textContent.indexOf('aws.primary') !== -1 &&
  fbZone.textContent.indexOf('aws.fb') !== -1);
check('banner_title_has_reason',
  !!fbZone && (fbZone.querySelector('.fallback-banner') || {})
    .getAttribute &&
  fbZone.querySelector('.fallback-banner').getAttribute('title')
    .indexOf('boom') !== -1);
const _paintedHtml = fbZone ? fbZone.innerHTML : '';
updateStreamingUI({ content: 'partial answer grows', thinking: '', toolRounds: [],
  fallbackModel: 'aws.fb', fallbackFrom: 'aws.primary',
  fallbackReason: 'upstream_5xx: boom', fallbackKind: 'upstream_5xx' });
check('unchanged_fields_no_dom_rewrite',
  !!fbZone && fbZone.innerHTML === _paintedHtml);
updateStreamingUI({ content: 'partial answer grows more', thinking: '', toolRounds: [],
  fallbackModel: 'aws.fb2', fallbackFrom: 'aws.primary',
  fallbackReason: 'x', fallbackKind: 'y' });
check('changed_model_repaints',
  !!fbZone && fbZone.textContent.indexOf('aws.fb2') !== -1);
updateStreamingUI({ content: 'final', thinking: '', toolRounds: [] });
check('no_fallback_zone_cleared', !!fbZone && fbZone.innerHTML === '');

// ── Part B: SSE dispatch (model_fallback event + state sidecar) ──
// (conversations / activeConvId were declared BEFORE the streaming_ui eval —
// see the TDZ note there; here we only bind them onto win.)
win.conversations = conversations;
Object.defineProperty(win, 'activeConvId',
  { get: () => activeConvId, set: v => activeConvId = v, configurable: true });
win.streamBufs = new Map(); global.streamBufs = win.streamBufs;
win.activeStreams = new Map(); global.activeStreams = win.activeStreams;
const calls = {};
function spy(name) { calls[name] = 0; return (...a) => { calls[name]++; }; }
for (const n of ['twUpdate','twStart','twStop','finishStream','renderChat',
  'renderConversationList','buildTurnNav','saveConversations','updateContextBar',
  '_forceScrollToBottom','showToast','debugLog','showMessagesInDebug',
  '_handleAutopilotVuEvent','_retriggerHgTranslations','_streamTimerTouch',
  '_reportClientError','_seedStreamTimerStart']) {
  win[n] = global[n] = spy(n);
}
win._streamingBubbleHTML = global._streamingBubbleHTML = () => '<div id="streaming-msg"></div>';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<svg></svg>';
win.ConvView = global.ConvView = { finalizeStreaming: spy('finalizeStreaming'),
  removeMessage: spy('removeMessage'), replaceAll: spy('replaceAll'),
  upsertMessage: spy('upsertMessage'), startStreaming: spy('startStreaming') };
win.Artifacts = global.Artifacts = { attachToMessage: spy('attachToMessage') };
win.setStreamPhase = global.setStreamPhase = spy('setStreamPhase');
win.getStreamSession = global.getStreamSession = () => ({ phase: null });
win.streamSessions = global.streamSessions = new Map();
win.flashGaugeForArchive = global.flashGaugeForArchive = spy('flashGaugeForArchive');
win.Api = global.Api = { project: { status: () => Promise.resolve(null) } };
win.getActiveConv = global.getActiveConv =
  () => conversations.find(c => c.id === activeConvId);
win.errorEnvelopeMessage = global.errorEnvelopeMessage = (e) =>
  (e && typeof e === 'object' ? (e.message || e.detail || '') : '');
win._debugCache = global._debugCache = {};
win._applyProjectData = global._applyProjectData = spy('_applyProjectData');
win.syncConversationToServer = global.syncConversationToServer = spy('syncConversationToServer');
win._autoTranslateHumanGuidance = global._autoTranslateHumanGuidance = spy('_autoTranslateHumanGuidance');
global.autoTranslate = win.autoTranslate = false;
win.convAutoTranslate = global.convAutoTranslate = (c) => false;
let _idc = 0;
win._ensureMsgId = global._ensureMsgId = (m) => { if (m && !m._msgId) m._msgId = 'mid-' + (++_idc); return m; };
win._resolveAssistantById = global._resolveAssistantById = (conv, id) =>
  (conv && conv.messages.find(m => m._msgId === id)) || null;
win._hasRealToolRound = global._hasRealToolRound = (m) => {
  const rounds = m && m.toolRounds;
  if (!Array.isArray(rounds)) return false;
  for (const r of rounds) {
    if (!r || typeof r !== 'object') continue;
    if (r.status === 'done' || r.toolContent) return true;
    if (Array.isArray(r.results) && r.results.length) return true;
  }
  return false;
};
win._spliceInjectRow = global._spliceInjectRow = (arr, row) => { arr.push(row); return arr; };

eval(fs.readFileSync(process.argv[5], 'utf8'));   // ui/stream_reducer.js
eval(fs.readFileSync(process.argv[6], 'utf8'));   // ui/sse_handlers_tool.js
eval(fs.readFileSync(process.argv[7], 'utf8'));   // ui/sse_handlers_swarm.js
eval(fs.readFileSync(process.argv[8], 'utf8'));   // ui/sse_handlers_io.js
eval(fs.readFileSync(process.argv[9], 'utf8'));   // ui/sse_handlers_misc.js
eval(fs.readFileSync(process.argv[10], 'utf8'));  // ui/sse_handlers_lifecycle.js
eval(fs.readFileSync(SSE_PIPELINE, 'utf8'));      // ui/sse_pipeline.js

const T = win.__sse_test__;
check('seam_exposed', !!(T && T.dispatchSSEEvent && T.makeCtx));
function setup() {
  conversations.length = 0;
  const am = { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'mid-w' };
  conversations.push({ id: 'c1', messages: [{ role: 'user', content: 'hi' }, am] });
  activeConvId = 'c1';
  const ctx = T.makeCtx({ convId: 'c1', taskId: 't1',
    stream: { controller: { signal: { aborted: false } } }, assistantMsg: am });
  return { am, ctx };
}
function line(obj) { return 'data: ' + JSON.stringify(obj); }

{
  const { am, ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'model_fallback',
    fallbackModel: 'aws.fb', fallbackFrom: 'aws.primary',
    fallbackReason: 'upstream_5xx: boom', fallbackKind: 'upstream_5xx' }), ctx);
  check('dispatch_stamps_fallbackModel', am.fallbackModel === 'aws.fb');
  check('dispatch_stamps_fallbackFrom', am.fallbackFrom === 'aws.primary');
  check('dispatch_stamps_reason_kind',
    am.fallbackReason === 'upstream_5xx: boom' && am.fallbackKind === 'upstream_5xx');
  check('dispatch_triggers_repaint', calls['twUpdate'] >= 1);
}
{
  const { am, ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'state', content: 'so far', thinking: '',
    fallbackModel: 'aws.fb', fallbackFrom: 'aws.primary',
    fallbackReason: 'r', fallbackKind: 'k' }), ctx);
  check('state_adopts_fallback_fields',
    am.fallbackModel === 'aws.fb' && am.fallbackFrom === 'aws.primary' &&
    am.fallbackReason === 'r' && am.fallbackKind === 'k');
}

console.log(out.join('\n'));
"""


def _run_harness(streaming_ui_path: str, sse_pipeline_path: str) -> str:
    harness = os.path.join(HERE, '_mfb_banner_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, streaming_ui_path, ROOT, sse_pipeline_path,
             os.path.join(JS, 'ui', 'stream_reducer.js'),
             os.path.join(JS, 'ui', 'sse_handlers_tool.js'),
             os.path.join(JS, 'ui', 'sse_handlers_swarm.js'),
             os.path.join(JS, 'ui', 'sse_handlers_io.js'),
             os.path.join(JS, 'ui', 'sse_handlers_misc.js'),
             os.path.join(JS, 'ui', 'sse_handlers_lifecycle.js')],
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
def test_model_fallback_banner_end_to_end():
    output = _run_harness(os.path.join(JS, 'ui', 'streaming_ui.js'),
                          os.path.join(JS, 'ui', 'sse_pipeline.js'))
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'model-fallback banner failures:\n' + output
    # Exact assertion floor (P0-2 discipline): the harness emits exactly 19
    # checks — a silently dropped check is as red as a failing one.
    assert output.count('PASS') == 19, f'expected exactly 19 PASS, got:\n{output}'
    print(output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_banner_zone_render_is_load_bearing(tmp_path):
    """NEUTER: delete the fb-zone render block from a COPY of streaming_ui.js —
    the Part-A banner assertions MUST flip red (proves the block is what
    paints the banner, not some incidental DOM write)."""
    src = open(os.path.join(JS, 'ui', 'streaming_ui.js')).read()
    # Target the PAINT block specifically — the builder's own comment also
    # starts with '/* ★ Model-fallback banner', so an unanchored prefix match
    # would delete the builder + half of updateStreamingUI (broken syntax
    # instead of a missing feature).
    m = re.search(r'/\* ★ Model-fallback banner: painted.*?\n  \}\n', src, re.S)
    assert m, 'fb-zone render block not found — source-scan guard stale'
    neutered = src[:m.start()] + src[m.end():]
    copy = tmp_path / 'streaming_ui.js'
    copy.write_text(neutered)
    output = _run_harness(str(copy), os.path.join(JS, 'ui', 'sse_pipeline.js'))
    assert 'FAIL banner_painted_in_bubble' in output, (
        'neutered render must fail banner_painted_in_bubble:\n' + output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_dispatch_branch_is_load_bearing(tmp_path):
    """NEUTER: delete the model_fallback dispatch branch from a COPY of
    sse_pipeline.js — the event becomes a no-op and the stamp assertions
    MUST flip red."""
    src = open(os.path.join(JS, 'ui', 'sse_pipeline.js')).read()
    m = re.search(r'\} else if \(ev\.type === "model_fallback"\) \{\n.*?\n    \}', src, re.S)
    assert m, 'model_fallback dispatch branch not found — source-scan guard stale'
    # The match consumes the branch's OWN closing `    }` — the replacement
    # must hand it back or the next else-if dangles (SyntaxError, which is a
    # broken neuter, not a broken feature).
    neutered = src[:m.start()] + '} else if (false) {\n    }' + src[m.end():]
    copy = tmp_path / 'sse_pipeline.js'
    copy.write_text(neutered)
    output = _run_harness(os.path.join(JS, 'ui', 'streaming_ui.js'), str(copy))
    assert 'FAIL dispatch_stamps_fallbackModel' in output, (
        'neutered dispatch must fail dispatch_stamps_fallbackModel:\n' + output)


# ═══════════════════════════════════════════════════════════════════════════
# Part C: source-scan guards (load-bearing seams the harness can't reach)
# ═══════════════════════════════════════════════════════════════════════════
def test_source_scan_contract():
    pipeline = open(os.path.join(JS, 'ui', 'sse_pipeline.js')).read()
    assert 'ev.type === "model_fallback"' in pipeline, (
        'sse_pipeline.js must dispatch the model_fallback event')
    assert 'ev.fallbackModel' in pipeline, (
        'sse_pipeline.js state handler must adopt the fallback sidecar')

    misc = open(os.path.join(JS, 'ui', 'sse_handlers_misc.js')).read()
    assert 'function _handleModelFallback' in misc, (
        'sse_handlers_misc.js must define _handleModelFallback')
    for field in ('fallbackModel', 'fallbackFrom', 'fallbackReason', 'fallbackKind'):
        assert f'assistantMsg.{field}' in misc, (
            f'_handleModelFallback must stamp assistantMsg.{field}')

    sui = open(os.path.join(JS, 'ui', 'streaming_ui.js')).read()
    assert 'data-zone="fallback"' in sui, (
        '_ensureStreamZones must seed the fallback zone')
    assert 'data-fb-key' in sui, (
        'updateStreamingUI must fingerprint-gate the banner repaint')
    assert 'function renderModelFallbackBannerHtml' in sui

    hst = open(os.path.join(JS, 'core', 'health_stream_timer.js')).read()
    assert 'fallbackModel' in hst, (
        '_streamFrameArg must forward the fallback fields (synthetic payload)')

    sl = open(os.path.join(JS, 'ui', 'stream_lifecycle.js')).read()
    assert sl.count('fallbackModel') >= 2, (
        'stream_lifecycle synthetic payloads must forward fallbackModel '
        '(bubble rebuild + deferred repaint)')

    i18n = open(os.path.join(JS, 'i18n.js')).read()
    for key in ('stream.fallback.banner', 'stream.fallback.bannerTip'):
        assert key in i18n, f'i18n key {key} missing'

    css = open(os.path.join(ROOT, 'static', 'styles.css')).read()
    assert '.fallback-banner' in css, 'styles.css must style .fallback-banner'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
