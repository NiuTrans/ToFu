"""jsdom regression for the Project Brain Team/Peers column (project-brain-peers.js).

WHY
The Team column is the cohesion surface — it renders the LIVE sibling roster
(presence ⋈ task ⋈ claimed-epic) plus the peer-message thread. Two behaviours
are load-bearing and easy to silently break:

  • the roster renders one card per peer with the presence-dot state derived
    from the peer's live task status (running → active/green, else idle/amber),
    and shows what each peer is *advancing* (the claimed epic);
  • the peer-message THREAD is extracted from the feed by the fromConv/toConv
    payload markers — the exact seam that makes cross-conversation messages
    visible at the project level.

This harness loads the REAL shipped ``project-brain-peers.js`` under jsdom,
mounts the ACTUAL ``#projectBrainPeersBody`` element, and drives ``renderPeers``
+ ``extractPeerThread`` with a realistic roster + feed.

Frontend NEGATIVE CONTROL: patch a COPY of project-brain-peers.js reverting the
``if (!pl.fromConv || !pl.toConv) continue;`` thread filter to always-include,
run the SAME harness, and assert a non-peer feed note (a plain 'started' event
with no fromConv/toConv) now leaks into the thread. The shipped file is
asserted byte-identical afterwards.

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


# The jsdom driver. Mounts the real Team-column element, stubs the globals the
# module reads at runtime (Icon / t / escapeHtml / loadConversation), then
# calls the REAL renderPeers + extractPeerThread and reports the resulting DOM.
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
// node -e SCRIPT a b  →  process.argv === ['node', 'a', 'b'] (no script slot).
const SRC = process.argv[1];
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="projectBrainPeersBody"><div class="pb-peers-empty">none</div></div>' +
  '</body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
// Minimal runtime-global stubs the module reads.
win.t = global.t = (k) => k;                 // echo the key (assert on keys)
win.Icon = global.Icon = () => '<svg></svg>';
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.loadConversation = global.loadConversation = () => {};

eval(fs.readFileSync(SRC, 'utf8'));  // project-brain-peers.js

const P = win.ProjectBrainPeers;
const out = {};

// ── Roster: two peers — one running (active), one idle; + a sub-agent. ──
const status = { peers: [
  { convId: 'convAAAA1111', title: 'Refactor parser', taskStatus: 'running',
    round: 4, claimedEpic: 'Rewrite the tokenizer', currentFile: 'lex.py' },
  { convId: 'convBBBB2222', title: 'Docs pass', taskStatus: '', round: 0 },
  { convId: 'convAAAA1111', agentId: 'a2', title: 'Refactor parser',
    statusLabel: 'searching' },
]};

// ── Feed: two peer notes (fromConv/toConv) + one NON-peer note (a plain
//    'started' event) that MUST NOT leak into the thread. ──
const feedEvents = [
  { seq: 5, kind: 'note', summary: 'note → conv convBBBB: heads up on lex.py',
    ts: Date.now() - 60000, payload: { fromConv: 'convAAAA1111', toConv: 'convBBBB2222', kind: 'note' } },
  { seq: 7, kind: 'note', summary: 'intervention → conv convAAAA: stop, dup epic',
    ts: Date.now() - 30000, payload: { fromConv: 'convBBBB2222', toConv: 'convAAAA1111', kind: 'intervention' } },
  // A plain 'note' with NO fromConv/toConv (e.g. a board reopen note) — only
  // the fromConv/toConv filter (the NC target) excludes it, NOT the kind check.
  { seq: 9, kind: 'note', summary: 'Reopened: some epic', ts: Date.now(),
    payload: { reopened: true } },
];

const thread = P.extractPeerThread(feedEvents);
P.renderPeers(status, thread);

const body = win.document.getElementById('projectBrainPeersBody');
out.cardCount = body.querySelectorAll('.pb-peer-card').length;
out.dotStates = Array.from(body.querySelectorAll('.pb-peer-dot'))
  .map(d => d.getAttribute('data-state'));
out.agentCards = body.querySelectorAll('.pb-peer-agent').length;
out.doingText = Array.from(body.querySelectorAll('.pb-peer-doing')).map(e => e.textContent);
out.rosterHead = (body.querySelector('.pb-peers-roster-head') || {}).textContent || '';
// Thread
out.threadLen = thread.length;
out.threadKinds = thread.map(m => m.kind);
out.msgRows = body.querySelectorAll('.pb-peer-msg').length;
out.interveneRows = body.querySelectorAll('.pb-peer-msg-intervene').length;
// The from/to ids in the thread carry [data-conv-id] so the panel's hover
// preview reaches them.
out.threadCidCount = body.querySelectorAll('.pb-peer-msg-cid[data-conv-id]').length;
// _peerState pure check
out.stateActive = P._peerState({ taskStatus: 'running' });
out.stateIdle = P._peerState({ taskStatus: '' });

console.log('__RESULT__' + JSON.stringify(out));
"""


def _run(src):
    proc = subprocess.run(
        ['node', '-e', _HARNESS, src, ROOT],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed: {proc.stderr or proc.stdout}')
    for line in proc.stdout.splitlines():
        if line.startswith('__RESULT__'):
            return json.loads(line[len('__RESULT__'):])
    raise AssertionError(f'no result line: {proc.stdout}\n{proc.stderr}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_peers_roster_and_thread_render():
    out = _run(_PEERS_SRC)
    # Roster: 3 cards (2 convs + 1 sub-agent).
    assert out['cardCount'] == 3, out
    # Presence dots: the running peer → active, the idle peer → idle, the
    # sub-agent (no taskStatus) → idle.
    assert out['dotStates'] == ['active', 'idle', 'idle'], out
    assert out['agentCards'] == 1, out
    # The active peer's "doing" line names the epic it is advancing.
    joined = ' '.join(out['doingText'])
    assert 'peerAdvancing' in joined, out          # i18n key echoed
    assert out['rosterHead'] == 'projectBrain.peersHere', out
    # Thread: exactly the TWO peer notes (the plain 'started' event excluded).
    assert out['threadLen'] == 2, out
    assert out['threadKinds'] == ['note', 'intervention'], out
    assert out['msgRows'] == 2, out
    assert out['interveneRows'] == 1, out
    # Two peer notes × 2 ids each → 4 [data-conv-id] spans for hover-preview.
    assert out['threadCidCount'] == 4, \
        f'thread from/to ids must carry [data-conv-id] for hover preview: {out}'
    # Pure state mapping.
    assert out['stateActive'] == 'active' and out['stateIdle'] == 'idle', out


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_thread_filter_is_load_bearing(tmp_path):
    """NC: revert the fromConv/toConv thread filter to always-include → a
    plain non-peer feed note (the 'started' event) LEAKS into the thread,
    breaking the count. Proves the filter is what isolates cross-conversation
    messages from ordinary feed activity. Shipped file restored byte-identical.
    """
    with open(_PEERS_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = '      if (!pl.fromConv || !pl.toConv) continue;'
    assert anchor in original, 'thread-filter anchor not found'
    patched = original.replace(
        anchor, '      if (false) continue;  // NC filter disabled', 1)
    nc_file = os.path.join(tmp_path, 'project-brain-peers-nc.js')
    with open(nc_file, 'w', encoding='utf-8') as f:
        f.write(patched)

    out = _run(nc_file)
    # With the filter disabled, the 3rd feed event (a plain 'started' note with
    # no fromConv/toConv) is no longer excluded → thread grows to 3.
    assert out['threadLen'] == 3, \
        f'NC: without the fromConv/toConv filter the non-peer note must leak: {out}'

    # Shipped file untouched.
    with open(_PEERS_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-peers.js must be byte-identical'
