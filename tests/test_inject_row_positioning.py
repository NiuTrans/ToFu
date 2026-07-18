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
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CORE_JS = os.path.join(ROOT, 'static', 'js', 'core.js')
TR_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'tool_rounds.js')
SUI_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'streaming_ui.js')

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
    end = src.index('if (typeof window !== "undefined") {\n  window._rehydrateInjectRows')
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


# ═══════════════════ Leg 3: live DOM reposition (jsdom) ══════════════════════

def _extract_reposition_fn() -> str:
    """Pull _repositionInjectGroups out of streaming_ui.js (DOM+CSS only, evals
    standalone under jsdom)."""
    src = open(SUI_JS, encoding='utf-8').read()
    start = src.index('function _repositionInjectGroups(')
    # End at the next top-level function definition after it.
    end = src.index('\nfunction ', start + 1)
    chunk = src[start:end]
    assert '_repositionInjectGroups' in chunk
    return chunk


_L3_HARNESS = r"""
const path = require('path');
const ROOT = process.env.ROOT;
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="body"></div></body>', { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document; global.CSS = dom.window.CSS;
eval(process.env.FN_SRC);

const out = [];
function check(name, cond){ out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function build() {
  // Mimic the live DOM AFTER the main _syncToolRoundsDOM loop: real groups L0,
  // L1 (L0 preceded by its prose siblings), and the synthetic S-group appended
  // to the TAIL (the incremental-append bug).
  const body = document.getElementById('body');
  body.innerHTML = '';
  const mk = (cls, key, txt) => { const d = document.createElement('div'); d.className = cls; if (key != null) d.setAttribute('data-llm-round', key); if (cls.indexOf('seg-') === 0 && key) d.setAttribute('data-seg-round', key); d.textContent = txt || ''; body.appendChild(d); return d; };
  mk('seg-thinking', null, 'think0'); body.lastChild.setAttribute('data-seg-round', 'L0');
  mk('seg-narration', null, 'narr0'); body.lastChild.setAttribute('data-seg-round', 'L0');
  const L0 = mk('ptool-turn', 'L0', 'TOOLS0');
  const L1 = mk('ptool-turn', 'L1', 'TOOLS1');
  const S  = mk('ptool-turn', 'S9000001', 'CHIP');   // appended to tail
  return { body, L0, L1, S };
}

// Positioned: the synthetic group moves ABOVE the anchor round's earliest prose.
{
  const { body, L0, S } = build();
  const toolRounds = [
    { roundNum: 1, llmRound: 0, toolCallId: 'tc_1', status: 'done' },
    { roundNum: 2, llmRound: 1, toolCallId: 'tc_2', status: 'done' },
    { roundNum: 9000001, _userSteerInject: true, steerRound: 1 },  // anchor L0
  ];
  _repositionInjectGroups(body, toolRounds);
  const kids = Array.from(body.children);
  const iChip = kids.indexOf(S);
  // The chip must now sit ABOVE both L0's prose blocks and the L0 group.
  const firstL0Prose = kids.findIndex(k => k.getAttribute('data-seg-round') === 'L0');
  const iL0 = kids.indexOf(L0);
  check('live_chip_above_anchor_prose', iChip >= 0 && iChip < firstL0Prose);
  check('live_chip_above_anchor_group', iChip < iL0);
  check('live_chip_not_tail', iChip !== kids.length - 1);
}
// NEUTER control: skip the reposition → the synthetic group stays at tail.
{
  const { body, S } = build();
  // (do NOT call _repositionInjectGroups)
  const kids = Array.from(body.children);
  check('NC_chip_stays_tail_without_reposition', kids.indexOf(S) === kids.length - 1);
}
console.log(out.join('\n'));
"""


@pytest.mark.skipif(not (_HAS_NODE and _HAS_JSDOM),
                    reason='node + jsdom not installed')
def test_live_reposition_moves_chip_above_anchor():
    out = _run_node(_L3_HARNESS, {'ROOT': ROOT, 'FN_SRC': _extract_reposition_fn()})
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'live reposition failures:\n' + out
    assert 'PASS NC_chip_stays_tail_without_reposition' in out, (
        'NEUTER control missing — reposition not proven load-bearing:\n' + out)


# ═══════════════════ Leg 4: settled seg-timeline (node) ══════════════════════

def _extract_timeline_fns() -> str:
    src = open(TR_JS, encoding='utf-8').read()
    start = src.index('function _roundsByToolCallId(')
    end = src.index('\nfunction _renderUnifiedGroup(')
    chunk = src[start:end]
    assert 'renderSegmentTimelineHTML' in chunk
    return chunk


_TIMELINE_STUBS = r"""
function escapeHtml(s){ return String(s == null ? '' : s); }
function renderMarkdown(s){ return '<md>' + String(s) + '</md>'; }
function t(k){ return k; }
function getToolRoundsFromMsg(m){ return (m && m.toolRounds) || []; }
function _toolPanelHeaderLabel(rounds, active){ return 'HDR[' + (rounds||[]).length + ']'; }
function _renderToolGroupsHTML(rounds, allRounds){
  return (rounds || []).map(function(r){ return '<TOOL name=' + (r.toolName||'') + '>'; }).join('');
}
function stripNoTranslateTags(text){ return text; }
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
                             'const _injByAnchor = new Map(); const realRounds = allRounds;');
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


if __name__ == '__main__':
    test_front_spliced_synthetic_row_is_wire_neutral()
    if _HAS_NODE:
        print(_run_node(_L1_HARNESS, {'FN_SRC': _extract_core_inject_fns()}))
        print(_run_node(_L4_HARNESS, {'FN_SRC': _extract_timeline_fns()}))
        print(_run_node(_L4_NC_HARNESS, {'FN_SRC': _extract_timeline_fns()}))
        if _HAS_JSDOM:
            print(_run_node(_L3_HARNESS, {'ROOT': ROOT, 'FN_SRC': _extract_reposition_fn()}))
    print('DONE')
