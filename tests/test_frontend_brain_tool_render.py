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

Loads the real retained ``ui/tool_rounds.js`` + ``ui/tool_rounds_rich.js``
under jsdom in manifest order, mirroring their shared lexical-scope contract,
and calls the REAL ``_renderUnifiedToolLine`` (the same entry the transcript
uses), so a broken route / missing branch fails here. The structured conv-meta
renderers (``_renderBoardSnapshot`` / ``_renderPeerDelivery`` / …) moved to
``tool_rounds_rich.js`` in the 2026-08-01 Epic-E split (fcddc420) while
typed tool-family/icon presentation and the dispatch stay in the core graph —
each renderer ships a double-neuter NC that patches a COPY of
WHICHEVER file holds the anchor, asserts the structured markup is GONE (falls
back to the prose dump), and restores byte-identical.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
from _runtime_sections import runtime_section_path  # noqa: E402

_TR_SRC = runtime_section_path('ui/tool_rounds.js')
# The conv-meta structured renderers live in the adjacent rich section since
# the 2026-08-01 Epic-E split (fcddc420); core keeps the dispatch + the
# typed family predicate + the glyph map. The suite exercises BOTH so a drift
# in either file is caught.
_TR_RICH_SRC = runtime_section_path('ui/tool_rounds_rich.js')
_TR_ALL_SRCS = [_TR_SRC, _TR_RICH_SRC]


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

// argv[2] = JSON list of source paths (core first, then the adjacent rich
// section — manifest order). Concatenated into one eval: in the browser both
// files share the global (lexical) scope — tool_rounds_rich.js reads core's
// top-level functions and typed predicates at call time — and a
// single eval reproduces that shared scope exactly (per-file evals would trap
// core's const declarations in a discarded lexical environment).
eval(JSON.parse(process.argv[2])
  .map((p) => fs.readFileSync(p, 'utf8'))
  .join('\n;\n'));

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

// ── project_board_post → transition card MUST show the posted epic title +
//    id chip + open status (the reported "shows nothing" bug). ──
const postRound = {
  status: 'done', toolName: 'project_board_post', query: 'project_board_post',
  toolContent: 'Posted epic pt_abc123def456 to the board.', toolRounds: [],
  results: [{ source: 'Board', boardTransition: {
    verb: 'post', taskId: 'pt_abc123def456',
    title: 'Redesign the release dashboard', status: 'open' } }],
};
const postHtml = _renderUnifiedToolLine(postRound, false);
check('post_transition_class', postHtml.includes('ptool-board-transition'));
check('post_transition_title', postHtml.includes('Redesign the release dashboard'));
check('post_transition_id_chip', postHtml.includes('ptool-board-tr-id') && postHtml.includes('pt_abc123def456'));
check('post_transition_verb', postHtml.includes('posted') || postHtml.includes('post'));
check('post_transition_open_status', postHtml.includes('ptool-board-mini-open'));
check('post_transition_head_friendly', postHtml.includes('Updated the team board'));

// ── A transition with an EMPTY title must degrade to a labelled placeholder,
//    NOT render a bare verb badge with nothing after it (defensive fallback
//    for when the backend couldn't resolve a title). ──
const untitledRound = {
  status: 'done', toolName: 'project_board_post', query: 'project_board_post',
  toolContent: 'Posted epic pt_deadbeef00 to the board.', toolRounds: [],
  results: [{ source: 'Board', boardTransition: {
    verb: 'post', taskId: 'pt_deadbeef00', title: '', status: 'open' } }],
};
const untitledHtml = _renderUnifiedToolLine(untitledRound, false);
check('untitled_placeholder', untitledHtml.includes('ptool-board-tr-untitled'));
check('untitled_still_has_id', untitledHtml.includes('pt_deadbeef00'));

// ── A FAILED board mutation MUST render a visible failed card (the reported
//    bug: a failed release showed a normal green card, failure only in the raw
//    model text). ok:false + error → failed badge + error row + no status. ──
const failedRound = {
  status: 'done', toolName: 'project_board_post', query: 'project_board_post',
  toolContent: 'Error posting epic: board full: 200 active epics.', toolRounds: [],
  results: [{ source: 'Board', boardTransition: {
    verb: 'post', taskId: '', title: 'Redesign the release dashboard',
    status: '', ok: false, error: 'board full: 200 active epics' } }],
};
const failedHtml = _renderUnifiedToolLine(failedRound, false);
check('failed_transition_class', failedHtml.includes('ptool-board-transition-failed'));
check('failed_transition_badge', failedHtml.includes('ptool-board-tr-failed'));
check('failed_transition_error', failedHtml.includes('ptool-board-tr-error') && failedHtml.includes('board full: 200 active epics'));
check('failed_transition_title', failedHtml.includes('Redesign the release dashboard'));
// a failed mutation must NOT render a status chip (no guessed 'open')
check('failed_transition_no_status', !failedHtml.includes('ptool-board-tr-status'));
// a SUCCESSFUL transition (ok!==false) keeps the normal status chip, no fail markup
check('ok_transition_no_fail', !trHtml.includes('ptool-board-transition-failed') && !trHtml.includes('ptool-board-tr-failed'));

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

// NOTE: the board has DELIBERATELY no deferred/parked lane (the shelving
// mechanism was removed — see lib/conversations/project_board.py:452 "there is
// deliberately NO parked/deferred state"). The prior `deferred_lane_class` /
// `deferred_epic_title` / `board_defer_is_conv_meta` assertions tested a
// removed feature and were retired.

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

// feed is classified as a conversation-metadata tool (otherwise content hides)
check('feed_is_conv_meta', isConversationMetadataToolRound({ toolName: 'project_feed_read' }));

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

// ── project_commit → commit result card (committed) ──
const commitRound = {
  status: 'done', toolName: 'project_commit', query: 'project_commit',
  toolContent: 'RAW COMMIT PROSE', toolRounds: [],
  results: [{ source: 'Board', commitResult: {
    mode: 'commit', ok: true, verified: true, commitSha: 'abc123def456',
    committed: ['lib/foo.py', 'static/bar.js'], clean: ['lib/foo.py', 'static/bar.js'],
    excluded: [{ path: 'shared.py', reason: 'foreign hunks present', numstat: '+3/-1' }],
  } }],
};
const cHtml = _renderUnifiedToolLine(commitRound, false);
check('commit_class', cHtml.includes('ptool-commit'));
check('commit_outcome_committed', cHtml.includes('ptool-commit-outcome-committed'));
check('commit_sha', cHtml.includes('abc123def456'));
check('commit_file', cHtml.includes('lib/foo.py') && cHtml.includes('static/bar.js'));
check('commit_held_file', cHtml.includes('shared.py'));
check('commit_held_reason', cHtml.includes('foreign hunks present'));
check('commit_held_numstat', cHtml.includes('+3/-1'));
check('commit_not_md_dump', !cHtml.includes('MD-DUMP:RAW COMMIT PROSE'));
check('commit_is_conv_meta', isConversationMetadataToolRound({ toolName: 'project_commit' }));
check('commit_head_friendly', cHtml.includes('Committed this conversation'));
check('commit_why_caption', cHtml.includes('ptool-convmeta-why') && cHtml.includes('provably authored'));
check('commit_src_git', cHtml.includes('ptool-convmeta-src') && cHtml.includes('Git'));
// icon must be the git-commit glyph (center circle on a line), NOT the generic wrench
check('commit_icon_gitcommit', cHtml.includes('<line x1="3" y1="12" x2="9" y2="12"/>'));
check('commit_icon_not_wrench', !cHtml.includes('M14.7 6.3a1 1 0 0 0 0 1.4'));
// action card ⇒ open by default
check('commit_open', _isOpen(cHtml));

// ── project_commit plan (dry-run) → would-commit + plan-only outcome ──
const commitPlan = {
  status: 'done', toolName: 'project_commit', query: 'project_commit',
  toolContent: 'RAW', toolRounds: [],
  results: [{ source: 'Board', commitResult: {
    mode: 'plan', ok: true, clean: ['lib/baz.py'], committed: [], excluded: [] } }],
};
const cpHtml = _renderUnifiedToolLine(commitPlan, false);
check('commit_plan_outcome', cpHtml.includes('ptool-commit-outcome-planned'));
check('commit_plan_would', cpHtml.includes('lib/baz.py'));

// ── project_commit failure (nothing clean) → failed outcome + error ──
const commitFail = {
  status: 'done', toolName: 'project_commit', query: 'project_commit',
  toolContent: 'RAW', toolRounds: [],
  results: [{ source: 'Board', commitResult: {
    mode: 'commit', ok: false, error: 'nothing clean to commit',
    clean: [], committed: [], excluded: [] } }],
};
const cfHtml = _renderUnifiedToolLine(commitFail, false);
check('commit_fail_outcome', cfHtml.includes('ptool-commit-outcome-failed'));
check('commit_fail_error', cfHtml.includes('nothing clean to commit'));

// ── get_conversation → the settled result, never a second-read digest ──
// Historical snapshots can carry a convDigest built by a later DB read. Give
// that metadata a deliberately conflicting body and prove it cannot replace
// either call's V2 result.
const unrelatedDigest = { convId: 'same-conversation', title: 'STALE DIGEST',
  msgCount: 32, messages: [{ index: 1, role: 'user', text: 'UNRELATED_DIGEST_BODY' }] };
function conversationRound(roundNum, marker, artifactRef) {
  return {
    roundNum, status: 'done', toolName: 'get_conversation',
    query: 'get_conversation: same-conversation', toolRounds: [],
    toolContent: JSON.stringify({ contractVersion: 'tofu.tool-result/v2',
      artifactRef, status: 'partial', summary: marker }),
    results: [{ source: 'Conversations', convDigest: unrelatedDigest }],
  };
}
const firstConversationHtml = _renderUnifiedToolLine(
  conversationRound(41, 'BACKEND_PAGE_ONE', 'tool-result:first'), false,
);
const secondConversationHtml = _renderUnifiedToolLine(
  conversationRound(42, 'BACKEND_PAGE_TWO', 'tool-result:second'), false,
);
check('conversation_result_uses_generic_authority',
  firstConversationHtml.includes('ptool-result-block')
  && firstConversationHtml.includes('data-tool-result-authority="toolContent"'));
check('conversation_result_is_single_disclosure',
  (firstConversationHtml.match(/<details\b/g) || []).length === 1
  && !firstConversationHtml.includes('conversation-tool__authoritative-result'));
check('conversation_result_ignores_conflicting_digest',
  firstConversationHtml.includes('BACKEND_PAGE_ONE')
  && firstConversationHtml.includes('tool-result:first')
  && !firstConversationHtml.includes('UNRELATED_DIGEST_BODY')
  && !firstConversationHtml.includes('ptool-convdigest'));
check('conversation_calls_keep_their_own_results',
  !firstConversationHtml.includes('BACKEND_PAGE_TWO')
  && secondConversationHtml.includes('BACKEND_PAGE_TWO')
  && secondConversationHtml.includes('tool-result:second')
  && !secondConversationHtml.includes('BACKEND_PAGE_ONE'));
check('conversation_result_stays_collapsed',
  !firstConversationHtml.includes('ptool-result-block" open'));
check('conversation_tool_family_is_unchanged',
  isConversationMetadataToolRound({ toolName: 'get_conversation' }));
const emptyConversationHtml = _renderUnifiedToolLine({
  roundNum: 43, status: 'done', toolName: 'get_conversation',
  query: 'get_conversation: empty1',
  toolContent: 'Conversation "Empty conv" [empty1] exists but has no messages.',
  results: [{ source: 'Conversations', convDigest: unrelatedDigest }],
}, false);
check('empty_conversation_uses_backend_result',
  emptyConversationHtml.includes('exists but has no messages')
  && !emptyConversationHtml.includes('UNRELATED_DIGEST_BODY'));

console.log(out.join('\n'));
process.exit(0);
"""


def _run(src_paths):
    # Full unit gates execute this module in several worker processes. A fixed
    # repository-local filename lets them overwrite one another between the
    # write and Node startup, yielding impossible mixtures of assertions.
    with tempfile.TemporaryDirectory(prefix='tofu-brain-tool-render-') as tmp:
        harness = os.path.join(tmp, 'harness.js')
        with open(harness, 'w', encoding='utf-8') as f:
            f.write(_HARNESS)
        proc = subprocess.run(
            ['node', harness, json.dumps(list(src_paths)), ROOT],
            capture_output=True, text=True, timeout=60)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_structured_brain_tool_renderers():
    output = _run(_TR_ALL_SRCS)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'structured brain-tool render failures:\n' + output
    for must in (
        'PASS board_mini_class', 'PASS board_mini_open_epic',
        'PASS board_mini_claimed_epic', 'PASS board_mini_owner',
        'PASS board_mini_auto_badge', 'PASS board_mini_not_md_dump',
        'PASS transition_class', 'PASS transition_title', 'PASS transition_verb',
        'PASS post_transition_class', 'PASS post_transition_title',
        'PASS post_transition_id_chip', 'PASS post_transition_verb',
        'PASS post_transition_open_status', 'PASS post_transition_head_friendly',
        'PASS untitled_placeholder', 'PASS untitled_still_has_id',
        'PASS failed_transition_class', 'PASS failed_transition_badge',
        'PASS failed_transition_error', 'PASS failed_transition_title',
        'PASS failed_transition_no_status', 'PASS ok_transition_no_fail',
        'PASS peer_list_class', 'PASS peer_who', 'PASS peer_round',
        'PASS peer_epic', 'PASS peer_not_md_dump', 'PASS peer_empty',
        'PASS proposal_class', 'PASS proposal_text', 'PASS proposal_pending',
        'PASS read_falls_back_to_md',
        'PASS feed_list_class', 'PASS feed_who', 'PASS feed_summary',
        'PASS feed_kind', 'PASS feed_not_md_dump', 'PASS feed_empty',
        'PASS feed_is_conv_meta',
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
        'PASS commit_class', 'PASS commit_outcome_committed',
        'PASS commit_sha', 'PASS commit_file', 'PASS commit_held_file',
        'PASS commit_held_reason', 'PASS commit_held_numstat',
        'PASS commit_not_md_dump', 'PASS commit_is_conv_meta',
        'PASS commit_head_friendly', 'PASS commit_why_caption',
        'PASS commit_src_git', 'PASS commit_icon_gitcommit',
        'PASS commit_icon_not_wrench', 'PASS commit_open',
        'PASS commit_plan_outcome', 'PASS commit_plan_would',
        'PASS commit_fail_outcome', 'PASS commit_fail_error',
        'PASS conversation_result_uses_generic_authority',
        'PASS conversation_result_is_single_disclosure',
        'PASS conversation_result_ignores_conflicting_digest',
        'PASS conversation_calls_keep_their_own_results',
        'PASS conversation_result_stays_collapsed',
        'PASS conversation_tool_family_is_unchanged',
        'PASS empty_conversation_uses_backend_result',
    ):
        assert must in output, output


def _nc(anchor, replacement, must_fail, must_still_pass):
    """Double-neuter helper: find which shipped file holds the anchor (core
    tool_rounds.js vs the adjacent tool_rounds_rich.js — NC anchors span both
    since the 2026-08-01 split), patch a COPY, run BOTH files with the copy
    substituted, assert the target checks flip to FAIL while a control check
    stays PASS, then assert BOTH shipped files are byte-identical (never
    touched — we only ran a copy)."""
    originals = {}
    owner = None
    for src in _TR_ALL_SRCS:
        with open(src, encoding='utf-8') as f:
            originals[src] = f.read()
        if anchor in originals[src]:
            assert owner is None, f'NC anchor present in BOTH files: {anchor[:60]!r}'
            owner = src
    assert owner is not None, f'NC anchor not found in either file: {anchor[:60]!r}'
    patched = originals[owner].replace(anchor, replacement, 1)
    assert patched != originals[owner], 'NC replacement was a no-op'
    # NC cases can patch different owners concurrently. Give each one a
    # private copy so it cannot execute another worker's mutation.
    with tempfile.TemporaryDirectory(prefix='tofu-brain-tool-render-nc-') as tmp:
        copy_path = os.path.join(tmp, 'owner-copy.js')
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        run_list = [copy_path if s == owner else s for s in _TR_ALL_SRCS]
        output = _run(run_list)
        for m in must_fail:
            assert ('FAIL ' + m) in output, \
                f'NC: expected {m} to FAIL with branch disabled:\n{output}'
        for m in must_still_pass:
            assert ('PASS ' + m) in output, \
                f'NC must be surgical — {m} should still PASS:\n{output}'
    for src, text in originals.items():
        with open(src, encoding='utf-8') as f:
            assert f.read() == text, f'shipped {os.path.basename(src)} must be byte-identical'


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
def test_NC_failed_transition_branch_is_load_bearing():
    """Force `failed` to always be false in _renderBoardTransition → a failed
    mutation reverts to the OLD look (no failed badge / error row) → the failed
    checks FAIL while a normal transition + board mini still render."""
    _nc(
        anchor='  const failed = tr.ok === false;',
        replacement='  const failed = false;',
        must_fail=['failed_transition_class', 'failed_transition_badge',
                   'failed_transition_error'],
        must_still_pass=['transition_class', 'board_mini_class',
                         'ok_transition_no_fail'],
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
def test_NC_commit_result_renderer_is_load_bearing():
    """Disable the commitResult branch → project_commit falls back to the MD
    dump → commit_class FAILS while board + peer renderers still work."""
    _nc(
        anchor='  if (meta.commitResult) return _renderCommitResult(meta.commitResult);',
        replacement='  if (false) return _renderCommitResult(meta.commitResult);',
        must_fail=['commit_class', 'commit_not_md_dump'],
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
