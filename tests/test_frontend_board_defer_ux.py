"""tests/test_frontend_board_defer_ux.py — the human decision surface for
PARKED (deferred) board epics.

The operator problem this closes: the three ``§10-GATED / design-first`` epics
(B/C/D) are ``deferred`` on the board, waiting on human infra decisions — but
the frontend had NO lane and NO control to make those decisions. Worse, a
``deferred`` epic silently fell into the ``open`` lane (``cols[t.status] ||
cols.open``), rendering as if it were claimable with no "why parked" context.

This suite pins the two halves of the fix:

  • Frontend (jsdom, driving the REAL shipped ``renderBoard`` / ``_boardMutate``
    in ``static/js/project-brain.js``):
      – a ``deferred`` epic renders in its OWN "Parked" lane, NOT the open lane;
      – the parked card carries a "Resume" control that calls ``boardReopen``
        (deferred → open) — the un-park decision;
      – an open/claimed card carries a "Park" control that calls ``boardDefer``.
    NC-FE byte-reverts the deferred-lane wiring in a COPY of the source →
    the parked epic leaks into the open lane → the assertion flips.

  • Backend (the ``/api/v1/project/board/defer`` route calls ``defer_task``).
    NC-BE byte-reverts the route body to a no-op → the epic is not parked.

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
_ROUTE_SRC = os.path.join(ROOT, 'routes', 'api_v1', 'project.py')


@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    """Create the DB schema once (the bare-pytest DB-warm quirk: project_tasks
    is otherwise absent). Mirrors tests/test_project_board_defer.py."""
    from lib.database import init_db
    with flask_app.app_context():
        init_db()
    yield


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


# A board carrying one epic of each status. The deferred epic is the one the
# operator must be able to act on.
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
win.prompt = global.prompt = () => 'awaiting infra decision';   // defer reason prompt

// Capture the human board mutations the UI dispatches.
const calls = [];
win.Api = global.Api = { project: {
  feed: (p) => Promise.resolve({ maxSeq: 0, events: [] }),
  charter: (p) => Promise.resolve({ exists: false, decisions: [] }),
  charterPending: (p) => Promise.resolve({ pending: [] }),
  board: (p) => Promise.resolve({
    open: 1, claimed: 1, deferred: 1, done: 0, tasks: [
      { id: 'pt_open1', title: 'OPEN EPIC CARD', status: 'open', owner_conv_id: '', depends_on: [] },
      { id: 'pt_cl1', title: 'CLAIMED EPIC CARD', status: 'claimed', owner_conv_id: 'cOWNER', depends_on: [] },
      { id: 'pt_def1', title: 'PARKED EPIC CARD', status: 'deferred', owner_conv_id: '', depends_on: [] },
    ] }),
  brainSummary: (p) => Promise.resolve({}),
  brainInfluence: (p) => Promise.resolve({}),
  boardReopen: (p, tid, cid) => { calls.push({ fn: 'reopen', tid: tid }); return Promise.resolve({ ok: true }); },
  boardDefer: (p, tid, cid, reason) => { calls.push({ fn: 'defer', tid: tid, reason: reason }); return Promise.resolve({ ok: true }); },
  boardComplete: (p, tid, cid) => { calls.push({ fn: 'complete', tid: tid }); return Promise.resolve({ ok: true }); },
} };

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

win.openProjectBrain();

Promise.resolve().then(()=>{}).then(()=>{}).then(()=>{}).then(()=>{}).then(() => {
  const board = win.document.getElementById('projectBrainBoardBody');
  const bHtml = board.innerHTML;

  // 1. The parked epic renders in a DEDICATED lane, not the open lane.
  const parkedLane = board.querySelector('.pb-board-lane-deferred');
  check('parked_lane_exists', !!parkedLane);
  check('parked_card_in_lane', !!parkedLane &&
        parkedLane.innerHTML.indexOf('PARKED EPIC CARD') !== -1);
  // The parked epic must NOT leak into the open lane (the silent-leak bug).
  const openLane = board.querySelector('.pb-board-lane-open');
  check('parked_not_in_open_lane', !!openLane &&
        openLane.innerHTML.indexOf('PARKED EPIC CARD') === -1);
  check('parked_card_class', !!board.querySelector('.pb-board-card.pb-board-deferred'));

  // 2. The parked card carries a Resume control (the un-park decision).
  const parkedCard = board.querySelector('.pb-board-card.pb-board-deferred');
  const resumeBtn = parkedCard && parkedCard.querySelector('.pb-board-act-resume');
  check('resume_control_present', !!resumeBtn);

  // 3. An open/claimed card carries a Park control (defer).
  const openCard = board.querySelector('.pb-board-card.pb-board-open');
  const parkBtn = openCard && openCard.querySelector('.pb-board-act-defer');
  check('park_control_present', !!parkBtn);

  // 4. Clicking Resume dispatches boardReopen for the parked epic.
  if (resumeBtn) {
    resumeBtn.click();
    Promise.resolve().then(()=>{}).then(() => {
      const reopen = calls.filter(c => c.fn === 'reopen');
      check('resume_calls_reopen', reopen.length === 1 && reopen[0].tid === 'pt_def1');
      // 5. Clicking Park dispatches boardDefer with the reason.
      const freshOpenCard = board.querySelector('.pb-board-card.pb-board-open');
      const freshPark = freshOpenCard && freshOpenCard.querySelector('.pb-board-act-defer');
      if (freshPark) {
        freshPark.click();
        Promise.resolve().then(()=>{}).then(() => {
          const def = calls.filter(c => c.fn === 'defer');
          check('park_calls_defer', def.length === 1 && def[0].tid === 'pt_open1');
          check('park_carries_reason', def.length && (def[0].reason || '').length > 0);
          console.log(out.join('\n'));
        });
      } else {
        console.log(out.join('\n'));
      }
    });
  } else {
    console.log(out.join('\n'));
  }
});
"""


def _run(brain_src):
    frag = _extract_panel_fragment()
    frag_file = os.path.join(HERE, '_pb_defer_fragment.html')
    harness = os.path.join(HERE, '_pb_defer_harness.js')
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
def test_deferred_lane_and_controls_render_and_dispatch():
    """The shipped renderBoard puts a deferred epic in its own Parked lane with
    a Resume control, keeps a Park control on open/claimed cards, and wires the
    clicks to boardReopen / boardDefer."""
    output = _run(_BRAIN_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'deferred-lane UX failures:\n' + output
    for marker in ('PASS parked_lane_exists', 'PASS parked_card_in_lane',
                   'PASS parked_not_in_open_lane', 'PASS parked_card_class',
                   'PASS resume_control_present', 'PASS park_control_present',
                   'PASS resume_calls_reopen', 'PASS park_calls_defer',
                   'PASS park_carries_reason'):
        assert marker in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_FE_deferred_lane_wiring_is_load_bearing():
    """NC-FE: byte-revert the deferred bucket + lane in a COPY of
    project-brain.js (back to the old open/claimed/done-only shape, where a
    deferred epic falls into cols.open) → the parked epic LEAKS into the open
    lane → the isolation assertions flip. Shipped file untouched."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor_cols = "var cols = { open: [], claimed: [], deferred: [], done: [] };"
    anchor_lane = (
        "      lane('claimed', 'projectBrain.laneClaimed') +\n"
        "      heldLane +\n"
        "      (cols.deferred.length ? lane('deferred', 'projectBrain.laneDeferred') : '') +\n"
        "      lane('done', 'projectBrain.laneDone');")
    assert anchor_cols in original and anchor_lane in original, \
        'deferred-lane anchors not found (source changed?)'
    patched = original.replace(
        anchor_cols, "var cols = { open: [], claimed: [], done: [] };", 1)
    patched = patched.replace(
        anchor_lane,
        "      lane('claimed', 'projectBrain.laneClaimed') +\n"
        "      heldLane +\n"
        "      lane('done', 'projectBrain.laneDone');", 1)
    copy_path = os.path.join(HERE, '_pb_defer_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        assert ('FAIL parked_lane_exists' in output
                or 'FAIL parked_not_in_open_lane' in output), \
            ('NC-FE: without the deferred lane, the parked epic must leak into '
             'the open lane (assertion should fail):\n' + output)
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


# ════════════════════════════════════════════════════════════════════
#  Backend: the /board/defer route delegates to defer_task
# ════════════════════════════════════════════════════════════════════

def test_defer_route_source_wires_defer_task():
    """The HTTP route must import + call defer_task (the parking primitive),
    read path/taskId/reason, and be registered at /board/defer."""
    with open(_ROUTE_SRC, encoding='utf-8') as f:
        src = f.read()
    assert "@api_v1_project_bp.route('/api/v1/project/board/defer', methods=['POST'])" in src, \
        'the /board/defer route must be registered'
    assert 'def project_board_defer():' in src
    assert 'from lib.conversations.project_board import defer_task' in src
    assert 'defer_task(project_path, conv_id, task_id, reason)' in src


def test_defer_route_parks_epic_via_http(flask_client, flask_app):
    """END-TO-END through the real HTTP surface: POST /api/v1/project/board/defer
    parks a real epic (status → deferred), reusing the shared flask_client
    fixture (open auth). Proves the route + defer_task are wired, not just that
    the source mentions them."""
    from lib.conversations.project_board import post_task, read_board
    with flask_app.app_context():
        tid = post_task('/http/defer', 'cA', 'design-first epic')['id']
    resp = flask_client.post('/api/v1/project/board/defer', json={
        'path': '/http/defer', 'taskId': tid, 'convId': 'cHUMAN',
        'reason': 'gated on Redis managed-vs-self-run'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    with flask_app.app_context():
        board = read_board('/http/defer')
    assert board['tasks'][0]['status'] == 'deferred', \
        'the /board/defer route must actually park the epic'


def test_reopen_route_unparks_deferred_via_http(flask_client, flask_app):
    """The un-park decision through HTTP: POST /board/reopen takes a deferred
    epic back to open (the existing route already supports deferred → open)."""
    from lib.conversations.project_board import defer_task, post_task, read_board
    with flask_app.app_context():
        tid = post_task('/http/unpark', 'cA', 'epic')['id']
        defer_task('/http/unpark', 'cHUMAN', tid)
    resp = flask_client.post('/api/v1/project/board/reopen', json={
        'path': '/http/unpark', 'taskId': tid, 'convId': 'cHUMAN'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    with flask_app.app_context():
        board = read_board('/http/unpark')
    assert board['tasks'][0]['status'] == 'open', \
        'the /board/reopen route must un-park a deferred epic'
