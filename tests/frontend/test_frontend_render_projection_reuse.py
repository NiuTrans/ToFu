"""Render-cost contracts for the conversation view model.

The reducer preserves a Turn object's identity while its projection is
unchanged, so `selectTurnBlocks` memoizes on that object and the keyed Surface
can trust a reused `blocks` array.  These tests pin the reuse contract that the
per-frame streaming path depends on; without it every animation frame re-parses
every loaded Turn and re-walks every mounted block.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "frontend/src/conversation/index.ts"


def _ready() -> bool:
    bundler = ROOT / "scripts" / "vite_test_bundle.mjs"
    if not shutil.which("node") or not bundler.is_file():
        return False
    return subprocess.run(
        [shutil.which("node"), "-e", "require('jsdom')"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).returncode == 0


pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    not _ready(), reason="node + jsdom + Vite test bundler required")]


@pytest.fixture(scope="module")
def conversation_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    built = tmp_path_factory.mktemp("render-reuse") / "conversation.cjs"
    result = subprocess.run(
        [str(ROOT / "scripts" / "vite_test_bundle.mjs"), str(ENTRY),
         "--bundle", "--format=cjs", "--platform=node",
         f"--outfile={built}", "--log-level=error"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return built


def _run(bundle: Path, source: str) -> dict:
    script = f"""
const feature = require({json.dumps(str(bundle))});
{source}
"""
    result = subprocess.run(
        [shutil.which("node"), "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_unchanged_turn_reuses_blocks_and_only_the_changed_turn_rebuilds(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const makeTurn = (id, revision, text) => ({
  turnId:id, conversationId:'conv-a', laneId:'main', ordinal:Number(id.split('-')[1]),
  actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null, projectionRevision:revision,
  projection:{segments:[
    {type:'thinking', blockId:`thinking:${id}`, text:'reason', llmRound:1},
    {type:'tool_use', blockId:`tool:${id}`, id, name:'search', input:{q:id},
      result:{status:'done', content:'hit'}, llmRound:1},
    {type:'text', blockId:'text:terminal', text, deliverable:true, terminal:true},
  ], toolRounds:[{roundNum:1, llmRound:1, toolCallId:id, toolName:'search',
    toolArgs:{q:id}, status:'done', results:[{title:'r'}]}],
    content:text, model:'m'},
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:revision,
});
const t1 = makeTurn('turn-1', 1, 'one');
const t2 = makeTurn('turn-2', 1, 'two');
const state = {
  conversationId:'conv-a', conversationRevision:2, transport:'live',
  turnsById:{'turn-1':t1, 'turn-2':t2}, laneOrder:{main:['turn-1','turn-2']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{},
};
const before = JSON.stringify(state);
const first = feature.selectConversationViewModel(state, {});
const second = feature.selectConversationViewModel(state, {});
/* Selector must not mutate authority even while memoizing. */
const unchanged = before === JSON.stringify(state);

/* One Turn advances its revision with a structurally different projection. */
const t2next = {...t2, projectionRevision:2,
  projection:{...t2.projection, segments:[
    ...t2.projection.segments.slice(0, -1),
    {type:'text', blockId:'text:terminal', text:'two plus', deliverable:true,
      terminal:true},
  ], content:'two plus'}};
const nextState = {...state,
  turnsById:{'turn-1':t1, 'turn-2':t2next}, conversationRevision:3};
const third = feature.selectConversationViewModel(nextState, {});

const blocksOf = (vm, id) => vm.mainLane.turns.find((turn) => turn.turnId === id).blocks;
console.log(JSON.stringify({
  unchanged,
  sameTurn1Blocks: blocksOf(first, 'turn-1') === blocksOf(second, 'turn-1'),
  sameTurn2Blocks: blocksOf(first, 'turn-2') === blocksOf(second, 'turn-2'),
  keptTurn1BlocksAcrossChange:
    blocksOf(third, 'turn-1') === blocksOf(first, 'turn-1'),
  rebuiltTurn2BlocksAcrossChange:
    blocksOf(third, 'turn-2') !== blocksOf(second, 'turn-2'),
  turn2Text: blocksOf(third, 'turn-2').at(-1).markdown,
}));
""")
    assert result == {
        "unchanged": True,
        "sameTurn1Blocks": True,
        "sameTurn2Blocks": True,
        "keptTurn1BlocksAcrossChange": True,
        "rebuiltTurn2BlocksAcrossChange": True,
        "turn2Text": "two plus",
    }


def test_surface_skips_reconciling_a_reused_blocks_array(
        conversation_bundle: Path):
    """A reused `blocks` array is the Surface's proof that the mounted subtree
    is already correct: a redundant render performs no reconciliation at all,
    while the next genuinely changed projection heals the container."""
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const turn = (revision, text, segments) => ({
  turnId:'turn-1', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null, projection:{segments, content:text},
  projectionRevision:revision, settlement:{outcome:'completed'},
  createdAt:1, updatedAt:1,
});
const segments = (text) => [
  {type:'text', blockId:'text:terminal', text, deliverable:true, terminal:true},
];
const makeState = (t) => ({
  conversationId:'conv-a', conversationRevision:t.projectionRevision,
  transport:'live', turnsById:{'turn-1':t}, laneOrder:{main:['turn-1']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{},
});
const surface = feature.createConversationSurface(
  document.getElementById('chat'),
  feature.createClassicConversationRenderers({ renderSafeMarkdownHtml:v => v }),
);
/* Reuse the exact Turn object so the memoized blocks array is identical. */
const stableTurn = turn(1, 'one', segments('one'));
const vm1 = feature.selectConversationViewModel(makeState(stableTurn), {});
surface.render(vm1);
/* No new projection is created between these renders, so a reconciliation
 * pass is unnecessary.  Removing the node proves the pass is skipped: it is
 * only re-created once the projection actually changes. */
surface.root.querySelector('[data-block-id="text:terminal"]').remove();
const vm2 = feature.selectConversationViewModel(makeState(stableTurn), {});
surface.render(vm2);
const skipped = !surface.root.querySelector('[data-block-id="text:terminal"]');
/* A real projection change rebuilds the array and re-mounts the node. */
const vm3 = feature.selectConversationViewModel(
  makeState(turn(2, 'two', segments('two'))), {});
surface.render(vm3);
const healed = surface.root.querySelector('[data-block-id="text:terminal"]').textContent;
surface.dispose();
dom.window.close();
console.log(JSON.stringify({skipped, healed}));
""")
    assert result == {"skipped": True, "healed": "two"}
