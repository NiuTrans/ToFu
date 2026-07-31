"""tests/test_frontend_lazy_sentinel_anchor.py — chatInner message-ORDER guard.

WHY
---
Reported symptom: "the user message of the FIRST round is rendered at the very
BOTTOM of chatInner". Reproduced against the real shipped code — the DOM order
came out `m4…m23, m0, m1, m2, m3`, i.e. the conversation's OPENING turns sat
below its newest one.

It is a TWO-STEP chain, and neither step looks wrong on its own:

  1. `ui/chat_render.js` surgical reconcile — for the FIRST message of the
     reconcile window (`_prevEl` and `_cursor` both null) the insertion anchor
     used to fall back to `inner.firstChild`. But when a lazy window is active
     `firstChild` is `#_lazyLoadSentinel`, NOT a message. Anchoring there
     inserts message #1 ABOVE the sentinel, which pushes the sentinel down one
     slot — on EVERY background repaint. Cost / file-change / compaction data
     all land as background repaints on a conversation open, so after a few of
     them the sentinel has migrated to the very BOTTOM of `#chatInner`.
     Invisible on screen: the sentinel is a thin one-line strip.

  2. `ui/streaming_render.js::_loadOlderMessages` splices the older batch in
     with `sentinel.after(frag)`. With the sentinel now at the bottom, the
     OLDEST messages are inserted BELOW the newest one.

Step 1 only becomes visible when the reader scrolls up and step 2 fires. This
harness drives BOTH real functions end-to-end and asserts the two invariants:

  A. the head sentinel STAYS the first child across background repaints;
  B. after a real `_loadOlderMessages()`, DOM order == conv.messages order.

NEUTER: restore the `inner.firstChild` fallback (the pre-fix code) and prove
both assertions FAIL — i.e. the sentinel-skipping anchor is load-bearing.

THE MIRROR BUG AT THE TAIL (same shape, opposite end)
-----------------------------------------------------
Fixing only the head left the identical hole at the bottom, because the head
anchor was a CLOSURE inside `renderChat`'s surgical block and `ConvView` could
not reach it. `_ensureBottomSentinel` pins itself with `inner.appendChild(s)`,
so it is the LAST child; `ConvView.apply` and `ConvView.startStreaming` both
used `insertAdjacentHTML('beforeend', …)`, which lands AFTER it:

    seed:                       a, b, SENT_BOT
    after ConvView.apply(NEW):  a, b, SENT_BOT, NEW
    after startStreaming():     a, b, SENT_BOT, NEW, LIVE

Reachable in production: `_evictBelowWindow` only bails while a stream is live,
so a settled conversation past `_MAX_RENDER_WINDOW` (80) that the reader
scrolls up in grows a bottom sentinel — and the composer is still right there.
Worse, `_loadNewerMessages` splices its batch with `sentinel.before(frag)`, so
the recovered tail lands ABOVE the message you just sent — the head inversion
all over again.

THREE LINES OF DEFENCE (a scenario test only catches the scenario you thought of)
--------------------------------------------------------------------------------
  1. ONE shared ordered-insert primitive — `core/chatinner_dom.js` owns
     `chatInnerHeadAnchor()` / `chatInnerTailAnchor()` / `chatInnerInsert()`.
     Every writer (renderChat ×2, ConvView.apply, ConvView.startStreaming)
     routes through it. No writer names `firstChild` / `beforeend` /
     `appendChild` against `#chatInner` again.
  2. A RUNTIME order invariant (`assertChatInnerOrder`) — debug-mode only,
     one-shot, named site, mirroring the identity-gate tripwire. Both bugs hid
     for months because nothing ever asserted RENDER_CONTRACT Invariant 1.
  3. A STATIC guard — any file other than the primitive touching `#chatInner`
     with a raw positional API fails the build. That is what would have caught
     the tail case at commit time.

NEUTER coverage: the head anchor, the tail anchor, and the runtime invariant
are each independently stripped and proven load-bearing.

`ui/streaming_render.js` and `ui/chat_render.js` are eval-ed as ONE
concatenated script, exactly as `lib/js_bundler.py` emits them, because their
top-level `let` bindings (`_lazyConvId` / `_lazyRenderedFrom` /
`_lazyRenderedTo` / `_INITIAL_RENDER`) share a single script scope. Eval-ing
them separately puts each file's `let` in its own scope and `renderChat` cannot
see them — the harness would fail for a reason unrelated to the invariant.

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
JS_DIR = os.path.join(ROOT, 'static', 'js')
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')

# The shipped anchor expression the fix introduced, and the pre-fix expression
# the NEUTER restores. Kept as module constants so a rename breaks LOUDLY here
# rather than silently turning the NEUTER into a no-op.
_FIXED_ANCHOR = ('const _want = _prevEl ? _prevEl.nextSibling '
                 ': (_cursor ? _cursor.nextSibling : _headAnchor());')
_LEGACY_ANCHOR = ('const _want = _prevEl ? _prevEl.nextSibling '
                  ': (_cursor ? _cursor.nextSibling : inner.firstChild);')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[5];
const NC = process.argv[6] || '';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.setTimeout = win.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { if (typeof fn === 'function') fn(); return 0; };
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };
global.IntersectionObserver = win.IntersectionObserver = function () {
  return { observe(){}, unobserve(){}, disconnect(){} };
};

const out = [];
function check(name, cond, extra) { out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : '')); }

// ── Idle conv: no live stream, so renderChat takes the static surgical path. ──
win.activeStreams = global.activeStreams = new Map();
win.activeConvId = global.activeConvId = 'c1';
win.t = global.t = (k) => k;
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
win.renderToolRoundsHTML = global.renderToolRoundsHTML = () => '';
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML = () => '';
const _noop = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderBranchZone','renderTurnCtxNote',
  'renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_stampFreshness','buildTurnNav','calcCostCny',
  '_forceScrollToBottom','scrollToBottom','isNearBottom','showStreamingUIForConv',
  '_captureScrollAnchor','_restoreScrollAnchor','_applyAutopilotRunFolds',
]) { if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; } }
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
win.BASE_PATH = global.BASE_PATH = '';
win._prefetchConvCosts = global._prefetchConvCosts = () => ({ then: () => {} });
win._prefetchConvFileChanges = global._prefetchConvFileChanges = () => ({ then: () => {} });
win._editingMsgIdx = global._editingMsgIdx = null;
win._activeBranch = global._activeBranch = null;
win._openScrollConvId = global._openScrollConvId = null;
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
// Never-equal fingerprint so Guard 2 never SKIPS the surgical re-render.
win._convRenderFingerprint = global._convRenderFingerprint =
  (c) => 'fp:' + (c ? c.messages.length : 0) + ':' + Math.random();

// jsdom has no layout engine — give the container the geometry
// _loadOlderMessages reads for its scroll compensation.
const _ct = win.document.getElementById('chatContainer');
Object.defineProperty(_ct, 'scrollHeight', { get: () => 5000, configurable: true });
_ct.scrollTop = 0;

let chatSrc = fs.readFileSync(process.argv[2], 'utf8');
const FIXED = process.argv[7];
const LEGACY = process.argv[8];
if (NC === 'firstchild') {
  // NEUTER: restore the pre-fix anchor — fall back to `inner.firstChild`,
  // which is the SENTINEL when a lazy window is active.
  if (chatSrc.indexOf(FIXED) === -1) {
    console.log('FAIL neuter_not_applied (fixed anchor sentinel absent)');
    console.log(out.join('\n')); process.exit(0);
  }
  chatSrc = chatSrc.replace(FIXED, LEGACY);
}

(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // safe_html.js
(0, eval)(fs.readFileSync(process.argv[3].replace('escape_html.js', 'translation_model.js'), 'utf8'));
(0, eval)(fs.readFileSync(process.argv[3].replace('core/escape_html.js', 'ui/translation_indicator.js'), 'utf8'));

// streaming_render.js + chat_render.js CONCATENATED into one script, exactly as
// lib/js_bundler.py emits them — see the module docstring for why.
const streamSrc = fs.readFileSync(process.argv[3].replace('core/escape_html.js', 'ui/streaming_render.js'), 'utf8');
const api = (0, eval)(
  streamSrc + '\n;\n' + chatSrc + '\n;({renderChat, _loadOlderMessages});');
const renderChat = api.renderChat;
const _loadOlderMessages = api._loadOlderMessages;
if (typeof renderChat !== 'function' || typeof _loadOlderMessages !== 'function') {
  console.log('FAIL fns_exposed'); console.log(out.join('\n')); process.exit(0);
}
check('fns_exposed', true);

function mkMsg(id, role, text) {
  return { role: role || 'assistant', _msgId: id, content: text || ('body ' + id) };
}
function layout() {
  const inner = win.document.getElementById('chatInner');
  return Array.from(inner.children).map(el =>
    el.id === '_lazyLoadSentinel' ? 'SENTINEL'
      : el.id === '_lazyLoadSentinelBottom' ? 'SENT_BOT'
      : (el.getAttribute('data-msg-id') || el.id));
}

// 24 messages; _INITIAL_RENDER is 20 → the first paint renders m4..m23 plus the
// head sentinel standing in for the 4 older ones.
const msgs = [];
for (let i = 0; i < 24; i++) msgs.push(mkMsg('m' + i, i % 2 ? 'assistant' : 'user', 'body ' + i));
const conv = { id: 'c1', messages: msgs };
win.conversations = global.conversations = [conv];
win.getActiveConv = global.getActiveConv = () => conv;

// ── 1) Conversation open (full render). ──
renderChat(conv, true);
check('seed_sentinel_at_head', layout()[0] === 'SENTINEL', 'layout0=' + layout()[0]);

// ── 2) Background repaints — this is how cost / file-change / compaction data
//       lands on every conversation open. Each one runs the surgical path. ──
for (let k = 0; k < 3; k++) renderChat(conv, false);

// INVARIANT A: the sentinel is layout furniture pinned at the HEAD. If the
// reconcile treats it as a message node it gets pushed to the bottom here.
check('sentinel_stays_at_head', layout()[0] === 'SENTINEL',
  'layout=' + layout().join(',').slice(0, 200));

// ── 3) Reader scrolls up → the IntersectionObserver fires the REAL loader,
//       which splices the older batch in with `sentinel.after(frag)`. ──
_loadOlderMessages();

// INVARIANT B: DOM order == conv.messages order. This is the user-visible fact
// ("first-round user message renders at the bottom").
const L = layout().filter(x => x !== 'SENTINEL' && x !== 'SENT_BOT');
const want = conv.messages.map(m => m._msgId).filter(id => L.indexOf(id) !== -1);
check('dom_order_matches_messages', L.join(',') === want.join(','),
  'dom=' + L.join(',').slice(0, 200));

// Explicit oldest-vs-newest probe — states the reported symptom directly, so a
// failure names the bug rather than just "order differs".
const idxM0 = L.indexOf('m0');
const idxM23 = L.indexOf('m23');
check('oldest_renders_above_newest', idxM0 >= 0 && idxM0 < idxM23,
  'm0@' + idxM0 + ' m23@' + idxM23);

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_lazy_sentinel_anchor_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             CHAT_RENDER,     # argv[2]
             ESCAPE_HTML,     # argv[3]
             SAFE_HTML,       # argv[4]
             ROOT,            # argv[5]
             nc,              # argv[6]
             _FIXED_ANCHOR,   # argv[7]
             _LEGACY_ANCHOR,  # argv[8]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


def _lines(output):
    return {ln[5:].split(' ')[0]: ln[:4].strip()
            for ln in output.splitlines() if ln[:4].strip() in ('PASS', 'FAIL')}


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_lazy_sentinel_stays_at_head_and_order_holds():
    """Background repaints must not migrate #_lazyLoadSentinel down, and after a
    real _loadOlderMessages the DOM order must equal conv.messages order."""
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'lazy-sentinel anchor failures:\n' + output
    lines = _lines(output)
    for key in ('seed_sentinel_at_head', 'sentinel_stays_at_head',
                'dom_order_matches_messages', 'oldest_renders_above_newest'):
        assert lines.get(key) == 'PASS', f'{key} not PASS:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_firstchild_anchor_pushes_sentinel_down_and_inverts_order():
    """NEUTER: restore the `inner.firstChild` fallback (pre-fix code). The
    sentinel then migrates to the bottom and _loadOlderMessages splices the
    OLDEST messages below the newest — proving the fix is load-bearing."""
    output = _run('firstchild')
    assert 'FAIL neuter_not_applied' not in output, (
        'the fixed anchor `_headAnchor()` is absent from chat_render.js — the '
        f'fix has not landed (or was renamed without updating this guard):\n{output}')
    lines = _lines(output)
    assert lines.get('sentinel_stays_at_head') == 'FAIL', (
        'sentinel did NOT migrate under the legacy anchor — the guard no longer '
        f'reproduces the bug it protects against:\n{output}')
    assert (lines.get('dom_order_matches_messages') == 'FAIL'
            or lines.get('oldest_renders_above_newest') == 'FAIL'), (
        'message order survived the migrated sentinel — the order invariant is '
        f'not actually protected by this anchor:\n{output}')


# ══════════════════════════════════════════════════════════════════════
#  2. THE MIRROR BUG AT THE TAIL — ConvView vs the BOTTOM sentinel
# ══════════════════════════════════════════════════════════════════════

CONV_VIEW = os.path.join(JS_DIR, 'conv_view.js')
CHATINNER_DOM = os.path.join(JS_DIR, 'core', 'chatinner_dom.js')

_TAIL_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[3] || '';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const out = [];
function check(n, c, e) { out.push((c ? 'PASS ' : 'FAIL ') + n + (e ? ' ' + e : '')); }

/* Capture the production beacon so the invariant's reach can be asserted. */
let beacons = [];
win.Api = global.Api = { clientError: { report: (p) => { beacons.push(p); } } };

global.activeConvId = win.activeConvId = 'c1';
const conv = { id: 'c1', messages: [] };
global.conversations = win.conversations = [conv];
global._ensureMsgId = win._ensureMsgId = (m) => {
  if (!m._msgId) m._msgId = 'tmp_' + Math.random().toString(36).slice(2);
  return m._msgId;
};
global.renderMessage = win.renderMessage = (m, i) =>
  `<div class="message" id="msg-${i}" data-msg-id="${m._msgId}">${m.content}</div>`;
global._streamingBubbleHTML = win._streamingBubbleHTML = (role, st, tm, id) =>
  `<div class="message" id="streaming-msg"${id ? ` data-msg-id="${id}"` : ''}>live</div>`;
global._lastRenderedFingerprint = win._lastRenderedFingerprint = '';
global._convRenderFingerprint = win._convRenderFingerprint = () => 'fp';

// The ordered-insert primitive must load BEFORE its consumer, exactly as the
// bundler orders them.
let primSrc = '';
try { primSrc = fs.readFileSync(path.join(ROOT, 'static/js/core/chatinner_dom.js'), 'utf8'); }
catch (e) {
  console.log('FAIL primitive_missing core/chatinner_dom.js not found');
  console.log(out.join('\n')); process.exit(0);
}
if (NC === 'tail_anchor') {
  // NEUTER: make the TAIL anchor degenerate to a plain append (pre-fix).
  const before = primSrc;
  primSrc = primSrc.replace(/function chatInnerTailAnchor\(inner\) \{[\s\S]*?\n\}/,
    'function chatInnerTailAnchor(inner) { return null; }');
  if (primSrc === before) {
    console.log('FAIL neuter_tail_not_applied'); console.log(out.join('\n')); process.exit(0);
  }
}
(0, eval)(primSrc);
(0, eval)(fs.readFileSync(path.join(ROOT, 'static/js/conv_view.js'), 'utf8'));
if (!win.ConvView) { console.log('FAIL convview_missing'); console.log(out.join('\n')); process.exit(0); }
check('convview_loaded', true);

const inner = document.getElementById('chatInner');
function layout() {
  return Array.from(inner.children).map(el =>
    el.id === '_lazyLoadSentinelBottom' ? 'SENT_BOT'
      : (el.getAttribute('data-msg-id') || el.id));
}

// Seed two settled bubbles, then a BOTTOM sentinel built exactly the way
// _ensureBottomSentinel builds it (inner.appendChild -> it is the last child).
[['a', 0], ['b', 1]].forEach(([id, i]) => {
  const m = { _msgId: id, role: 'user', content: 'body ' + id };
  conv.messages.push(m);
  inner.insertAdjacentHTML('beforeend', global.renderMessage(m, i));
});
const sent = document.createElement('div');
sent.id = '_lazyLoadSentinelBottom';
sent.className = 'lazy-sentinel';
inner.appendChild(sent);
check('seed_bottom_sentinel_is_last',
  layout()[layout().length - 1] === 'SENT_BOT', layout().join(','));

// The user SENDS a message -> main_send_pipeline calls ConvView.apply.
const newMsg = { _msgId: 'NEW', role: 'user', content: 'my new message' };
conv.messages.push(newMsg);
win.ConvView.apply('c1', conv.messages.length - 1, newMsg);
let L = layout();
check('sent_msg_above_bottom_sentinel',
  L.indexOf('NEW') >= 0 && L.indexOf('NEW') < L.indexOf('SENT_BOT'),
  'layout=' + L.join(','));

// ...and the live streaming bubble takes the same beforeend path.
// NB: layout() maps a node to its data-msg-id when present, so the live bubble
// appears as 'LIVE' (the msgId we stamped), not as 'streaming-msg'.
win.ConvView.startStreaming('c1', { role: 'worker', msgId: 'LIVE' });
L = layout();
check('streaming_bubble_above_bottom_sentinel',
  L.indexOf('LIVE') >= 0 && L.indexOf('LIVE') < L.indexOf('SENT_BOT'),
  'layout=' + L.join(','));

// The bottom sentinel must still be the LAST child — it is the anchor
// _loadNewerMessages splices against with sentinel.before(frag).
check('bottom_sentinel_still_last',
  layout()[layout().length - 1] === 'SENT_BOT', 'layout=' + layout().join(','));

/* ── ★ Does the runtime invariant COVER this tail path? ──────────────────
 * Under the tail_anchor NEUTER the primitive degrades to a plain append, so
 * ConvView.apply reproduces the original bug. The invariant must then REPORT
 * — naming ConvView.apply, not renderChat. That is the whole point of moving
 * the check to the chatInnerInsert chokepoint: proving the tail writers are
 * watched, not only the two renderChat exits that were already fixed. */
if (NC === 'tail_anchor') {
  check('neutered_tail_reported_by_invariant',
    win.chatInnerOrderViolated() === true,
    'violated=' + win.chatInnerOrderViolated());
  check('neutered_tail_names_convview',
    String(win.chatInnerOrderViolationSite()).indexOf('ConvView') !== -1,
    'site=' + win.chatInnerOrderViolationSite());
  check('neutered_tail_beaconed', beacons.length >= 1, 'beacons=' + beacons.length);
} else {
  /* Healthy primitive: the same writes must leave the invariant SILENT. */
  check('healthy_tail_no_violation',
    win.chatInnerOrderViolated() === false && beacons.length === 0,
    'violated=' + win.chatInnerOrderViolated() + ' beacons=' + beacons.length);
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run_tail(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_lazy_tail_anchor_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_TAIL_HARNESS)
    try:
        proc = subprocess.run(['node', harness, ROOT, nc],
                              capture_output=True, text=True, timeout=60)
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
def test_send_and_stream_land_above_the_bottom_sentinel():
    """THE MIRROR OF THE HEAD BUG. With a bottom sentinel present (a settled
    conversation past _MAX_RENDER_WINDOW that the reader scrolled up in), a
    sent message and the streaming bubble must render ABOVE it — the sentinel
    is layout furniture and must stay the last child, because
    _loadNewerMessages splices the recovered tail with sentinel.before()."""
    output = _run_tail('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'tail-anchor failures:\n' + output
    lines = _lines(output)
    for key in ('seed_bottom_sentinel_is_last', 'sent_msg_above_bottom_sentinel',
                'streaming_bubble_above_bottom_sentinel',
                'bottom_sentinel_still_last', 'healthy_tail_no_violation'):
        assert lines.get(key) == 'PASS', f'{key} not PASS:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_tail_anchor_lets_sends_fall_below_the_sentinel():
    """NEUTER: degenerate the tail anchor to a plain append (the pre-fix
    `beforeend`). The sent message and the streaming bubble then land BELOW
    the bottom sentinel — proving the tail anchor is load-bearing."""
    output = _run_tail('tail_anchor')
    assert 'FAIL neuter_tail_not_applied' not in output, (
        'chatInnerTailAnchor was renamed without updating this NEUTER — the '
        f'guard would silently stop biting:\n{output}')
    lines = _lines(output)
    assert (lines.get('sent_msg_above_bottom_sentinel') == 'FAIL'
            or lines.get('streaming_bubble_above_bottom_sentinel') == 'FAIL'), (
        'a degenerate tail anchor did NOT break ordering — the guard no longer '
        f'reproduces the bug it protects against:\n{output}')
    # ★ And the RUNTIME invariant must have caught it on the ConvView path —
    # the coverage that did not exist when the check lived only on renderChat.
    for key in ('neutered_tail_reported_by_invariant',
                'neutered_tail_names_convview', 'neutered_tail_beaconed'):
        assert lines.get(key) == 'PASS', (
            f'{key} not PASS — the invariant does not cover the tail writers, '
            f'so it is still blind to the shape it exists to catch:\n{output}')


# ═══════════════════════════════════════════════════════════════════════════
#  3. STATIC GUARD — nobody but the primitive may positionally write #chatInner
# ═══════════════════════════════════════════════════════════════════════════

# Raw positional writes that land relative to the CHILD LIST, so lazy-window
# furniture (#_lazyLoadSentinel / #_lazyLoadSentinelBottom) silently sorts
# wrong. `insertBefore` is absent on purpose: it takes an explicit anchor, and
# the reconcile legitimately computes precise siblings — the bug class here is
# the ANCHORLESS append / firstChild-as-anchor form.
_RAW_WRITE_PATTERNS = [
    (r"insertAdjacentHTML\(\s*['\"]beforeend['\"]", "insertAdjacentHTML('beforeend')"),
    (r"insertAdjacentHTML\(\s*['\"]afterbegin['\"]", "insertAdjacentHTML('afterbegin')"),
    (r"\.appendChild\(", '.appendChild()'),
    (r"\.prepend\(", '.prepend()'),
]

# Files allowed to touch #chatInner positionally.
#   * core/chatinner_dom.js  — IS the primitive.
#   * ui/streaming_render.js — OWNS the furniture (_ensureBottomSentinel pins
#     the strip with appendChild; _loadOlderMessages / _loadNewerMessages
#     splice relative to their own sentinel). The furniture owner is exactly
#     who is allowed to position furniture.
#   * conv_view.js — retains ONE `beforeend` inside the `_cvInsert` fallback,
#     a loud build-order canary for a missing primitive.
_POSITIONAL_ALLOWLIST = {
    'core/chatinner_dom.js',
    'ui/streaming_render.js',
    'conv_view.js',
}


def _js_files():
    for dirpath, _dirs, files in os.walk(JS_DIR):
        for fn in files:
            if not fn.endswith('.js'):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, JS_DIR).replace(os.sep, '/')
            # Built bundles are generated artefacts, not sources.
            if rel.startswith('bundle-') or rel.startswith('feature-'):
                continue
            yield rel, full


def _strip_comments(src: str) -> str:
    """Blank out // and /* */ comments so prose describing the bug is not an
    offender, while PRESERVING line numbers (each removed line becomes empty)
    so reported locations point at the real source line.

    Delegates to the SINGLE shared implementation (charter #24), and doing so is
    a CORRECTNESS FIX rather than a lateral move: the local
    ``re.sub(r'/\\*.*?\\*/', _blank, ...)`` this replaced BROKE the very promise
    in the sentence above. Measured on this guard's largest target,
    ``static/js/ui/chat_render.js`` (2097 source lines), the local pass returned
    2093 — four lines short, so every reported location past that point named
    the wrong source line. The shared ``inline=True`` pass returns exactly 2097.
    On the other four scanned files the two agree line-for-line.

    (The shared pass also had a trailing-line off-by-one, found while measuring
    this; it is fixed in tests/_source_scan.py and pinned by
    test_source_scan_primitives.py::test_inline_mode_preserves_line_count.)
    """
    from tests._source_scan import strip_comments
    return strip_comments(src, lang='js', inline=True)


def test_no_file_positionally_writes_chatinner_outside_the_primitive():
    """THE COMMIT-TIME GUARD — this is what would have caught the tail bug.

    #chatInner's child list mixes messages with lazy-window furniture. Any
    anchorless positional write ('beforeend' / appendChild / prepend /
    'afterbegin') sorts relative to that furniture and therefore relative to
    NOTHING meaningful — which is how a sent message ended up below the bottom
    sentinel and the oldest messages ended up below the newest.

    Rule: route the write through core/chatinner_dom.js::chatInnerInsert, or
    add the file to _POSITIONAL_ALLOWLIST with a reason (furniture owners
    qualify; message writers do not).

    Scope: only writes whose RECEIVER is the #chatInner element. Other
    containers (document.body, a modal, a toast host) are none of this
    invariant's business — flagging them would make the guard noise, and noise
    gets tuned out.
    """
    # Local names bound to the #chatInner element, per the codebase's own
    # idiom: `const inner = document.getElementById('chatInner')`.
    binder = re.compile(
        r"(?:const|let|var)\s+(\w+)\s*=\s*[^;\n]*getElementById\(\s*['\"]chatInner['\"]")
    offenders = []
    for rel, full in _js_files():
        if rel in _POSITIONAL_ALLOWLIST:
            continue
        with open(full, encoding='utf-8') as f:
            src = _strip_comments(f.read())
        if 'chatInner' not in src:
            continue
        receivers = set(binder.findall(src))
        receivers.add('chatInner')
        lines = src.splitlines()
        for line_no, line in enumerate(lines, 1):
            for pat, label in _RAW_WRITE_PATTERNS:
                m = re.search(pat, line)
                if not m:
                    continue
                left = line[:m.start()]
                recv = re.search(r"([\w$]+)\s*\.?\s*$", left)
                recv_name = recv.group(1) if recv else ''
                inline_inner = ("getElementById('chatInner')" in left
                                or 'getElementById("chatInner")' in left)
                if not (recv_name in receivers or inline_inner):
                    continue
                # ALLOWED: the raw call sits inside a
                # `typeof chatInnerInsert === 'function'` availability guard —
                # i.e. it is the build-order canary for a missing primitive,
                # not a bypass of it.
                window = '\n'.join(lines[max(0, line_no - 8):line_no])
                if 'chatInnerInsert' in window:
                    continue
                offenders.append(
                    f'{rel}:{line_no}: {label} on #chatInner — '
                    f'{line.strip()[:110]}')
    assert not offenders, (
        'Raw positional write(s) into #chatInner. These sort relative to '
        'lazy-window furniture and silently invert message order (the head bug '
        'in f1691021 and its tail mirror). Route through '
        'core/chatinner_dom.js::chatInnerInsert, or allowlist the file if it '
        'OWNS the furniture:\n' + '\n'.join(offenders))


def _bundler_list(name):
    """Parse a top-level list literal out of the REAL lib/js_bundler.py, so
    this invariant can never drift from what the bundler actually ships."""
    with open(os.path.join(ROOT, 'lib', 'js_bundler.py'), encoding='utf-8') as f:
        src = f.read()
    marker = name + ' = ['
    start = src.index(marker) + len(marker)
    depth, i = 1, start
    while i < len(src) and depth:
        if src[i] == '[':
            depth += 1
        elif src[i] == ']':
            depth -= 1
        i += 1
    return re.findall(r"'([^']+\.js)'", src[start:i])


def test_primitive_loads_before_every_writer():
    """BUILD-ORDER INVARIANT. Both writers resolve `chatInnerInsert` as a
    cross-file runtime lookup and fall back to the raw pre-fix behaviour when
    it is absent — so a deferred/reordered primitive silently reintroduces the
    exact bug. Same trap the identity gate documents: deferring a consumer
    away from its predicate degrades it, quietly.
    """
    bundle = _bundler_list('_BUNDLE_FILES')
    deferred = _bundler_list('_DEFERRED_FILES') or []
    assert bundle, 'could not parse _BUNDLE_FILES from lib/js_bundler.py'

    primitive = 'core/chatinner_dom.js'
    writers = ['ui/chat_render.js', 'conv_view.js']

    assert primitive in bundle, (
        f'{primitive} must be in _BUNDLE_FILES — it owns the only '
        'furniture-aware insert; without it every writer falls back to a raw '
        'append and message order silently inverts.')
    assert primitive not in deferred, (
        f'{primitive} was moved into _DEFERRED_FILES. Its writers fall back to '
        'raw positional appends when it is missing, so deferring it '
        'reintroduces the ordering bug it exists to prevent.')
    p_idx = bundle.index(primitive)
    for w in writers:
        assert w in bundle, (
            f'{w} is not in _BUNDLE_FILES — the build-order invariant can no '
            'longer be verified')
        assert p_idx < bundle.index(w), (
            f'ORDER VIOLATION: {primitive} (idx {p_idx}) must load BEFORE '
            f'{w} (idx {bundle.index(w)}). {w} resolves chatInnerInsert at '
            'runtime and degrades to a raw append when it is undefined.')


# ═══════════════════════════════════════════════════════════════════════════
#  4. RUNTIME ORDER INVARIANT — catches the SHAPE, not a remembered scenario
# ═══════════════════════════════════════════════════════════════════════════

_INVARIANT_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NC = process.argv[3] || '';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document;

const out = [];
function check(n, c, e) { out.push((c ? 'PASS ' : 'FAIL ') + n + (e ? ' ' + e : '')); }

let warned = [];
let beacons = [];
global.console = { warn: (m) => warned.push(String(m)), log: console.log, error: console.error };
/* The production beacon the invariant rides (Api.clientError.report →
 * POST /api/client-error). Capturing it proves the signal LEAVES the page. */
win.Api = global.Api = { clientError: { report: (p) => { beacons.push(p); } } };

let src = fs.readFileSync(path.join(ROOT, 'static/js/core/chatinner_dom.js'), 'utf8');
if (NC === 'invariant') {
  const before = src;
  src = src.replace(/function assertChatInnerOrder\(inner, conv, site\) \{/,
                    'function assertChatInnerOrder(inner, conv, site) { return true;');
  if (src === before) { console.log('FAIL neuter_invariant_not_applied'); console.log(out.join('\n')); process.exit(0); }
}
(0, eval)(src);

const inner = document.getElementById('chatInner');
function seed(html) { inner.innerHTML = html; win.resetChatInnerOrderForTests(); warned = []; beacons = []; }
const msg = (id) => `<div class="message" data-msg-id="${id}"></div>`;
const HEAD = '<div id="_lazyLoadSentinel"></div>';
const conv = { id: 'c1', messages: [{_msgId:'a'},{_msgId:'b'},{_msgId:'c'},{_msgId:'d'}] };

// 1) Healthy: sentinel at head standing in for elided 'a', then b,c,d in order.
seed(HEAD + msg('b') + msg('c') + msg('d'));
check('healthy_passes', win.assertChatInnerOrder(inner, conv, 't1') === true);
check('healthy_is_silent', warned.length === 0 && beacons.length === 0,
  'warned=' + warned.length + ' beacons=' + beacons.length);

// 2) Out of order — the user-visible symptom (oldest below newest).
seed(msg('c') + msg('d') + msg('a') + msg('b'));
check('out_of_order_detected', win.assertChatInnerOrder(inner, conv, 't2') === false);
check('out_of_order_named_site', win.chatInnerOrderViolationSite() === 't2',
  'site=' + win.chatInnerOrderViolationSite());
check('out_of_order_reported', warned.length === 1 &&
  warned[0].indexOf('RENDER ORDER VIOLATION') !== -1, 'warned=' + warned.length);

// 3) Misplaced sentinel BETWEEN array-adjacent messages — the head bug caught
//    mid-migration, BEFORE it drifts far enough to invert anything.
seed(msg('a') + HEAD + msg('b') + msg('c'));
check('misplaced_sentinel_detected', win.assertChatInnerOrder(inner, conv, 't3') === false);
check('misplaced_sentinel_message', warned.length === 1 &&
  warned[0].indexOf('MISPLACED SENTINEL') !== -1, warned[0] ? warned[0].slice(0, 80) : '');

// 4) Latched: a violation recurs on every repaint; it must report ONCE.
seed(msg('c') + msg('a'));
win.assertChatInnerOrder(inner, conv, 't4');
win.assertChatInnerOrder(inner, conv, 't4');
win.assertChatInnerOrder(inner, conv, 't4');
check('violation_is_latched', warned.length === 1 && beacons.length === 1,
  'warned=' + warned.length + ' beacons=' + beacons.length);

// 5) ★ PRODUCTION VISIBILITY (the owner-ratified decision). debug_mode is
//    False on every real deployment; a debug-gated invariant is therefore
//    inert exactly where both real bugs happened. It must detect and BEACON
//    regardless of the flag.
seed(msg('c') + msg('a'));
win._featureFlags = { debug_mode: false };
check('detects_with_debug_off', win.assertChatInnerOrder(inner, conv, 't5') === false);
check('beacons_with_debug_off', beacons.length === 1 &&
  String(beacons[0].message).indexOf('RENDER ORDER VIOLATION') !== -1,
  'beacons=' + beacons.length);
check('beacon_carries_site', beacons.length === 1 && beacons[0].extra &&
  beacons[0].extra.site === 't5', 'site=' + (beacons[0] && beacons[0].extra && beacons[0].extra.site));

// 6) A legal lazy WINDOW (contiguous subset, furniture at both ends) passes —
//    otherwise the invariant would cry wolf on normal operation.
seed('<div id="_lazyLoadSentinel"></div>' + msg('b') + msg('c') +
     '<div id="_lazyLoadSentinelBottom"></div>');
check('lazy_window_subset_passes',
  win.assertChatInnerOrder(inner, conv, 't6') === true && warned.length === 0,
  'warned=' + warned.length);

// 7) ★ THE CHOKEPOINT: chatInnerInsert itself must run the invariant when the
//    caller passes conv. This is what makes the TAIL path covered — the shape
//    that was dark when the check lived only on renderChat's exits.
seed(msg('a') + msg('b') + '<div id="_lazyLoadSentinelBottom"></div>');
const convT = { id: 'c1', messages: [{_msgId:'a'},{_msgId:'b'},{_msgId:'NEW'}] };
/* Force the PRE-FIX tail behaviour by handing the primitive an explicit
 * `before: null` anchor — i.e. a plain append, which lands BELOW the bottom
 * sentinel exactly as the old raw `beforeend` did. */
win.chatInnerInsert(inner, msg('NEW'),
  { before: null, conv: convT, site: 'probe.tailAppend' });
check('chokepoint_detects_tail_violation', win.chatInnerOrderViolated() === true);
check('chokepoint_names_caller',
  win.chatInnerOrderViolationSite() === 'probe.tailAppend',
  'site=' + win.chatInnerOrderViolationSite());
check('chokepoint_beacons', beacons.length === 1, 'beacons=' + beacons.length);

// 8) ...and a CORRECT furniture-aware tail insert through the same chokepoint
//    stays silent (the invariant is not just "always fires on insert").
seed(msg('a') + msg('b') + '<div id="_lazyLoadSentinelBottom"></div>');
win.chatInnerInsert(inner, msg('NEW'),
  { position: 'tail', conv: convT, site: 'probe.tailCorrect' });
check('chokepoint_silent_when_correct',
  win.chatInnerOrderViolated() === false && beacons.length === 0,
  'violated=' + win.chatInnerOrderViolated() + ' beacons=' + beacons.length);

console.log(out.join('\n'));
process.exit(0);
"""


def _run_invariant(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_chatinner_invariant_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_INVARIANT_HARNESS)
    try:
        proc = subprocess.run(['node', harness, ROOT, nc],
                              capture_output=True, text=True, timeout=60)
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
def test_runtime_order_invariant_detects_both_bug_shapes():
    """The invariant must catch BOTH shapes — an out-of-order projection and a
    sentinel misplaced between array-adjacent messages — while staying silent
    on a healthy render and on a legal lazy window.

    It must ALSO fire with ``debug_mode`` OFF (the production default) and
    reach the server over the existing client-error beacon, and it must run
    from inside ``chatInnerInsert`` so the TAIL writers are covered — not just
    renderChat's exits, which is where it was structurally blind."""
    output = _run_invariant('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'runtime order-invariant failures:\n' + output
    lines = _lines(output)
    for key in ('healthy_passes', 'healthy_is_silent', 'out_of_order_detected',
                'out_of_order_named_site', 'out_of_order_reported',
                'misplaced_sentinel_detected', 'misplaced_sentinel_message',
                'violation_is_latched',
                'detects_with_debug_off', 'beacons_with_debug_off',
                'beacon_carries_site',
                'lazy_window_subset_passes',
                'chokepoint_detects_tail_violation', 'chokepoint_names_caller',
                'chokepoint_beacons', 'chokepoint_silent_when_correct'):
        assert lines.get(key) == 'PASS', f'{key} not PASS:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_invariant_short_circuit_goes_blind():
    """NEUTER: make the invariant return true unconditionally → both detection
    faces go red. Proves the walk (not the assertion) is what sees the break."""
    output = _run_invariant('invariant')
    assert 'FAIL neuter_invariant_not_applied' not in output, (
        f'assertChatInnerOrder was renamed without updating this NEUTER:\n{output}')
    lines = _lines(output)
    assert lines.get('out_of_order_detected') == 'FAIL', (
        f'a short-circuited invariant still reported disorder:\n{output}')
    assert lines.get('misplaced_sentinel_detected') == 'FAIL', (
        f'a short-circuited invariant still reported a misplaced sentinel:\n{output}')
