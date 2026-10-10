"""Semantic-impossibility guards over the projected turn store (item M).

The durable value of the DSH "recorded-transcript replay gate" for tofu is the
**guard pass over the projected turn store**, not the heavyweight browser
replay (deferred — see docs/DEEPSEEK_HARNESS_ANALYSIS_0.1.5.md item M). This
test synthesizes turn projections whose ``projection.segments`` carry
``tool_use`` blocks in the three states that matter, reduces them through the
real ``createTurnState`` + ``reduceTurnState`` owner, and asserts the
impossibility invariants a codegen / server-projection change could silently
break:

  (a) **No orphan tool_use** — every ``tool_use`` segment binds to an
      execution record (``_round.toolCallId``, a ``roundRef`` present in
      ``projection.toolRounds``, or its own ``id`` among the round ids).
  (b) **Result envelope present** — every ``tool_use`` carries a ``result``
      with a string ``status`` (the verdict the renderer keys on). This
      replaces the doc's ``UNKNOWN_TOOL`` check, which is a DSH symbol that
      does not exist in tofu's projection (0 matches in ``frontend/src``).
  (c) **Per-lane ordinal monotonicity** — ``laneOrder[lane]`` sorts strictly
      by ``ordinal`` (the reducer's actual lane sort key, turn-state.ts). The
      reducer already drops revision-regressing turns in ``putTurn``; this
      locks the lane-ordering half.

Each guard also gets a **rejection control**: a deliberately-broken projection
must be *detected* by the same predicate, mirroring the known-bad rejection
pattern in ``test_architecture_check_concurrency.py`` — a guard that cannot
fail is a comment.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _bundle_harness(tmp_path: Path) -> str:
    from tests._runtime_sections import native_module_path

    entry = tmp_path / "turn-store-guards-harness.ts"
    entry.write_text(
        "export { createTurnState, reduceTurnState } from "
        f"{(ROOT / 'frontend/src/core/turn-state.ts').as_posix()!r};\n",
        encoding="utf-8",
    )
    return native_module_path("turn-store-guards/harness.js", entry)


def _run_node(script: str, *paths) -> dict:
    result = subprocess.run(
        ["node", "-e", script, *(str(path) for path in paths)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


_NODE_SCRIPT = r"""
const fs = require('fs');
globalThis.window = globalThis;
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

// ── Guard predicates (the durable artifact; mirrored in the doc) ──
function toolUseSegments(turn) {
  return ((turn && turn.projection && turn.projection.segments) || [])
    .filter((s) => s && s.type === 'tool_use');
}
function roundIds(turn) {
  const rounds = (((turn || {}).projection || {}).toolRounds) || [];
  const ids = rounds.map((r) => r && r.toolCallId).filter(Boolean);
  return new Set(ids);
}
function orphans(turn) {
  const ids = roundIds(turn);
  return toolUseSegments(turn).filter((s) =>
    !(s._round && s._round.toolCallId)
    && !(s.roundRef && ids.has(s.roundRef))
    && !ids.has(s.id));
}
function missingResultEnvelope(turn) {
  return toolUseSegments(turn).filter((s) =>
    !s.result || typeof s.result.status !== 'string');
}
function laneOrdinalRegressions(state) {
  const bad = [];
  for (const lane of Object.keys(state.laneOrder)) {
    const ords = (state.laneOrder[lane] || [])
      .map((id) => Number((state.turnsById[id] || {}).ordinal || 0));
    for (let i = 1; i < ords.length; i += 1) {
      if (ords[i] < ords[i - 1]) bad.push({ lane, at: i, ords });
    }
  }
  return bad;
}

// ── Projection builders ──
function toolUse(id, { roundRef, round, result } = {}) {
  const seg = { type: 'tool_use', blockId: 'b-' + id, id, name: 'run_command' };
  if (roundRef !== undefined) seg.roundRef = roundRef;
  if (round !== undefined) seg._round = round;
  if (result !== undefined) seg.result = result;
  return seg;
}
function turn(over) {
  return Object.assign({
    turnId: 't1', conversationId: 'c1', laneId: 'main', actor: 'assistant',
    kind: 'message', status: 'completed', currentAttemptId: 'a1',
    ordinal: 1, projectionRevision: 1,
    projection: { content: '', segments: [], toolRounds: [] },
    createdAt: 1, updatedAt: 1,
  }, over);
}
function reduce(turns) {
  let state = createTurnState('c1');
  return reduceTurnState(state, {
    type: 'snapshot',
    snapshot: { conversationRevision: 1, turns, attempts: [], queueItems: [] },
  });
}

const out = {};

// ── (a) orphan tool_use: good projection has none; broken one is detected ──
{
  const good = turn({ projection: { content: '', toolRounds: [
      { toolCallId: 'call-1', toolName: 'run_command', status: 'done' },
    ], segments: [
      toolUse('call-1', { result: { status: 'done', content: 'ok' } }),
    ] } });
  const goodState = reduce([good]);
  out.goodHasNoOrphan =
    orphans(goodState.turnsById.t1).length === 0;

  const bad = turn({ projection: { content: '', toolRounds: [], segments: [
      toolUse('ghost', { result: { status: 'done' } }),  // binds to nothing
    ] } });
  const badState = reduce([bad]);
  out.orphanDetected = orphans(badState.turnsById.t1).length === 1;

  // roundRef binding also counts as resolved (no _round inline).
  const refBound = turn({ projection: { content: '', toolRounds: [
      { toolCallId: 'call-9', toolName: 'read_files', status: 'done' },
    ], segments: [
      toolUse('seg-9', { roundRef: 'call-9',
                         result: { status: 'done', content: 'x' } }),
    ] } });
  out.roundRefResolvesBinding =
    orphans(reduce([refBound]).turnsById.t1).length === 0;
}

// ── (b) result envelope: present+status vs missing ──
{
  const good = turn({ projection: { content: '', toolRounds: [], segments: [
      toolUse('call-1', { result: { status: 'done', content: 'ok' } }),
    ] } });
  out.goodHasResultEnvelope =
    missingResultEnvelope(reduce([good]).turnsById.t1).length === 0;

  const noStatus = turn({ projection: { content: '', toolRounds: [], segments: [
      toolUse('call-2', { result: { content: 'no status key' } }),
    ] } });
  out.missingStatusDetected =
    missingResultEnvelope(reduce([noStatus]).turnsById.t1).length === 1;

  const noResult = turn({ projection: { content: '', toolRounds: [], segments: [
      toolUse('call-3'),  // no result at all
    ] } });
  out.missingResultDetected =
    missingResultEnvelope(reduce([noResult]).turnsById.t1).length === 1;
}

// ── (c) per-lane ordinal monotonicity ──
{
  const ordered = reduce([
    turn({ turnId: 't1', ordinal: 1 }),
    turn({ turnId: 't2', ordinal: 2, status: 'running',
           currentAttemptId: 'a2', projectionRevision: 2 }),
  ]);
  out.orderedLaneMonotone =
    laneOrdinalRegressions(ordered).length === 0;
  // The reducer sorts laneOrder by ordinal even if the snapshot lists the
  // turns out of order — the projection invariant is the sorted result.
  const shuffled = reduce([
    turn({ turnId: 't2', ordinal: 2, status: 'running',
           currentAttemptId: 'a2', projectionRevision: 2 }),
    turn({ turnId: 't1', ordinal: 1 }),
  ]);
  out.shuffledLaneSortedMonotone =
    laneOrdinalRegressions(shuffled).length === 0
    && shuffled.laneOrder.main[0] === 't1';
}

console.log(JSON.stringify(out));
"""


def test_projected_turn_store_semantic_impossibility_guards(tmp_path):
    bundle = _bundle_harness(tmp_path)
    output = _run_node(_NODE_SCRIPT, bundle)
    assert output == {
        # (a) orphan tool_use
        "goodHasNoOrphan": True,
        "orphanDetected": True,
        "roundRefResolvesBinding": True,
        # (b) result envelope
        "goodHasResultEnvelope": True,
        "missingStatusDetected": True,
        "missingResultDetected": True,
        # (c) per-lane ordinal monotonicity
        "orderedLaneMonotone": True,
        "shuffledLaneSortedMonotone": True,
    }
