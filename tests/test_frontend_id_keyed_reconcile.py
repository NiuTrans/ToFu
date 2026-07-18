"""tests/test_frontend_id_keyed_reconcile.py — RENDER_CONTRACT Phase 1 guard.

WHY
---
`renderChat`'s surgical-diff path (static/js/ui/chat_render.js) historically
matched an existing bubble by `document.getElementById("msg-" + i)` (the ARRAY
POSITION) and removed stale nodes by `idx >= total || idx < startIdx` (also
position). Under index drift — a message inserted/removed mid-history, a
placeholder splice, a lazy-window offset — the position of a stable message
changes, so:

  • the position-keyed lookup MISSES the real node for a message and re-appends
    a second one → TWO identical bubbles for ONE conv.messages entry (the
    reported "twin bubble"); and/or
  • the wrong node is reused for a different message's content → a row
    COLLAPSES onto its neighbour's identity.

`docs/RENDER_CONTRACT.md` Invariant 2 mandates: the surgical reconcile MUST key
on the stable `_msgId` (already mirrored to `data-msg-id` by renderMessage),
never the mutable array index. This harness drives the REAL shipped renderChat
under jsdom across an index-shifting edit and asserts:

  1. after a mid-history INSERT, every conv.messages entry maps to exactly ONE
     DOM node (no twin), keyed by its `_msgId`;
  2. each rendered node's `data-msg-id` matches the message now at that array
     position (no identity collapse / cross-wiring);
  3. the `id="msg-N"` attribute is still present on every node (the positional
     handle other subsystems — edit_message.js / streaming_render.js — depend
     on is NOT removed, only supplemented by id-keyed matching).

NEUTER: force the reconcile back to positional matching (rewrite the id-keyed
lookup to the old `getElementById('msg-'+i)`), and prove the twin/identity
assertions then FAIL — i.e. id-keying is what makes the invariant hold.

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
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


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
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { if (typeof fn === 'function') fn(); return 0; };
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const out = [];
function check(name, cond, extra) { out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? (' ' + extra) : '')); }

// ── Idle conv: no live stream / task so renderChat takes the static surgical
//    path (not the streaming delegate) and finish bars are quiet. ──
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
  '_injectAnchoredBranches','_prefetchConvCosts','_prefetchConvFileChanges',
  '_stampFreshness','buildTurnNav','calcCostCny','_forceScrollToBottom',
  'scrollToBottom','isNearBottom','showStreamingUIForConv','_ensureLazyObserver',
  '_destroyLazyObserver','_captureScrollAnchor','_restoreScrollAnchor',
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; }
}
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;
// _prefetchConvCosts / _prefetchConvFileChanges are awaited via .then in
// renderChat — make them thenable-returning so the .then callback is inert.
win._prefetchConvCosts = global._prefetchConvCosts = () => ({ then: () => {} });
win._prefetchConvFileChanges = global._prefetchConvFileChanges = () => ({ then: () => {} });

// ── Cross-file module globals renderChat READS as free identifiers. They are
//    declared with `let` in core.js / other bundle files; since we eval only
//    chat_render.js standalone, they resolve against the global scope, so seed
//    them here (reads AND writes both then resolve). ──
win._editingMsgIdx = global._editingMsgIdx = null;
win._activeBranch = global._activeBranch = null;
win._lazyRenderedFrom = global._lazyRenderedFrom = 0;
win._lazyRenderedTo = global._lazyRenderedTo = Infinity;
win._lazyConvId = global._lazyConvId = null;
win._openScrollConvId = global._openScrollConvId = null;
win._lastRenderedFingerprint = global._lastRenderedFingerprint = '';
// Never-equal fingerprint so Guard 2 never SKIPS the surgical re-render (we
// want the surgical path to actually run each call). A real _convRenderFingerprint
// lives in core.js; its exact value is irrelevant to the reconcile-matching test.
win._convRenderFingerprint = global._convRenderFingerprint =
  (c) => 'fp:' + (c ? c.messages.length : 0) + ':' + Math.random();

let chatSrc = fs.readFileSync(process.argv[2], 'utf8');
if (NC === 'positional') {
  // NEUTER: force the reconcile back to POSITIONAL matching. Rewrite the
  // id-keyed element lookup helper to always resolve by array index, exactly
  // as the pre-Phase-1 code did. The token below is the sentinel the Phase-1
  // patch introduces; if it's absent the patch hasn't landed yet.
  const before = chatSrc;
  chatSrc = chatSrc.replace(
    /const el = _reconcileFindEl\([^;]*\);/,
    'const el = document.getElementById("msg-" + i);');
  if (chatSrc === before) { console.log('FAIL neuter_positional_not_applied (Phase-1 sentinel _reconcileFindEl absent)'); console.log(out.join('\n')); process.exit(0); }
}

(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // safe_html.js
(0, eval)(fs.readFileSync(process.argv[3].replace('escape_html.js', 'translation_model.js'), 'utf8'));
(0, eval)(fs.readFileSync(process.argv[3].replace('core/escape_html.js', 'ui/translation_indicator.js'), 'utf8'));
(0, eval)(chatSrc);  // chat_render.js (real / neutered)

if (typeof renderChat !== 'function') { console.log('FAIL fn_exposed renderChat missing'); process.exit(0); }
check('fn_exposed', true);

function mkMsg(id, role, text) {
  return { role: role || 'assistant', _msgId: id, content: text || ('body ' + id) };
}
function domNodes() {
  const inner = win.document.getElementById('chatInner');
  return Array.from(inner.querySelectorAll('[id^="msg-"]'));
}
function idOf(el) { return el.getAttribute('data-msg-id'); }

// A helper conv with N messages, each carrying a STABLE _msgId.
const conv = { id: 'c1', messages: [
  mkMsg('a', 'user', 'question A'),
  mkMsg('b', 'assistant', 'answer B'),
  mkMsg('c', 'user', 'question C'),
  mkMsg('d', 'assistant', 'answer D'),
] };
win.conversations = global.conversations = [conv];
win.getActiveConv = global.getActiveConv = () => conv;

// ── 1) Full render to seed the DOM (forceScroll=true takes the full path). ──
renderChat(conv, true);
let nodes = domNodes();
check('seed_four_nodes', nodes.length === 4, 'got=' + nodes.length);
check('seed_ids_in_order',
  nodes.map(idOf).join(',') === 'a,b,c,d',
  'got=' + nodes.map(idOf).join(','));

// The seed full-render pinned _lazyRenderedTo to the (then) total. Reset the
// window to uncapped so the bounded-window cap — a SEPARATE concern covered by
// test_frontend_bounded_render_window.py — doesn't drop the freshly-inserted
// tail. This test isolates the reconcile MATCHING logic, so the whole span is
// in scope.
win._lazyRenderedTo = global._lazyRenderedTo = Infinity;

// ── 2) Stamp a survivable JS marker on the DOM nodes for c and d BEFORE the
//       structural edit. A DOM node's expando property survives iff the node
//       object is REUSED; it is lost the instant the node is destroyed and
//       rebuilt (outerHTML / replaceWith parse a fresh node object). This is
//       the real render-state that positional matching silently discards on
//       every index shift — an expanded tool <details>, a translation-preview
//       zone keyed by data-msg-id, the __bgHtml stamp. The marker is our proxy
//       for all of it. ──
function stamp(id, val) {
  const el = win.document.querySelector('[data-msg-id="' + id + '"]');
  if (el) el.__preserved = val;
  return el;
}
const cNode0 = stamp('c', 'KEEP_C');
const dNode0 = stamp('d', 'KEEP_D');
check('pre_stamp_ok', !!cNode0 && !!dNode0);

// ── 3) INSERT a message in the MIDDLE (index drift for c,d) then surgical
//       re-render (forceScroll=false). c/d shift from idx 2/3 → 3/4. ──
conv.messages.splice(2, 0, mkMsg('x', 'assistant', 'inserted X'));
// messages are now: a, b, X, c, d
renderChat(conv, false);

nodes = domNodes();
const ids = nodes.map(idOf);

// (a) exactly one DOM node per message — NO twin.
check('no_twin_total_count', nodes.length === 5, 'got=' + nodes.length + ' ids=' + ids.join(','));
const seen = {};
let dup = null;
for (const id of ids) { if (seen[id]) dup = id; seen[id] = (seen[id] || 0) + 1; }
check('no_duplicate_msgid', dup === null, dup ? ('duplicated=' + dup) : '');

// (b) every conv.messages _msgId is present exactly once (no collapse/loss).
const want = conv.messages.map(m => m._msgId);
check('all_msgids_present',
  want.every(id => seen[id] === 1),
  'want=' + want.join(',') + ' seen=' + JSON.stringify(seen));

// (c) DOM order matches the array order (identity not cross-wired).
check('dom_order_matches_array',
  ids.join(',') === want.join(','),
  'dom=' + ids.join(',') + ' array=' + want.join(','));

// (d) ★ THE DISCRIMINATOR — shifted-but-unchanged nodes are REUSED, not
//     destroyed. The marker (and the SAME node object) must survive on c and d.
const cNow = win.document.querySelector('[data-msg-id="c"]');
const dNow = win.document.querySelector('[data-msg-id="d"]');
check('reused_c_state_preserved', !!cNow && cNow.__preserved === 'KEEP_C',
  'c.__preserved=' + (cNow && cNow.__preserved));
check('reused_d_state_preserved', !!dNow && dNow.__preserved === 'KEEP_D',
  'd.__preserved=' + (dNow && dNow.__preserved));
check('reused_is_same_node_object', cNow === cNode0 && dNow === dNode0);

// (e) the positional handle id="msg-N" on a REUSED, DRIFTED node is re-stamped
//     to the NEW index so positional consumers (edit_message.js) stay correct —
//     the handle is SUPPLEMENTED by id-keying, not left stale, not removed.
check('reused_id_restamped', !!dNow && dNow.id === 'msg-4', 'd.id=' + (dNow && dNow.id));
check('positional_id_preserved',
  nodes.every(el => /^msg-\d+$/.test(el.id)),
  'ids=' + nodes.map(el => el.id).join(','));

// (f) each node's rendered BODY belongs to the message it claims.
let bodyMismatch = null;
for (const el of nodes) {
  const id = idOf(el);
  const m = conv.messages.find(mm => mm._msgId === id);
  if (!m) { bodyMismatch = id + ' (no such msg)'; break; }
  if (el.textContent.indexOf(m.content) === -1) { bodyMismatch = id + ' body!=content'; break; }
}
check('body_matches_identity', bodyMismatch === null, bodyMismatch || '');

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_id_keyed_reconcile_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             CHAT_RENDER,   # argv[2]
             ESCAPE_HTML,   # argv[3]
             SAFE_HTML,     # argv[4]
             ROOT,          # argv[5]
             nc,            # argv[6]
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
def test_id_keyed_reconcile_survives_mid_history_insert():
    """A mid-history insert must not twin or collapse any bubble; the surgical
    reconcile keys on _msgId and preserves the positional id handle."""
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'id-keyed reconcile failures:\n' + output
    lines = _lines(output)
    for key in ('no_twin_total_count', 'no_duplicate_msgid', 'all_msgids_present',
                'dom_order_matches_array', 'body_matches_identity',
                'reused_c_state_preserved', 'reused_d_state_preserved',
                'reused_is_same_node_object', 'reused_id_restamped',
                'positional_id_preserved'):
        assert lines.get(key) == 'PASS', f'{key} not PASS:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_positional_matching_twins_or_collapses():
    """NEUTER: force positional (getElementById('msg-'+i)) matching in the
    surgical path. The twin / identity assertions MUST then fail, proving the
    id-keyed reconcile is load-bearing for Invariant 2."""
    output = _run('positional')
    assert 'FAIL neuter_positional_not_applied' not in output, (
        'the Phase-1 sentinel `_reconcileFindEl` is absent — the id-keyed '
        f'reconcile has not landed yet:\n{output}')
    lines = _lines(output)
    # Positional matching destroys+rebuilds every shifted node, so the reused-
    # node-object / preserved-state / re-stamped-id discriminators MUST fail
    # (or, in a worse variant, a twin/collapse appears). Any of these failing
    # proves the id-keyed reconcile is load-bearing for Invariant 2.
    regressed = (
        lines.get('reused_is_same_node_object') == 'FAIL'
        or lines.get('reused_c_state_preserved') == 'FAIL'
        or lines.get('reused_d_state_preserved') == 'FAIL'
        or lines.get('reused_id_restamped') == 'FAIL'
        or lines.get('no_duplicate_msgid') == 'FAIL'
        or lines.get('no_twin_total_count') == 'FAIL'
        or lines.get('dom_order_matches_array') == 'FAIL'
        or lines.get('body_matches_identity') == 'FAIL'
    )
    assert regressed, (
        'Positional matching did NOT lose node identity/state — the '
        f'id-keyed reconcile is not load-bearing:\n{output}')
