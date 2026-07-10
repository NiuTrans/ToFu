"""jsdom regression for the Team-column operator STOP affordance.

WHY
The Team column now lets the OPERATOR hard-abort a runaway sibling directly:
a conversation peer card that has a RUNNING task carries a danger "Stop" button.
On click it opens a themed danger-confirm; only on confirm does it call
``Api.project.brainPeerAbort(path, fromConv, toConv)`` — the human counterpart
to ``project_intervene(hard_abort=True)`` where the authenticated operator IS
the approval. Load-bearing behaviours:

  • The Stop button is present ONLY on a RUNNING conversation peer — an idle
    peer (nothing to abort) and a sub-agent (no own task/queue) get none.
  • The confirm GATE is load-bearing: a DENIED confirm must NOT call the abort
    API; an approved confirm calls it with the right (path, fromConv, toConv).
  • A click inside the stop affordance must NOT navigate to the peer (the
    card's click-to-open handler ignores clicks in ``.pb-peer-stop``).

Loads the REAL shipped ``project-brain-peers.js`` under jsdom. Frontend
NEGATIVE CONTROL: force the confirm to always-return-true in a COPY of the
source (neuter the gate) and assert a "denied" run now fires the abort — proving
the confirm is what protects the coercive action. Shipped file byte-identical.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
_PEERS_SRC = os.path.join(JS_DIR, 'project-brain-peers.js')


def _node_deps_available():
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# The harness takes a THIRD argv: the confirm decision ('yes' | 'no').
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[1];
const ROOT = process.argv[2];
const CONFIRM = process.argv[3];   // 'yes' | 'no'
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="projectBrainPeersBody"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
win.t = global.t = (k) => k;
win.Icon = global.Icon = () => '<svg></svg>';
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);

const nav = [];
win.loadConversation = global.loadConversation = (id) => { nav.push(id); };
win.activeConvId = global.activeConvId = 'cOPERATOR';
win.ProjectBrain = { _state: { path: '/proj/x' } };

// Themed confirm stub — returns a resolved promise per the argv decision.
const confirms = [];
win.showConfirm = global.showConfirm = (msg, opts) => {
  confirms.push({ msg, opts });
  return Promise.resolve(CONFIRM === 'yes');
};

// API seam — record brainPeerAbort calls.
const abortCalls = [];
win.Api = global.Api = { project: {
  brainPeers: () => Promise.resolve({ peers: [], count: 0 }),
  feed: () => Promise.resolve({ events: [] }),
  brainPeerMessage: () => Promise.resolve({ ok: true }),
  brainPeerAbort: (p, from, to) => {
    abortCalls.push({ path: p, from, to });
    return Promise.resolve({ ok: true, mode: 'hard_abort', aborted: 1 });
  },
}};

eval(fs.readFileSync(SRC, 'utf8'));
const P = win.ProjectBrainPeers;
const out = {};

// ── RUNNING conversation peer → carries a Stop button. ──
const running = P.buildPeerCard({ convId: 'cRUN', title: 'Runaway',
                                  taskStatus: 'running', round: 4 });
win.document.getElementById('projectBrainPeersBody').appendChild(running);
out.runningHasStop = !!running.querySelector('.pb-peer-stop-btn');

// ── IDLE conversation peer → no Stop (nothing to abort). ──
const idle = P.buildPeerCard({ convId: 'cIDLE', title: 'Idle', taskStatus: '' });
out.idleHasStop = !!idle.querySelector('.pb-peer-stop-btn');

// ── SUB-AGENT (running) → no Stop (no own task/queue). ──
const agent = P.buildPeerCard({ convId: 'cRUN', agentId: 'a1', title: 'Runaway',
                                taskStatus: 'running' });
out.agentHasStop = !!agent.querySelector('.pb-peer-stop-btn');

// ── Click inside the stop affordance must NOT navigate. ──
running.querySelector('.pb-peer-stop-btn').dispatchEvent(
  new win.MouseEvent('click', { bubbles: true }));

// Let the confirm promise + abort promise settle, then report.
Promise.resolve().then(()=>{}).then(()=>{}).then(() => {
  out.confirmShown = confirms.length;
  out.confirmDanger = confirms.length ? !!confirms[0].opts.danger : false;
  out.abortCall = abortCalls[0] || null;
  out.abortCount = abortCalls.length;
  out.navTotal = nav.length;
  console.log('__RESULT__' + JSON.stringify(out));
});
"""


def _run(src, confirm):
    proc = subprocess.run(
        ['node', '-e', _HARNESS, src, ROOT, confirm],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed: {proc.stderr or proc.stdout}')
    for line in proc.stdout.splitlines():
        if line.startswith('__RESULT__'):
            return json.loads(line[len('__RESULT__'):])
    raise AssertionError(f'no result line: {proc.stdout}\n{proc.stderr}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_stop_present_only_on_running_conv_peer_and_confirmed_abort():
    out = _run(_PEERS_SRC, 'yes')
    # Present only on a running conversation peer.
    assert out['runningHasStop'] is True, out
    assert out['idleHasStop'] is False, 'idle peer has nothing to abort'
    assert out['agentHasStop'] is False, 'a sub-agent has no own task/queue'
    # A danger-confirm was shown.
    assert out['confirmShown'] == 1 and out['confirmDanger'] is True, out
    # Confirmed → the abort API is called with (path, operator, target).
    call = out['abortCall']
    assert call is not None, out
    assert call['path'] == '/proj/x', out
    assert call['from'] == 'cOPERATOR', 'the operator acting conv is the sender'
    assert call['to'] == 'cRUN', "this card's peer is the abort target"
    # The stop click did not navigate the operator away.
    assert out['navTotal'] == 0, out


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_denied_confirm_does_not_abort():
    """The confirm GATE: a DENIED confirm must NOT call the abort API."""
    out = _run(_PEERS_SRC, 'no')
    assert out['confirmShown'] == 1, out
    assert out['abortCount'] == 0, 'a denied confirm must not hard-abort the peer'
    assert out['navTotal'] == 0, out


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_confirm_gate_is_load_bearing(tmp_path):
    """NC: force the confirm to always-resolve-true in a COPY of the source
    (neuter the gate). Now even the 'no' decision runs the abort → proving the
    confirm is what protects the coercive action. Shipped file byte-identical."""
    with open(_PEERS_SRC, encoding='utf-8') as f:
        original = f.read()
    # The gate resolves the themed confirm; force it to always be true.
    anchor = ("      if (typeof showConfirm === 'function') {\n"
              "        return Promise.resolve(showConfirm(msg, {")
    assert anchor in original, 'confirm-gate anchor not found'
    patched = original.replace(
        anchor,
        "      if (true) {\n"
        "        return Promise.resolve(true);  // NC: gate neutered\n"
        "        return Promise.resolve(showConfirm(msg, {",
        1)
    nc_file = os.path.join(tmp_path, 'project-brain-peers-nc.js')
    with open(nc_file, 'w', encoding='utf-8') as f:
        f.write(patched)

    # With the gate neutered, even a 'no' decision aborts (the stub isn't
    # consulted — confirm always resolves true).
    out = _run(nc_file, 'no')
    assert out['abortCount'] == 1, \
        f'NC: with the confirm gate neutered a denied stop must still abort: {out}'

    with open(_PEERS_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-peers.js must be byte-identical'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
