"""Live-streaming path drops superseded-orphan tool rounds (conv mrx3tv0ha8ffkc).

WHY (the reported bug — "superseded/interrupted still shows, not fixed")
------------------------------------------------------------------------
A FloorRetry / stream-retry duplicate whose tc_id never survived into the final
assistant_msg is stamped ``badge='superseded'`` (result-less, status downgraded
to 'aborted') by the backend ``reconcile_announced_rounds``. Its adopted /
recovered twin is the real call. The SETTLED / reload render paths
(``renderToolRoundsHTML`` / ``renderSegmentTimelineHTML`` in tool_rounds.js)
already DROP these husks via ``_isSupersededOrphanRound``.

But the LIVE streaming DOM sync — ``_syncToolRoundsDOM`` in streaming_ui.js —
rendered straight from ``msg.toolRounds`` and NEVER ran that filter, so the
husk showed a misleading "interrupted"/"superseded" chip for the whole rest of
the turn, only vanishing on a full page reload. That is the exact residual the
owner saw in conv mrx3tv0ha8ffkc (which persists tool data with
task_results.tool_rounds NULL, rendering via segments/toolRounds; its message[1]
carries 5 superseded husks and message[3] carries 11).

Two things are needed in the live path (a render-list filter alone is not
enough): the husk's ``[data-prn]`` slot was already CREATED as 'searching' on
``tool_start``, THEN downgraded by reconcile — so the fix must also PRUNE that
stale slot. This test drives the REAL shipped ``_syncToolRoundsDOM`` under
jsdom through exactly that lifecycle. Each assertion is paired with a NEUTER
proving the drop is load-bearing. Skips cleanly when node/jsdom absent.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit


# Shared globals the streaming_ui.js targets expect. _isSupersededOrphanRound
# lives in tool_rounds.js; the harness loads it as an extra target so the REAL
# predicate (not a stub) gates the drop.
_GLOBALS = r"""
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
"""

# A superseded husk exactly as reconcile_announced_rounds stamps it: null
# toolContent, results[0].badge='superseded', interrupted, status 'aborted'.
_HUSK = (
    "{ roundNum: 2, toolCallId: 'tcHusk', toolName: 'grep_search', "
    "status: 'aborted', llmRound: 1, toolContent: null, "
    "results: [{ badge: 'superseded', interrupted: true, toolName: 'grep_search', "
    "fetched: false, fetchedChars: 0, snippet: 'Superseded — resend adopted.' }] }"
)
# Its recovered twin: the real call that survived, same llmRound batch.
_TWIN = (
    "{ roundNum: 3, toolCallId: 'tcTwin', toolName: 'grep_search', "
    "status: 'done', llmRound: 1, toolContent: 'REAL RESULT BYTES', "
    "results: [{ badge: '', fetched: true, fetchedChars: 17, snippet: 'ok' }] }"
)
# A plain done round in a different batch.
_PLAIN = (
    "{ roundNum: 1, toolCallId: 'tcPlain', toolName: 'run_command', "
    "status: 'done', llmRound: 0, toolContent: 'out', "
    "results: [{ badge: 'done', fetched: true, fetchedChars: 3 }] }"
)


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>'
      + '<div id="streaming-msg" data-msg-id="mLive"><div id="streaming-body"></div></div>'
      + '</body>',
  targets: [process.argv[2], process.argv[4]],
  globals: __GLOBALS__,
});

const body = document.getElementById('streaming-body');
_ensureStreamZones(body);
const toolZone = body.querySelector('[data-zone="tool"]');

// ── Phase 1: LIVE — the husk arrives as 'searching' (tool_start), before
//    reconcile downgrades it. It legitimately renders as an in-flight slot. ──
const liveHusk = { roundNum: 2, toolCallId: 'tcHusk', toolName: 'grep_search',
                   status: 'searching', llmRound: 1 };
_syncToolRoundsDOM(toolZone, [__PLAIN__, liveHusk]);
const pbody = toolZone.querySelector('.ptool-panel-body');
check('phase1_husk_slot_present_while_searching',
  !!pbody.querySelector('[data-prn="2"]'));

// ── Phase 2: reconcile settles the round — same roundNum, now a superseded
//    husk (badge stamped, status 'aborted'), and its recovered TWIN arrives. ──
_syncToolRoundsDOM(toolZone, [__PLAIN__, __HUSK__, __TWIN__]);

// ★ THE FIX: the husk's stale slot is PRUNED; the twin + plain remain.
check('husk_slot_pruned', !pbody.querySelector('[data-prn="2"]'));
check('twin_slot_kept', !!pbody.querySelector('[data-prn="3"]'));
check('plain_slot_kept', !!pbody.querySelector('[data-prn="1"]'));

// ── Header counts REAL rounds only (husk excluded). Two real rounds → "2". ──
const hdr = toolZone.querySelector('.ptool-panel-label');
check('header_excludes_husk', !!hdr && /n=2\b/.test(hdr.textContent) && !/n=3\b/.test(hdr.textContent));

// ── A coalesced re-sync must keep the husk gone (idempotent). ──
_syncToolRoundsDOM(toolZone, [__PLAIN__, __HUSK__, __TWIN__]);
check('husk_stays_pruned_on_resync', !pbody.querySelector('[data-prn="2"]'));
check('twin_stays_on_resync', !!pbody.querySelector('[data-prn="3"]'));

report();
""".replace('__GLOBALS__', _GLOBALS).replace('__HUSK__', _HUSK) \
   .replace('__TWIN__', _TWIN).replace('__PLAIN__', _PLAIN)


def test_live_sync_drops_superseded_orphan_slot():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),
        body_js=_BODY,
        extra_targets=[os.path.join(JS_DIR, 'ui', 'tool_rounds.js')],
        min_pass=7,
        label='live-superseded-drop',
    )


# ═══════════════════════════════════════════════════════════════════════════
#  NEUTER — prove the live-path drop is load-bearing.
#
#  Override _isSupersededOrphanRound to ALWAYS return false (the pre-fix world,
#  where the live sync never filtered). The husk's slot then survives Phase 2
#  and renders its misleading chip — the exact reported regression.
# ═══════════════════════════════════════════════════════════════════════════
_BODY_NC = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>'
      + '<div id="streaming-msg" data-msg-id="mLive"><div id="streaming-body"></div></div>'
      + '</body>',
  targets: [process.argv[2], process.argv[4]],
  globals: __GLOBALS__,
});

// NEUTER: force the shared predicate off (simulate the pre-fix live path).
_isSupersededOrphanRound = function(){ return false; };
if (typeof globalThis !== 'undefined') globalThis._isSupersededOrphanRound = _isSupersededOrphanRound;

const body = document.getElementById('streaming-body');
_ensureStreamZones(body);
const toolZone = body.querySelector('[data-zone="tool"]');

const liveHusk = { roundNum: 2, toolCallId: 'tcHusk', toolName: 'grep_search',
                   status: 'searching', llmRound: 1 };
_syncToolRoundsDOM(toolZone, [__PLAIN__, liveHusk]);
_syncToolRoundsDOM(toolZone, [__PLAIN__, __HUSK__, __TWIN__]);

const pbody = toolZone.querySelector('.ptool-panel-body');
// With the neuter, the husk slot SURVIVES — the regression the fix removes.
check('NC_husk_slot_survives_without_filter', !!pbody.querySelector('[data-prn="2"]'));
// And the header over-counts (3 rounds incl. husk).
const hdr = toolZone.querySelector('.ptool-panel-label');
check('NC_header_overcounts', !!hdr && /n=3\b/.test(hdr.textContent));

report();
""".replace('__GLOBALS__', _GLOBALS).replace('__HUSK__', _HUSK) \
   .replace('__TWIN__', _TWIN).replace('__PLAIN__', _PLAIN)


def test_neuter_without_filter_husk_slot_survives():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),
        body_js=_BODY_NC,
        extra_targets=[os.path.join(JS_DIR, 'ui', 'tool_rounds.js')],
        min_pass=2,
        label='live-superseded-drop-nc',
    )


def test_source_live_sync_calls_superseded_predicate():
    """Source guard: _syncToolRoundsDOM must reference _isSupersededOrphanRound.
    If a refactor drops the call, the live streaming path silently regresses to
    rendering superseded husks (the reported bug) — so pin the reference."""
    su_path = os.path.join(JS_DIR, 'ui', 'streaming_ui.js')
    src = open(su_path, encoding='utf-8').read()
    start = src.index('function _syncToolRoundsDOM(')
    # Bound the scan to the function body (up to the next top-level function).
    nxt = src.find('\nfunction ', start + 1)
    body = src[start:nxt if nxt != -1 else len(src)]
    assert '_isSupersededOrphanRound' in body, \
        ('_syncToolRoundsDOM no longer filters superseded orphans — the live '
         'streaming path will render misleading interrupted/superseded chips '
         '(conv mrx3tv0ha8ffkc regression).')
