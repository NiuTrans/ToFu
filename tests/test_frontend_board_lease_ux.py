"""tests/test_frontend_board_lease_ux.py — the HUMAN-facing Held lane for path
LEASES (kind='lease').

The operator problem this closes: ``read_board`` now returns path-lease rows
(a durational "I'm editing these paths, hold off" reservation) to the board
API. A lease carries ``status='claimed'`` but is NOT an epic being advanced —
so the shipped ``renderBoard`` (which buckets purely on ``t.status``) would
render it as a **claimed epic in the Claimed lane** AND inflate the attention
badge (``open + claimed``). That is the same class of silent leak as the
"deferred epic fell into the Open lane" bug — the agent sees a clean backend
"Held" section while the operator sees a phantom claimed epic.

This suite pins the fix in the REAL shipped ``renderBoard``
(``static/js/project-brain.js``), driven through jsdom over the ACTUAL
``#projectBrainBoardBody`` from ``index.html``:

  • a ``kind='lease'`` (``status='claimed'``) row lands in a DEDICATED Held
    lane, NOT the Claimed lane;
  • the held card shows the held path + holder, and offers NO epic lifecycle
    actions (no Complete/Block/Park);
  • the lease does NOT increment the board attention badge.

NC-FE byte-reverts the lease partition in a COPY of the source → the lease
leaks into the Claimed lane + inflates the badge → the assertions flip. Shipped
file untouched.

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
_BRAIN_SRC = os.path.join(ROOT, 'static', 'js', 'project-brain.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


def _extract_panel_fragment():
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    start = html.find('<div class="project-brain-overlay"')
    assert start != -1, 'project-brain-overlay not found in index.html'
    end = html.find('<div class="chat-container"', start)
    assert end != -1, 'could not bound the overlay fragment'
    return html[start:end].strip()


# A board carrying one open epic, one CLAIMED epic, and one path LEASE
# (kind='lease', status='claimed'). The lease is the row the operator must
# NOT see as a claimed epic.
_HARNESS = r"""
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
win.t = global.t = (k, f) => (f == null ? k : f);
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};
win.getActiveConv = global.getActiveConv = () => ({ id: 'c1', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};
win.prompt = global.prompt = () => 'x';

// Capture the badge count the UI sets for the Board tab. renderBoard calls
// _setTabCount('pbTabCountBoard', N); we spy by pre-seeding the element and
// reading its text after render (that's what _setTabCount writes).
const calls = [];
win.Api = global.Api = { project: {
  feed: (p) => Promise.resolve({ maxSeq: 0, events: [] }),
  charter: (p) => Promise.resolve({ exists: false, decisions: [] }),
  charterPending: (p) => Promise.resolve({ pending: [] }),
  board: (p) => Promise.resolve({
    open: 1, claimed: 1, done: 0, tasks: [
      { id: 'pt_open1', title: 'OPEN EPIC CARD', status: 'open', kind: 'epic', owner_conv_id: '', depends_on: [] },
      { id: 'pt_cl1', title: 'CLAIMED EPIC CARD', status: 'claimed', kind: 'epic', owner_conv_id: 'cOWNER', depends_on: [] },
      { id: 'pt_lease1', title: 'static/styles.css', status: 'claimed', kind: 'lease', owner_conv_id: 'cHOLDER', depends_on: [] },
    ] }),
  brainSummary: (p) => Promise.resolve({}),
  brainInfluence: (p) => Promise.resolve({}),
  boardReopen: (p, tid, cid) => { calls.push({ fn: 'reopen', tid: tid }); return Promise.resolve({ ok: true }); },
  boardComplete: (p, tid, cid) => { calls.push({ fn: 'complete', tid: tid }); return Promise.resolve({ ok: true }); },
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

win.openProjectBrain();

Promise.resolve().then(()=>{}).then(()=>{}).then(()=>{}).then(()=>{}).then(() => {
  const board = win.document.getElementById('projectBrainBoardBody');

  // 1. The lease renders in a DEDICATED Held lane, not the Claimed lane.
  const heldLane = board.querySelector('.pb-board-lane-held');
  check('held_lane_exists', !!heldLane);
  check('lease_in_held_lane', !!heldLane &&
        heldLane.innerHTML.indexOf('static/styles.css') !== -1);
  const claimedLane = board.querySelector('.pb-board-lane-claimed');
  check('lease_not_in_claimed_lane', !!claimedLane &&
        claimedLane.innerHTML.indexOf('static/styles.css') === -1);
  // The genuine claimed EPIC must still be in the claimed lane.
  check('claimed_epic_still_in_claimed', !!claimedLane &&
        claimedLane.innerHTML.indexOf('CLAIMED EPIC CARD') !== -1);
  check('lease_card_class', !!board.querySelector('.pb-board-card.pb-board-held'));

  // 2. The held card shows the holder and offers NO epic lifecycle actions.
  const leaseCard = board.querySelector('.pb-board-card.pb-board-held');
  check('lease_shows_holder', !!leaseCard &&
        leaseCard.innerHTML.indexOf('cHOLDER') !== -1);
  check('lease_has_no_actions', !!leaseCard &&
        !leaseCard.querySelector('.pb-board-act'));

  // 3. The badge counts only real epics (open 1 + claimed 1 = 2), NOT the
  //    lease. _setTabCount wrote it into #pbTabCountBoard.
  const badge = win.document.getElementById('pbTabCountBoard');
  const badgeTxt = badge ? (badge.textContent || '').trim() : '(missing)';
  check('badge_excludes_lease', badgeTxt === '2');

  console.log('BADGE=' + badgeTxt);
  console.log(out.join('\n'));
});
"""


def _run(brain_src):
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_lease_fragment.html')
    harness = os.path.join(HERE, '_pb_lease_harness.js')
    with open(frag_file, 'w', encoding='utf-8') as f:
        f.write(frag)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
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
def test_lease_renders_in_held_lane_not_claimed():
    """The shipped renderBoard puts a kind='lease' row in its own Held lane
    (not the Claimed lane), shows the holder with no epic actions, and does NOT
    count the lease in the board attention badge."""
    output = _run(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'held-lane UX failures:\n' + output
    for marker in ('PASS held_lane_exists', 'PASS lease_in_held_lane',
                   'PASS lease_not_in_claimed_lane',
                   'PASS claimed_epic_still_in_claimed', 'PASS lease_card_class',
                   'PASS lease_shows_holder', 'PASS lease_has_no_actions',
                   'PASS badge_excludes_lease'):
        assert marker in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_FE_lease_partition_is_load_bearing():
    """NC-FE: byte-revert the lease partition in a COPY of project-brain.js
    (drop the `if (t.kind === 'lease')` skip so a lease falls through to
    `cols[t.status]` = the Claimed bucket) → the lease LEAKS into the Claimed
    lane AND inflates the badge to 3 → the isolation + badge assertions flip.
    Shipped file untouched."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = (
        "      if (t.kind === 'lease') { if (t.status === 'claimed') held.push(t); continue; }\n")
    assert anchor in original, 'lease-partition anchor not found (source changed?)'
    # Drop the lease partition entirely so a lease falls through to the later
    # `(cols[t.status] || cols.open).push(t)` = the Claimed bucket.
    patched = original.replace(anchor, "", 1)
    copy_path = os.path.join(HERE, '_pb_lease_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        assert ('FAIL lease_not_in_claimed_lane' in output
                or 'FAIL badge_excludes_lease' in output), \
            ('NC-FE: without the lease partition, the lease must leak into the '
             'Claimed lane and/or inflate the badge (assertion should fail):\n'
             + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'
