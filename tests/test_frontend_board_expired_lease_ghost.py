"""tests/test_frontend_board_expired_lease_ghost.py — the "occupied, no
assignee" ghost-card bug on the Project Brain board.

The operator problem this closes: a ``kind='lease'`` path reservation whose
soft TTL has EXPIRED is downgraded by the backend ``read_board`` to
``status:'open'`` with a BLANK ``owner_conv_id`` (``_effective_status`` +
``_row_to_task``). The agent-facing ``render_board_block`` correctly drops it
(its Held filter is ``kind=='lease' AND status=='claimed'``), but the frontend
``renderBoard`` (static/js/project-brain.js) partitioned Held on ``kind ===
'lease'`` ALONE — so the dead lease still rendered as an ownerless
"Held — do not edit" card that never garbage-collects.

This suite pins the fix by driving the REAL shipped ``renderBoard`` under
jsdom over a board carrying BOTH a live lease and an expired lease (exactly the
shape ``read_board`` returns): only the LIVE lease may appear in the Held lane.

NC-FE byte-reverts the ``status === 'claimed'`` guard in a COPY of the source
(back to the ``kind === 'lease'``-only partition) → the expired lease leaks
back into the Held lane as the ghost card → the assertion flips. Shipped file
untouched.

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
    """Pull the REAL #projectBrainOverlay markup out of the shipped index.html
    so #projectBrainBoardBody is the ACTUAL shipped element."""
    with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
        html = f.read()
    start = html.find('<div class="project-brain-overlay"')
    assert start != -1, 'project-brain-overlay not found in index.html'
    end = html.find('<div class="chat-container"', start)
    assert end != -1, 'could not bound the overlay fragment'
    return html[start:end].strip()


# A board carrying two path-leases: one LIVE (effective status 'claimed', a
# real held reservation) and one EXPIRED — which read_board already downgraded
# to status:'open' with a blank owner (the exact shape the backend returns for
# a dead lease). Only the LIVE one is a genuine hold.
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
win.t = global.t = (k, f) => (f == null ? k : f);   // echo fallback text
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};
win.getActiveConv = global.getActiveConv = () => ({ id: 'c1', projectPath: '/proj/real' });
win._getConvProjectPath = global._getConvProjectPath = (c) => (c && c.projectPath) || '';
win.pushSubscribe = global.pushSubscribe = () => {};
win.pushUnsubscribe = global.pushUnsubscribe = () => {};

win.Api = global.Api = { project: {
  feed: (p) => Promise.resolve({ maxSeq: 0, events: [] }),
  charter: (p) => Promise.resolve({ exists: false, decisions: [] }),
  charterPending: (p) => Promise.resolve({ pending: [] }),
  board: (p) => Promise.resolve({
    open: 1, claimed: 0, deferred: 0, done: 0, tasks: [
      { id: 'pt_epic1', title: 'A REAL OPEN EPIC', status: 'open', kind: 'epic', owner_conv_id: '', depends_on: [] },
      // LIVE lease — effective status 'claimed', a genuine held reservation.
      { id: 'pt_live_lease', title: 'LIVE HELD PATH', status: 'claimed', kind: 'lease', owner_conv_id: 'cHOLDER', depends_on: [] },
      // EXPIRED lease — read_board downgraded it: status 'open', blank owner.
      // This is the ghost the frontend must DROP, not render in Held.
      { id: 'pt_dead_lease', title: 'EXPIRED GHOST PATH', status: 'open', kind: 'lease', owner_conv_id: '', depends_on: [] },
    ] }),
  brainSummary: (p) => Promise.resolve({}),
  brainInfluence: (p) => Promise.resolve({}),
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

win.openProjectBrain();

Promise.resolve().then(()=>{}).then(()=>{}).then(()=>{}).then(()=>{}).then(() => {
  const board = win.document.getElementById('projectBrainBoardBody');
  const heldLane = board.querySelector('.pb-board-lane-held');

  // The LIVE lease is a genuine hold → it must appear in the Held lane.
  check('held_lane_exists', !!heldLane);
  check('live_lease_in_held', !!heldLane &&
        heldLane.innerHTML.indexOf('LIVE HELD PATH') !== -1);

  // The EXPIRED lease is the ghost: it must NOT appear anywhere — not in Held
  // (it holds nothing), and not in the Open epic lane (it's not an epic).
  check('expired_lease_not_in_held', !heldLane ||
        heldLane.innerHTML.indexOf('EXPIRED GHOST PATH') === -1);
  const openLane = board.querySelector('.pb-board-lane-open');
  check('expired_lease_not_in_open', !!openLane &&
        openLane.innerHTML.indexOf('EXPIRED GHOST PATH') === -1);
  check('expired_lease_absent_everywhere',
        board.innerHTML.indexOf('EXPIRED GHOST PATH') === -1);

  console.log(out.join('\n'));
});
"""


def _run(brain_src):
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_ghost_fragment.html')
    harness = os.path.join(HERE, '_pb_ghost_harness.js')
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
def test_expired_lease_dropped_from_held_lane():
    """The shipped renderBoard keeps a LIVE lease in the Held lane but DROPS an
    expired lease (read as status:'open', blank owner) — no ownerless ghost."""
    output = _run(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'expired-lease ghost failures:\n' + output
    for marker in ('PASS held_lane_exists', 'PASS live_lease_in_held',
                   'PASS expired_lease_not_in_held', 'PASS expired_lease_not_in_open',
                   'PASS expired_lease_absent_everywhere'):
        assert marker in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_FE_expired_lease_guard_is_load_bearing():
    """NC-FE: byte-revert the Held partition guard in a COPY of project-brain.js
    (back to the ``kind === 'lease'``-only shape, dropping the
    ``status === 'claimed'`` check) → the expired lease leaks back into the Held
    lane as the ghost card → the isolation assertions flip. Shipped file
    untouched."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("      if (t.kind === 'lease') { "
              "if (t.status === 'claimed') held.push(t); continue; }")
    assert anchor in original, \
        'expired-lease guard anchor not found (source changed?)'
    patched = original.replace(
        anchor, "      if (t.kind === 'lease') { held.push(t); continue; }", 1)
    assert patched != original, 'NC replacement was a no-op'
    copy_path = os.path.join(HERE, '_pb_ghost_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        assert ('FAIL expired_lease_not_in_held' in output
                or 'FAIL expired_lease_absent_everywhere' in output), \
            ('NC-FE: without the status==claimed guard, the expired lease must '
             'leak into the Held lane (assertion should fail):\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'
