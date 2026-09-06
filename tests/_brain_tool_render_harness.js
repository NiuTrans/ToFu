
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

// argv[2] = JSON list of source paths (core first, then the deferred rich
// module — bundle order). Concatenated into ONE eval: in the browser both
// files share the global (lexical) scope — tool_rounds_rich.js reads core's
// top-level consts (_CONV_META_TOOLS) and functions at call time — and a
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

// feed is a conv-meta tool (was missing from _CONV_META_TOOLS → content hidden)
check('feed_is_conv_meta', _isRoundConvMeta({ toolName: 'project_feed_read' }));

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
check('commit_is_conv_meta', _isRoundConvMeta({ toolName: 'project_commit' }));
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

// ── get_conversation → structured conversation-digest card ──
// The ugly case: get_conversation used to have NO structured renderer, so its
// raw ═══ / ── User Message # transcript fell through to the Markdown dump.
const digestRound = {
  status: 'done', toolName: 'get_conversation', query: 'get_conversation: mrne7eq0',
  toolContent: '═'.repeat(60) + '\nReferenced Conversation: "Prefix cache bug"\nRAW TRANSCRIPT PROSE',
  toolRounds: [],
  results: [{ source: 'Conversations', convDigest: {
    convId: 'mrne7eq0msc9fu', title: 'Prefix cache bug', preset: 'aws.claude-opus-4.8',
    msgCount: 1, truncated: false, messages: [
      { index: 1, role: 'user', text: 'Continue troubleshooting the prefix cache failure',
        images: 1 },
      { index: 2, role: 'assistant', text: 'Let me read cache.py',
        tools: ['read_files', 'grep_search'] },
    ] } }],
};
const dHtml = _renderUnifiedToolLine(digestRound, false);
check('digest_class', dHtml.includes('ptool-convdigest'));
check('digest_preset', dHtml.includes('aws.claude-opus-4.8') && dHtml.includes('ptool-convdigest-preset'));
check('digest_user_text', dHtml.includes('Continue troubleshooting the prefix cache failure'));
check('digest_assistant_text', dHtml.includes('Let me read cache.py'));
check('digest_role_chip', dHtml.includes('ptool-convdigest-role') && dHtml.includes('ptool-convdigest-user'));
check('digest_tools_hint', dHtml.includes('ptool-convdigest-tools') && dHtml.includes('read_files'));
check('digest_image_hint', dHtml.includes('ptool-convdigest-att') && dHtml.includes('1 image'));
// the raw ═══ transcript prose must NOT be dumped as Markdown
check('digest_not_md_dump', !dHtml.includes('MD-DUMP:'));
// ★ point-3: the card REPLACES the raw body — the verbatim transcript prose
//   carried on toolContent must NOT appear anywhere in the rendered output.
check('digest_replaces_raw_body', !dHtml.includes('RAW TRANSCRIPT PROSE'));
// ★ raw-mode (get_conversation raw=true): the backend now attaches convDigest
//   even though toolContent is the big "═══ Raw Conversation Record" + JSON
//   dump. The card must render and the raw JSON dump must be REPLACED (this is
//   exactly the reported screenshot: raw JSON blob instead of a card).
const digestRawWithCard = {
  status: 'done', toolName: 'get_conversation', query: 'get_conversation: rawcid',
  toolContent: '═'.repeat(60) + '\nRaw Conversation Record: "Big raw dump"\n'
    + '```json\n{"id":"rawcid","messages":[...]}\n``` RAW-JSON-BLOB-MARKER',
  toolRounds: [],
  results: [{ source: 'Conversations', convDigest: {
    convId: 'rawcid', title: 'Big raw dump', preset: 'sonnet',
    msgCount: 2, truncated: false, messages: [
      { index: 1, role: 'user', text: 'raw mode question' },
      { index: 2, role: 'assistant', text: 'raw mode answer' },
    ] } }],
};
const rawHtml = _renderUnifiedToolLine(digestRawWithCard, false);
check('rawmode_card_rendered', rawHtml.includes('ptool-convdigest') && rawHtml.includes('raw mode answer'));
check('rawmode_raw_json_replaced', !rawHtml.includes('RAW-JSON-BLOB-MARKER') && !rawHtml.includes('Raw Conversation Record'));
check('digest_is_conv_meta', _isRoundConvMeta({ toolName: 'get_conversation' }));
// get_conversation is the PRIMARY viewing product → default EXPANDED (not a
// collapsed routine read). The message count lives in the digest meta row.
function _isOpenD(h) { return h.includes('ptool-convmeta-block" open'); }
check('digest_open', _isOpenD(dHtml));
check('digest_count_in_meta', dHtml.includes('ptool-convdigest-msgcount') && dHtml.includes('1 messages'));
check('digest_why_caption', dHtml.includes('ptool-convmeta-why') && dHtml.includes('full transcript'));
check('digest_head_friendly', dHtml.includes('Opened a past conversation'));

// get_conversation WITHOUT structured meta (e.g. raw-mode dump) → Markdown fallback
const digestRaw = {
  status: 'done', toolName: 'get_conversation', query: 'get_conversation',
  toolContent: 'RAW JSON DUMP PROSE', toolRounds: [],
  results: [{ source: 'Conversations' }],
};
check('digest_raw_falls_back', _renderUnifiedToolLine(digestRaw, false).includes('MD-DUMP:RAW JSON DUMP PROSE'));

console.log(out.join('\n'));
// tool_rounds_rich.js installs a 1Hz countdown setInterval
// (window._timerCountdownTicker) that keeps node's event loop alive → the
// subprocess would hang until the pytest timeout. Clear it and exit
// explicitly. (Documented harness trap.)
try { if (global.window && global.window._timerCountdownTicker) clearInterval(global.window._timerCountdownTicker); } catch (_e) {}
process.exit(0);
