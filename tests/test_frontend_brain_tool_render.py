"""tests/test_frontend_brain_tool_render.py — structured Project-Brain tool cards.

Phase 3 of the Project-Brain optimization: the 11 brain/conv-meta tools used to
render as ONE generic Markdown blob (``_renderConvMetaBlock`` dumping
``round.toolContent``). This pins the STRUCTURED per-tool renderers that replace
that dump, driven off the backend-attached structured meta
(``results[0].boardSnapshot`` / ``boardTransition`` / ``peerStatus`` /
``charterProposal``) — NOT re-parsed prose:

  • ``project_board_read``      → a mini-kanban (lane counts + epic titles).
  • board mutations             → an explicit transition line (verb + epic + status).
  • ``project_peer_status``     → live peer cards (conv id + status + round + epic).
  • ``project_charter_propose`` → the proposal text + a "pending human review" affordance.

Loads the REAL shipped ``ui/tool_rounds.js`` under jsdom and calls the REAL
``_renderUnifiedToolLine`` (the same entry the transcript uses), so a broken
route / missing branch fails here. Each renderer ships a double-neuter NC:
patch a COPY to disable the structured branch, assert the structured markup is
GONE (falls back to the prose dump), restore byte-identical.

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
_TR_SRC = os.path.join(JS_DIR, 'ui', 'tool_rounds.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t = (k, d) => (d || k);
// renderMarkdown is the FALLBACK path — mark its output so we can assert the
// structured renderer replaced it (structured card ⇒ no MD-DUMP marker).
global.renderMarkdown = (s) => 'MD-DUMP:' + String(s);
global.Icon = (n) => '<svg data-icon="' + n + '"></svg>';
global._shortUrl = (u) => u;
global.formatNumber = (n) => String(n);

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/tool_rounds.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── project_board_read → mini-kanban ──
const boardRound = {
  status: 'done', toolName: 'project_board_read', query: 'project_board_read',
  toolContent: 'RAW BOARD PROSE', toolRounds: [],
  results: [{ source: 'Board', boardSnapshot: {
    open: 1, claimed: 1, done: 1, lanes: {
      open: [{ id: 'pt_o', title: 'OPEN EPIC A', owner: '', dispatched: false }],
      claimed: [{ id: 'pt_c', title: 'CLAIMED EPIC B', owner: 'cOWNER', dispatched: true }],
      done: [{ id: 'pt_d', title: 'DONE EPIC C', owner: '', dispatched: false }],
    } } }],
};
const bHtml = _renderUnifiedToolLine(boardRound, false);
check('board_mini_class', bHtml.includes('ptool-board-mini'));
check('board_mini_open_epic', bHtml.includes('OPEN EPIC A'));
check('board_mini_claimed_epic', bHtml.includes('CLAIMED EPIC B'));
check('board_mini_owner', bHtml.includes('cOWNER'));
check('board_mini_auto_badge', bHtml.includes('ptool-board-mini-auto'));
check('board_mini_not_md_dump', !bHtml.includes('MD-DUMP:RAW BOARD PROSE'));

// ── board mutation → transition line ──
const trRound = {
  status: 'done', toolName: 'project_board_complete', query: 'project_board_complete',
  toolContent: 'Marked done.', toolRounds: [],
  results: [{ source: 'Board', boardTransition: {
    verb: 'complete', taskId: 'pt_x', title: 'FINISH EPIC', status: 'done' } }],
};
const trHtml = _renderUnifiedToolLine(trRound, false);
check('transition_class', trHtml.includes('ptool-board-transition'));
check('transition_title', trHtml.includes('FINISH EPIC'));
check('transition_verb', trHtml.includes('completed') || trHtml.includes('complete'));

// ── project_peer_status → peer cards ──
const peerRound = {
  status: 'done', toolName: 'project_peer_status', query: 'project_peer_status',
  toolContent: 'RAW PEER PROSE', toolRounds: [],
  results: [{ source: 'Peer', peerStatus: { count: 1, peers: [
    { convId: 'cabc12345', agentId: '', title: 'Peer Conv', statusLabel: 'generating',
      round: 7, currentFile: '', claimedEpic: 'Refactor parser' } ] } }],
};
const pHtml = _renderUnifiedToolLine(peerRound, false);
check('peer_list_class', pHtml.includes('ptool-peer-list'));
check('peer_who', pHtml.includes('Peer Conv'));
check('peer_round', pHtml.includes('round 7'));
check('peer_epic', pHtml.includes('Refactor parser'));
check('peer_not_md_dump', !pHtml.includes('MD-DUMP:RAW PEER PROSE'));

// peer empty state
const peerEmpty = {
  status: 'done', toolName: 'project_peer_status', query: 'project_peer_status',
  toolContent: 'none', toolRounds: [],
  results: [{ source: 'Peer', peerStatus: { count: 0, peers: [] } }],
};
check('peer_empty', _renderUnifiedToolLine(peerEmpty, false).includes('ptool-peer-empty'));

// ── project_charter_propose → proposal card ──
const propRound = {
  status: 'done', toolName: 'project_charter_propose', query: 'project_charter_propose',
  toolContent: 'Proposed.', toolRounds: [],
  results: [{ source: 'Charter', charterProposal: {
    proposal: 'Adopt the lease model', title: 'Lease', pending: true } }],
};
const propHtml = _renderUnifiedToolLine(propRound, false);
check('proposal_class', propHtml.includes('ptool-charter-proposal'));
check('proposal_text', propHtml.includes('Adopt the lease model'));
check('proposal_pending', propHtml.includes('ptool-charter-prop-pending'));

// ── charter_read WITHOUT structured meta → falls back to Markdown dump ──
const readRound = {
  status: 'done', toolName: 'project_charter_read', query: 'project_charter_read',
  toolContent: 'NORTH STAR PROSE', toolRounds: [],
  results: [{ source: 'Charter' }],
};
const readHtml = _renderUnifiedToolLine(readRound, false);
check('read_falls_back_to_md', readHtml.includes('MD-DUMP:NORTH STAR PROSE'));

console.log(out.join('\n'));
"""


def _run(src_path):
    harness = os.path.join(HERE, '_brain_tool_render_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, src_path, ROOT],
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
def test_structured_brain_tool_renderers():
    output = _run(_TR_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'structured brain-tool render failures:\n' + output
    for must in (
        'PASS board_mini_class', 'PASS board_mini_open_epic',
        'PASS board_mini_claimed_epic', 'PASS board_mini_owner',
        'PASS board_mini_auto_badge', 'PASS board_mini_not_md_dump',
        'PASS transition_class', 'PASS transition_title', 'PASS transition_verb',
        'PASS peer_list_class', 'PASS peer_who', 'PASS peer_round',
        'PASS peer_epic', 'PASS peer_not_md_dump', 'PASS peer_empty',
        'PASS proposal_class', 'PASS proposal_text', 'PASS proposal_pending',
        'PASS read_falls_back_to_md',
    ):
        assert must in output, output


def _nc(anchor, replacement, must_fail, must_still_pass):
    """Double-neuter helper: patch a COPY, run, assert the target checks flip to
    FAIL while a control check stays PASS, then assert the shipped file is
    byte-identical (never touched — we only ran a copy)."""
    with open(_TR_SRC, encoding='utf-8') as f:
        original = f.read()
    assert anchor in original, f'NC anchor not found: {anchor[:60]!r}'
    patched = original.replace(anchor, replacement, 1)
    assert patched != original, 'NC replacement was a no-op'
    copy_path = os.path.join(HERE, '_brain_tool_render_nc_copy.js')
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        for m in must_fail:
            assert ('FAIL ' + m) in output, \
                f'NC: expected {m} to FAIL with branch disabled:\n{output}'
        for m in must_still_pass:
            assert ('PASS ' + m) in output, \
                f'NC must be surgical — {m} should still PASS:\n{output}'
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_TR_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped tool_rounds.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_board_snapshot_renderer_is_load_bearing():
    """Disable the boardSnapshot branch → board_read falls back to the MD dump →
    board_mini_class FAILS while the peer card (separate branch) still renders."""
    _nc(
        anchor='  if (meta.boardSnapshot) return _renderBoardSnapshot(meta.boardSnapshot);',
        replacement='  if (false) return _renderBoardSnapshot(meta.boardSnapshot);',
        must_fail=['board_mini_class', 'board_mini_not_md_dump'],
        must_still_pass=['peer_list_class', 'proposal_class'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_peer_status_renderer_is_load_bearing():
    """Disable the peerStatus branch → peer_status falls back to the MD dump →
    peer_list_class FAILS while the board mini-kanban still renders."""
    _nc(
        anchor='  if (meta.peerStatus) return _renderPeerStatus(meta.peerStatus);',
        replacement='  if (false) return _renderPeerStatus(meta.peerStatus);',
        must_fail=['peer_list_class', 'peer_not_md_dump'],
        must_still_pass=['board_mini_class', 'proposal_class'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_charter_proposal_renderer_is_load_bearing():
    """Disable the charterProposal branch → propose falls back to the MD dump →
    proposal_class FAILS while board + peer renderers still work."""
    _nc(
        anchor='  if (meta.charterProposal) return _renderCharterProposal(meta.charterProposal);',
        replacement='  if (false) return _renderCharterProposal(meta.charterProposal);',
        must_fail=['proposal_class', 'proposal_pending'],
        must_still_pass=['board_mini_class', 'peer_list_class'],
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
