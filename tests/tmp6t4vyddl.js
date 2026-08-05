
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>'
      + '<div id="streaming-msg" data-msg-id="mLive"><div id="streaming-body"></div></div>'
      + '</body>',
  targets: [process.argv[2], process.argv[4]],
  globals: 
  {
    activeConvId: 'c1',
    conversations: [{ id: 'c1', messages: [
      { role: 'assistant', content: 'x', _msgId: 'mLive' },
    ] }],
    stripNoTranslateTags: (s) => s,
    isNearBottom: () => false,
    scrollToBottom: () => {},
    renderMarkdown: (s) => '<md>' + String(s) + '</md>',
    escapeHtml: (s) => String(s == null ? '' : s),
    t: (k, v) => k + (v && v.n != null ? ('|n=' + v.n) : ''),
    _stampFreshness: () => {},
    _buildSwarmInboxChipsHTML: () => '',
    renderTurnProvenanceHtml: () => '',
    renderMcpLoginHintHtml: () => '',
    renderPreferenceLearnedHtml: () => '',
    _fcFingerprint: () => 0,
    _extractFileChangesFromRoundsAsync: async () => [],
    _renderFileChangesHtml: () => '',
    _isRoundSwarm: () => false,
    _buildSwarmPanelHTML: () => '',
    _renderStreamRoundProse: () => {},
    _renderUnifiedToolLine: (r) => '<div class="ptool-line">' + (r.toolName || '') + '</div>',
    _renderTurnHead: () => '<div class="ptool-turn-head"></div>',
    _renderSoloRoundTag: (rno) => '<div class="ptool-turn-rno-solo">' + rno + '</div>',
    _turnLabelText: () => 'parallel',
    getToolRoundsFromMsg: (m) => (m && m.toolRounds) || [],
    _toolPanelHeaderLabel: () => 'HDR',
  }
,
});

const body = document.getElementById('streaming-body');
_ensureStreamZones(body);
const toolZone = body.querySelector('[data-zone="tool"]');

// ── Phase 1: LIVE — the husk arrives as 'searching' (tool_start), before
//    reconcile downgrades it. It legitimately renders as an in-flight slot. ──
const liveHusk = { roundNum: 2, toolCallId: 'tcHusk', toolName: 'grep_search',
                   status: 'searching', llmRound: 1 };
_syncToolRoundsDOM(toolZone, [{ roundNum: 1, toolCallId: 'tcPlain', toolName: 'run_command', status: 'done', llmRound: 0, toolContent: 'out', results: [{ badge: 'done', fetched: true, fetchedChars: 3 }] }, liveHusk]);
const pbody = toolZone.querySelector('.ptool-panel-body');
check('phase1_husk_slot_present_while_searching',
  !!pbody.querySelector('[data-prn="2"]'));

// ── Phase 2: reconcile settles the round — same roundNum, now a superseded
//    husk (badge stamped, status 'aborted'), and its recovered TWIN arrives. ──
_syncToolRoundsDOM(toolZone, [{ roundNum: 1, toolCallId: 'tcPlain', toolName: 'run_command', status: 'done', llmRound: 0, toolContent: 'out', results: [{ badge: 'done', fetched: true, fetchedChars: 3 }] }, { roundNum: 2, toolCallId: 'tcHusk', toolName: 'grep_search', status: 'aborted', llmRound: 1, toolContent: null, results: [{ badge: 'superseded', interrupted: true, toolName: 'grep_search', fetched: false, fetchedChars: 0, snippet: 'Superseded — resend adopted.' }] }, { roundNum: 3, toolCallId: 'tcTwin', toolName: 'grep_search', status: 'done', llmRound: 1, toolContent: 'REAL RESULT BYTES', results: [{ badge: '', fetched: true, fetchedChars: 17, snippet: 'ok' }] }]);

// ★ THE FIX: the husk's stale slot is PRUNED; the twin + plain remain.
check('husk_slot_pruned', !pbody.querySelector('[data-prn="2"]'));
check('twin_slot_kept', !!pbody.querySelector('[data-prn="3"]'));
check('plain_slot_kept', !!pbody.querySelector('[data-prn="1"]'));

// ── Header counts REAL rounds only (husk excluded). Two real rounds → "2". ──
const hdr = toolZone.querySelector('.ptool-panel-label');
check('header_excludes_husk', !!hdr && /n=2\b/.test(hdr.textContent) && !/n=3\b/.test(hdr.textContent));

// ── A coalesced re-sync must keep the husk gone (idempotent). ──
_syncToolRoundsDOM(toolZone, [{ roundNum: 1, toolCallId: 'tcPlain', toolName: 'run_command', status: 'done', llmRound: 0, toolContent: 'out', results: [{ badge: 'done', fetched: true, fetchedChars: 3 }] }, { roundNum: 2, toolCallId: 'tcHusk', toolName: 'grep_search', status: 'aborted', llmRound: 1, toolContent: null, results: [{ badge: 'superseded', interrupted: true, toolName: 'grep_search', fetched: false, fetchedChars: 0, snippet: 'Superseded — resend adopted.' }] }, { roundNum: 3, toolCallId: 'tcTwin', toolName: 'grep_search', status: 'done', llmRound: 1, toolContent: 'REAL RESULT BYTES', results: [{ badge: '', fetched: true, fetchedChars: 17, snippet: 'ok' }] }]);
check('husk_stays_pruned_on_resync', !pbody.querySelector('[data-prn="2"]'));
check('twin_stays_on_resync', !!pbody.querySelector('[data-prn="3"]'));

report();
