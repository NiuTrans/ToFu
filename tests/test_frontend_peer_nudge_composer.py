"""jsdom regression for the Team-column NUDGE composer (project-brain-peers.js).

WHY
The Team column now lets the OPERATOR message a sibling conversation: each
conversation peer card carries a "Nudge" button that toggles an inline composer
(textarea + Send). On send it calls ``Api.project.brainPeerMessage(path,
fromConv, toConv, text)`` — the single human seam onto ``send_peer_message`` —
and surfaces the result inline. Two behaviours are load-bearing:

  • Send calls the API with the RIGHT four args (path, the operator's acting
    conv as sender, this card's target conv, the textarea text) and shows the
    "sent" status; a rate-limited error surfaces the rate-limit string.
  • A click INSIDE the composer must NOT navigate to the peer conversation
    (the card's click-to-open handler must ignore clicks landing in
    ``.pb-peer-nudge``) — otherwise typing/sending would yank the operator away.

This harness loads the REAL shipped ``project-brain-peers.js`` under jsdom,
mounts the actual ``#projectBrainPeersBody``, stubs ``Api.project`` +
``activeConvId`` + ``window.ProjectBrain._state.path``, drives ``buildPeerCard``
(which appends the affordance) and the composer flow.

Frontend NEGATIVE CONTROL: revert the card-click guard
(``if (e.target ... .closest('.pb-peer-nudge')) return;``) to always-navigate,
then assert a composer click DOES navigate (loadConversation fires) — proving
the guard is what keeps the operator in place while composing. Shipped file
restored byte-identical.

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


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[1];
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="projectBrainPeersBody"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
win.t = global.t = (k) => k;
win.Icon = global.Icon = () => '<svg></svg>';
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);

// Track navigation attempts (card click → open conv).
const nav = [];
win.loadConversation = global.loadConversation = (id) => { nav.push(id); };

// The operator's acting conversation (their proxy sender).
win.activeConvId = global.activeConvId = 'cOPERATOR';
// The displayed project path (project-brain-peers reads ProjectBrain._state.path).
win.ProjectBrain = { _state: { path: '/proj/x' } };

// Stub the API seam — record calls; the FIRST send resolves ok, a flagged
// second rejects with a rate_limited ApiError-shape.
const apiCalls = [];
let rejectNext = false;
win.Api = global.Api = { project: {
  brainPeers: () => Promise.resolve({ peers: [], count: 0 }),
  feed: () => Promise.resolve({ events: [] }),
  brainPeerMessage: (p, from, to, text) => {
    apiCalls.push({ path: p, from, to, text });
    if (rejectNext) {
      const e = new Error('rate_limited'); e.code = 'rate_limited';
      return Promise.reject(e);
    }
    return Promise.resolve({ ok: true, queueId: 'q1' });
  },
}};

eval(fs.readFileSync(SRC, 'utf8'));
const P = win.ProjectBrainPeers;
const out = {};

// ── Build a conversation peer card; it must carry the nudge affordance. ──
const card = P.buildPeerCard({ convId: 'cTARGET1', title: 'Docs pass',
                               taskStatus: 'running', round: 2 });
win.document.getElementById('projectBrainPeersBody').appendChild(card);
out.hasNudge = !!card.querySelector('.pb-peer-nudge');
out.hasToggle = !!card.querySelector('.pb-peer-nudge-toggle');

// A SUB-AGENT card must NOT get a composer (no queue of its own).
const agentCard = P.buildPeerCard({ convId: 'cTARGET1', agentId: 'a1',
                                    title: 'Docs pass' });
out.agentHasNudge = !!agentCard.querySelector('.pb-peer-nudge');

// ── Toggle: composer hidden → visible. ──
const composer = card.querySelector('.pb-peer-nudge-composer');
out.composerHiddenInitially = composer.hidden;
card.querySelector('.pb-peer-nudge-toggle').click();
out.composerVisibleAfterToggle = !composer.hidden;

// ── Click INSIDE the composer must NOT navigate. ──
const ta = card.querySelector('.pb-peer-nudge-input');
ta.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
out.navAfterComposerClick = nav.length;   // expect 0

// ── Fill + Send → API called with (path, fromConv, toConv, text). ──
ta.value = 'please rebase onto main';
card.querySelector('.pb-peer-nudge-send').click();

// Resolve microtasks, then a second (rejecting) send, then report.
Promise.resolve().then(() => {}).then(() => {
  out.apiCall = apiCalls[0] || null;
  out.statusOkText = (card.querySelector('.pb-peer-nudge-status') || {}).textContent || '';
  out.statusOkClass = (card.querySelector('.pb-peer-nudge-status') || {}).className || '';

  // Now drive a rate-limited send on a fresh card.
  rejectNext = true;
  const card2 = P.buildPeerCard({ convId: 'cTARGET2', title: 'Other' });
  win.document.getElementById('projectBrainPeersBody').appendChild(card2);
  card2.querySelector('.pb-peer-nudge-toggle').click();
  const ta2 = card2.querySelector('.pb-peer-nudge-input');
  ta2.value = 'ping';
  card2.querySelector('.pb-peer-nudge-send').click();
  return Promise.resolve().then(() => {}).then(() => {
    out.rateStatusText = (card2.querySelector('.pb-peer-nudge-status') || {}).textContent || '';
    out.rateStatusClass = (card2.querySelector('.pb-peer-nudge-status') || {}).className || '';
    out.navTotal = nav.length;   // still 0 — no send ever navigated
    console.log('__RESULT__' + JSON.stringify(out));
  });
});
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
def test_nudge_composer_send_and_no_nav():
    out = _run(_PEERS_SRC)
    # Affordance present on a conv card, absent on a sub-agent card.
    assert out['hasNudge'] and out['hasToggle'], out
    assert out['agentHasNudge'] is False, 'a sub-agent has no queue → no composer'
    # Toggle reveals the composer.
    assert out['composerHiddenInitially'] is True, out
    assert out['composerVisibleAfterToggle'] is True, out
    # A click inside the composer did NOT navigate away.
    assert out['navAfterComposerClick'] == 0, out
    # Send called the API with the right four args.
    call = out['apiCall']
    assert call is not None, out
    assert call['path'] == '/proj/x', out
    assert call['from'] == 'cOPERATOR', 'the operator acting conv is the sender'
    assert call['to'] == 'cTARGET1', "this card's peer is the target"
    assert call['text'] == 'please rebase onto main', out
    # Success status shown.
    assert 'peerNudgeSent' in out['statusOkText'], out
    assert 'pb-peer-nudge-status-ok' in out['statusOkClass'], out
    # A rate-limited send surfaces the rate-limit string (err class).
    assert 'peerNudgeRateLimited' in out['rateStatusText'], out
    assert 'pb-peer-nudge-status-err' in out['rateStatusClass'], out
    # No send ever navigated the operator away.
    assert out['navTotal'] == 0, out


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_card_click_guard_is_load_bearing(tmp_path):
    """NC: revert the card-click composer guard to always-navigate → a click
    inside the composer now fires loadConversation (yanking the operator away
    mid-compose). Proves the guard is what keeps the composer usable. Shipped
    file restored byte-identical."""
    with open(_PEERS_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("        // A click inside the nudge composer must NOT navigate away.\n"
              "        if (e.target && e.target.closest && e.target.closest('.pb-peer-nudge')) return;")
    assert anchor in original, 'card-click guard anchor not found'
    patched = original.replace(
        anchor, "        // NC guard disabled", 1)
    nc_file = os.path.join(tmp_path, 'project-brain-peers-nc.js')
    with open(nc_file, 'w', encoding='utf-8') as f:
        f.write(patched)

    out = _run(nc_file)
    # With the guard removed, the composer click bubbles to the card handler and
    # navigates → navAfterComposerClick becomes nonzero (the baseline is 0).
    assert out['navAfterComposerClick'] >= 1, \
        f'NC: without the guard a composer click must navigate away: {out}'

    with open(_PEERS_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-peers.js must be byte-identical'
