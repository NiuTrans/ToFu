#!/usr/bin/env python3
"""RENDER_CONTRACT Phase 3 — GOLDEN parity: cold projection == live projection.

TESTS-FIRST: RED on HEAD by design (the reducer does not exist yet).

The acceptance anchor for Phase 3. Today FIVE independent assemblers hand-mutate
the same in-memory shape (assistantMsg.content/.thinking/.toolRounds) with
different write disciplines (LIVE `+=` append, COLD verbatim `=` +
_snapshotLongerRounds, POLL keep-longer, DONE verbatim, VU render). So the SAME
logical turn can project differently depending on which path delivered it —
tool-round jitter + cold-reopen twinning.

Phase 3 introduces ONE pure reducer, ``static/js/ui/stream_reducer.js``:

    projectStreamEvents(events)        -> {content, thinking, toolRounds}   // LIVE/WARM fold
    projectColdSnapshot(snapshot)      -> {content, thinking, toolRounds}   // COLD state event
    reduceStreamState(state, event)    -> newState                          // the primitive
    locateRound(state, event)          -> round|null                        // the ONE index normalizer

This test builds ONE logical turn as (a) the ordered LIVE event stream and
(b) the COLD ``state`` snapshot the server would emit for that same settled
turn, projects BOTH through the reducer, and asserts the two results are
BYTE-IDENTICAL (JSON.stringify equal). RED now — the module is absent, so the
harness reports ``reducer_missing`` and the test fails. GREEN once the reducer
lands and both paths fold through it (docs/RENDER_CONTRACT_PHASE3_PLAN.md §7
steps 2–3).

Extraction-and-eval in node, mirroring tests/test_frontend_conn_transient_reconnect.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
REDUCER_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'stream_reducer.js')


def _run_node(body: str) -> dict:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')
    with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False) as f:
        f.write(body)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=25)
        # A syntax/absence failure is itself a RED signal, surfaced cleanly.
        if out.returncode != 0:
            return {'ok': False, 'reason': 'node_error', 'stderr': out.stderr[-800:]}
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(tmp)


# Enriched golden fixtures (owner condition 5). Each is a logical turn expressed
# as (a) the ordered LIVE event stream and (b) the COLD `state` snapshot the
# server emits for that SAME settled turn. The reducer must project both to a
# byte-identical {content,thinking,toolRounds}.
_FIXTURE = r'''
// ── F1: single tool round (start→result→done) with prose on both sides ──
const F1_LIVE = [
  { type: 'delta', content: 'Let me check' },
  { type: 'delta', thinking: 'thinking a bit' },
  { type: 'tool_start', roundNum: 1, toolName: 'run_command', toolCallId: 'tc1', query: 'ls' },
  { type: 'tool_result', roundNum: 1, toolCallId: 'tc1', results: [{toolName:'run_command',title:'ok',snippet:'a.py'}] },
  { type: 'tool_done', roundNum: 1, toolCallId: 'tc1', content: 'a.py\nb.py' },
  { type: 'delta', content: ' the logs.' },
];
const F1_COLD = {
  content: 'Let me check the logs.',
  thinking: 'thinking a bit',
  toolRounds: [
    { roundNum: 1, toolName: 'run_command', toolCallId: 'tc1', query: 'ls',
      status: 'done', results: [{toolName:'run_command',title:'ok',snippet:'a.py'}],
      toolContent: 'a.py\nb.py' },
  ],
};

// ── F2: MULTI-ROUND turn (2 tool rounds) + a delta_reset in between ──
// Round 1 streams narration prose, then issues a tool call → delta_reset drops
// the live prose and stamps it as assistantContent on the round's first entry.
// Round 2 does the same. Final prose is the real answer.
const F2_LIVE = [
  { type: 'delta', content: 'First I list files.' },
  { type: 'tool_start', roundNum: 1, llmRound: 0, toolName: 'run_command', toolCallId: 'tc1', query: 'ls' },
  { type: 'delta_reset', roundNum: 0 },
  { type: 'tool_result', roundNum: 1, toolCallId: 'tc1', results: [{toolName:'run_command',title:'ok',snippet:'a.py'}] },
  { type: 'tool_done', roundNum: 1, toolCallId: 'tc1', content: 'a.py' },
  { type: 'delta', content: 'Now I grep.' },
  { type: 'tool_start', roundNum: 2, llmRound: 1, toolName: 'grep', toolCallId: 'tc2', query: 'foo' },
  { type: 'delta_reset', roundNum: 1 },
  { type: 'tool_result', roundNum: 2, toolCallId: 'tc2', results: [{toolName:'grep',title:'hit',snippet:'foo:3'}] },
  { type: 'tool_done', roundNum: 2, toolCallId: 'tc2', content: 'foo:3' },
  { type: 'delta', content: 'Done — found it.' },
];
const F2_COLD = {
  content: 'Done — found it.',
  thinking: '',
  toolRounds: [
    { roundNum: 1, llmRound: 0, toolName: 'run_command', toolCallId: 'tc1', query: 'ls',
      status: 'done', results: [{toolName:'run_command',title:'ok',snippet:'a.py'}],
      toolContent: 'a.py', assistantContent: 'First I list files.' },
    { roundNum: 2, llmRound: 1, toolName: 'grep', toolCallId: 'tc2', query: 'foo',
      status: 'done', results: [{toolName:'grep',title:'hit',snippet:'foo:3'}],
      toolContent: 'foo:3', assistantContent: 'Now I grep.' },
  ],
};

// ── F3: MID-ROUND COLD reconnect — the _snapshotLongerRounds case ──
// LIVE already showed 2 rounds. A cold `state` snapshot arrives sourced from the
// 5s checkpoint that only captured 1 round (SHORTER). The keep-longer routing
// (owned by the pipeline, NOT the reducer) must feed the reducer the merged
// (longer) set; here we assert the reducer, given the SAME complete round set on
// both sides, projects identically — and separately (F3_SHORT) that a shorter
// cold snapshot, once merged to the longer set, equals the live projection.
const F3_LIVE = [
  { type: 'tool_start', roundNum: 1, llmRound: 0, toolName: 'run_command', toolCallId: 'tc1', query: 'ls' },
  { type: 'tool_done', roundNum: 1, toolCallId: 'tc1', content: 'a.py' },
  { type: 'tool_start', roundNum: 2, llmRound: 1, toolName: 'grep', toolCallId: 'tc2', query: 'foo' },
  { type: 'tool_done', roundNum: 2, toolCallId: 'tc2', content: 'foo:3' },
  { type: 'delta', content: 'ok' },
];
// The SHORT cold snapshot (checkpoint lagged, only round 1). This is what the
// pipeline receives; the keep-longer merge must widen it back to the live set
// BEFORE it reaches persistence — modeled here by mergeColdRounds().
const F3_COLD_SHORT = {
  content: 'ok', thinking: '',
  toolRounds: [
    { roundNum: 1, llmRound: 0, toolName: 'run_command', toolCallId: 'tc1', query: 'ls',
      status: 'done', toolContent: 'a.py' },
  ],
};

// ── F4: EXPLICIT ROUND BOUNDARIES (RENDER_CONTRACT Phase 3, last criterion) ──
// A round_start opens the round, deltas + a tool round run inside it, round_end
// closes it, then a prose-only terminal round (round_start → deltas → round_end,
// NO tool call). The boundary events are TRANSIENT — they carry no persisted
// round field — so the reducer must project the LIVE fold to the SAME committed
// {content,thinking,toolRounds} shape as the settled COLD snapshot (which never
// stores round_start/end). Also proves tool_start with NO llmRound inherits the
// open round from round_start (batch grouping keys off the explicit boundary).
const F4_LIVE = [
  { type: 'round_start', roundNum: 0 },
  { type: 'delta', content: 'First I list files.' },
  { type: 'tool_start', roundNum: 1, toolName: 'run_command', toolCallId: 'tc1', query: 'ls' },  // NO llmRound → inherits _currentRound=0
  { type: 'delta_reset', roundNum: 0 },
  { type: 'tool_result', roundNum: 1, toolCallId: 'tc1', results: [{toolName:'run_command',title:'ok',snippet:'a.py'}] },
  { type: 'tool_done', roundNum: 1, toolCallId: 'tc1', content: 'a.py' },
  { type: 'round_end', roundNum: 0, reason: 'tools' },
  { type: 'round_start', roundNum: 1 },
  { type: 'delta', content: 'Done — found it.' },
  { type: 'round_end', roundNum: 1, reason: 'final' },
];
const F4_COLD = {
  content: 'Done — found it.',
  thinking: '',
  toolRounds: [
    { roundNum: 1, llmRound: 0, toolName: 'run_command', toolCallId: 'tc1', query: 'ls',
      status: 'done', results: [{toolName:'run_command',title:'ok',snippet:'a.py'}],
      toolContent: 'a.py', assistantContent: 'First I list files.' },
  ],
};
'''


def _harness(assert_body: str) -> str:
    # Try to load the reducer module; if absent, emit a clean reducer_missing.
    return f'''
import * as fs from 'node:fs';
const REDUCER_PATH = {json.dumps(REDUCER_JS)};
let R = null;
try {{
  if (fs.existsSync(REDUCER_PATH)) {{
    const src = fs.readFileSync(REDUCER_PATH, 'utf-8');
    // The module is plain window-scope JS (bundled, no exports). Eval it and
    // pull the three public fns off the resulting scope.
    const sandbox = {{}};
    const fn = new Function('exports', src + '\\n;' +
      'exports.projectStreamEvents = (typeof projectStreamEvents!=="undefined")?projectStreamEvents:null;' +
      'exports.projectColdSnapshot = (typeof projectColdSnapshot!=="undefined")?projectColdSnapshot:null;' +
      'exports.reduceStreamState = (typeof reduceStreamState!=="undefined")?reduceStreamState:null;' +
      'exports.canonicalizeProjectionForCompare = (typeof canonicalizeProjectionForCompare!=="undefined")?canonicalizeProjectionForCompare:null;');
    fn(sandbox);
    R = sandbox;
  }}
}} catch (e) {{ R = {{ _err: String(e) }}; }}
{_FIXTURE}
if (!R || !R.projectStreamEvents || !R.projectColdSnapshot) {{
  console.log(JSON.stringify({{ ok:false, reason:'reducer_missing',
    detail:'static/js/ui/stream_reducer.js must export projectStreamEvents + projectColdSnapshot' }}));
}} else {{
{assert_body}
}}
'''


def _parity(name: str, live_expr: str, cold_expr: str) -> dict:
    body = _harness(f'''
  const C = R.canonicalizeProjectionForCompare;
  const live = C(R.projectStreamEvents({live_expr}));
  const cold = C(R.projectColdSnapshot({cold_expr}));
  const liveJson = JSON.stringify(live);
  const coldJson = JSON.stringify(cold);
  console.log(JSON.stringify({{ ok:true, equal: liveJson === coldJson, live: liveJson, cold: coldJson }}));
''')
    r = _run_node(body)
    if not r.get('ok'):
        pytest.fail(
            f'[{name}] Phase-3 reducer not present / node error (tests-first RED): '
            f"{r.get('reason')} — {r.get('detail') or r.get('stderr','')}.")
    return r


def test_F1_single_round_parity():
    """F1: single tool round + prose both sides — live fold == cold snapshot."""
    r = _parity('F1', 'F1_LIVE', 'F1_COLD')
    assert r['equal'], f'F1 divergence:\n  live={r["live"]}\n  cold={r["cold"]}'


def test_F2_multiround_delta_reset_parity():
    """F2: TWO tool rounds with a delta_reset per round — the per-round narration
    must be stamped as assistantContent on each round's first entry, exactly as
    the settled cold snapshot carries it. This is the tool-round-jitter case."""
    r = _parity('F2', 'F2_LIVE', 'F2_COLD')
    assert r['equal'], (
        'F2 MULTI-ROUND / delta_reset divergence — live fold did not stamp '
        'per-round narration the way the settled snapshot carries it:\n'
        f'  live={r["live"]}\n  cold={r["cold"]}')


def test_F3_midround_cold_reconnect_keeplonger_parity():
    """F3: the _snapshotLongerRounds case. LIVE showed 2 rounds; a cold snapshot
    lagged to 1 round. The keep-longer MERGE (routing concern) must widen the
    short cold snapshot back to the live round set BEFORE projection; then the
    merged cold projection equals the live projection. We model the merge here
    (mergeColdRounds = keep the longer toolRounds) and assert parity — proving
    the reducer + a keep-longer merge subsumes _snapshotLongerRounds."""
    body = _harness('''
  const live = R.projectStreamEvents(F3_LIVE);
  // Emulate the pipeline's keep-longer merge feeding the reducer: the cold
  // snapshot's rounds are widened to the live-length set before projecting.
  const liveRounds = live.toolRounds;  // loss-less production projection
  const merged = Object.assign({}, F3_COLD_SHORT, {
    toolRounds: (F3_COLD_SHORT.toolRounds.length >= liveRounds.length)
      ? F3_COLD_SHORT.toolRounds : liveRounds,
  });
  const C = R.canonicalizeProjectionForCompare;
  const cold = R.projectColdSnapshot(merged);
  const liveJson = JSON.stringify(C(live));
  const coldJson = JSON.stringify(C(cold));
  console.log(JSON.stringify({ ok:true, equal: liveJson === coldJson,
    live: liveJson, cold: coldJson,
    shortWouldShrink: F3_COLD_SHORT.toolRounds.length < liveRounds.length }));
''')
    r = _run_node(body)
    if not r.get('ok'):
        pytest.fail(f'[F3] reducer/node error: {r.get("reason")} {r.get("stderr","")}')
    assert r['shortWouldShrink'] is True, (
        'F3 premise broken: the cold snapshot should be SHORTER than live '
        '(the mid-round reconnect case)')
    assert r['equal'], (
        'F3 MID-ROUND reconnect divergence — after the keep-longer merge the '
        'cold projection must equal the live projection (this is what makes '
        f'retiring _snapshotLongerRounds safe):\n  live={r["live"]}\n  cold={r["cold"]}')


def test_F4_round_boundaries_project_identically():
    """F4: explicit round_start/round_end boundaries are TRANSIENT — the LIVE
    fold (which sees them) must project to the SAME committed shape as the COLD
    snapshot (which never stores them). Also: the tool_start carries NO llmRound
    and must inherit the open round (0) from round_start, so its canonicalized
    llmRound matches the settled snapshot's."""
    r = _parity('F4', 'F4_LIVE', 'F4_COLD')
    assert r['equal'], (
        'F4 ROUND-BOUNDARY divergence — the live fold with explicit '
        'round_start/round_end did not project to the settled snapshot shape '
        '(boundaries must be transient + tool_start must inherit the open '
        f'round):\n  live={r["live"]}\n  cold={r["cold"]}')


def test_NC_dropped_round_start_breaks_batch_attribution():
    """NEUTER for the round-boundary routing: if round_start is DROPPED (the
    reducer never sees the explicit boundary), a tool_start carrying no llmRound
    can no longer inherit the open round — its llmRound stays null and the
    projection DIVERGES from the settled snapshot. Proves the reducer genuinely
    keys batch attribution off the explicit boundary, not incidental state."""
    body = _harness('''
  // F4_LIVE with the round_start events STRIPPED (simulate a dropped boundary).
  const dropped = F4_LIVE.filter(e => e.type !== 'round_start');
  const C = R.canonicalizeProjectionForCompare;
  const live = C(R.projectStreamEvents(dropped));
  const cold = C(R.projectColdSnapshot(F4_COLD));
  const liveJson = JSON.stringify(live);
  const coldJson = JSON.stringify(cold);
  // The tool round's llmRound should now be MISSING (null → omitted), so the
  // two projections must DIFFER.
  console.log(JSON.stringify({ ok:true, equal: liveJson === coldJson,
    live: liveJson, cold: coldJson }));
''')
    r = _run_node(body)
    if not r.get('ok'):
        pytest.fail(f'reducer/node error: {r.get("reason")} {r.get("stderr","")}')
    assert not r['equal'], (
        'NEUTER FAILED: dropping round_start still produced an identical '
        'projection — the reducer is NOT keying batch attribution off the '
        'explicit round_start boundary (tool_start must inherit the open round '
        'from round_start, so removing it must diverge).')


def test_cold_projection_is_lossless_preserves_all_round_fields():
    """PRODUCTION-safety: projectColdSnapshot must NOT drop round fields the
    render needs. A cold snapshot's rounds carry approvalId / searchDiag /
    engineBreakdown / vertical / path / _mcpLoginHint etc. — the projection is
    loss-less (only the internal _continueToolRounds scratch may be dropped).
    Guards against the canonicalizer (test-only) being wired into production."""
    body = _harness('''
  const snap = { content:'x', thinking:'', toolRounds:[
    { roundNum:1, toolName:'run_command', toolCallId:'tc1', status:'done',
      approvalId:'ap1', searchDiag:{q:1}, engineBreakdown:{e:2}, vertical:'code',
      path:'/tmp/x', guidanceId:'g1', results:[{toolName:'run_command'}] },
  ]};
  const proj = R.projectColdSnapshot(snap);
  const r = proj.toolRounds[0] || {};
  console.log(JSON.stringify({ ok:true,
    keptApproval: r.approvalId === 'ap1',
    keptSearchDiag: !!r.searchDiag,
    keptEngine: !!r.engineBreakdown,
    keptVertical: r.vertical === 'code',
    keptPath: r.path === '/tmp/x',
    keptGuidance: r.guidanceId === 'g1',
  }));
''')
    r = _run_node(body)
    if not r.get('ok'):
        pytest.fail(f"reducer/node error: {r.get('reason')} {r.get('stderr','')}")
    missing = [k for k in ('keptApproval', 'keptSearchDiag', 'keptEngine',
                           'keptVertical', 'keptPath', 'keptGuidance') if not r.get(k)]
    assert not missing, (
        'LOSSY PROJECTION: projectColdSnapshot dropped production round fields '
        f'{missing} — a cold reconnect would lose approval/diag/vertical/path '
        'state the render depends on. The canonicalizer is test-only; production '
        'projection must preserve every field.')


def test_reducer_module_exists_and_is_pure():
    """The reducer module must exist and expose the pure projection API."""
    body = _harness('''
  console.log(JSON.stringify({ ok:true, hasReduce: typeof R.reduceStreamState === 'function' }));
''')
    r = _run_node(body)
    if not r.get('ok'):
        pytest.fail(f"tests-first RED: stream_reducer.js absent — reason={r.get('reason')}")
    assert r['hasReduce'], 'stream_reducer.js must expose reduceStreamState(state, event)'


if __name__ == '__main__':
    for fn in (test_reducer_module_exists_and_is_pure,
               test_F1_single_round_parity,
               test_F2_multiround_delta_reset_parity,
               test_F3_midround_cold_reconnect_keeplonger_parity):
        try:
            fn(); print('  PASS', fn.__name__)
        except Exception as e:  # noqa: BLE001
            print('  RED ', fn.__name__, '::', str(e)[:200])
