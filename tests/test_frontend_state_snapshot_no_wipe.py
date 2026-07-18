"""Regression: the SSE ``state`` handler applies a snapshot's content/thinking
VERBATIM, and reset events still clear the bubble.

WHY THIS CHANGED (2026-07-11)
-----------------------------
`state` is a FULL RESYNC snapshot. It USED to be applied through a
`_snapshotLonger` keep-longer belt because a cold reconnect could replay a
5s-stale ``task_results`` checkpoint SHORTER than the client's live buffer.
That shrink source is now GONE — a ``state`` snapshot can no longer be shorter
than the client buffer from ANY backend path:
  * warm resume / fresh: content = ``task['content']`` accumulated UNDER
    ``content_lock`` BEFORE the delta is emitted (manager.py `_on_content`),
    so it is always >= what the client received;
  * cold paths (SSE gen_persisted/gen_done, poll DB-fallback): content is
    ``fold_cold_state_text`` = ``max(folded task_events log, checkpoint)``,
    and each delta is persisted BEFORE it is pushed (durable-before-visible),
    so the fold is never behind the client.
So the plain-assistant / worker / planner / critic state sites now assign
content/thinking VERBATIM (`msg.content = ev.content || ""`); the text
keep-longer belt was retired. See tests/test_event_fold_cold_replay.py +
tests/test_event_persist_before_push.py + test_frontend_reconnect_keeplonger_invariant.py.
(toolRounds is NOT foldable yet, so it keeps `_snapshotLongerRounds` — covered
by the invariant test, not here.)

This drives the REAL shipped `state` handler via the
``window.__sse_test__.dispatchSSEEvent`` seam under jsdom, and asserts:
  (1) a LONGER/equal `state` snapshot → APPLIED verbatim (real resync).
  (2) the legitimate reset path (`retry_reset`) STILL clears content to "".
  (3) SOURCE GUARD: the plain-assistant state site assigns content verbatim,
      NOT through a `_snapshotLonger` belt (the belt is removed).

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

// ── (1) A state snapshot is applied VERBATIM (real backend-authoritative
//    resync). The backend guarantees it is never shorter than the client
//    buffer (fold = max(log, checkpoint); persist-before-push), so verbatim
//    is correct — no keep-longer needed. ──
{
  const { am, ctx } = setup('Hello', 'a');
  const full = 'Hello, this is the fuller resynced answer.';
  T.dispatchSSEEvent(line({ type: 'state', endpointMode: false,
    content: full, thinking: 'abc' }), ctx);
  check('state_applies_content_verbatim', am.content === full);
  check('state_applies_thinking_verbatim', am.thinking === 'abc');
}

// ── (1b) An equal-length resync applies verbatim (idempotent). ──
{
  const { am, ctx } = setup(LONG, 'r');
  T.dispatchSSEEvent(line({ type: 'state', endpointMode: false,
    content: LONG, thinking: 'r2' }), ctx);
  check('state_equal_applies_verbatim', am.content === LONG && am.thinking === 'r2');
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
    assert not fails, 'state-snapshot verbatim failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{output}'

    # Source guard: the plain-assistant state branch now assigns content
    # VERBATIM (backend-authoritative), NOT through the retired _snapshotLonger
    # text belt.
    with open(pipeline, encoding='utf-8') as f:
        src = f.read()
    assert 'assistantMsg.content = ev.content || ""' in src, (
        'regression: the plain-assistant `state` handler no longer assigns '
        'content verbatim — the verbatim projection was changed.')
    assert '_snapshotLonger(' not in src, (
        'the retired _snapshotLonger text belt reappeared — state text must be '
        'a verbatim projection of the backend-authoritative snapshot.')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_state_snapshot_verbatim_neuter(tmp_path):
    """NEUTER: break the verbatim projection in a COPY of sse_pipeline.js by
    forcing the plain-assistant state assignment to a constant, and prove the
    verbatim-apply check FAILS while the reset check stays green. Proves the
    test genuinely discriminates the verbatim contract. Real file untouched."""
    pipeline = os.path.join(JS_DIR, 'ui', 'sse_pipeline.js')
    with open(pipeline, encoding='utf-8') as f:
        src = f.read()
    fixed = 'assistantMsg.content = ev.content || "";'
    assert fixed in src, 'verbatim assignment not found — update the neuter target'
    neutered_src = src.replace(
        fixed, 'assistantMsg.content = "__NEUTERED__";', 1)
    assert neutered_src != src, 'neuter did not change the source'
    nfile = tmp_path / 'sse_pipeline_neutered.js'
    nfile.write_text(neutered_src, encoding='utf-8')

    proc = _run_harness(str(nfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    # The neuter MUST break the verbatim apply…
    assert lines.get('state_applies_content_verbatim') is False, (
        'NEUTER did not bite: the state handler did not apply content verbatim '
        'even after the assignment was corrupted — the test does not '
        'discriminate the contract.\n' + output)
    # …while the legitimate reset path is unaffected (rides a different event).
    assert lines.get('retry_reset_clears_content') is True, (
        'neuter unexpectedly changed the retry_reset path:\n' + output)
