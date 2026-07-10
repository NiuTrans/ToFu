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
// The delivery card routes toConv through convTitleById (a real global in the
// bundle). Provide it + a loaded conversation list so the id→title resolution
// path is actually EXERCISED here (previously it was undefined → the card fell
// back to `conv cdef1234`, so the resolution was never tested).
global.conversations = [
  { id: 'cdef1234deadbeef', title: 'Overlap Watch Conv' },
  { id: 'cghi5678cafef00d', title: 'Dup Epic Conv' },
];
global.convTitleById = function (cid) {
  if (!cid) return '';
  let hit = global.conversations.find((c) => c.id === cid);
  if (!hit) {
    const pre = global.conversations.filter((c) => c.id && c.id.indexOf(cid) === 0);
    if (pre.length === 1) hit = pre[0];
  }
  return hit ? hit.title : 'Untitled chat';
};

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

// ── board_read deferred lane renders (parked epics were previously invisible) ──
const deferRound = {
  status: 'done', toolName: 'project_board_read', query: 'project_board_read',
  toolContent: 'RAW', toolRounds: [],
  results: [{ source: 'Board', boardSnapshot: {
    open: 0, claimed: 0, deferred: 1, done: 0, lanes: {
      deferred: [{ id: 'pt_p', title: 'PARKED EPIC Z', owner: '', dispatched: false }],
    } } }],
};
const dHtml = _renderUnifiedToolLine(deferRound, false);
check('deferred_lane_class', dHtml.includes('ptool-board-mini-deferred'));
check('deferred_epic_title', dHtml.includes('PARKED EPIC Z'));

// ── project_feed_read → chronological activity list ──
const feedRound = {
  status: 'done', toolName: 'project_feed_read', query: 'project_feed_read',
  toolContent: 'RAW FEED PROSE', toolRounds: [],
  results: [{ source: 'Peer', feedActivity: { count: 1, events: [
    { kind: 'completed', title: 'Sibling Conv', convId: 'cxyz9999',
      summary: 'Fixed the parser bug', ts: Date.now() - 120000, mine: false } ] } }],
};
const fHtml = _renderUnifiedToolLine(feedRound, false);
check('feed_list_class', fHtml.includes('ptool-feed-list'));
check('feed_who', fHtml.includes('Sibling Conv'));
check('feed_summary', fHtml.includes('Fixed the parser bug'));
// The jsdom `t` stub returns the fallback default (the raw kind), so the
// label appears as 'completed'; the real i18n renders 'Completed'/'完成'.
check('feed_kind', fHtml.includes('ptool-feed-completed') && fHtml.includes('ptool-feed-kind'));
check('feed_not_md_dump', !fHtml.includes('MD-DUMP:RAW FEED PROSE'));

// feed is a conv-meta tool (was missing from _CONV_META_TOOLS → content hidden)
check('feed_is_conv_meta', _isRoundConvMeta({ toolName: 'project_feed_read' }));
check('board_defer_is_conv_meta', _isRoundConvMeta({ toolName: 'project_board_defer' }));

// feed empty state
const feedEmpty = {
  status: 'done', toolName: 'project_feed_read', query: 'project_feed_read',
  toolContent: 'none', toolRounds: [],
  results: [{ source: 'Peer', feedActivity: { count: 0, events: [] } }],
};
check('feed_empty', _renderUnifiedToolLine(feedEmpty, false).includes('ptool-feed-empty'));

// feed event with NO title but a resolvable convId → the row must show the
// TITLE via convTitleById, never a raw `conv <id>` (the reported bug). Uses a
// loaded id (cdef1234deadbeef → 'Overlap Watch Conv').
const feedNoTitle = {
  status: 'done', toolName: 'project_feed_read', query: 'project_feed_read',
  toolContent: 'RAW', toolRounds: [],
  results: [{ source: 'Peer', feedActivity: { count: 1, events: [
    { kind: 'started', title: '', convId: 'cdef1234deadbeef',
      summary: 'The team panel is too ugly', ts: Date.now() - 60000, mine: false } ] } }],
};
const fntHtml = _renderUnifiedToolLine(feedNoTitle, false);
check('feed_notitle_resolves_title', fntHtml.includes('Overlap Watch Conv'));
check('feed_notitle_not_raw_id', !fntHtml.includes('conv cdef1234'));

// ── project_message → delivery card ──
const msgRound = {
  status: 'done', toolName: 'project_message', query: 'project_message',
  toolContent: 'Message delivered to conversation cdef1234 — it will see your note.',
  toolRounds: [],
  results: [{ source: 'Peer', peerDelivery: {
    tool: 'project_message', toConv: 'cdef1234', text: 'Watch out for the overlap',
    hardAbort: false, outcome: 'delivered' } }],
};
const mHtml = _renderUnifiedToolLine(msgRound, false);
check('peermsg_class', mHtml.includes('ptool-peermsg'));
// The target is resolved to its TITLE (not the raw id); the id survives only
// in the title= tooltip. This exercises the real convTitleById path.
check('peermsg_target', mHtml.includes('Overlap Watch Conv'));
check('peermsg_target_id_in_tooltip', mHtml.includes('title="cdef1234"'));
check('peermsg_target_not_raw', !mHtml.includes('conv cdef1234'));
check('peermsg_text', mHtml.includes('Watch out for the overlap'));
check('peermsg_outcome', mHtml.includes('ptool-peermsg-outcome-delivered'));
check('peermsg_not_md_dump', !mHtml.includes('MD-DUMP:'));

// ── project_intervene (hard, denied) → delivery card with denied outcome ──
const intvRound = {
  status: 'done', toolName: 'project_intervene', query: 'project_intervene',
  toolContent: 'Hard abort was DENIED by the user.', toolRounds: [],
  results: [{ source: 'Peer', peerDelivery: {
    tool: 'project_intervene', toConv: 'cghi5678', text: 'stop duplicating epic X',
    hardAbort: true, outcome: 'denied' } }],
};
const iHtml = _renderUnifiedToolLine(intvRound, false);
check('intervene_class', iHtml.includes('ptool-peermsg'));
check('intervene_denied', iHtml.includes('ptool-peermsg-denied') || iHtml.includes('ptool-peermsg-outcome-denied'));

// ── Localized header + "why this ran" caption (the user's core complaint:
//    the raw "Live peer status" header + no explanation of what it means). ──
// The jsdom `t` stub returns the English fallback (2nd arg), so we assert the
// friendly labels replace the raw backend display string (`query`).
check('peer_head_not_raw_query', !pHtml.includes('project_peer_status') || pHtml.includes('who else is working'));
check('peer_head_friendly', pHtml.includes('Checked who else is working now'));
check('peer_why_caption', pHtml.includes('ptool-convmeta-why') && pHtml.includes('running right now'));
check('board_head_friendly', bHtml.includes('Checked the team board'));
check('board_why_caption', bHtml.includes('ptool-convmeta-why') && bHtml.includes('shared to-do board'));
check('feed_why_caption', fHtml.includes('ptool-convmeta-why') && fHtml.includes('timeline'));
check('message_why_caption', mHtml.includes('ptool-convmeta-why') && mHtml.includes('advisory note'));
// board mutation gets the mutate header + caption (not the read one)
check('board_mutate_head', trHtml.includes('Updated the team board'));
check('board_mutate_why', trHtml.includes('shared to-do board'));
// localized peer status token: "generating" → the (fallback) generating label,
// rendered via the localizer not verbatim-only. The peer round used statusLabel
// 'generating'; assert the localized path ran (fallback == same word here, so
// just confirm it appears inside a peer-detail, i.e. the localizer didn't drop it).
check('peer_status_token', pHtml.includes('generating'));

// ── Default-collapse routine coordination READS; keep action cards OPEN. ──
// Rendered markup is `<details class="ptool-convmeta-block"${openAttr} data-rn=`
// so an OPEN card contains `ptool-convmeta-block" open` and a COLLAPSED one
// contains `ptool-convmeta-block" data-rn` (no open before data-rn).
function _isOpen(h) { return h.includes('ptool-convmeta-block" open'); }
function _isCollapsed(h) { return h.includes('ptool-convmeta-block" data-rn') && !_isOpen(h); }
// routine reads → collapsed
check('peer_collapsed', _isCollapsed(pHtml));
check('board_read_collapsed', _isCollapsed(bHtml));
check('feed_collapsed', _isCollapsed(fHtml));
check('charter_read_collapsed', _isCollapsed(readHtml));
// action / decision cards → open
check('board_mutate_open', _isOpen(trHtml));
check('message_open', _isOpen(mHtml));
check('intervene_open', _isOpen(iHtml));
check('proposal_open', _isOpen(propHtml));
// ── At-a-glance count chip on the COLLAPSED read summaries. ──
check('peer_count_chip', pHtml.includes('ptool-convmeta-count') && pHtml.includes('1 active'));
check('board_count_chip', bHtml.includes('ptool-convmeta-count') && bHtml.includes('1 open'));
check('feed_count_chip', fHtml.includes('ptool-convmeta-count') && fHtml.includes('1 events'));
// OPEN cards do NOT get a redundant count chip (body is already visible)
check('open_no_count_chip', !trHtml.includes('ptool-convmeta-count') && !mHtml.includes('ptool-convmeta-count'));

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
        'PASS deferred_lane_class', 'PASS deferred_epic_title',
        'PASS feed_list_class', 'PASS feed_who', 'PASS feed_summary',
        'PASS feed_kind', 'PASS feed_not_md_dump', 'PASS feed_empty',
        'PASS feed_is_conv_meta', 'PASS board_defer_is_conv_meta',
        'PASS feed_notitle_resolves_title', 'PASS feed_notitle_not_raw_id',
        'PASS peermsg_class', 'PASS peermsg_target',
        'PASS peermsg_target_id_in_tooltip', 'PASS peermsg_target_not_raw',
        'PASS peermsg_text',
        'PASS peermsg_outcome', 'PASS peermsg_not_md_dump',
        'PASS intervene_class', 'PASS intervene_denied',
        'PASS peer_head_friendly', 'PASS peer_why_caption',
        'PASS board_head_friendly', 'PASS board_why_caption',
        'PASS feed_why_caption', 'PASS message_why_caption',
        'PASS board_mutate_head', 'PASS board_mutate_why',
        'PASS peer_status_token',
        'PASS peer_collapsed', 'PASS board_read_collapsed',
        'PASS feed_collapsed', 'PASS charter_read_collapsed',
        'PASS board_mutate_open', 'PASS message_open',
        'PASS intervene_open', 'PASS proposal_open',
        'PASS peer_count_chip', 'PASS board_count_chip',
        'PASS feed_count_chip', 'PASS open_no_count_chip',
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


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_feed_activity_renderer_is_load_bearing():
    """Disable the feedActivity branch → project_feed_read falls back to the MD
    dump → feed_list_class FAILS while board + peer renderers still work."""
    _nc(
        anchor='  if (meta.feedActivity) return _renderFeedActivity(meta.feedActivity);',
        replacement='  if (false) return _renderFeedActivity(meta.feedActivity);',
        must_fail=['feed_list_class', 'feed_not_md_dump'],
        must_still_pass=['board_mini_class', 'peer_list_class'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_peer_delivery_renderer_is_load_bearing():
    """Disable the peerDelivery branch → project_message falls back to the MD
    dump → peermsg_class FAILS while board + peer-status renderers still work."""
    _nc(
        anchor='  if (meta.peerDelivery) return _renderPeerDelivery(meta.peerDelivery);',
        replacement='  if (false) return _renderPeerDelivery(meta.peerDelivery);',
        must_fail=['peermsg_class', 'peermsg_not_md_dump'],
        must_still_pass=['board_mini_class', 'peer_list_class'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_delivery_card_title_resolution_is_load_bearing():
    """Neuter the convTitleById branch in _renderPeerDelivery → the target
    reverts to the raw `conv cdef1234` id, so peermsg_target (title) and
    peermsg_target_not_raw FAIL while the card itself still renders."""
    _nc(
        anchor='  const _target = (typeof convTitleById === "function" && pd.toConv)\n    ? convTitleById(pd.toConv)\n    : ("conv " + String(pd.toConv || "").slice(0, 8));',
        replacement='  const _target = ("conv " + String(pd.toConv || "").slice(0, 8));',
        must_fail=['peermsg_target', 'peermsg_target_not_raw'],
        must_still_pass=['peermsg_class', 'peermsg_text', 'peer_list_class'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_feed_read_coverage_in_conv_meta_set():
    """Remove project_feed_read from _CONV_META_TOOLS → it stops routing to the
    structured card (feed_is_conv_meta FAILS) while board_defer stays covered."""
    _nc(
        anchor='  "project_peer_status", "project_feed_read",',
        replacement='  "project_peer_status",',
        must_fail=['feed_is_conv_meta', 'feed_list_class'],
        must_still_pass=['board_defer_is_conv_meta', 'board_mini_class'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_purpose_caption_is_load_bearing():
    """Neuter _convMetaPurpose to always return '' → every conv-meta card loses
    its explanatory caption (peer/board/feed/message why-captions all FAIL)
    while the structured card bodies still render (peer_list_class PASSES)."""
    _nc(
        anchor='  const entry = P[tn];\n  return entry ? _t(entry[0], entry[1]) : "";',
        replacement='  const entry = P[tn];\n  return entry ? "" : "";',
        must_fail=['peer_why_caption', 'board_why_caption',
                   'feed_why_caption', 'message_why_caption'],
        must_still_pass=['peer_list_class', 'board_mini_class',
                         'peer_head_friendly'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_localized_header_is_load_bearing():
    """Neuter _convMetaHeadLabel to always return the raw display string → the
    friendly localized headers FAIL while the structured bodies still render."""
    _nc(
        anchor='  const entry = M[tn];\n  return entry ? _t(entry[0], entry[1]) : raw;',
        replacement='  const entry = M[tn];\n  return raw;',
        must_fail=['peer_head_friendly', 'board_head_friendly'],
        must_still_pass=['peer_why_caption', 'peer_list_class'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_default_collapse_policy_is_load_bearing():
    """Force _convMetaDefaultOpen to always return true (the OLD always-open
    behaviour) → the routine reads stop collapsing (peer/board/feed/charter
    collapsed checks FAIL) while the action cards stay open (still PASS)."""
    _nc(
        anchor='  return !_CONV_META_ROUTINE_READS.has(tn);',
        replacement='  return true; return !_CONV_META_ROUTINE_READS.has(tn);',
        must_fail=['peer_collapsed', 'board_read_collapsed',
                   'feed_collapsed', 'charter_read_collapsed'],
        must_still_pass=['board_mutate_open', 'message_open',
                         'proposal_open', 'peer_list_class'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_summary_count_chip_is_load_bearing():
    """Neuter _convMetaSummaryChip to always return '' → the collapsed reads
    lose their at-a-glance count chip (peer/board/feed count checks FAIL) while
    the cards still collapse (collapsed checks still PASS)."""
    _nc(
        anchor='  if (n == null) return "";\n  return `<span class="ptool-convmeta-count">${escapeHtml(label)}</span>`;',
        replacement='  if (n == null) return "";\n  return "";',
        must_fail=['peer_count_chip', 'board_count_chip', 'feed_count_chip'],
        must_still_pass=['peer_collapsed', 'board_read_collapsed',
                         'open_no_count_chip'],
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
