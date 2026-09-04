"""Manual-compaction closure and context-gauge contracts.

The retained context-bar owner is exercised with small Node harnesses: idle
guarding, authoritative Turn hydration, one terminal toast, live summary
progress, and post-compaction usage selection. The typed ConversationSurface
suite separately owns compaction-card rendering; this file only pins the
adapter's request-render and gauge-refresh seams.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


def _node_available() -> bool:
    return bool(shutil.which('node'))


# ── Harness for context-bar.js: gauge fallback + idle guard + closure ──
_CTXBAR_HARNESS = r"""
const fs = require('fs');
global.window = global;

// ── Minimal DOM stub (context-bar builds nodes but the IIFE tolerates a
//    missing .chat-wrapper by returning null from _ensureBar). ──
function _mkEl() {
  const el = {
    _children: [], classList: { add(){}, remove(){}, toggle(){} },
    dataset: {}, style: { setProperty(){} }, attributes: {},
    setAttribute(k,v){ this.attributes[k]=v; }, getAttribute(k){ return this.attributes[k]; },
    appendChild(c){ this._children.push(c); return c; }, removeChild(){},
    querySelector(){ return _mkEl(); }, querySelectorAll(){ return []; },
    addEventListener(){}, removeEventListener(){}, remove(){},
    getBoundingClientRect(){ return { left:0, right:0, top:0, bottom:0 }; },
    get isConnected(){ return true; }, set innerHTML(v){}, get innerHTML(){ return ''; },
    set textContent(v){}, get textContent(){ return ''; },
  };
  return el;
}
global.document = {
  getElementById(){ return null; },
  querySelector(){ return null; },   // no .chat-wrapper → _ensureBar returns null (fine)
  createElement(){ return _mkEl(); },
  body: _mkEl(),
  addEventListener(){}, removeEventListener(){},
  readyState: 'complete',
};
global.requestAnimationFrame = (fn) => { fn(); return 0; };
global.setTimeout = (fn) => { return 0; };   // don't auto-fire (avoid popover close loops)
global.clearTimeout = () => {};

// ── State the context-bar reads ──
let CONV = null;
global.activeConvId = 'c1';
global.getConvById = (id) => (CONV && CONV.id === id) ? CONV : null;
global.config = { model: 'm' };
global.serverModel = 'm';
global.activeStreams = new Map();
global._contextPolicy = { default_limit: 200000, output_reserve: 8000,
  compaction_reserve: 4000, summary_trigger_ratio: 0.9, min_usable_ratio: 0.5, per_model: {} };
global.t = (k, vars) => k;   // identity i18n

// ── Spies ──
let posted = null, reloaded = false, rendered = false, flashed = null, cacheRemoved = false;
global.Api = { compactions: {
  compactNow: async (cid, opts) => { posted = { cid, opts }; return {
    ok: true, archiveId: 42, tokensBefore: 50000, tokensAfter: 8000,
    msgsBefore: 40, msgsAfter: 5, reductionPct: 84 }; },
}};
global.ConvCache = { remove: async () => { cacheRemoved = true; } };
global.convIsBusy = (conv) => Boolean(conv && conv.activeTaskId);
global.hydrateConversationRuntime = async () => { reloaded = true; };
global.showToast = () => {};
global.requestAuthoritativeConversationRender = () => { rendered = true; };

eval(fs.readFileSync(process.argv[2], 'utf8'));   // context-bar.js

const out = [];
function check(n, c) { out.push((c ? 'PASS ' : 'FAIL ') + n); }

(async () => {
  check('fn_exposed', typeof window.runManualCompaction === 'function'
                   && typeof window._resolveContextLimit === 'function');

  // ── (1) GAUGE SCHEME B: the summary msg (no usage) drives the level via
  //        _estimatedPromptTokens. We can't read _lastUsageTokens directly
  //        (private), so we assert via the closure: after a successful
  //        compaction the gauge recompute uses the estimate. Instead we test
  //        the fallback at the source by checking the shipped code path with a
  //        conv whose only assistant is the summary. We expose it indirectly:
  //        updateContextBar reads it; we just ensure no throw + closure works.

  // ── (2) IDLE GUARD: task active → no POST ──
  CONV = { id: 'c1', model: 'm', activeTaskId: 'task-1',
           _testTurns: [{ role:'assistant', _isCompactionSummary:true, _estimatedPromptTokens: 8000 }] };
  posted = null;
  await window.runManualCompaction('c1');
  check('idle_guard_no_post_when_task_active', posted === null);

  // ── (3) SUCCESS CLOSURE: idle conv → POST + full closure fires ──
  CONV = { id: 'c1', model: 'm', activeTaskId: null,
           _testTurns: [{ role:'user', content:'go' },
                      { role:'assistant', content:'x', usage:{ prompt_tokens: 20000 } }] };
  posted = null; reloaded = false; rendered = false; flashed = null; cacheRemoved = false;
  await window.runManualCompaction('c1');
  check('closure_posted', posted && posted.cid === 'c1');
  check('closure_cache_removed', cacheRemoved === true);
  check('closure_reloaded_messages', reloaded === true);
  check('closure_rerendered', rendered === true);
  check('closure_needs_load_set', CONV._turnSnapshotRequired === true);

  console.log(out.join('\n'));
})();
"""

# ── flashGaugeForArchive spy is injected via a wrapper (it's defined inside
#    the IIFE and exported to window.flashGaugeForArchive). ──
_CTXBAR_HARNESS = _CTXBAR_HARNESS.replace(
    "eval(fs.readFileSync(process.argv[2], 'utf8'));   // context-bar.js",
    "eval(fs.readFileSync(process.argv[2], 'utf8'));   // context-bar.js\n"
    "const _origFlash = window.flashGaugeForArchive;\n"
    "window.flashGaugeForArchive = (id) => { flashed = id; };")


def _run(harness_src: str, js_path: str, tmp_name: str):
    harness = os.path.join(HERE, tmp_name)
    with open(harness, 'w') as f:
        f.write(harness_src)
    try:
        return subprocess.run(['node', harness, js_path],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_context_bar_idle_guard_and_closure():
    proc = _run(_CTXBAR_HARNESS, os.path.join(JS_DIR, 'context-bar.js'),
                '_ctxbar_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'manual-compaction closure regression:\n' + output
    for want in ('PASS idle_guard_no_post_when_task_active', 'PASS closure_posted',
                 'PASS closure_cache_removed', 'PASS closure_reloaded_messages',
                 'PASS closure_rerendered'):
        assert want in output, f'missing {want}\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_context_bar_idle_guard_neuter(tmp_path):
    """NEUTER: force the idle probe to always report idle → a task-active conv
    would then POST. Proves the idle guard is load-bearing."""
    src = open(os.path.join(JS_DIR, 'context-bar.js'), encoding='utf-8').read()
    anchor = "  function _convHasLiveTask(conv) {"
    assert anchor in src
    neutered = src.replace(anchor, anchor + "\n    return false;  // NEUTER", 1)
    assert neutered != src
    nfile = tmp_path / 'context-bar-neutered.js'
    nfile.write_text(neutered, encoding='utf-8')
    proc = _run(_CTXBAR_HARNESS, str(nfile), '_ctxbar_neuter_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    # With the guard neutered, the task-active case POSTs → the guard check FAILS.
    assert 'FAIL idle_guard_no_post_when_task_active' in output, (
        'NEUTER did not bite — idle guard is not actually gating the POST:\n' + output)


# ── Gauge scheme-B fallback: test _lastUsageTokens via a dedicated harness ──
_GAUGE_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.document = { getElementById(){return null;}, querySelector(){return null;},
  createElement(){return {classList:{add(){},remove(){},toggle(){}},dataset:{},style:{setProperty(){}},
    setAttribute(){},appendChild(c){return c;},querySelector(){return null;},addEventListener(){},
    set innerHTML(v){},set textContent(v){}};}, body:{appendChild(){}}, addEventListener(){}, readyState:'complete' };
global.requestAnimationFrame=(fn)=>{fn();return 0;};
global.setTimeout=()=>0; global.clearTimeout=()=>{};
global.config={model:'m'}; global.serverModel='m'; global.activeStreams=new Map();
global._contextPolicy={default_limit:200000,output_reserve:8000,compaction_reserve:4000,
  summary_trigger_ratio:0.9,min_usable_ratio:0.5,per_model:{}};
global.t=(k)=>k;
let CONV=null; global.activeConvId='c1'; global.getConvById=(id)=>CONV;
eval(fs.readFileSync(process.argv[2],'utf8'));

// Source-level guard that the scheme-B fallback exists and is wired to the
// compaction summary. The exact ORDERING of the branches is asserted by the
// behavioral test (test_gauge_reserve_stale_usage_does_not_shadow_summary),
// which drives the real render path — so here we only assert the fallback
// references the summary estimate, is guarded by `_isCompactionSummary`, and
// returns `summaryEst` (the recency-ordered scheme-B fallback added when the
// gauge was fixed to not be shadowed by preserved reserve turns).
const src = fs.readFileSync(process.argv[2],'utf8');
const idxEst = src.indexOf('_estimatedPromptTokens');
const out=[];
out.push((typeof window._resolveContextLimit==='function'?'PASS ':'FAIL ')+'file_loaded');
out.push((/return\s+summaryEst\s*;/.test(src) ?'PASS ':'FAIL ')+'gauge_scheme_b_fallback_returned');
out.push((src.indexOf('_isCompactionSummary')>0 && idxEst>0 ?'PASS ':'FAIL ')+'gauge_fallback_guarded_by_summary_flag');
console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_gauge_scheme_b_fallback_present():
    proc = _run(_GAUGE_HARNESS, os.path.join(JS_DIR, 'context-bar.js'),
                '_gauge_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL' not in output, 'gauge scheme-B fallback missing/misordered:\n' + output


# ── Gauge scheme-B BEHAVIOR: the summary estimate must win over a preserved
#    reserve turn's STALE pre-compaction usage (the "ball never changes" bug).
#
#    A manual /compact rewrites the conversation to
#      [system] + [anchor?] + [summary] + [preserved reserve turns...]
#    The preserved reserve assistants sit AFTER the summary in ARRAY order but
#    ran BEFORE it, so they still carry their PRE-compaction usage (the huge old
#    prompt size). A plain newest-by-index walk reads that stale number and the
#    liquid ball never drops. The summary carries the true post-compaction size
#    in `_estimatedPromptTokens` and, minted at compaction time, has the NEWEST
#    timestamp — so it must win until a genuinely newer real turn arrives.
#    Driven through the PUBLIC updateContextBar() render path (a real
#    .chat-wrapper is stubbed so _ensureBar builds the bar). The percentage the
#    bar writes to `--ctx-arc-pct` is `used / limit`, so we recover `used` from
#    it — no dependency on any private helper or non-HEAD export. ──
_GAUGE_BEHAVIOR_HARNESS = r"""
const fs = require('fs');
global.window = global;
const LIMIT = 200000;
// A capturing element: records --ctx-arc-pct writes + dataset so we can read
// back the rendered fill level after updateContextBar().
let ARC_PCT = null;
function _mkEl() {
  const el = {
    _children: [], classList: { add(){}, remove(){}, toggle(){} },
    dataset: {}, attributes: {},
    style: { setProperty(k,v){ if (k === '--ctx-arc-pct') ARC_PCT = v; } },
    setAttribute(k,v){ this.attributes[k]=v; }, getAttribute(k){ return this.attributes[k]; },
    appendChild(c){ this._children.push(c); return c; },
    prepend(c){ this._children.unshift(c); return c; },
    removeChild(){}, remove(){},
    querySelector(){ return _mkEl(); }, querySelectorAll(){ return []; },
    addEventListener(){}, removeEventListener(){},
    getBoundingClientRect(){ return { left:0, right:0, top:0, bottom:0 }; },
    get isConnected(){ return true; }, set innerHTML(v){}, get innerHTML(){ return ''; },
    set textContent(v){}, get textContent(){ return ''; },
  };
  return el;
}
const WRAPPER = _mkEl();
// The gauge lives IN the #convStatusStrip host since 4dee9231 — _ensureBar
// returns null (and _doUpdate + the live-summary anchor silently no-op)
// without it, so the strip must exist in the stub DOM.
const STRIP = _mkEl();
global.document = {
  getElementById(sel){ return sel === 'convStatusStrip' ? STRIP : null; },
  querySelector(sel){ return sel === '.chat-wrapper' ? WRAPPER : null; },
  createElement(){ return _mkEl(); },
  body: _mkEl(),
  addEventListener(){}, removeEventListener(){}, readyState:'complete',
};
global.requestAnimationFrame=(fn)=>{fn();return 0;};
global.setTimeout=()=>0; global.clearTimeout=()=>{};
global.config={model:'m'}; global.serverModel='m'; global.activeStreams=new Map();
global._contextPolicy={default_limit:LIMIT,output_reserve:8000,compaction_reserve:4000,
  summary_trigger_ratio:0.9,min_usable_ratio:0.5,per_model:{}};
global.t=(k)=>k;
let CONV=null; global.activeConvId='c1'; global.getConvById=(id)=>((CONV&&CONV.id===id)?CONV:null);
global.ConversationTurnRead = {
  ordered(conv) { return (conv && conv._testTurns || []).map((projection, index) => ({
    turnId: projection._turnId || 't' + index,
    actor: projection.role === 'user' ? 'human' : projection.role,
    projection, updatedAt: projection.timestamp || 0,
  })); },
  state(conv) {
    const liveRoundUsageByTurn = {};
    this.ordered(conv).forEach((turn) => {
      if (turn.projection._liveLastRoundUsage) {
        liveRoundUsageByTurn[turn.turnId] = turn.projection._liveLastRoundUsage;
      }
    });
    return { liveRoundUsageByTurn };
  },
};
eval(fs.readFileSync(process.argv[2],'utf8'));   // context-bar.js
const out=[];
function check(n,c){out.push((c?'PASS ':'FAIL ')+n);}
// Recover the `used` token count the bar rendered from the --ctx-arc-pct write.
function renderedUsed(){ ARC_PCT=null; window.updateContextBar();
  return ARC_PCT==null ? null : Math.round(parseFloat(ARC_PCT)/100*LIMIT); }

// (A) Just-compacted conv: summary (ts=5000, newest) + a preserved reserve
//     assistant (ts=2001) still carrying its STALE 180k pre-compaction usage.
//     The rendered `used` MUST be the 8k post-compaction estimate, NOT 180k.
CONV = { id:'c1', model:'m', _testTurns: [
  { role:'user', content:'goal', timestamp: 1000 },
  { role:'assistant', _isCompactionSummary:true, _estimatedPromptTokens:8000,
    content:'summary', timestamp: 5000 },
  { role:'user', content:'recent', timestamp: 2000 },
  { role:'assistant', content:'x', timestamp: 2001,
    apiRounds:[{ usage:{ prompt_tokens:180000 } }] },
] };
check('reserve_stale_usage_does_not_shadow_summary', renderedUsed() === 8000);

// (B) After a genuinely NEW post-compaction turn (ts newer than the summary),
//     its fresh usage must take over — the summary is not sticky forever.
CONV._testTurns.push({ role:'user', content:'next', timestamp: 6000 });
CONV._testTurns.push({ role:'assistant', content:'y', timestamp: 6001,
  apiRounds:[{ usage:{ prompt_tokens:12000 } }] });
check('fresh_post_compaction_turn_takes_over', renderedUsed() === 12000);

// (C) No compaction at all → unchanged behavior: newest real usage wins.
CONV = { id:'c1', model:'m', _testTurns: [
  { role:'user', content:'q', timestamp: 1000 },
  { role:'assistant', content:'a', timestamp: 1001,
    apiRounds:[{ usage:{ prompt_tokens:33000 } }] },
] };
check('no_compaction_newest_usage_unchanged', renderedUsed() === 33000);
console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_gauge_reserve_stale_usage_does_not_shadow_summary():
    """The 'context ball never changes after compaction' bug: a preserved
    reserve turn's stale pre-compaction usage must NOT shadow the summary's
    post-compaction `_estimatedPromptTokens`."""
    proc = _run(_GAUGE_BEHAVIOR_HARNESS, os.path.join(JS_DIR, 'context-bar.js'),
                '_gauge_behavior_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'gauge scheme-B shadowing regression:\n' + output
    for want in ('PASS reserve_stale_usage_does_not_shadow_summary',
                 'PASS fresh_post_compaction_turn_takes_over',
                 'PASS no_compaction_newest_usage_unchanged'):
        assert want in output, f'missing {want}\n{output}'


# ── Gauge legacy-path honesty (2026-08-20 fake 100% bug) ──
# A reloaded v2 message carries the turn's ACCUMULATED usage (tokens billed
# across every round). When apiRounds is absent the divisor is unknown, so
# the legacy fallback MUST NOT present the bill as the last round's prompt —
# ÷1 showed "1.3M / 1.1M = 100%" on a conv whose real prompt was ~170k.
# The discriminator is PROVENANCE: turn-native messages carry `_turnId`
# (operations.py:_turn_to_legacy_message) — for them, missing apiRounds
# means the projection dropped the rounds, so no reading beats a fake one.
# A genuinely legacy (pre-turn-protocol) message keeps its ÷1 reading. ──
_GAUGE_LEGACY_HARNESS = r"""
const fs = require('fs');
global.window = global;
function _mkEl() {
  const el = { _children: [], classList: { add(){}, remove(){}, toggle(){} },
    dataset: {}, attributes: {},
    style: { setProperty(){} },
    setAttribute(k,v){ this.attributes[k]=v; }, getAttribute(k){ return this.attributes[k]; },
    appendChild(c){ this._children.push(c); return c; },
    prepend(c){ this._children.unshift(c); return c; },
    removeChild(){}, remove(){},
    querySelector(){ return _mkEl(); }, querySelectorAll(){ return []; },
    addEventListener(){}, removeEventListener(){},
    getBoundingClientRect(){ return { left:0, right:0, top:0, bottom:0 }; },
    get isConnected(){ return true; }, set innerHTML(v){}, get innerHTML(){ return ''; },
    set textContent(v){}, get textContent(){ return ''; } };
  return el;
}
const STRIP = _mkEl();
global.document = {
  getElementById(sel){ return sel === 'convStatusStrip' ? STRIP : null; },
  querySelector(){ return null; },
  createElement(){ return _mkEl(); }, body: _mkEl(),
  addEventListener(){}, removeEventListener(){}, readyState:'complete',
};
global.requestAnimationFrame=(fn)=>{fn();return 0;};
global.setTimeout=()=>0; global.clearTimeout=()=>{};
global.config={model:'m'}; global.serverModel='m'; global.activeStreams=new Map();
global._contextPolicy={default_limit:200000,output_reserve:8000,compaction_reserve:4000,
  summary_trigger_ratio:0.9,min_usable_ratio:0.5,per_model:{}};
global.t=(k)=>k;
let CONV=null; global.activeConvId='c1'; global.getConvById=(id)=>((CONV&&CONV.id===id)?CONV:null);
global.ConversationTurnRead = {
  ordered(conv) { return (conv && conv._testTurns || []).map((projection, index) => ({
    turnId: projection._turnId || 't' + index,
    actor: projection.role === 'user' ? 'human' : projection.role,
    projection, updatedAt: projection.timestamp || 0,
  })); },
  state() { return { liveRoundUsageByTurn: {} }; },
};
eval(fs.readFileSync(process.argv[2],'utf8'));   // context-bar.js
const out=[];
function check(n,c){out.push((c?'PASS ':'FAIL ')+n);}
function used(){ return window.contextUsageSummary().used; }

// (A) v2-reloaded message (turn-native `_turnId`, projection dropped the
//     rounds): accumulated usage, NO apiRounds → NO reading.
CONV = { id:'c1', model:'m', _testTurns: [
  { role:'user', content:'q', timestamp: 1000 },
  { role:'assistant', content:'a', timestamp: 1001, _turnId:'t1',
    usage:{ prompt_tokens: 1300000 } },
] };
check('accumulated_bill_without_round_count_is_not_a_prompt_size', used() === 0);

// (B) Same bill WITH a known round count (two zero-usage anomaly rounds) →
//     averaged, not raw.
CONV._testTurns[1].apiRounds = [ { usage:{} }, { usage:{}} ];
check('known_round_count_averages_the_bill', used() === 650000);

// (C) apiRounds with real per-round usage → the exact last round wins
//     (path 2, unchanged behavior).
CONV._testTurns[1].apiRounds = [ { usage:{ prompt_tokens: 168000 } } ];
check('per_round_reading_wins_over_average', used() === 168000);

// (D) Every retained conversation is now projected as Turns. Without a known
//     round count an accumulated bill remains unknown, even if a test fixture
//     omits the old compatibility `_turnId` marker.
delete CONV._testTurns[1]._turnId;
delete CONV._testTurns[1].apiRounds;
CONV._testTurns[1].usage = { prompt_tokens: 50000 };
check('turn_projection_without_round_count_stays_unknown', used() === 0);
console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_gauge_never_presents_accumulated_bill_as_prompt_size():
    """The 2026-08-20 fake-100% bug: a reloaded v2 message has the turn's
    accumulated usage but no apiRounds; the gauge must show no reading rather
    than divide the bill by one and call it the last round's prompt."""
    proc = _run(_GAUGE_LEGACY_HARNESS, os.path.join(JS_DIR, 'context-bar.js'),
                '_gauge_legacy_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'gauge legacy-path honesty regression:\n' + output
    for want in ('PASS accumulated_bill_without_round_count_is_not_a_prompt_size',
                 'PASS known_round_count_averages_the_bill',
                 'PASS per_round_reading_wins_over_average',
                 'PASS turn_projection_without_round_count_stays_unknown'):
        assert want in output, f'missing {want}\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_gauge_legacy_path_neuter(tmp_path):
    """NEUTER: drop the known-round-count guard → the accumulated bill is
    presented raw again (the fake-100% bug returns). Proves the guard is
    load-bearing."""
    src = open(os.path.join(JS_DIR, 'context-bar.js'), encoding='utf-8').read()
    anchor = ("        const n = typeof agentApiRoundCount === 'function'\n"
              '          ? agentApiRoundCount(m.apiRounds)\n'
              '          : (Array.isArray(m.apiRounds) && m.apiRounds.length) || 0;\n'
              '        if (t > 0 && n > 0) return n > 1 ? Math.round(t / n) : t;')
    assert anchor in src, 'legacy-path guard anchor not found (fix regressed?)'
    neutered = src.replace(
        anchor,
        ("        const n = typeof agentApiRoundCount === 'function'\n"
         '          ? agentApiRoundCount(m.apiRounds)\n'
         '          : (Array.isArray(m.apiRounds) && m.apiRounds.length) || 1;  // NEUTER\n'
         '        if (t > 0 && n > 0) return n > 1 ? Math.round(t / n) : t;'), 1)
    assert neutered != src
    nfile = tmp_path / 'context-bar-legacy-neutered.js'
    nfile.write_text(neutered, encoding='utf-8')
    proc = _run(_GAUGE_LEGACY_HARNESS, str(nfile), '_gauge_legacy_neuter_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL accumulated_bill_without_round_count_is_not_a_prompt_size' in output, (
        'NEUTER did not bite — the guard is not actually gating the legacy path:\n'
        + output)

# ── B: live-streaming summary overlay. runManualCompaction must subscribe to
#    the ('compaction', convId) push channel BEFORE the POST, grow a live card
#    from summary_delta frames, and unsubscribe + tear the card down when done.
#    Driven through the REAL runManualCompaction with a fake push bus. ──
_STREAM_HARNESS = r"""
const fs = require('fs');
global.window = global;
let LIVE = null;   // the live-summary overlay element created by the code
function _mkEl(tag) {
  const el = { tag, _children: [], classList: { add(){}, remove(){}, toggle(){} },
    dataset: {}, style: { setProperty(){} }, attributes: {}, _text: '', _html: '',
    setAttribute(k,v){ this.attributes[k]=v; }, getAttribute(k){ return this.attributes[k]; },
    appendChild(c){ this._children.push(c); return c; }, removeChild(){},
    prepend(c){ this._children.unshift(c); return c; },
    querySelector(sel){
      // Only the live-summary body query returns the growable body node; every
      // other selector (the bubble's wave-group etc.) gets a full element.
      if (sel === '.ctx-live-summary-body' && this._body) return this._body;
      return _mkEl(tag);
    },
    querySelectorAll(){ return []; },
    addEventListener(){}, removeEventListener(){}, remove(){ this._removed = true; },
    getBoundingClientRect(){ return { left:0, right:0, top:0, bottom:0 }; },
    get isConnected(){ return true; },
    set innerHTML(v){ this._html = v;
      // model the live-summary-body child node the overlay queries for.
      this._body = { _text:'', scrollTop:0, scrollHeight:0,
        set textContent(v){ this._text = v; }, get textContent(){ return this._text; } };
    },
    get innerHTML(){ return this._html; },
    set textContent(v){ this._text = v; }, get textContent(){ return this._text; } };
  return el;
}
const WRAPPER = _mkEl('div');   // stands in for .chat-wrapper so _ensureBar builds
const STRIP = _mkEl('div');    // #convStatusStrip — the gauge host; the live card anchors on _state.el
global.document = {
  getElementById(sel){ return sel === 'convStatusStrip' ? STRIP : null; },
  querySelector(sel){ return sel === '.chat-wrapper' ? WRAPPER : null; },
  createElement(){ return _mkEl('div'); },
  body: { appendChild(c){ if (c && c.id === 'ctxLiveSummary') LIVE = c; return c; } },
  addEventListener(){}, removeEventListener(){}, readyState:'complete',
};
global.requestAnimationFrame = (fn) => { fn(); return 0; };
global.setTimeout = (fn) => { return 0; };
global.clearTimeout = () => {};
let CONV = null;
global.activeConvId = 'c1';
global.getConvById = (id) => (CONV && CONV.id === id) ? CONV : null;
global.config = { model: 'm' }; global.serverModel = 'm';
global.activeStreams = new Map();
global._contextPolicy = { default_limit: 200000, output_reserve: 8000,
  compaction_reserve: 4000, summary_trigger_ratio: 0.9, min_usable_ratio: 0.5, per_model: {} };
global.t = (k, vars) => k;

// ── Fake push bus: record subscribe/unsubscribe; let the test drive frames ──
let SUBS = [], UNSUBS = [], handler = null;
global.pushSubscribe = (channel, taskId, fn) => { SUBS.push([channel, taskId]); handler = fn; };
global.pushUnsubscribe = (channel, taskId, fn) => { UNSUBS.push([channel, taskId]); };

// The POST resolves only AFTER we've pushed deltas, so we can assert the live
// card grew mid-flight. compactNow drives frames through the captured handler.
global.Api = { compactions: { compactNow: async (cid) => {
  // simulate the backend stream landing during the awaited POST
  if (handler) {
    handler({ channel:'compaction', taskId: cid, type:'summary_start', archiveId: 3 });
    handler({ channel:'compaction', taskId: cid, type:'summary_delta', text:'Hello ', archiveId: 3 });
    handler({ channel:'compaction', taskId: cid, type:'summary_delta', text:'world', archiveId: 3 });
  }
  return { ok: true, archiveId: 3, tokensBefore: 50000, tokensAfter: 8000, reductionPct: 84 };
}}};
global.ConvCache = { remove: async () => {} };
global.hydrateConversationRuntime = async () => {};
global.showToast = () => {};
global.requestAuthoritativeConversationRender = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));   // context-bar.js

const out = [];
function check(n, c) { out.push((c ? 'PASS ' : 'FAIL ') + n); }
(async () => {
  CONV = { id:'c1', model:'m', activeTaskId:null, _testTurns:[
    { role:'user', content:'go' },
    { role:'assistant', content:'x', usage:{ prompt_tokens: 20000 } } ] };
  await window.runManualCompaction('c1');

  check('subscribed_before_post', SUBS.length === 1
        && SUBS[0][0] === 'compaction' && SUBS[0][1] === 'c1');
  check('unsubscribed_after', UNSUBS.length === 1
        && UNSUBS[0][0] === 'compaction' && UNSUBS[0][1] === 'c1');
  // the live overlay was created and grew from the deltas (Hello + world)
  check('live_card_created', LIVE !== null);
  check('live_card_grew_from_deltas',
        !!(LIVE && LIVE._body && LIVE._body.textContent === 'Hello world'));
  // and it was torn down at the end (remove() called)
  check('live_card_torn_down', !!(LIVE && LIVE._removed));
  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_manual_compaction_streams_live_summary():
    """B: the /compact button subscribes to the compaction push channel, grows a
    live card from summary_delta frames, and tears it down + unsubscribes when
    the POST closure completes."""
    proc = _run(_STREAM_HARNESS, os.path.join(JS_DIR, 'context-bar.js'),
                '_stream_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'live-summary streaming regression:\n' + output
    for want in ('PASS subscribed_before_post', 'PASS unsubscribed_after',
                 'PASS live_card_created', 'PASS live_card_grew_from_deltas',
                 'PASS live_card_torn_down'):
        assert want in output, f'missing {want}\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_manual_compaction_streaming_absent_push_is_safe(tmp_path):
    """NEUTER/robustness: with pushSubscribe UNDEFINED (older bundle / no
    socket), runManualCompaction must still POST and complete — the live card
    is best-effort and never a hard dependency."""
    src = open(os.path.join(JS_DIR, 'context-bar.js'), encoding='utf-8').read()
    # Guard wiring must be present: the subscribe is gated on typeof.
    assert "typeof pushSubscribe === 'function'" in src, (
        'streaming subscribe is not guarded on pushSubscribe availability')
    # Run the toast harness (which does NOT define pushSubscribe) — the success
    # path must still fire exactly one success toast, proving no hard dep.
    proc = _run(_TOAST_HARNESS, os.path.join(JS_DIR, 'context-bar.js'),
                '_stream_absent_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'PASS ok_single_toast' in output and 'PASS ok_is_success' in output, (
        'compaction broke when pushSubscribe was undefined:\n' + output)


# ══════════════════════════════════════════════════════════════════════════
#  SINGLE-TOAST CONTRACT (the screenshot bug: "正在压缩" + "无需压缩" stacked)
#
#  _runManualCompaction must fire AT MOST ONE toast per invocation, always a
#  TERMINAL one. There must be NO optimistic "starting/running" toast that can
#  co-exist with the terminal "nothing"/"done"/"failed" toast. Progress is on
#  the chip spinner (data-compacting), not a toast.
# ══════════════════════════════════════════════════════════════════════════
_TOAST_HARNESS = r"""
const fs = require('fs');
global.window = global;
function _mkEl() {
  const el = { _children: [], classList: { add(){}, remove(){}, toggle(){} },
    dataset: {}, style: { setProperty(){} }, attributes: {},
    setAttribute(k,v){ this.attributes[k]=v; }, getAttribute(k){ return this.attributes[k]; },
    appendChild(c){ this._children.push(c); return c; }, removeChild(){},
    querySelector(){ return _mkEl(); }, querySelectorAll(){ return []; },
    addEventListener(){}, removeEventListener(){}, remove(){},
    getBoundingClientRect(){ return { left:0, right:0, top:0, bottom:0 }; },
    get isConnected(){ return true; }, set innerHTML(v){}, get innerHTML(){ return ''; },
    set textContent(v){}, get textContent(){ return ''; } };
  return el;
}
global.document = { getElementById(){ return null; }, querySelector(){ return null; },
  createElement(){ return _mkEl(); }, body: _mkEl(),
  addEventListener(){}, removeEventListener(){}, readyState: 'complete' };
global.requestAnimationFrame = (fn) => { fn(); return 0; };
global.setTimeout = (fn) => { return 0; };
global.clearTimeout = () => {};
let CONV = null;
global.activeConvId = 'c1';
global.getConvById = (id) => (CONV && CONV.id === id) ? CONV : null;
global.config = { model: 'm' }; global.serverModel = 'm';
global.activeStreams = new Map();
global._contextPolicy = { default_limit: 200000, output_reserve: 8000,
  compaction_reserve: 4000, summary_trigger_ratio: 0.9, min_usable_ratio: 0.5, per_model: {} };
global.t = (k, vars) => k;   // identity i18n → toast msg === the i18n KEY

// ── Toast spy: RECORD every call (msg, level) ──
let TOASTS = [];
global.showToast = (msg, level) => { TOASTS.push({ msg, level }); };
global.ConvCache = { remove: async () => {} };
global.hydrateConversationRuntime = async () => {};
global.requestAuthoritativeConversationRender = () => {};

// compactNow behavior is swapped per-case via global.__mode.
global.__mode = 'nothing';
global.Api = { compactions: { compactNow: async (cid) => {
  if (global.__mode === 'nothing') { const e = new Error('nothing'); e.code = 'nothing_to_compact'; throw e; }
  if (global.__mode === 'nothing_softfail') { return { ok: false, code: 'nothing_to_compact' }; }
  if (global.__mode === 'ok') { return { ok: true, archiveId: 7, tokensBefore: 50000, tokensAfter: 8000, reductionPct: 84 }; }
  const e = new Error('boom'); throw e;
}}};

eval(fs.readFileSync(process.argv[2], 'utf8'));   // context-bar.js

const out = [];
function check(n, c) { out.push((c ? 'PASS ' : 'FAIL ') + n); }

(async () => {
  CONV = { id: 'c1', model: 'm', activeTaskId: null, _testTurns: [] };

  // Case 1: nothing_to_compact (thrown) → EXACTLY ONE toast, level=info,
  //         and the "running" key must NEVER be present.
  TOASTS = []; global.__mode = 'nothing';
  await window.runManualCompaction('c1');
  check('nothing_single_toast', TOASTS.length === 1);
  check('nothing_is_info_not_warning', TOASTS.length === 1 && TOASTS[0].level === 'info');
  check('no_running_toast_ever', !TOASTS.some(t => t.msg === 'compactNow.running'));
  check('nothing_and_running_never_coexist',
        !(TOASTS.some(t => t.msg === 'compactNow.running')
          && TOASTS.some(t => t.msg === 'compactNow.nothing')));

  // Case 2: nothing_to_compact (non-throwing {ok:false,code}) → same contract.
  TOASTS = []; global.__mode = 'nothing_softfail';
  await window.runManualCompaction('c1');
  check('softfail_single_toast', TOASTS.length === 1);
  check('softfail_is_info', TOASTS.length === 1 && TOASTS[0].level === 'info');

  // Case 3: success → EXACTLY ONE terminal toast, level=success, no running.
  TOASTS = []; global.__mode = 'ok';
  await window.runManualCompaction('c1');
  check('ok_single_toast', TOASTS.length === 1);
  check('ok_is_success', TOASTS.length === 1 && TOASTS[0].level === 'success');
  check('ok_no_running', !TOASTS.some(t => t.msg === 'compactNow.running'));

  console.log(out.join('\n'));
})();
"""



@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_manual_compaction_single_terminal_toast():
    """The screenshot bug guard: nothing_to_compact must produce exactly ONE
    toast (info level), and the optimistic 'running' toast must never fire —
    so '正在压缩' and '无需压缩' can never appear together."""
    proc = _run(_TOAST_HARNESS, os.path.join(JS_DIR, 'context-bar.js'),
                '_toast_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'single-toast contract regression:\n' + output
    for want in ('PASS nothing_single_toast', 'PASS nothing_is_info_not_warning',
                 'PASS no_running_toast_ever', 'PASS nothing_and_running_never_coexist',
                 'PASS softfail_single_toast', 'PASS ok_single_toast',
                 'PASS ok_is_success'):
        assert want in output, f'missing {want}\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_manual_compaction_single_toast_neuter(tmp_path):
    """NEUTER: re-introduce the optimistic 'running' info toast at the top of
    _runManualCompaction → the nothing_to_compact path now emits TWO toasts
    ('running' + 'nothing'), exactly the contradictory-stack bug. Proves the
    single-toast contract is load-bearing."""
    src = open(os.path.join(JS_DIR, 'context-bar.js'), encoding='utf-8').read()
    # Inject a running toast right after the idle-guard early return, mimicking
    # the old bug. Anchor on the _setCompacting(true) line the fix introduced.
    anchor = "    let outcome = null;\n    _setCompacting(true);"
    assert anchor in src, 'single-toast fix anchor not found (fix regressed?)'
    neutered = src.replace(
        anchor,
        "    let outcome = null;\n"
        "    if (typeof showToast === 'function') showToast(_tt('compactNow.running','running'), 'info');  // NEUTER\n"
        "    _setCompacting(true);", 1)
    assert neutered != src
    nfile = tmp_path / 'context-bar-toast-neutered.js'
    nfile.write_text(neutered, encoding='utf-8')
    proc = _run(_TOAST_HARNESS, str(nfile), '_toast_neuter_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    # With the optimistic toast back, the nothing path emits 2 toasts and the
    # coexistence check FAILS.
    assert ('FAIL nothing_single_toast' in output
            or 'FAIL nothing_and_running_never_coexist' in output), (
        'NEUTER did not bite — re-adding the running toast did not break the '
        'single-toast contract:\n' + output)


# ── Gauge v2 live feed (2026-08-23 "context sphere frozen during generation"
#    root fix). Under turns-protocol v2 the v1 SSE lane never runs, so the
#    per-round reading now rides the durable turn projection as
#    `lastRoundUsage` (lib/turn_lifecycle.py::_task_projection, stashed by
#    llm_fallback._emit_round_usage). `_lastUsageTokens` reads it as path 1b —
#    after `_liveLastRoundUsage` (v1 session reading), before `apiRounds`
#    (which only lands at finalize). ──
_GAUGE_V2_LIVE_HARNESS = r"""
const fs = require('fs');
global.window = global;
function _mkEl() {
  const el = { _children: [], classList: { add(){}, remove(){}, toggle(){} },
    dataset: {}, attributes: {},
    style: { setProperty(){} },
    setAttribute(k,v){ this.attributes[k]=v; }, getAttribute(k){ return this.attributes[k]; },
    appendChild(c){ this._children.push(c); return c; },
    prepend(c){ this._children.unshift(c); return c; },
    removeChild(){}, remove(){},
    querySelector(){ return _mkEl(); }, querySelectorAll(){ return []; },
    addEventListener(){}, removeEventListener(){},
    getBoundingClientRect(){ return { left:0, right:0, top:0, bottom:0 }; },
    get isConnected(){ return true; }, set innerHTML(v){}, get innerHTML(){ return ''; },
    set textContent(v){}, get textContent(){ return ''; } };
  return el;
}
const STRIP = _mkEl();
global.document = {
  getElementById(sel){ return sel === 'convStatusStrip' ? STRIP : null; },
  querySelector(){ return null; },
  createElement(){ return _mkEl(); }, body: _mkEl(),
  addEventListener(){}, removeEventListener(){}, readyState:'complete',
};
global.requestAnimationFrame=(fn)=>{fn();return 0;};
global.setTimeout=()=>0; global.clearTimeout=()=>{};
global.config={model:'m'}; global.serverModel='m'; global.activeStreams=new Map();
global._contextPolicy={default_limit:200000,output_reserve:8000,compaction_reserve:4000,
  summary_trigger_ratio:0.9,min_usable_ratio:0.5,per_model:{}};
global.t=(k)=>k;
let CONV=null; global.activeConvId='c1'; global.getConvById=(id)=>((CONV&&CONV.id===id)?CONV:null);
global.ConversationTurnRead = {
  ordered(conv) { return (conv && conv._testTurns || []).map((projection, index) => ({
    turnId: projection._turnId || 't' + index,
    actor: projection.role === 'user' ? 'human' : projection.role,
    projection, updatedAt: projection.timestamp || 0,
  })); },
  state(conv) {
    const liveRoundUsageByTurn = {};
    this.ordered(conv).forEach((turn) => {
      if (turn.projection._liveLastRoundUsage) {
        liveRoundUsageByTurn[turn.turnId] = turn.projection._liveLastRoundUsage;
      }
    });
    return { liveRoundUsageByTurn };
  },
};
eval(fs.readFileSync(process.argv[2],'utf8'));   // context-bar.js
const out=[];
function check(n,c){out.push((c?'PASS ':'FAIL ')+n);}
function used(){ return window.contextUsageSummary().used; }

// (A) THE REGRESSION PIN — a mid-turn v2 message: turn-native, NO apiRounds
//     (they land at finalize), NO accumulated usage yet, but the projection
//     carries lastRoundUsage. The gauge MUST render it (before the fix this
//     read 0 — the frozen sphere).
CONV = { id:'c1', model:'m', _testTurns: [
  { role:'user', content:'q', timestamp: 1000 },
  { role:'assistant', content:'', timestamp: 1001, _turnId:'t1',
    lastRoundUsage:{ round:3, model:'m', tag:'R3', tokensIn:47000, tokensOut:300 } },
] };
check('v2_live_lastRoundUsage_drives_gauge', used() === 47000);

// (B) Per-round freshness: lastRoundUsage is rewritten EVERY round, so it
//     wins over an apiRounds snapshot that predates the latest round.
CONV._testTurns[1].apiRounds = [ { usage:{ prompt_tokens: 30000 } } ];
check('v2_lastRoundUsage_beats_stale_apiRounds', used() === 47000);

// (C) v1 parity: a session-only _liveLastRoundUsage (v1 SSE lane) still
//     outranks the projection-carried reading.
CONV._testTurns[1]._liveLastRoundUsage = { tokensIn: 60000 };
check('v1_live_reading_outranks_projection_reading', used() === 60000);
delete CONV._testTurns[1]._liveLastRoundUsage;

// (D) Zero readings are skipped (the in-flight bubble must not shadow the
//     previous turn's number): tokensIn 0 falls through to apiRounds.
CONV._testTurns[1].lastRoundUsage = { round:0, model:'', tag:'', tokensIn:0, tokensOut:0 };
check('zero_tokensIn_falls_through_to_apiRounds', used() === 30000);

// (E) Historical settled projection: an internal compaction accounting row
//     may trail the response-authoring API row. It must not become the gauge's
//     prompt reading (the same classification drives finish-route display).
delete CONV._testTurns[1].lastRoundUsage;
CONV._testTurns[1].apiRounds = [
  { tag:'R3', usage:{ prompt_tokens: 30000 } },
  { kind:'compaction', tag:'COMPACTION-L2', usage:{ prompt_tokens: 4000 } },
];
check('trailing_compaction_is_not_agent_prompt', used() === 30000);
console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_gauge_v2_live_last_round_usage():
    """v2 turn lane: the projection-carried lastRoundUsage must drive the
    gauge between tool rounds (the frozen-sphere regression pin)."""
    proc = _run(_GAUGE_V2_LIVE_HARNESS, os.path.join(JS_DIR, 'context-bar.js'),
                '_gauge_v2_live_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'gauge v2 live-feed regression:\n' + output
    for want in ('PASS v2_live_lastRoundUsage_drives_gauge',
                 'PASS v2_lastRoundUsage_beats_stale_apiRounds',
                 'PASS v1_live_reading_outranks_projection_reading',
                 'PASS zero_tokensIn_falls_through_to_apiRounds',
                 'PASS trailing_compaction_is_not_agent_prompt'):
        assert want in output, f'missing {want}\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_gauge_v2_live_neuter(tmp_path):
    """NEUTER: drop the path-1b branch → the mid-turn v2 message reads 0
    again (the frozen sphere returns). Proves the branch is load-bearing."""
    src = open(os.path.join(JS_DIR, 'context-bar.js'), encoding='utf-8').read()
    anchor = ("      if (m.lastRoundUsage && m.lastRoundUsage.tokensIn > 0) {\n"
              "        return m.lastRoundUsage.tokensIn;\n"
              "      }")
    assert anchor in src, 'path-1b anchor not found (fix regressed?)'
    neutered = src.replace(anchor, '', 1)
    assert neutered != src
    nfile = tmp_path / 'context-bar-v2live-neutered.js'
    nfile.write_text(neutered, encoding='utf-8')
    proc = _run(_GAUGE_V2_LIVE_HARNESS, str(nfile), '_gauge_v2live_neuter_harness.js')
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL v2_live_lastRoundUsage_drives_gauge' in output, (
        'NEUTER did not bite — path 1b is not actually feeding the gauge:\n'
        + output)


# The typed ConversationSurface commit is now the single repaint seam.
def test_turn_surface_commit_refreshes_context_gauge():
    adapter = open(
        os.path.join(JS_DIR, 'main', 'conversation_turn_store.js'),
        encoding='utf-8',
    ).read()
    start = adapter.index('  afterConversationCommit(')
    end = adapter.index('\n  },\n});', start)
    commit = adapter[start:end]
    assert "typeof _cvRefreshContextGauge === 'function'" in commit
    assert '_cvRefreshContextGauge();' in commit

    owner = open(
        os.path.join(JS_DIR, 'ui', 'stream_lifecycle.js'),
        encoding='utf-8',
    ).read()
    assert 'function _cvRefreshContextGauge()' in owner
    assert 'runtimeScope.updateContextBar();' in owner
