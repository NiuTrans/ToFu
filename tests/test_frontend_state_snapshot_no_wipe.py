"""Regression: an SSE ``state`` snapshot must NEVER wipe durable checkpointed
content/thinking with an empty (or shorter) snapshot.

WHY
---
`state` is a FULL RESYNC snapshot, NOT a reset. Resets ride their own dedicated
events — `retry_reset` / `delta_reset` — each of which clears BOTH the message
AND the streaming buffer together (see `sse_pipeline.js`). So an empty or
shorter `ev.content` in a `state` snapshot is a LAGGING / reconnect-before-
accumulation snapshot (the server observed its content-lock between append
cycles, or the socket connected before content accumulated), never an
instruction to blank the bubble.

The plain-assistant branch of the `state` handler used to do
`assistantMsg.content = ev.content || ""` unconditionally — the SAME bug class
as the `_twFlush` raw-buffer wipe: an empty snapshot clobbered already-
checkpointed content back to blank ("等待中…" / lost text). The fix routes the
overwrite through `_snapshotLonger(msg, ev, field)`, which keeps whichever side
is longer — mirroring `_pollFallback`'s regression-safe merge for `data.content`
and the reconnect branch's `|| assistantMsg.content` idiom.

RESET-SAFETY (why keep-longer can't resurrect intentionally-cleared text):
`state`, `retry_reset`, `delta_reset` are mutually-exclusive `else if` arms on
`ev.type`. After a reset the message content is `""`; a `state` snapshot that
lands next also carries `""` (server reset its accumulator on the same task) →
both empty → stays empty. Client-longer only happens when the client is AHEAD
via un-flushed deltas — keeping the longer side is correct there. And stale
snapshots from an aborted/superseded task are discarded by the SyncFix guard
before reaching this branch. So there is NO legitimate empty-reset path through
the `state` handler.

This drives the REAL shipped `state` handler via the
``window.__sse_test__.dispatchSSEEvent`` seam under jsdom, and asserts:
  (1) empty `state` over a populated message → content/thinking PRESERVED.
  (2) shorter `state` over longer accumulated content → PRESERVED (lock-cycle lag).
  (3) a genuinely LONGER `state` snapshot → APPLIED (real resync grows the bubble).
  (4) the legitimate reset path (`retry_reset`) STILL clears content to "".

DOUBLE-NEUTER (run below): revert `_snapshotLonger` to the raw
`incoming` (== `ev.content || ""`) in a COPY of sse_pipeline.js → check (1)
FAILS (the wipe returns), while the reset check (4) stays green. Real file
untouched.

Runs the REAL shipped JS under jsdom; skips cleanly when node + jsdom aren't
installed.
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


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# Mirrors tests/test_frontend_sse_dispatch.py's harness: boot jsdom, stub every
# global the dispatcher calls, load the handler files + sse_pipeline.js, then
# drive single `state`/`retry_reset` events through the exposed test seam.
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>',
                      { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;
global.clearTimeout = win.clearTimeout = () => {};

let conversations = [];
let activeConvId = null;
const streamBufs = new Map();
const activeStreams = new Map();
win.conversations = conversations;
Object.defineProperty(win, 'activeConvId', { get: () => activeConvId, set: v => activeConvId = v });
win.streamBufs = streamBufs;
win.activeStreams = activeStreams;

const calls = {};
function spy(name) { calls[name] = 0; return (...a) => { calls[name]++; }; }
for (const n of ['twUpdate','twStart','twStop','finishStream','renderChat',
  'renderConversationList','buildTurnNav','saveConversations','updateContextBar',
  'scrollToBottom','_forceScrollToBottom','showToast','debugLog','showMessagesInDebug',
  '_handleAutopilotVuEvent','_retriggerHgTranslations','_streamTimerTouch',
  '_reportClientError']) {
  win[n] = global[n] = spy(n);
}
win._streamingBubbleHTML = global._streamingBubbleHTML = () => '<div id="streaming-msg"></div>';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<svg></svg>';
win.renderMarkdown = global.renderMarkdown = (s) => s;
win.ConvView = global.ConvView = { finalizeStreaming: spy('finalizeStreaming') };
win.Artifacts = global.Artifacts = { attachToMessage: spy('attachToMessage') };
win.flashGaugeForArchive = global.flashGaugeForArchive = spy('flashGaugeForArchive');
win.Api = global.Api = { project: { status: () => Promise.resolve(null) } };
win.getActiveConv = global.getActiveConv = () => conversations.find(c => c.id === activeConvId);
win.errorEnvelopeMessage = global.errorEnvelopeMessage = (e) =>
  (e && typeof e === 'object' ? (e.message || e.detail || '') : (typeof e === 'string' ? e : ''));
win._debugCache = global._debugCache = {};
win._applyProjectData = global._applyProjectData = spy('_applyProjectData');
win.syncConversationToServer = global.syncConversationToServer = spy('syncConversationToServer');
win._autoTranslateHumanGuidance = global._autoTranslateHumanGuidance = spy('_autoTranslateHumanGuidance');
global.autoTranslate = win.autoTranslate = false;
win.convAutoTranslate = global.convAutoTranslate = (c) =>
  (c && c.autoTranslate !== undefined) ? !!c.autoTranslate : false;
win.updateContextBar = global.updateContextBar = spy('updateContextBar');
if (typeof global.requestAnimationFrame !== 'function') {
  global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { try { fn(); } catch (_) {} return 0; };
}
let _idc = 0;
win._ensureMsgId = global._ensureMsgId = (m) => { if (m && !m._msgId) m._msgId = 'mid-' + (++_idc); return m; };
win._resolveAssistantById = global._resolveAssistantById = (conv, id) =>
  (conv && conv.messages.find(m => m._msgId === id)) || null;

eval(fs.readFileSync(process.argv[4], 'utf8'));  // ui/sse_handlers_tool.js
eval(fs.readFileSync(process.argv[5], 'utf8'));  // ui/sse_handlers_swarm.js
eval(fs.readFileSync(process.argv[6], 'utf8'));  // ui/sse_handlers_io.js
eval(fs.readFileSync(process.argv[7], 'utf8'));  // ui/sse_handlers_misc.js
eval(fs.readFileSync(process.argv[8], 'utf8'));  // ui/sse_handlers_lifecycle.js
eval(fs.readFileSync(process.argv[2], 'utf8'));  // sse_pipeline.js

const T = win.__sse_test__;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (!T || typeof T.dispatchSSEEvent !== 'function' || typeof T.makeCtx !== 'function') {
  console.log('FAIL seam_exposed window.__sse_test__.{makeCtx,dispatchSSEEvent} missing');
  process.exit(0);
}
check('seam_exposed', true);

// Build a conv + a worker assistant that ALREADY holds checkpointed content
// (as after a mid-stream reconnect where deltas accumulated before the state
//  snapshot arrives).
function setup(seedContent, seedThinking) {
  conversations.length = 0;
  const am = { role: 'assistant', content: seedContent || '', thinking: seedThinking || '',
               toolRounds: [], _msgId: 'mid-worker' };
  const conv = { id: 'c1', messages: [{ role: 'user', content: 'hi' }, am] };
  conversations.push(conv);
  activeConvId = 'c1';
  const buf = { content: seedContent || '', thinking: seedThinking || '', toolRounds: [] };
  streamBufs.set('c1', buf);
  const ctx = T.makeCtx({ convId: 'c1', taskId: 't1',
    stream: { controller: { signal: { aborted: false } } },
    assistantMsg: am, buf });
  return { conv, am, buf, ctx };
}
function line(obj) { return 'data: ' + JSON.stringify(obj); }

const LONG = 'The quick brown fox jumps over the lazy dog. '.repeat(4);  // ~180 chars

// ── (1) EMPTY state snapshot over a populated message → PRESERVED. ──
{
  const { am, buf, ctx } = setup(LONG, 'reasoning so far');
  T.dispatchSSEEvent(line({ type: 'state', endpointMode: false,
    content: '', thinking: '' }), ctx);
  check('empty_state_preserves_content', am.content === LONG);
  check('empty_state_preserves_thinking', am.thinking === 'reasoning so far');
  check('empty_state_preserves_buf', buf.content === LONG);
}

// ── (2) SHORTER state snapshot over longer accumulated content → PRESERVED
//    (poll/lock-cycle lag: server snapshot briefly trails client deltas). ──
{
  const { am, ctx } = setup(LONG, '');
  T.dispatchSSEEvent(line({ type: 'state', endpointMode: false,
    content: LONG.slice(0, 20) }), ctx);   // shorter than accumulated
  check('shorter_state_preserves_content', am.content === LONG);
}

// ── (3) GENUINELY LONGER state snapshot → APPLIED (real resync grows bubble). ──
{
  const { am, ctx } = setup('Hello', 'a');
  const longer = 'Hello, this is the fuller resynced answer.';
  T.dispatchSSEEvent(line({ type: 'state', endpointMode: false,
    content: longer, thinking: 'abc' }), ctx);
  check('longer_state_applies_content', am.content === longer);
  check('longer_state_applies_thinking', am.thinking === 'abc');
}

// ── (4) LEGITIMATE RESET path (retry_reset) STILL clears content to "". ──
//    Proves the keep-longer state invariant does NOT block real resets — those
//    ride a DIFFERENT event that clears both message + buffer. ──
{
  const { am, buf, ctx } = setup(LONG, 'partial reasoning');
  T.dispatchSSEEvent(line({ type: 'retry_reset' }), ctx);
  check('retry_reset_clears_content', am.content === '');
  check('retry_reset_clears_thinking', am.thinking === '');
  check('retry_reset_clears_buf', buf.content === '' && buf.thinking === '');
}

console.log(out.join('\n'));
"""

_ARGS = [
    os.path.join(JS_DIR, 'ui', 'sse_pipeline.js'),   # argv[2] (overridden for neuter)
    ROOT,                                            # argv[3]
    os.path.join(JS_DIR, 'ui', 'sse_handlers_tool.js'),       # argv[4]
    os.path.join(JS_DIR, 'ui', 'sse_handlers_swarm.js'),      # argv[5]
    os.path.join(JS_DIR, 'ui', 'sse_handlers_io.js'),         # argv[6]
    os.path.join(JS_DIR, 'ui', 'sse_handlers_misc.js'),       # argv[7]
    os.path.join(JS_DIR, 'ui', 'sse_handlers_lifecycle.js'),  # argv[8]
]


def _run_harness(pipeline_path: str):
    harness = os.path.join(HERE, '_state_snapshot_no_wipe_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    args = list(_ARGS)
    args[0] = pipeline_path  # argv[2]
    try:
        proc = subprocess.run(
            ['node', harness] + args,
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    return proc


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_state_snapshot_no_wipe():
    pipeline = os.path.join(JS_DIR, 'ui', 'sse_pipeline.js')
    proc = _run_harness(pipeline)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'state-snapshot no-wipe failures:\n' + output
    assert output.count('PASS') >= 9, f'expected >=9 PASS lines, got:\n{output}'

    # Source guard: the plain-assistant state branch must go through the
    # keep-longer helper, not a raw `ev.content || ""` wipe.
    with open(pipeline, encoding='utf-8') as f:
        src = f.read()
    assert '_snapshotLonger(assistantMsg, ev,' in src, (
        'regression: the plain-assistant `state` handler no longer routes '
        "through _snapshotLonger — an empty snapshot can wipe checkpointed content.")


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_state_snapshot_no_wipe_double_neuter(tmp_path):
    """DOUBLE-NEUTER: revert `_snapshotLonger` to the raw incoming value (==
    the old `ev.content || ""` wipe) in a COPY of sse_pipeline.js and prove the
    empty-state check FAILS (the wipe returns), while the reset check stays
    green. Proves the test genuinely discriminates the fix. Real file
    untouched."""
    pipeline = os.path.join(JS_DIR, 'ui', 'sse_pipeline.js')
    with open(pipeline, encoding='utf-8') as f:
        src = f.read()
    fixed = 'return incoming.length >= current.length ? incoming : current;'
    assert fixed in src, 'keep-longer helper body not found — update the neuter target'
    neutered_src = src.replace(fixed, 'return incoming;  // NEUTERED: raw overwrite (bug)', 1)
    assert neutered_src != src, 'neuter did not change the source'
    nfile = tmp_path / 'sse_pipeline_neutered.js'
    nfile.write_text(neutered_src, encoding='utf-8')

    proc = _run_harness(str(nfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    # The neuter MUST break the empty-state preservation (the wipe returns)…
    assert lines.get('empty_state_preserves_content') is False, (
        'DOUBLE-NEUTER did not bite: with the raw-overwrite helper an empty '
        'state snapshot still preserved content — the test does not '
        'discriminate the fix.\n' + output)
    # …while the legitimate reset path is unaffected (rides a different event).
    assert lines.get('retry_reset_clears_content') is True, (
        'neuter unexpectedly changed the retry_reset path:\n' + output)
