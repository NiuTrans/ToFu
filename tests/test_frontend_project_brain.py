"""jsdom regression for the Project Brain Activity Feed (project-brain.js).

WHY
---
The feed renders a BACKFILL (REST) then switches to a LIVE push stream. The
boundary is deduped: a live frame with ``seq <= maxSeq`` (already backfilled)
or an ``event_id`` already rendered must be dropped — exactly the SSE
Last-Event-ID resume contract, and exactly the kind of off-by-one that
double-renders an event at the backfill→live seam. This harness loads the REAL
shipped ``project-brain.js`` under jsdom and asserts:

  • backfill of seqs 1–3 renders 3 rows, newest-on-top;
  • a live push with seq=2 (already seen) is DROPPED — no duplicate row;
  • a live push with seq=4 (new) is rendered;
  • a conv-chip click calls ``loadConversation(conv_id)``.

Frontend NEGATIVE CONTROL (the project's load-bearing-logic bar): patch a COPY
of project-brain.js reverting the ``seq <= _state.maxSeq`` dedup guard to
always-render, run the SAME harness, and assert the duplicate-seq-2 row now
APPEARS (the dedup assertion would fail). The shipped file is asserted
byte-identical afterwards.

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
_BRAIN_SRC = os.path.join(JS_DIR, 'project-brain.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="projectBrainActivityList"><div class="pb-activity-empty">none</div></div>' +
  '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

// ── Stubs the module touches ──
// Icon(): return a tiny static svg string.
win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);

// loadConversation spy.
const loaded = [];
win.loadConversation = global.loadConversation = (id) => { loaded.push(id); };

// pushSubscribe spy — capture the (channel, key, fn) so the harness can push.
let _pushFn = null, _pushChannel = null, _pushKey = null;
win.pushSubscribe = global.pushSubscribe = (channel, key, fn) => {
  _pushChannel = channel; _pushKey = key; _pushFn = fn;
};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};

// Api.project.feed stub — backfill returns seqs 1..3 (newest-first, as the
// backend does) for the requested path.
win.Api = global.Api = {
  project: {
    feed: (p, since) => Promise.resolve({
      maxSeq: 3,
      events: [
        { seq: 3, event_id: 'e3', kind: 'completed', conv_id: 'cB', title: 'Conv B', summary: 's3' },
        { seq: 2, event_id: 'e2', kind: 'started',   conv_id: 'cA', title: 'Conv A', summary: 's2' },
        { seq: 1, event_id: 'e1', kind: 'started',   conv_id: 'cA', title: 'Conv A', summary: 's1' },
      ],
    }),
  },
};

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const PB = win.ProjectBrain;
if (!PB || typeof PB.openFeed !== 'function') {
  console.log('FAIL fn_exposed ProjectBrain.openFeed missing');
  process.exit(0);
}
check('fn_exposed', true);

// Channel-key hash must be deterministic 16-hex (matches backend; verified
// separately against Python). Here just sanity-check shape + that subscribe
// used it.
const KEY = PB.projectKeyHash('/proj/x');
check('keyhash_16hex', /^[0-9a-f]{16}$/.test(KEY));

function listRows() {
  return win.document.querySelectorAll('#projectBrainActivityList .pb-activity-row');
}

// ── Drive openFeed → backfill (async Promise) then live pushes ──
PB.openFeed('/proj/x');

// openFeed subscribes synchronously; the backfill resolves on a microtask.
Promise.resolve().then(() => {}).then(() => {
  // After backfill: 3 rows, subscribe used the hashed key.
  check('subscribe_channel', _pushChannel === 'project');
  check('subscribe_key_hashed', _pushKey === KEY && _pushKey !== '/proj/x');
  check('backfill_3_rows', listRows().length === 3);
  // Newest-on-top: first row is seq 3.
  const first = listRows()[0];
  check('backfill_newest_on_top', first && first.dataset.seq === '3');
  // Empty placeholder removed.
  check('empty_removed', !win.document.querySelector('.pb-activity-empty'));

  // ── LIVE: push seq=2 (already backfilled) → MUST be deduped (no new row).
  _pushFn({ type: 'activity', event: {
    seq: 2, event_id: 'e2', kind: 'started', conv_id: 'cA', title: 'Conv A', summary: 's2-DUP' } });
  check('live_seq2_deduped', listRows().length === 3);

  // ── LIVE: push a DIFFERENT event_id but seq=2 → still <= maxSeq → dropped.
  _pushFn({ type: 'activity', event: {
    seq: 2, event_id: 'e2b', kind: 'note', conv_id: 'cA', title: 'Conv A', summary: 'stale' } });
  check('live_old_seq_dropped', listRows().length === 3);

  // ── LIVE: push seq=4 (new) → rendered, on top.
  _pushFn({ type: 'activity', event: {
    seq: 4, event_id: 'e4', kind: 'run_concluded', conv_id: 'cB', title: 'Conv B', summary: 's4' } });
  check('live_seq4_rendered', listRows().length === 4);
  check('live_seq4_on_top', listRows()[0].dataset.seq === '4');

  // ── LIVE: re-push seq=4 (same event_id) → idempotent, no dup.
  _pushFn({ type: 'activity', event: {
    seq: 4, event_id: 'e4', kind: 'run_concluded', conv_id: 'cB', title: 'Conv B', summary: 's4' } });
  check('live_seq4_idempotent', listRows().length === 4);

  // ── conv-chip click → loadConversation(conv_id).
  const chip = win.document.querySelector('.pb-conv-chip');
  check('chip_present', !!chip);
  if (chip) { chip.click(); check('chip_loads_conv', loaded.length === 1 && !!loaded[0]); }

  console.log(out.join('\n'));
});
"""


def _run_harness(src_path):
    harness = os.path.join(HERE, '_project_brain_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, src_path, ROOT],
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


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_project_brain_feed_dedup_and_chip():
    output = _run_harness(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'project-brain failures:\n' + output
    assert output.count('PASS') >= 12, f'expected >=12 PASS lines, got:\n{output}'


def _extract_panel_fragment():
    """Pull the REAL #projectBrainOverlay markup out of the shipped index.html.

    This is the whole point of the "renders into null" guard: the test must use
    the ACTUAL shipped DOM element, not a hand-built fixture, so a missing /
    renamed #projectBrainActivityList in index.html FAILS the test.
    """
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    start = html.find('<div class="project-brain-overlay"')
    assert start != -1, 'project-brain-overlay not found in index.html'
    # Walk to the matching close of the overlay div (it's a self-contained
    # block; find the marker comment that follows, or balance to chatContainer).
    end = html.find('<div class="chat-container"', start)
    assert end != -1, 'could not bound the overlay fragment'
    return html[start:end].strip()


# Harness that mounts the REAL index.html panel fragment (argv[4]) so
# #projectBrainActivityList is the shipped element, then opens the panel via
# the real openProjectBrain() path (resolving the project via stubbed
# getActiveConv/_getConvProjectPath) and asserts it populates.
_HARNESS_REAL_DOM = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};
// The displayed conversation's project path (the real resolver path).
win.getActiveConv = global.getActiveConv = () => ({ id: 'c1', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
// The panel makes TWO 'project' subscriptions: the Activity feed (keyed on the
// hashed path) and a panel-live Charter/Board refresh (keyed '*'). Capture the
// FEED subscribe specifically (the hashed, non-'*' key) so the live-push
// assertions drive the activity handler, not the refresh handler.
let _pushFn = null, _pushKey = null;
win.pushSubscribe = global.pushSubscribe = (ch, key, fn) => {
  if (ch === 'project' && key && key !== '*') { _pushKey = key; _pushFn = fn; }
};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};
win.Api = global.Api = { project: {
  feed: (p) => Promise.resolve({
    maxSeq: 2,
    events: [
      { seq: 2, event_id: 'e2', kind: 'completed', conv_id: 'cB', title: 'Conv B', summary: 'done' },
      { seq: 1, event_id: 'e1', kind: 'started',   conv_id: 'cA', title: 'Conv A', summary: 'go' },
    ] }),
  charter: (p) => Promise.resolve({ exists: false, version: 0, content: '', decisions: [] }),
  board: (p) => Promise.resolve({ open: 0, claimed: 0, done: 0, tasks: [] }),
  commitCharter: (p, b) => Promise.resolve({ ok: true }),
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// The shipped element MUST exist in the real fragment (this is the
// "renders into null" guard — a hand fixture can't catch a missing element).
const listEl = win.document.getElementById('projectBrainActivityList');
check('real_list_element_exists', !!listEl);

if (typeof win.openProjectBrain !== 'function') {
  console.log('FAIL openProjectBrain_exposed'); console.log(out.join('\n')); process.exit(0);
}
check('openProjectBrain_exposed', true);

// Open the panel via the REAL entry point (resolves /proj/real, backfills).
win.openProjectBrain();
const overlay = win.document.getElementById('projectBrainOverlay');
check('overlay_shown', overlay && overlay.hidden === false);
check('subscribed_to_real_project', _pushKey === win.ProjectBrain.projectKeyHash('/proj/real'));

Promise.resolve().then(()=>{}).then(() => {
  const rows = win.document.querySelectorAll('#projectBrainActivityList .pb-activity-row');
  check('real_dom_populated', rows.length === 2);
  // a live push lands in the same real element
  _pushFn({ type: 'activity', event: {
    seq: 3, event_id: 'e3', kind: 'run_concluded', conv_id: 'cB', title: 'Conv B', summary: 'wrap' } });
  const rows2 = win.document.querySelectorAll('#projectBrainActivityList .pb-activity-row');
  check('real_dom_live_appended', rows2.length === 3);
  check('real_dom_newest_top', rows2[0].dataset.seq === '3');
  // close → list cleared + overlay hidden
  win.closeProjectBrain();
  check('overlay_hidden_on_close', overlay.hidden === true);
  console.log(out.join('\n'));
});
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_project_brain_renders_into_real_index_html_panel():
    """Closes the "renders into null" gap: mount the ACTUAL #projectBrainOverlay
    fragment from index.html and assert openProjectBrain() populates the REAL
    shipped #projectBrainActivityList element."""
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_real_fragment.html')
    harness = os.path.join(HERE, '_pb_real_harness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(frag)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_REAL_DOM)
    try:
        proc = subprocess.run(
            ['node', harness, _BRAIN_SRC, ROOT, frag_file],
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'real-DOM render failures:\n' + output
    # The decisive ones: the real element exists AND got populated.
    assert 'PASS real_list_element_exists' in output, output
    assert 'PASS real_dom_populated' in output, output
    assert 'PASS real_dom_live_appended' in output, output


# ════════════════════════════════════════════════════════════════════
#  Cross-project isolation at the REAL push-routing boundary.
#  Loads the ACTUAL push.js + project-brain.js, drives frames through the
#  WebSocket onmessage path (NOT _onPush directly) so the channel-KEY routing
#  is what's under test — a frame addressed to project Y's hashed key must NOT
#  reach a panel opened on project X.
# ════════════════════════════════════════════════════════════════════

_PUSH_SRC = os.path.join(JS_DIR, 'push.js')

_HARNESS_ISOLATION = r"""
const fs = require('fs');
const path = require('path');
const PUSH_SRC = process.argv[2];
const BRAIN_SRC = process.argv[3];
const ROOT = process.argv[4];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="projectBrainActivityList"><div class="pb-activity-empty">none</div></div>' +
  '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};
win.apiUrl = global.apiUrl = (p) => p;

// ── Mock WebSocket: capture the instance so the harness can drive onmessage,
//    mimicking the real server delivering frames. Synchronous "open".
let _wsInstance = null;
function FakeWS(url) {
  this.url = url; this.readyState = 1;  // OPEN
  this.sent = [];
  _wsInstance = this;
  // push.js sets onopen/onmessage AFTER construction; fire onopen on a microtask.
  Promise.resolve().then(() => { if (this.onopen) this.onopen(); });
}
FakeWS.OPEN = 1; FakeWS.CONNECTING = 0; FakeWS.CLOSED = 3;
FakeWS.prototype.send = function (m) { this.sent.push(m); };
FakeWS.prototype.close = function () { this.readyState = 3; };
win.WebSocket = global.WebSocket = FakeWS;

// No backfill noise — feed returns empty so ONLY live frames create rows.
win.Api = global.Api = { project: { feed: () => Promise.resolve({ maxSeq: 0, events: [] }) } };

// Load the REAL push.js, then project-brain.js (shares window scope).
// NOTE: openFeed makes TWO 'project' subscriptions — the feed (hashed key) and
// the panel-live refresh ('*'). The REAL push.js routes both; this test drives
// frames through push.js's own routing (not a captured handler), so the extra
// '*' subscribe is harmless here — a keyY frame still won't reach keyX.
const ie = eval;
ie(fs.readFileSync(PUSH_SRC, 'utf8'));   // defines pushSubscribe/_push (real routing)
ie(fs.readFileSync(BRAIN_SRC, 'utf8'));  // ProjectBrain.openFeed → pushSubscribe('project', keyX)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const PB = win.ProjectBrain;
const keyX = PB.projectKeyHash('/proj/X');
const keyY = PB.projectKeyHash('/proj/Y');
check('keys_distinct', keyX !== keyY && keyX && keyY);

// Open the panel on project X (subscribes via the REAL pushSubscribe).
PB.openFeed('/proj/X');

function listRows() {
  return win.document.querySelectorAll('#projectBrainActivityList .pb-activity-row');
}
function deliver(keyHash, ev) {
  // Mimic the server: a frame on the 'project' channel, routed by taskId=keyHash.
  const frame = { channel: 'project', taskId: keyHash, type: 'activity', event: ev };
  _wsInstance.onmessage({ data: JSON.stringify(frame) });
}

// Wait for backfill microtask + the FakeWS onopen (which flushes the
// subscribe), then deliver the two frames through the REAL routing.
Promise.resolve().then(()=>{}).then(()=>{}).then(() => {
  check('ws_created', !!_wsInstance);
  // The panel subscribed on project:keyX (assert the subscribe frame went out).
  const subbed = _wsInstance.sent.some(m => {
    try { const o = JSON.parse(m); return o.action === 'subscribe' && o.channel === 'project' && o.taskId === keyX; }
    catch (_e) { return false; }
  });
  check('subscribed_project_keyX', subbed);

  // Deliver a frame for the OTHER project Y → must NOT render (wrong key).
  deliver(keyY, { seq: 10, event_id: 'y1', kind: 'started', conv_id: 'cY', title: 'Y conv', summary: 'PROJECT Y WORK' });
  check('y_frame_not_rendered', listRows().length === 0);

  // Deliver a frame for THIS project X → must render.
  deliver(keyX, { seq: 11, event_id: 'x1', kind: 'completed', conv_id: 'cX', title: 'X conv', summary: 'project X work' });
  check('x_frame_rendered', listRows().length === 1);

  // Decisive: the Y summary text must appear NOWHERE in the list.
  const html = win.document.getElementById('projectBrainActivityList').innerHTML;
  check('no_y_leak_in_dom', html.indexOf('PROJECT Y WORK') === -1);
  check('x_present_in_dom', html.indexOf('project X work') !== -1);

  console.log(out.join('\n'));
  // push.js installs a reconnect interval that keeps node's event loop alive;
  // force-exit once the assertions have printed (matches the other harnesses).
  process.exit(0);
});
"""


def _run_isolation(brain_src):
    harness = os.path.join(HERE, '_pb_isolation_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_ISOLATION)
    try:
        proc = subprocess.run(
            ['node', harness, _PUSH_SRC, brain_src, ROOT],
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
def test_cross_project_isolation_at_push_routing():
    """A frame minted for project Y (different hashed key) must NOT reach a
    panel opened on project X — proven through the REAL push.js routing
    (subscribe by key → onmessage routes by taskId), not _onPush directly."""
    output = _run_isolation(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'cross-project isolation failures:\n' + output
    assert 'PASS y_frame_not_rendered' in output, output
    assert 'PASS no_y_leak_in_dom' in output, output
    assert 'PASS x_frame_rendered' in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_isolation_wildcard_subscription_leaks(tmp_path):
    """NC: simulate an over-broad subscription — patch a COPY of
    project-brain.js so it subscribes with taskId='*' (the wildcard
    presence.js deliberately uses). push.js then routes EVERY project's frame
    to the panel → project Y leaks in → the isolation assertions FAIL.
    Shipped file untouched (only a copy is patched)."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "pushSubscribe('project', projectKeyHash(path), _onPush);"
    assert anchor in original, 'subscribe anchor not found'
    patched = original.replace(
        anchor, "pushSubscribe('project', '*', _onPush);  // NC over-broad", 1)
    copy_path = os.path.join(HERE, '_pb_isolation_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_isolation(copy_path)
        # With a wildcard subscription, push.js's global-handler branch routes
        # the Y frame to the panel → it renders → isolation breaks.
        assert ('FAIL y_frame_not_rendered' in output
                or 'FAIL no_y_leak_in_dom' in output), \
            ('NC: a wildcard subscription must LEAK project Y into the X '
             'panel (isolation assertion should fail):\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


# ════════════════════════════════════════════════════════════════════
#  Charter + Board columns render into the REAL index.html fragment,
#  and clicking commit calls the human-gated commit route.
# ════════════════════════════════════════════════════════════════════

_HARNESS_CHARTER_BOARD = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.t = global.t = (k) => k;             // key-echo is fine (we assert on content)
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};
win.getActiveConv = global.getActiveConv = () => ({ id: 'c1', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};

// Capture commit calls.
const committed = [];
win.Api = global.Api = { project: {
  feed: (p) => Promise.resolve({ maxSeq: 0, events: [
    { seq: 5, event_id: 'pp1', kind: 'proposed_decision', conv_id: 'cZ',
      title: 'Conv Z', summary: 'Adopt the soft-lease board',
      payload: { proposal: 'Adopt the soft-lease board' } },
  ] }),
  charter: (p) => Promise.resolve({
    exists: true, version: 7, content: 'NORTH STAR TEXT',
    decisions: [{ text: 'Decision Alpha' }], updated_by_conv: 'cA', updated_at: 1,
  }),
  charterPending: (p) => Promise.resolve({ pending: [
    { proposalId: 'prop_abc123', event_id: 'pp1', conv_id: 'cZ', title: 'Conv Z',
      summary: 'Adopt the soft-lease board' },
  ] }),
  dismissProposal: (p, pid) => Promise.resolve({ ok: true }),
  board: (p) => Promise.resolve({
    open: 1, claimed: 1, done: 0, tasks: [
      { id: 'pt_open1', title: 'OPEN EPIC CARD', status: 'open', owner_conv_id: '', depends_on: [] },
      { id: 'pt_cl1', title: 'CLAIMED EPIC CARD', status: 'claimed', owner_conv_id: 'cOWNER', dispatched: true, depends_on: [] },
    ] }),
  commitCharter: (p, body) => { committed.push({ p: p, body: body }); return Promise.resolve({ ok: true, version: 8 }); },
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Real shipped column bodies must exist (the renders-into-null guard, applied
// to Charter + Board too).
check('charter_body_exists', !!win.document.getElementById('projectBrainCharterBody'));
check('board_body_exists', !!win.document.getElementById('projectBrainBoardBody'));

win.openProjectBrain();

// charter + board load on a microtask (chained Promises). Drain a few.
Promise.resolve().then(()=>{}).then(()=>{}).then(()=>{}).then(()=>{}).then(() => {
  const charter = win.document.getElementById('projectBrainCharterBody');
  const board = win.document.getElementById('projectBrainBoardBody');
  const cHtml = charter.innerHTML;
  // Charter content + committed decision both rendered.
  check('charter_northstar_rendered', cHtml.indexOf('NORTH STAR TEXT') !== -1);
  check('charter_decision_rendered', cHtml.indexOf('Decision Alpha') !== -1);
  // The pending proposal + its commit control are present (the human gate).
  check('proposal_rendered', cHtml.indexOf('Adopt the soft-lease board') !== -1);
  const commitBtn = charter.querySelector('.pb-proposal-commit');
  check('commit_control_present', !!commitBtn);

  // Board kanban: a claimed card with the owner chip + an open card.
  const bHtml = board.innerHTML;
  check('board_open_card', bHtml.indexOf('OPEN EPIC CARD') !== -1);
  check('board_claimed_card', bHtml.indexOf('CLAIMED EPIC CARD') !== -1);
  check('board_owner_chip', bHtml.indexOf('cOWNER') !== -1);
  check('board_claimed_class', !!board.querySelector('.pb-board-card.pb-board-claimed'));
  // Feature A: the brain-dispatched claim shows the autonomy badge.
  check('board_dispatched_badge', !!board.querySelector('.pb-board-badge-dispatched'));

  // Click commit → calls the commit route with the proposal text + summary.
  if (commitBtn) {
    commitBtn.click();
    Promise.resolve().then(()=>{}).then(() => {
      check('commit_called', committed.length === 1);
      check('commit_path', committed.length && committed[0].p === '/proj/real');
      check('commit_carries_text', committed.length &&
        (committed[0].body.add_decision || '').indexOf('Adopt the soft-lease board') !== -1);
      // Re-anchored 2026-07-29 (was: expected_version === 7). Committing a
      // proposal is a pure APPEND, and appends commute — the version baked in
      // at render time goes stale the moment any sibling self-commits, so
      // pinning it made the button 409 exactly when the project was busy.
      // Concurrent appends are kept safe by the backend CAS instead.
      check('commit_sends_no_version', committed.length &&
        committed[0].body.expected_version === undefined);
      // Root-cause fix: the commit must carry resolves_proposal (the id from
      // the pending list) so the proposal drops out of pending durably.
      check('commit_carries_proposal', committed.length &&
        (committed[0].body.resolves_proposal || '') === 'prop_abc123');
      console.log(out.join('\n'));
    });
  } else {
    console.log(out.join('\n'));
  }
});
"""


def _run_charter_board(brain_src):
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_cb_fragment.html')
    harness = os.path.join(HERE, '_pb_cb_harness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(frag)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_CHARTER_BOARD)
    try:
        proc = subprocess.run(
            ['node', harness, brain_src, ROOT, frag_file],
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_charter_board_render_into_real_fragment_and_commit():
    """Charter + Board columns populate from stubbed Api.project.charter/board
    into the REAL index.html fragment, and clicking commit calls the
    human-gated commit route with the proposal text + expected_version."""
    output = _run_charter_board(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'charter/board render failures:\n' + output
    for must in ('PASS charter_northstar_rendered', 'PASS charter_decision_rendered',
                 'PASS proposal_rendered', 'PASS commit_control_present',
                 'PASS board_open_card', 'PASS board_claimed_card',
                 'PASS board_owner_chip', 'PASS board_dispatched_badge',
                 'PASS commit_called', 'PASS commit_carries_proposal',
                 'PASS commit_carries_text', 'PASS commit_sends_no_version'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_commit_wiring_is_load_bearing():
    """Frontend NC: neuter the commit-button click wiring in a COPY → clicking
    commit no longer calls the route → commit_called FAILS. Shipped file
    byte-identical afterward."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "        _commitCharterDecision(path, text, ver, pid, btn, summary);"
    assert anchor in original, 'commit-wiring anchor not found'
    patched = original.replace(anchor, "        void 0;  // NC (commit wiring disabled)", 1)
    copy_path = os.path.join(HERE, '_pb_cb_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_charter_board(copy_path)
        assert 'FAIL commit_called' in output, \
            ('NC: disabling the commit click wiring must make commit_called '
             'FAIL:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_dispatched_badge_is_load_bearing():
    """Frontend NC: neuter the dispatched-badge branch in _boardCard → the
    badge no longer renders → board_dispatched_badge FAILS. Byte-identical
    restore."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "    var badge = t.dispatched"
    assert anchor in original, 'dispatched-badge anchor not found'
    patched = original.replace(anchor, "    var badge = false && t.dispatched", 1)
    copy_path = os.path.join(HERE, '_pb_badge_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_charter_board(copy_path)
        assert 'FAIL board_dispatched_badge' in output, \
            ('NC: disabling the dispatched-badge branch must make '
             'board_dispatched_badge FAIL:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_dedup_guard_is_load_bearing():
    """Frontend NC: revert the seq<=maxSeq dedup guard in a COPY → the
    'live_seq2_deduped' assertion FAILS (a 4th row appears). Restore the
    shipped file byte-identical."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "if (!fromBackfill && ev.seq && ev.seq <= _state.maxSeq) return false;"
    assert anchor in original, 'dedup-guard anchor not found'
    patched = original.replace(
        anchor,
        "if (false && !fromBackfill && ev.seq && ev.seq <= _state.maxSeq) return false;  // NC",
        1)
    copy_path = os.path.join(HERE, '_project_brain_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_harness(copy_path)
        # With the seq-window guard disabled, a live frame carrying a NEW
        # event_id but a STALE seq (<= maxSeq) is no longer dropped → it
        # renders a spurious row → `live_old_seq_dropped` FAILS. (The
        # same-event_id seq=2 case is still caught by the separate event_id
        # dedup, so it's NOT the discriminating assertion — the seq WINDOW is.)
        assert 'FAIL live_old_seq_dropped' in output, \
            ('NC: disabling the seq-window dedup guard must make '
             'live_old_seq_dropped FAIL:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    # The shipped source must be untouched (we only ever wrote a copy).
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'



# ════════════════════════════════════════════════════════════════════
#  Empty-Activity-column placeholder restore.
#  openFeed() first calls closeFeed(), which wipes list.innerHTML (removing
#  the shipped "No activity yet" placeholder). If the backfill then returns
#  ZERO events, the column would be a BLANK VOID — not even the placeholder.
#  This mounts the REAL index.html fragment, stubs an EMPTY feed, and asserts
#  the placeholder is restored (0 rows, .pb-activity-empty present).
# ════════════════════════════════════════════════════════════════════

_HARNESS_EMPTY_FEED = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};
win.getActiveConv = global.getActiveConv = () => ({ id: 'c1', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};
// EMPTY feed (the exact state a brand-new project is in) + empty charter/board.
win.Api = global.Api = { project: {
  feed: (p) => Promise.resolve({ maxSeq: 0, events: [] }),
  charter: (p) => Promise.resolve({ exists: false, version: 0, content: '', decisions: [] }),
  charterPending: (p) => Promise.resolve({ pending: [] }),
  board: (p) => Promise.resolve({ open: 0, claimed: 0, done: 0, tasks: [] }),
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

win.openProjectBrain();

function listEl() { return win.document.getElementById('projectBrainActivityList'); }
function rows() { return listEl().querySelectorAll('.pb-activity-row'); }

// Drain a couple microtasks so the (async) backfill resolves.
Promise.resolve().then(()=>{}).then(()=>{}).then(() => {
  // No rows AND the empty-state placeholder must be present (not a blank void).
  check('no_rows', rows().length === 0);
  check('empty_placeholder_present', !!listEl().querySelector('.pb-activity-empty'));
  // The Board + Charter columns already render their own empty states.
  const board = win.document.getElementById('projectBrainBoardBody');
  const charter = win.document.getElementById('projectBrainCharterBody');
  check('board_empty_present', !!board.querySelector('.pb-board-empty'));
  check('charter_empty_present', !!charter.querySelector('.pb-charter-empty'));
  console.log(out.join('\n'));
});
"""


def _run_empty_feed(brain_src):
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_empty_fragment.html')
    harness = os.path.join(HERE, '_pb_empty_harness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(frag)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_EMPTY_FEED)
    try:
        proc = subprocess.run(
            ['node', harness, brain_src, ROOT, frag_file],
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_empty_feed_restores_activity_placeholder():
    """An empty backfill must NOT leave the Activity column a blank void —
    the "No activity yet" placeholder is restored after closeFeed() wiped it.
    (The screenshot bug: a brand-new project rendered an empty Activity void.)"""
    output = _run_empty_feed(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'empty-feed placeholder failures:\n' + output
    assert 'PASS no_rows' in output, output
    assert 'PASS empty_placeholder_present' in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_empty_placeholder_restore_is_load_bearing():
    """Frontend NC: neuter the _ensureActivityEmptyState() call in openFeed's
    backfill handler → an empty backfill leaves the column blank →
    empty_placeholder_present FAILS. Byte-identical restore."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    # Neuter BOTH call sites (success + catch) so the placeholder is never
    # restored on an empty backfill.
    anchor = "      _ensureActivityEmptyState();"
    assert original.count(anchor) >= 1, 'empty-state-restore anchor not found'
    patched = original.replace(anchor, "      void 0;  // NC (empty-state restore disabled)")
    copy_path = os.path.join(HERE, '_pb_empty_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_empty_feed(copy_path)
        assert 'FAIL empty_placeholder_present' in output, \
            ('NC: disabling the empty-state restore must make '
             'empty_placeholder_present FAIL (blank Activity void):\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'



# ════════════════════════════════════════════════════════════════════
#  Activity legend + per-row icon title + per-row timestamp.
#  The feed glyphs were self-documenting to nobody: no legend, no tooltip,
#  no "when". This harness mounts the REAL index.html fragment, opens the
#  panel, and asserts:
#    • the legend renders EXACTLY the 10 backend VALID_KINDS (one chip each,
#      each with an SVG glyph + a localized label);
#    • every backfilled row's icon carries a title == its localized kind name;
#    • every row shows a relative-time element (from `ts`).
#  A localizing `t()` echoes 'projectBrain.kind.<kind>' so we can assert the
#  legend covers all 10 kinds by key.
# ════════════════════════════════════════════════════════════════════

# The 10 kinds the backend freezes in lib/conversations/project_feed.VALID_KINDS.
_BRAIN_KINDS = ['started', 'completed', 'aborted', 'run_concluded', 'claimed',
                'blocked', 'decided', 'proposed_decision', 'dismissed', 'note']

_HARNESS_LEGEND_TIME = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
// key-echo t(): returns the key itself so the harness can assert legend
// coverage by 'projectBrain.kind.<kind>' AND so relative-time keys render.
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};
win.getActiveConv = global.getActiveConv = () => ({ id: 'c1', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};

// Backfill: two rows with a real `ts` a few minutes back, different kinds so
// the icon-title assertion checks the localized per-kind label.
const NOW = Date.now();
win.Api = global.Api = { project: {
  feed: (p) => Promise.resolve({ maxSeq: 2, events: [
    { seq: 2, event_id: 'e2', kind: 'claimed', conv_id: 'cB', title: 'Conv B', summary: 'claimed one', ts: NOW - 5*60000 },
    { seq: 1, event_id: 'e1', kind: 'started', conv_id: 'cA', title: 'Conv A', summary: 'started', ts: NOW - 2*60000 },
  ] }),
  charter: (p) => Promise.resolve({ exists: false, version: 0, content: '', decisions: [] }),
  charterPending: (p) => Promise.resolve({ pending: [] }),
  board: (p) => Promise.resolve({ open: 0, claimed: 0, done: 0, tasks: [] }),
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

win.openProjectBrain();

Promise.resolve().then(()=>{}).then(()=>{}).then(() => {
  const doc = win.document;
  // ── Legend: present, and covers exactly the 10 kinds (by key echo). ──
  const legend = doc.querySelector('.pb-activity-legend');
  check('legend_present', !!legend);
  const items = legend ? legend.querySelectorAll('.pb-legend-item') : [];
  check('legend_10_items', items.length === 10);
  const kinds = %KINDS%;
  let allKinds = true;
  for (const k of kinds) {
    const hasKey = legend && legend.innerHTML.indexOf('projectBrain.kind.' + k) !== -1;
    const hasClass = legend && !!legend.querySelector('.pb-legend-item.pb-kind-' + k);
    if (!hasKey || !hasClass) { allKinds = false; }
  }
  check('legend_covers_all_kinds', allKinds);
  // every legend chip has an SVG glyph (no bare text / emoji).
  let allGlyphs = true;
  for (const it of items) { if (!it.querySelector('svg')) allGlyphs = false; }
  check('legend_all_svg', allGlyphs && items.length === 10);

  // ── Per-row icon title == localized kind name; per-row timestamp shown. ──
  const rows = doc.querySelectorAll('#projectBrainActivityList .pb-activity-row');
  check('two_rows', rows.length === 2);
  let titlesOk = true, timesOk = true;
  for (const r of rows) {
    const ico = r.querySelector('.pb-activity-icon');
    const kind = (r.className.match(/pb-kind-(\w+)/) || [])[1] || '';
    if (!ico || ico.getAttribute('title') !== 'projectBrain.kind.' + kind) titlesOk = false;
    const timeEl = r.querySelector('.pb-activity-time');
    if (!timeEl || !timeEl.textContent) timesOk = false;
  }
  check('row_icon_titles', titlesOk);
  check('row_timestamps', timesOk);

  console.log(out.join('\n'));
});
""".replace('%KINDS%', repr(_BRAIN_KINDS).replace("'", '"'))


def _run_legend_time(brain_src):
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_legend_fragment.html')
    harness = os.path.join(HERE, '_pb_legend_harness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(frag)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_LEGEND_TIME)
    try:
        proc = subprocess.run(
            ['node', harness, brain_src, ROOT, frag_file],
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_activity_legend_and_row_titles_and_timestamps():
    """The Activity column renders a 10-kind legend (SVG + label), every row
    icon carries a localized kind-name title, and every row shows a timestamp
    derived from the event `ts`."""
    output = _run_legend_time(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'legend/timestamp failures:\n' + output
    for must in ('PASS legend_present', 'PASS legend_10_items',
                 'PASS legend_covers_all_kinds', 'PASS legend_all_svg',
                 'PASS row_icon_titles', 'PASS row_timestamps'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_legend_render_is_load_bearing():
    """Frontend NC: neuter the _renderLegend() call in openFeed → the legend
    no longer renders → legend_present + legend_10_items FAIL. Byte-identical
    restore."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "    _renderLegend();"
    assert anchor in original, 'legend-render anchor not found'
    patched = original.replace(anchor, "    void 0;  // NC (legend render disabled)", 1)
    copy_path = os.path.join(HERE, '_pb_legend_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_legend_time(copy_path)
        assert 'FAIL legend_present' in output, \
            ('NC: disabling _renderLegend() must make legend_present FAIL:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_row_timestamp_is_load_bearing():
    """Frontend NC: neuter the per-row timestamp append in buildActivityRow →
    rows carry no .pb-activity-time → row_timestamps FAILS. Byte-identical
    restore. (Proves the timestamp is genuinely rendered, not incidental.)"""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "      body.appendChild(timeEl);"
    assert anchor in original, 'timestamp-append anchor not found'
    patched = original.replace(anchor, "      void 0;  // NC (timestamp append disabled)", 1)
    copy_path = os.path.join(HERE, '_pb_time_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_legend_time(copy_path)
        assert 'FAIL row_timestamps' in output, \
            ('NC: disabling the timestamp append must make row_timestamps '
             'FAIL:\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


# ════════════════════════════════════════════════════════════════════
#  Board HUMAN action controls (Phase 1 step 4): complete/block on
#  open+claimed cards; REOPEN on claimed AND done cards (the "break a
#  stuck live claim" + "revive" lever). Clicking reopen calls
#  Api.project.boardReopen(path, taskId, convId) with the resolved
#  displayed-conv id. Renders into the REAL index.html board fragment.
# ════════════════════════════════════════════════════════════════════

_HARNESS_BOARD_ACTIONS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};
win.getActiveConv = global.getActiveConv = () => ({ id: 'c1', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};
// The displayed conversation id the human mutation is attributed to.
win.activeConvId = global.activeConvId = 'cDISPLAYED';
// Silence the block-reason prompt (not exercised here).
win.prompt = global.prompt = () => '';

// Capture board mutation calls.
const calls = { reopen: [], complete: [], block: [], post: [] };
win.Api = global.Api = { project: {
  feed: (p) => Promise.resolve({ maxSeq: 0, events: [] }),
  charter: (p) => Promise.resolve({ exists: false, version: 0, content: '', decisions: [] }),
  charterPending: (p) => Promise.resolve({ pending: [] }),
  board: (p) => Promise.resolve({
    open: 1, claimed: 1, done: 1, tasks: [
      { id: 'pt_open1', title: 'OPEN CARD', status: 'open', owner_conv_id: '', depends_on: [] },
      { id: 'pt_cl1', title: 'CLAIMED CARD', status: 'claimed', owner_conv_id: 'cOWNER', depends_on: [] },
      { id: 'pt_done1', title: 'DONE CARD', status: 'done', owner_conv_id: '', depends_on: [] },
    ] }),
  boardReopen: (p, taskId, convId) => { calls.reopen.push({ p, taskId, convId }); return Promise.resolve({ ok: true, from: 'claimed' }); },
  boardComplete: (p, taskId, convId) => { calls.complete.push({ p, taskId, convId }); return Promise.resolve({ ok: true }); },
  boardBlock: (p, taskId, convId, reason) => { calls.block.push({ p, taskId, convId, reason }); return Promise.resolve({ ok: true }); },
  boardPost: (p, body) => { calls.post.push({ p, body }); return Promise.resolve({ ok: true, id: 'pt_new' }); },
  brainInfluence: (p, c) => Promise.resolve({ convId: c, charter: {}, board: {}, pendingDecisions: [] }),
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const PB = win.ProjectBrain;
check('renderBoard_exposed', PB && typeof PB.renderBoard === 'function');

// Render the board directly with a done + claimed + open card.
PB.renderBoard({ open: 1, claimed: 1, done: 1, tasks: [
  { id: 'pt_open1', title: 'OPEN CARD', status: 'open', owner_conv_id: '', depends_on: [] },
  { id: 'pt_cl1', title: 'CLAIMED CARD', status: 'claimed', owner_conv_id: 'cOWNER', depends_on: [] },
  { id: 'pt_done1', title: 'DONE CARD', status: 'done', owner_conv_id: '', depends_on: [] },
] });
const board = win.document.getElementById('projectBrainBoardBody');

function cardOf(id) { return board.querySelector('.pb-board-card[data-task-id="' + id + '"]'); }
function reopenBtn(id) { var c = cardOf(id); return c ? c.querySelector('.pb-board-act-reopen') : null; }

// Reopen control MUST render on BOTH claimed and done cards (the lever must
// appear on claimed too — "break a stuck live claim").
check('reopen_on_claimed', !!reopenBtn('pt_cl1'));
check('reopen_on_done', !!reopenBtn('pt_done1'));
// Open card has NO reopen (it's already open) but HAS complete + block.
check('no_reopen_on_open', !reopenBtn('pt_open1'));
check('complete_on_open', !!cardOf('pt_open1').querySelector('.pb-board-act-complete'));
check('block_on_open', !!cardOf('pt_open1').querySelector('.pb-board-act-block'));
// "New epic" affordance present + enabled (there IS a displayed conv).
const newBtn = board.querySelector('#pbBoardNewBtn');
check('new_epic_present', !!newBtn);
check('new_epic_enabled', newBtn && !newBtn.disabled);

// Click reopen on the CLAIMED card → boardReopen(path, taskId, convId).
const rb = reopenBtn('pt_cl1');
if (rb) {
  rb.click();
  Promise.resolve().then(()=>{}).then(() => {
    check('reopen_called', calls.reopen.length === 1);
    check('reopen_task_id', calls.reopen.length && calls.reopen[0].taskId === 'pt_cl1');
    check('reopen_path', calls.reopen.length && calls.reopen[0].p === '/proj/real');
    check('reopen_carries_conv', calls.reopen.length && calls.reopen[0].convId === 'cDISPLAYED');
    console.log(out.join('\n'));
  });
} else {
  console.log(out.join('\n'));
}
"""


def _run_board_actions(brain_src):
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_ba_fragment.html')
    harness = os.path.join(HERE, '_pb_ba_harness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(frag)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_BOARD_ACTIONS)
    try:
        proc = subprocess.run(
            ['node', harness, brain_src, ROOT, frag_file],
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_board_human_actions_render_and_reopen_calls_api():
    """The reopen control renders on BOTH claimed and done cards; clicking it
    calls Api.project.boardReopen with the resolved displayed-conv id. Complete
    + block render on open/claimed; the New-epic affordance is present."""
    output = _run_board_actions(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'board-action render failures:\n' + output
    for must in ('PASS reopen_on_claimed', 'PASS reopen_on_done',
                 'PASS no_reopen_on_open', 'PASS complete_on_open',
                 'PASS block_on_open', 'PASS new_epic_present',
                 'PASS new_epic_enabled', 'PASS reopen_called',
                 'PASS reopen_task_id', 'PASS reopen_path',
                 'PASS reopen_carries_conv'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_reopen_control_is_load_bearing():
    """Frontend NC (double-neuter): remove the reopen-control branch in
    _boardCard → the reopen button no longer renders on claimed/done cards →
    reopen_on_claimed + reopen_on_done FAIL. Byte-identical restore."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("    if (t.status === 'claimed' || t.status === 'done') {\n"
              "      acts.push(_boardActionBtn('reopen', 'refresh', "
              "'projectBrain.actReopen', 'Reopen'));\n"
              "    }")
    assert anchor in original, 'reopen-control anchor not found'
    patched = original.replace(
        anchor, "    // NC (reopen control disabled)", 1)
    copy_path = os.path.join(HERE, '_pb_reopen_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_board_actions(copy_path)
        assert 'FAIL reopen_on_claimed' in output and 'FAIL reopen_on_done' in output, \
            ('NC: disabling the reopen-control branch must make '
             'reopen_on_claimed + reopen_on_done FAIL:\n' + output)
        # discriminating: complete/block still render (only reopen removed).
        assert 'PASS complete_on_open' in output, \
            'NC must be surgical — complete control unaffected:\n' + output
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


# ════════════════════════════════════════════════════════════════════
#  Truncation fix (frontend): renderCharter must render + commit the FULL
#  payload.proposal text, NOT the 280-char feed-row summary. This is the
#  frontend half of the summary-first bug the backend test covers.
# ════════════════════════════════════════════════════════════════════

# A proposal whose FULL payload text differs from (and is far longer than) the
# short feed-row summary — so a summary-first renderer is discriminated.
_FULL_PROPOSAL = ('FULL-HEAD ' + ('z' * 600) + ' FULL-TAIL-SENTINEL')

_HARNESS_FULLTEXT = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const FULL = process.argv[5];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};
win.getActiveConv = global.getActiveConv = () => ({ id: 'c1', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};

const committed = [];
// The pending proposal: a SHORT feed summary (the 280-char cap) but the FULL
// text in payload.proposal — exactly the shape pending_proposals now returns.
win.Api = global.Api = { project: {
  feed: (p) => Promise.resolve({ maxSeq: 0, events: [] }),
  charter: (p) => Promise.resolve({ exists: true, version: 7, content: 'NS',
    decisions: [], updated_by_conv: 'cA', updated_at: 1 }),
  charterPending: (p) => Promise.resolve({ pending: [
    { proposalId: 'prop_full', event_id: 'pp9', conv_id: 'cZ', title: 'Conv Z',
      summary: FULL, payload: { proposal: FULL } },
  ] }),
  dismissProposal: (p, pid) => Promise.resolve({ ok: true }),
  board: (p) => Promise.resolve({ open: 0, claimed: 0, done: 0, tasks: [] }),
  commitCharter: (p, body) => { committed.push({ p: p, body: body }); return Promise.resolve({ ok: true, version: 8 }); },
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

win.openProjectBrain();
Promise.resolve().then(()=>{}).then(()=>{}).then(()=>{}).then(()=>{}).then(() => {
  const charter = win.document.getElementById('projectBrainCharterBody');
  const cHtml = charter.innerHTML;
  // The FULL proposal tail must be rendered (not clipped to a short summary).
  check('proposal_full_rendered', cHtml.indexOf('FULL-TAIL-SENTINEL') !== -1);
  const commitBtn = charter.querySelector('.pb-proposal-commit');
  check('commit_control_present', !!commitBtn);
  // The commit button's data-text must carry the FULL proposal.
  check('commit_data_text_full',
    !!commitBtn && (commitBtn.getAttribute('data-text') || '').indexOf('FULL-TAIL-SENTINEL') !== -1);
  if (commitBtn) {
    commitBtn.click();
    Promise.resolve().then(()=>{}).then(() => {
      // And the commit route receives the FULL text (so the stored decision
      // is not the 280-char summary).
      check('commit_carries_full_text', committed.length &&
        (committed[0].body.add_decision || '').indexOf('FULL-TAIL-SENTINEL') !== -1);
      console.log(out.join('\n'));
    });
  } else {
    console.log(out.join('\n'));
  }
});
"""


def _run_fulltext(brain_src):
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_full_fragment.html')
    harness = os.path.join(HERE, '_pb_full_harness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(frag)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_FULLTEXT)
    try:
        proc = subprocess.run(
            ['node', harness, brain_src, ROOT, frag_file, _FULL_PROPOSAL],
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_charter_renders_and_commits_full_proposal_text():
    """renderCharter must render the FULL payload.proposal (not the 280-char
    feed summary) and the commit control must carry that full text — the
    frontend half of the truncated-decision fix."""
    output = _run_fulltext(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'full-text render failures:\n' + output
    for must in ('PASS proposal_full_rendered', 'PASS commit_data_text_full',
                 'PASS commit_carries_full_text'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_charter_summary_first_truncates_rendered_proposal():
    """Frontend NC (double-neuter): revert renderCharter's ptext to
    summary-FIRST in a COPY. Because THIS harness feeds an identical summary +
    payload, we ALSO shorten the stubbed summary via a second surgical patch of
    the harness is overkill — instead the NC targets the SHIPPED renderer: with
    payload-first removed, ptext falls back to p.summary only if p.payload is
    absent. To bite deterministically we neuter to read p.summary EXCLUSIVELY
    and feed a shorter summary at runtime.  Byte-identical restore."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("        var ptext = (p.payload && p.payload.proposal) "
              "|| p.summary || '';")
    assert anchor in original, 'ptext anchor not found'
    # Neuter: use ONLY p.summary (ignore the full payload). Combined with a
    # harness that supplies a SHORT summary + FULL payload, this drops the tail.
    patched = original.replace(
        anchor, "        var ptext = p.summary || '';  // NC (summary-only)", 1)
    copy_path = os.path.join(HERE, '_pb_full_nc_copy.js')
    # A harness variant whose pending summary is SHORT (no sentinel) but payload
    # is FULL — so summary-only rendering loses FULL-TAIL-SENTINEL.
    nc_harness = _HARNESS_FULLTEXT.replace(
        "summary: FULL, payload: { proposal: FULL } },",
        "summary: 'SHORT-SUMMARY', payload: { proposal: FULL } },")
    nc_harness_path = os.path.join(HERE, '_pb_full_nc_harness.js')
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_full_nc_fragment.html')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        with open(nc_harness_path, 'w', encoding='utf-8') as f:
            f.write(nc_harness)
        with open(frag_file, 'w', encoding='utf-8') as f:
            f.write(frag)
        proc = subprocess.run(
            ['node', nc_harness_path, copy_path, ROOT, frag_file, _FULL_PROPOSAL],
            capture_output=True, text=True, timeout=60)
        output = proc.stdout.strip()
        assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
        assert 'FAIL proposal_full_rendered' in output \
            and 'FAIL commit_carries_full_text' in output, \
            ('NC: summary-only ptext must drop the full-text tail:\n' + output)
    finally:
        for p in (copy_path, nc_harness_path, frag_file):
            try:
                os.remove(p)
            except OSError:
                pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


# ════════════════════════════════════════════════════════════════════
#  Influence-lens deep-link: openProjectBrainInfluence opens the panel AND
#  un-hides + flashes the per-conversation Influence lens (rather than just
#  opening the panel at the top). This is the public entry point a caller uses
#  to jump straight to "how is THIS conversation influenced".
# ════════════════════════════════════════════════════════════════════

_HARNESS_DEEPLINK = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' +
  fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};
win.activeConvId = global.activeConvId = 'convA';
win.getActiveConv = global.getActiveConv = () => ({ id: 'convA', title: 'A', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';

// convA is bound by a charter (so the lens is non-empty → banner un-hides).
const INF = { projectPath: '/proj/real', convId: 'convA',
  charter: { exists: true, injected: true, content: 'NS', decisions: ['D1'] },
  board: { injected: true, mine: [], avoid: [], open: [] }, pendingDecisions: [] };
win.Api = global.Api = { project: {
  feed: (p) => Promise.resolve({ maxSeq: 0, events: [] }),
  charter: (p) => Promise.resolve({ exists: true, version: 1, content: 'NS', decisions: [{ text: 'D1' }] }),
  charterPending: (p) => Promise.resolve({ pending: [] }),
  board: (p) => Promise.resolve({ open: 0, claimed: 0, done: 0, tasks: [] }),
  brainInfluence: (p, cid) => Promise.resolve(INF),
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
check('deeplink_exposed', typeof win.openProjectBrainInfluence === 'function');

// Simulate the bar's onclick deep-link.
win.openProjectBrainInfluence();
// openProjectBrain runs synchronously (un-hides overlay); influence loads on a
// microtask, then the flash fires on a 120ms setTimeout. Wait it out.
setTimeout(() => {
  const overlay = win.document.getElementById('projectBrainOverlay');
  const banner = win.document.getElementById('projectBrainInfluence');
  check('overlay_open', overlay && overlay.hidden === false);
  check('influence_banner_visible', banner && banner.hidden === false);
  // The deep-link flash class must have been applied to the lens.
  check('lens_flashed', banner && banner.classList.contains('pb-influence-flash'));
  console.log(out.join('\n'));
}, 400);
"""


def _run_deeplink(brain_src):
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_dl_fragment.html')
    harness = os.path.join(HERE, '_pb_dl_harness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(frag)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_DEEPLINK)
    try:
        proc = subprocess.run(
            ['node', harness, brain_src, ROOT, frag_file],
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_conv_influence_bar_deeplinks_to_lens():
    """openProjectBrainInfluence opens the panel AND un-hides + flashes the
    per-conversation Influence lens — the deep-link entry point that jumps
    straight to "how is THIS conversation influenced"."""
    output = _run_deeplink(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'deep-link failures:\n' + output
    for must in ('PASS deeplink_exposed', 'PASS overlay_open',
                 'PASS influence_banner_visible', 'PASS lens_flashed'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_deeplink_flash_is_load_bearing():
    """Frontend NC (double-neuter): remove the flash-class add in
    openProjectBrainInfluence → the lens is no longer flashed → lens_flashed
    FAILS while the panel still opens. Byte-identical restore."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "      banner.classList.add('pb-influence-flash');"
    assert anchor in original, 'flash-add anchor not found'
    patched = original.replace(anchor, "      void 0;  // NC (flash disabled)", 1)
    copy_path = os.path.join(HERE, '_pb_dl_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_deeplink(copy_path)
        assert 'FAIL lens_flashed' in output, \
            ('NC: removing the flash-add must make lens_flashed FAIL:\n' + output)
        # discriminating: the panel still opens (only the flash was removed).
        assert 'PASS overlay_open' in output and 'PASS influence_banner_visible' in output, \
            'NC must be surgical — the panel still opens:\n' + output
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


# ════════════════════════════════════════════════════════════════════
#  No-project open — the panel must say WHY it is empty, never open into
#  blank or stale tabs.
#
#  Reported bug (2026-07-28): the collab bar still showed the previous
#  conversation's project counts on a fresh New Chat; clicking it opened the
#  panel with _displayedProjectPath() === '' → openProjectBrain loaded
#  NOTHING → every tab blank while the bar claimed "N need you · M open".
#  The else-branch now paints an explicit no-project empty state into every
#  tab body and clears the count badges.
# ════════════════════════════════════════════════════════════════════

_HARNESS_NO_PROJECT = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const FRAG = process.argv[4];
const fragment = fs.readFileSync(FRAG, 'utf8');
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body>' + fragment + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;

win.Icon = global.Icon = (name) => '<svg data-icon="' + name + '"></svg>';
win.t = global.t = (k) => k;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
// PROJECT-LESS state: no active conv at all (the New Chat page), and no
// projectState singleton — exactly what _displayedProjectPath() resolves to ''.
win.getActiveConv = global.getActiveConv = () => null;
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};
// Any fetch firing here would prove the panel loads for a project it does not have.
let _fetchFired = false;
win.Api = global.Api = { project: new Proxy({}, { get: () => () => { _fetchFired = true; return Promise.resolve(null); } }) };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

win.openProjectBrain({ needsYou: 1 });   // the stale bar's exact call shape
const overlay = win.document.getElementById('projectBrainOverlay');
check('overlay_opens', overlay && overlay.hidden === false);
const BODIES = ['projectBrainAttentionBody', 'projectBrainCharterBody',
                'projectBrainBoardBody', 'projectBrainActivityList',
                'projectBrainPeersBody', 'projectBrainStatusBody'];
let allEmpty = true;
for (const id of BODIES) {
  const el = win.document.getElementById(id);
  if (!el || el.innerHTML.indexOf('pb-no-project') === -1) allEmpty = false;
}
check('all_tabs_show_no_project_state', allEmpty);
check('no_fetch_fired', _fetchFired === false);
const badges = ['pbTabCountAttention', 'pbTabCountCharter', 'pbTabCountBoard', 'pbTabCountPeers'];
let badgesCleared = true;
for (const id of badges) {
  const el = win.document.getElementById(id);
  if (el && el.hidden === false) badgesCleared = false;
}
check('count_badges_cleared', badgesCleared);
const banner = win.document.getElementById('projectBrainInfluence');
check('influence_banner_hidden', !banner || banner.hidden === true);
console.log(out.join('\n'));
"""


def _run_no_project(brain_src: str) -> str:
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_np_fragment.html')
    harness = os.path.join(HERE, '_pb_np_harness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(frag)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS_NO_PROJECT)
    try:
        proc = subprocess.run(
            ['node', harness, brain_src, ROOT, frag_file],
            capture_output=True, text=True, timeout=60)
    finally:
        for p in (frag_file, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_open_panel_without_project_shows_explicit_empty_state():
    """Opening the Brain panel with no displayed project paints the explicit
    no-project state in every tab body (never blank/stale tabs) and fires no
    data fetch."""
    output = _run_no_project(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'no-project open failures:\n' + output
    for must in ('PASS overlay_opens', 'PASS all_tabs_show_no_project_state',
                 'PASS no_fetch_fired', 'PASS count_badges_cleared',
                 'PASS influence_banner_hidden'):
        assert must in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_no_project_empty_state_is_load_bearing():
    """NEUTER: drop the _renderNoProject() call from the else-branch → the
    panel opens into void tabs again (baked defaults / stale content, no
    pb-no-project) → all_tabs_show_no_project_state goes red."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = '      _renderNoProject();'
    assert anchor in original, 'no-project render call anchor not found'
    patched = original.replace(
        anchor, '      void 0;  // NC (no-project empty state disabled)', 1)
    copy_path = os.path.join(HERE, '_pb_np_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run_no_project(copy_path)
        assert 'FAIL all_tabs_show_no_project_state' in output, (
            'NC: removing the empty-state render must fail the assertion:\n'
            + output)
        # discriminating: the panel itself still opens.
        assert 'PASS overlay_opens' in output, output
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'
