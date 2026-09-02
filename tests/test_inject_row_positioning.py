#!/usr/bin/env python3
"""tests/test_inject_row_positioning.py — mid-turn inject rows anchor to the
round they were consumed at, in EVERY render path (never tail-dumped, never
dropped).

THE bug this pins (owner report + verified diagnosis)
-----------------------------------------------------
A human "steer" (and its siblings — peer message, async <swarm-update>) sent
WHILE a turn is generating renders as a synthetic display-only toolRound
("你中途插入了 N 条消息"). Historically that row was ALWAYS appended to the tail
of ``toolRounds`` (``roundNum: 9000000+len``) and its recorded
``steerRound``/``peerRound``/``inboxRound`` was NEVER used to position it. So:

  * LIVE stream  → the chip sank to the absolute bottom of the tool panel
    (the screenshot symptom).
  * SETTLED seg-timeline → the chip VANISHED entirely: the backend deliberately
    excludes synthetic rows from ``msg.segments``
    (``_assemble.py`` ``is_synthetic_inbox_round`` guard), and
    ``renderSegmentTimelineHTML`` builds batches purely from segments +
    resolves tool bodies by ``toolCallId`` (synthetic rows have none) → the
    chip is in NO batch → dropped.

The fix (all three legs share ONE anchor rule):
  anchor llmRound = injectRound - 1  (backend emits round=round_num+1, real
  tool rounds carry llmRound=round_num — so injectRound-1 == the batch that
  consumed the injected message). The chip renders at the TOP of that round's
  output (before its thinking / narration / tools), in live AND settled.

Legs
----
  1. ARRAY SPLICE (node): ``_spliceInjectRow`` inserts the row immediately
     before the first REAL round with the anchor llmRound; no anchor → tail.
  2. REHYDRATE (node): the real ``getToolRoundsFromMsg`` + ``_rehydrateInjectRows``
     place a reloaded steer sidecar BEFORE its anchor real round (not at tail).
  3. LIVE DOM REPOSITION (jsdom): ``_repositionInjectGroups`` moves the synthetic
     group above the anchor round's prose siblings; NEUTER = skip it → tail.
  4. SETTLED TIMELINE (node): ``renderSegmentTimelineHTML`` renders the chip and
     places it before the anchor batch; NEUTER = drop the extraction → chip gone.
  5. WIRE NEUTRALITY (python): a synthetic row spliced to the FRONT/MIDDLE of
     toolRounds still reconstructs byte-identical to the real-only baseline
     (splicing does not perturb the wire — the row is filtered by marker, not
     position, and never carries llmRound onto the wire anyway).

Run::
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_inject_row_positioning.py -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path, runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CORE_JS = runtime_section_path('core.js')
TR_JS = runtime_section_path('ui/tool_rounds.js')
VIEW_MODEL_TS = os.path.join(
    ROOT, 'frontend', 'src', 'conversation', 'presentation',
    'conversation-view-model.ts')

_HAS_NODE = shutil.which('node') is not None
_HAS_JSDOM = os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


def _run_node(harness: str, env_extra: dict) -> str:
    env = dict(os.environ, **env_extra)
    proc = subprocess.run(['node', '-e', harness], capture_output=True,
                          text=True, timeout=30, env=env)
    assert proc.returncode == 0, f'node failed: {proc.stderr}\nSTDOUT:{proc.stdout}'
    return proc.stdout.strip()


# ═══════════════════ Leg 1 + 2: array splice + rehydrate ════════════════════

def _extract_core_inject_fns() -> str:
    """Pull getToolRoundsFromMsg + _spliceInjectRow + _rehydrateInjectRows out of
    core.js (all three sit contiguously, ending before the window export)."""
    src = open(CORE_JS, encoding='utf-8').read()
    start = src.index('function getToolRoundsFromMsg(')
    end = src.index('if (typeof window !== "undefined") {\n  runtimeScope._rehydrateInjectRows')
    chunk = src[start:end]
    assert '_spliceInjectRow' in chunk, 'extraction missed _spliceInjectRow'
    assert '_rehydrateInjectRows' in chunk, 'extraction missed _rehydrateInjectRows'
    return chunk


_L1_HARNESS = r"""
const fnSrc = process.env.FN_SRC;
eval(fnSrc);
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Leg 1: _spliceInjectRow places the row before the anchor real round ──
{
  const real = [
    { roundNum: 1, llmRound: 0, toolCallId: 'tc_1', status: 'done' },
    { roundNum: 2, llmRound: 1, toolCallId: 'tc_2', status: 'done' },
  ];
  const row = { roundNum: 9000001, _userSteerInject: true, steerRound: 1 };
  _spliceInjectRow(real, row, 0);   // anchor llmRound 0 (= steerRound 1 - 1)
  const at = real.indexOf(row);
  const anchorAt = real.findIndex(r => r.llmRound === 0 && !r._userSteerInject);
  check('splice_before_anchor', at >= 0 && at === anchorAt - 1);
  check('splice_not_tail', at !== real.length - 1);
}
// No anchor present → tail fallback.
{
  const real = [{ roundNum: 1, llmRound: 5, toolCallId: 'tc_1', status: 'done' }];
  const row = { roundNum: 9000001, _userSteerInject: true, steerRound: 1 };
  _spliceInjectRow(real, row, 0);
  check('splice_tail_when_no_anchor', real[real.length - 1] === row);
}

// ── Leg 2: rehydrate positions the reloaded sidecar before its anchor ──
{
  const reloaded = {
    role: 'assistant', content: 'answer',
    toolRounds: [
      { roundNum: 1, llmRound: 0, toolCallId: 'tc_1', toolName: 'web_search',
        toolArgs: '{}', toolContent: 'r0', status: 'done' },
      { roundNum: 2, llmRound: 1, toolCallId: 'tc_2', toolName: 'read_files',
        toolArgs: '{}', toolContent: 'r1', status: 'done' },
    ],
    // steer consumed at round 1 (0-based llmRound 0) → anchor llmRound 0.
    _userSteerInjects: [{ round: 1, count: 1, previews: [{ text: 'focus X' }] }],
  };
  const rows = getToolRoundsFromMsg(reloaded);
  const steerAt = rows.findIndex(r => r._userSteerInject);
  const anchorAt = rows.findIndex(r => r.llmRound === 0 && !r._userSteerInject);
  check('rehydrate_steer_present', steerAt >= 0);
  check('rehydrate_steer_before_anchor', steerAt >= 0 && steerAt === anchorAt - 1);
  check('rehydrate_steer_not_tail', steerAt !== rows.length - 1);
  // Source array untouched (display-only copy discipline).
  check('rehydrate_source_not_mutated', reloaded.toolRounds.length === 2);
}
console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _HAS_NODE, reason='node not installed')
def test_splice_and_rehydrate_position_before_anchor():
    out = _run_node(_L1_HARNESS, {'FN_SRC': _extract_core_inject_fns()})
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'splice/rehydrate positioning failures:\n' + out
    assert out.count('PASS') >= 7, f'expected >=7 PASS, got:\n{out}'


# ═══════════════════ Leg 3: native live projection ordering ══════════════════

_NATIVE_INJECTION_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3], targets: [process.argv[2]],
});
const turn = {
  turnId: 'turn-1', conversationId: 'conv-1', laneId: 'main', ordinal: 1,
  actor: 'assistant', kind: 'reply', runId: '', status: 'completed',
  currentAttemptId: null, projectionRevision: 1, settlement: {},
  createdAt: 1, updatedAt: 1,
  projection: {
    segments: [
      { type: 'thinking', blockId: 'thinking:0', text: 'think', llmRound: 0 },
      { type: 'tool_use', blockId: 'tool:a', id: 'a', name: 'web_search',
        input: {}, result: {}, llmRound: 0 },
      { type: 'tool_use', blockId: 'tool:b', id: 'b', name: 'read_files',
        input: {}, result: {}, llmRound: 1 },
    ],
    toolRounds: [
      { toolCallId: 'a', llmRound: 0 }, { toolCallId: 'b', llmRound: 1 },
    ],
    _userSteerInjects: [{ blockId: 'inject:steer:1', round: 1,
      count: 1, previews: [{ text: 'focus X' }] }],
  },
};
const blocks = global.selectTurnBlocks(turn);
const injectAt = blocks.findIndex(block => block.kind === 'injections');
const anchorAt = blocks.findIndex(block => block.source?.llmRound === 0);
check('native_inject_present', injectAt >= 0);
check('native_inject_before_anchor', injectAt === anchorAt - 1);
check('native_inject_not_tail', injectAt !== blocks.length - 1);
report();
"""


def test_native_live_projection_places_chip_before_anchor():
    target = native_module_path(
        'conversation-view-model.js', VIEW_MODEL_TS)
    run_harness(
        target_js=target,
        body_js=_NATIVE_INJECTION_HARNESS,
        expect_pass=3,
        label='native-injection-order',
    )


# ═══════════════════ Leg 4: settled seg-timeline (node) ══════════════════════

def _extract_timeline_fns() -> str:
    src = open(TR_JS, encoding='utf-8').read()
    project_start = src.index('function _projectTodoRoundsForDisplay(')
    project_end = src.index('\nfunction ', project_start + 1)
    project = src[project_start:project_end]
    start = src.index('function _roundsByToolCallId(')
    end = src.index('\nfunction _renderUnifiedGroup(')
    chunk = project + '\n' + src[start:end]
    assert 'renderSegmentTimelineHTML' in chunk
    return chunk


_TIMELINE_STUBS = r"""
function escapeHtml(s){ return String(s == null ? '' : s); }
function renderMarkdown(s){ return '<md>' + String(s) + '</md>'; }
function t(k){ return k; }
function Icon(name, size){ return '<ICON name=' + name + ' size=' + size + '>'; }
function getToolRoundsFromMsg(m){ return (m && m.toolRounds) || []; }
function _toolPanelHeaderLabel(rounds, active){ return 'HDR[' + (rounds||[]).length + ']'; }
function _renderToolGroupsHTML(rounds, allRounds){
  return (rounds || []).map(function(r){ return '<TOOL name=' + (r.toolName||'') + '>'; }).join('');
}
function stripNoTranslateTags(text){ return text; }
// renderSegmentTimelineHTML now calls the shared _isSupersededOrphanRound
// predicate (to drop FloorRetry/stream-retry superseded orphans). This suite's
// rounds are all real (status:'done') or synthetic inject rows — none is a
// superseded orphan — so a faithful mirror of the real predicate returns false
// for every one here, leaving inject-positioning behaviour unchanged.
function _isSupersededOrphanRound(r){
  if (!r || r.status !== 'aborted') return false;
  const meta = (r.results && r.results[0]) || {};
  const hasRealResult = r.toolContent != null || (meta && (meta.fetched || (meta.fetchedChars | 0) > 0));
  return meta.badge === 'superseded' && !hasRealResult;
}
// _renderToolSlot renders a synthetic inject row's chip in the timeline. Stub it
// to an identifiable marker so we can assert PRESENCE + ORDER.
function _renderToolSlot(r, ctx){
  if (r && r._userSteerInject) return '<INJECT kind=steer round=' + r.steerRound + '>';
  if (r && r._peerInject) return '<INJECT kind=peer round=' + r.peerRound + '>';
  if (r && r._inboxInject) return '<INJECT kind=inbox round=' + r.inboxRound + '>';
  return '<SLOT name=' + (r.toolName||'') + '>';
}
"""

_L4_HARNESS = _TIMELINE_STUBS + r"""
eval(process.env.FN_SRC);
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Two real batches (L0 grep, L1 apply_diff) + a steer inject consumed at round 1
// (anchor llmRound 0). allRounds (what getToolRoundsFromMsg returns) carries the
// synthetic row; msg.segments (backend SoT) does NOT — the exact drop scenario.
const msg = {
  toolRounds: [
    { roundNum: 9000001, _userSteerInject: true, steerRound: 1 },
    { roundNum: 1, llmRound: 0, toolCallId: 'a1', toolName: 'grep_search', status: 'done' },
    { roundNum: 2, llmRound: 1, toolCallId: 'b1', toolName: 'apply_diff', status: 'done' },
  ],
};
const segments = [
  { type: 'thinking', text: 'think0', deliverable: false, llmRound: 0 },
  { type: 'text', text: 'narr0', deliverable: false, llmRound: 0 },
  { type: 'tool_use', id: 'a1', name: 'grep_search', llmRound: 0 },
  { type: 'text', text: 'narr1', deliverable: false, llmRound: 1 },
  { type: 'tool_use', id: 'b1', name: 'apply_diff', llmRound: 1 },
  { type: 'text', text: 'ANSWER', deliverable: true, terminal: true },
];
const html = renderSegmentTimelineHTML(segments, msg, 0);
function idx(n){ return html.indexOf(n); }

check('timeline_rendered', !!html);
check('chip_present', idx('<INJECT kind=steer round=1>') >= 0);
// The chip must sit BEFORE round-0's tools (grep) — i.e. at the top of round 0.
check('chip_before_anchor_tool', idx('<INJECT kind=steer round=1>') >= 0 &&
      idx('<INJECT kind=steer round=1>') < idx('<TOOL name=grep_search>'));
// And before round-0's own thinking/narration (top of the round). Per-batch
// thinking renders the raw text inside .thinking-text (escapeHtml, NOT
// renderMarkdown), so match on the bare 'think0' token.
check('chip_before_anchor_prose', idx('<INJECT kind=steer round=1>') >= 0 &&
      idx('think0') >= 0 && idx('<INJECT kind=steer round=1>') < idx('think0'));
// Deliverable still excluded.
check('deliverable_excluded', idx('ANSWER') === -1);
// Header count must EXCLUDE the synthetic row (2 real tools, not 3).
check('header_counts_real_only', idx('HDR[2]') >= 0);

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _HAS_NODE, reason='node not installed')
def test_settled_timeline_renders_chip_at_anchor():
    out = _run_node(_L4_HARNESS, {'FN_SRC': _extract_timeline_fns()})
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'settled timeline failures:\n' + out
    assert out.count('PASS') >= 6, f'expected >=6 PASS, got:\n{out}'


_L4_NC_HARNESS = _TIMELINE_STUBS + r"""
// NEUTER: strip the inject-extraction block from renderSegmentTimelineHTML so it
// reverts to the pre-fix behaviour (chip in no batch → dropped). Proves the
// extraction/prepend is load-bearing for settled visibility.
let src = process.env.FN_SRC;
const neutered = src.replace(/const _injByAnchor = new Map\(\);[\s\S]*?\/\* END_INJECT_EXTRACTION \*\//,
                             'const _injByAnchor = new Map(); const _supersededTcIds = new Set(); const realRounds = allRounds;');
if (neutered === src) { console.log('FAIL nc_pattern_matched'); process.exit(0); }
eval(neutered);
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const msg = { toolRounds: [
  { roundNum: 9000001, _userSteerInject: true, steerRound: 1 },
  { roundNum: 1, llmRound: 0, toolCallId: 'a1', toolName: 'grep_search', status: 'done' },
]};
const segments = [
  { type: 'tool_use', id: 'a1', name: 'grep_search', llmRound: 0 },
  { type: 'text', text: 'ANSWER', deliverable: true, terminal: true },
];
const html = renderSegmentTimelineHTML(segments, msg, 0);
check('nc_chip_absent_without_extraction', html.indexOf('<INJECT') === -1);
console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _HAS_NODE, reason='node not installed')
def test_NC_settled_timeline_drops_chip_without_extraction():
    out = _run_node(_L4_NC_HARNESS, {'FN_SRC': _extract_timeline_fns()})
    assert 'PASS nc_chip_absent_without_extraction' in out, (
        'NEUTER control failed — extraction not load-bearing, or the sentinel '
        'marker /* END_INJECT_EXTRACTION */ drifted:\n' + out)


# ═══════════════════ Leg 5: wire neutrality (python) ═════════════════════════

def _real_rounds() -> list[dict]:
    return [
        {'roundNum': 1, 'llmRound': 0, 'toolCallId': 'tc_1', 'toolName': 'web_search',
         'toolArgs': '{"q":"x"}', 'toolContent': 'A', 'status': 'done'},
        {'roundNum': 2, 'llmRound': 1, 'toolCallId': 'tc_2', 'toolName': 'read_files',
         'toolArgs': '{"path":"a"}', 'toolContent': 'B', 'status': 'done'},
    ]


def test_front_spliced_synthetic_row_is_wire_neutral():
    """A synthetic row spliced to the FRONT/MIDDLE (the new anchored position)
    still reconstructs byte-identical to the real-only baseline — the wire
    filter keys on the marker flag, not the row's position, and the synthetic
    row never carries llmRound onto the wire."""
    from lib.tasks_pkg.conv_message_builder._toolcalls import (
        _reconstruct_tool_call_messages,
    )
    baseline = _reconstruct_tool_call_messages(_real_rounds())
    assert baseline is not None

    front = _real_rounds()
    front.insert(0, {'roundNum': 9000001, 'status': 'done', '_userSteerInject': True,
                     '_steerKey': 'steer:1', 'steerRound': 1, 'steerCount': 1,
                     'steerPreviews': [{'text': 'focus X'}]})
    front_wire = _reconstruct_tool_call_messages(front)

    assert json.dumps(front_wire, sort_keys=True) == json.dumps(baseline, sort_keys=True), (
        'a front-spliced synthetic row perturbed the wire')



# ═══ Leg 6: synthetic-row roundNum is STABLE across streaming passes ══════
# Incident anchor: conv mt2x5y77kk19qc (2026-08-21). One intent-stall nudge
# rendered as THREE identical chips in the live tool panel: the rehydrated
# row's roundNum was `9000000 + out.length`, so it drifted (9000001 → 9000003
# → 9000004) as real rounds streamed in; `_syncToolRoundsDOM` keys groups
# (`S{roundNum}`) and slots (`data-prn`) by roundNum and never collects stale
# synthetic groups → one chip per drifted key. Fix: lane-base + injectRound.

_L6_HARNESS = r"""
const fnSrc = process.env.FN_SRC;
eval(fnSrc);
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function realRound(n){ return { roundNum: n, llmRound: n - 1, toolCallId: 'tc_' + n,
  toolName: 'grep_search', toolArgs: '{}', toolContent: 'r' + n, status: 'done' }; }

// The SAME stall record, rehydrated against a growing real-round base — this
// is exactly what successive live render passes see mid-turn.
const stall = [{ round: 3, tool: 'run_command', failedRound: 1, badge: 'exit 6' }];
const rowA = getToolRoundsFromMsg({ role: 'assistant', toolRounds: [realRound(1)], _stallNudges: stall })
  .find(r => r._stallNudge);
const rowB = getToolRoundsFromMsg({ role: 'assistant',
  toolRounds: [realRound(1), realRound(2), realRound(3), realRound(4)], _stallNudges: stall })
  .find(r => r._stallNudge);
check('stall_chip_present_both_passes', !!rowA && !!rowB);
check('stall_roundnum_stable_across_growth', !!rowA && !!rowB && rowA.roundNum === rowB.roundNum);
check('stall_roundnum_above_real_range', !!rowA && rowA.roundNum >= 9000000);

// The steer / peer / inbox lanes share the same stable-key scheme.
const sidecars = {
  _userSteerInjects: [{ round: 2, count: 1 }],
  _peerInjects: [{ round: 2, count: 1 }],
  _inboxInjects: [{ round: 2, count: 1 }],
};
const keyOf = (rows) => {
  const o = {};
  for (const r of rows) {
    if (r._userSteerInject) o.steer = r.roundNum;
    if (r._peerInject) o.peer = r.roundNum;
    if (r._inboxInject) o.inbox = r.roundNum;
  }
  return o;
};
const small = keyOf(getToolRoundsFromMsg({ role: 'assistant', toolRounds: [realRound(1)], ...sidecars }));
const grown = keyOf(getToolRoundsFromMsg({ role: 'assistant',
  toolRounds: [realRound(1), realRound(2), realRound(3)], ...sidecars }));
check('steer_peer_inbox_roundnum_stable',
  small.steer === grown.steer && small.peer === grown.peer && small.inbox === grown.inbox);
check('lane_roundnums_distinct',
  small.steer !== small.peer && small.peer !== small.inbox && small.inbox !== small.steer);
console.log(out.join('\n'));
"""

_L6_NC_HARNESS = r"""
// NEUTER: restore the old length-derived formula for the stall lane and prove
// the stability assertion above actually bites (roundNums DRIFT under it).
let src = process.env.FN_SRC;
const neutered = src.replace('roundNum: _INJECT_ROUND_BASE.stall + rnd,',
                             'roundNum: 9000000 + out.length,');
if (neutered === src) { console.log('FAIL nc_pattern_matched'); process.exit(0); }
eval(neutered);
const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function realRound(n){ return { roundNum: n, llmRound: n - 1, toolCallId: 'tc_' + n,
  toolName: 'grep_search', toolArgs: '{}', toolContent: 'r' + n, status: 'done' }; }
const stall = [{ round: 3, tool: 'run_command', failedRound: 1, badge: 'exit 6' }];
const rowA = getToolRoundsFromMsg({ role: 'assistant', toolRounds: [realRound(1)], _stallNudges: stall })
  .find(r => r._stallNudge);
const rowB = getToolRoundsFromMsg({ role: 'assistant',
  toolRounds: [realRound(1), realRound(2), realRound(3), realRound(4)], _stallNudges: stall })
  .find(r => r._stallNudge);
check('nc_length_derived_roundnum_drifts', !!rowA && !!rowB && rowA.roundNum !== rowB.roundNum);
console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _HAS_NODE, reason='node not installed')
def test_inject_row_roundnum_stable_across_streaming_passes():
    out = _run_node(_L6_HARNESS, {'FN_SRC': _extract_core_inject_fns()})
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'roundNum stability failures:\n' + out
    assert out.count('PASS') >= 5, f'expected >=5 PASS, got:\n{out}'


@pytest.mark.skipif(not _HAS_NODE, reason='node not installed')
def test_NC_length_derived_roundnum_drifts():
    out = _run_node(_L6_NC_HARNESS, {'FN_SRC': _extract_core_inject_fns()})
    assert 'PASS nc_length_derived_roundnum_drifts' in out, (
        'NEUTER control failed — stability assertion not load-bearing, or the '
        '_INJECT_ROUND_BASE.stall pattern drifted:\n' + out)


def test_native_projection_owns_all_injection_lanes_and_anchor_rule():
    """The declarative surface consumes stable wire block ids directly; the
    deleted live-DOM lane-number shims must not be its identity authority."""
    src = open(VIEW_MODEL_TS, encoding='utf-8').read()
    for declaration in (
        "['inbox', projection._inboxInjects]",
        "['peer', projection._peerInjects]",
        "['user-steer', projection._userSteerInjects]",
        "['stall-nudge', projection._stallNudges]",
    ):
        assert declaration in src
    assert 'const anchorLlmRound = round == null ? null : round - 1;' in src
    assert 'blocks.splice(anchorIndex >= 0 ? anchorIndex : blocks.length, 0, block);' in src


if __name__ == '__main__':
    test_front_spliced_synthetic_row_is_wire_neutral()
    if _HAS_NODE:
        print(_run_node(_L1_HARNESS, {'FN_SRC': _extract_core_inject_fns()}))
        print(_run_node(_L4_HARNESS, {'FN_SRC': _extract_timeline_fns()}))
        print(_run_node(_L4_NC_HARNESS, {'FN_SRC': _extract_timeline_fns()}))
    print('DONE')
