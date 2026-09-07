#!/usr/bin/env python3
"""Public contract tests for attempt-aware tool execution grouping.

The pure TypeScript presentation owner groups tool rounds and ordered narration
segments by durable attempt scope plus executor-local LLM round. The retained
HTML adapter consumes this result but does not own or duplicate the policy.

This suite executes the bundled TypeScript owner directly. Counterexamples pin
the important boundary: parallel calls in one attempt collapse, while a
continued/resumed attempt or a legacy counter reset never does.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OWNER_JS = native_module_path(
    '.native/tool-execution-groups-contract.js',
    ROOT / 'frontend/src/conversation/presentation/tool-execution-groups.ts',
)

_HARNESS = r"""
eval(process.env.OWNER_SRC);
const results = [];
function check(name, condition) {
  results.push((condition ? 'PASS ' : 'FAIL ') + name);
}

const parallel = computeToolBatches([
  { roundNum: 1, llmRound: 0, attemptId: 'attempt-a', toolCallId: 'one' },
  { roundNum: 2, llmRound: 0, attemptId: 'attempt-a', toolCallId: 'two' },
]);
check('parallel_calls_share_one_batch',
  parallel.length === 1 && parallel[0].rounds.length === 2);
check('scoped_batch_has_stable_key',
  parallel[0].key === 'Aattempt-a|L0');
check('single_attempt_is_annotated',
  parallel[0].attemptOrdinal === 1 && parallel[0].totalAttempts === 1);
check('group_round_metadata_is_one_based',
  toolGroupRoundNumber(parallel[0]) === 1
  && toolGroupRoundDisplay(parallel[0]) === '1');

const resumed = computeToolBatches([
  { roundNum: 1, llmRound: 0, attemptId: 'attempt-old', toolCallId: 'old' },
  { roundNum: 1, llmRound: 0, attemptId: 'attempt-new', toolCallId: 'new' },
]);
check('resumed_attempt_never_collapses',
  resumed.length === 2 && resumed[0].key !== resumed[1].key);
check('resumed_attempt_ordinals_are_explicit',
  resumed[0].attemptOrdinal === 1 && resumed[1].attemptOrdinal === 2
  && resumed.every((group) => group.totalAttempts === 2));
check('resumed_attempt_label_is_explicit',
  toolGroupRoundDisplay(resumed[1]) === 'A2 · R1'
  && toolGroupRoundTitle(resumed[1], (key, values) =>
    key === 'ri.trTipAttempt' ? `attempt ${values.n}` : `round ${values.n}`
  ) === 'attempt 2 · round 1');
check('turn_count_uses_attempt_aware_batches', countToolTurns(resumed.flatMap(
  (group) => group.rounds,
)) === 2);

const sourceRound = { attemptId: 'attempt-source', llmRound: 3, roundNum: 4 };
check('segment_reads_source_round_identity',
  toolExecutionLlmRound({ type: 'text', _round: sourceRound }) === 3);
check('source_round_groups_segment_and_tool',
  computeExecutionBatches([
    { type: 'text', text: 'narration', _round: sourceRound },
    { type: 'tool_use', toolCallId: 'call', _round: sourceRound },
  ], true).length === 1);

const legacy = computeToolBatches([
  { roundNum: 1, llmRound: 0, toolCallId: 'first' },
  { roundNum: 2, llmRound: 1, toolCallId: 'middle' },
  { roundNum: 3, llmRound: 0, toolCallId: 'resumed' },
]);
check('legacy_noncontiguous_counter_recurrence_splits',
  legacy.length === 3 && legacy[2].key === 'L0#1');
check('legacy_counter_recurrence_starts_new_attempt',
  legacy[0].attemptOrdinal === 1 && legacy[1].attemptOrdinal === 1
  && legacy[2].attemptOrdinal === 2);

const adjacentReset = computeToolBatches([
  { roundNum: 4, llmRound: 0, toolCallId: 'old-last' },
  { roundNum: 1, llmRound: 0, toolCallId: 'new-first' },
]);
check('legacy_adjacent_round_reset_splits',
  adjacentReset.length === 2 && adjacentReset[1].attemptOrdinal === 2);

const frozenInput = Object.freeze([
  Object.freeze({ roundNum: 1, llmRound: 0, attemptId: 'frozen' }),
]);
const frozenBefore = JSON.stringify(frozenInput);
computeToolBatches(frozenInput);
check('grouping_does_not_mutate_projection_input',
  JSON.stringify(frozenInput) === frozenBefore);
check('invalid_group_metadata_fails_closed',
  toolGroupRoundNumber({ rounds: [{ llmRound: '0' }] }) === null
  && toolGroupRoundDisplay(null) === '');

console.log(results.join('\n'));
"""

_RENDER_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  targets: [process.argv[4], process.argv[2], process.argv[5]],
  globals: {
    _convRenderFingerprint: () => 0,
    conversations: [],
    activeConvId: null,
  },
});

function fragment(html) {
  const element = document.createElement('div');
  element.innerHTML = html;
  return element;
}

const rounds = [
  { roundNum: 1, llmRound: 0, toolName: 'grep_search', status: 'done' },
  { roundNum: 2, llmRound: 1, toolName: 'apply_diff', status: 'done' },
];
const segments = [
  { type: 'thinking', text: 'reason0', deliverable: false, llmRound: 0 },
  { type: 'text', text: 'narrate0-EN', translatedText: '第零轮译文',
    deliverable: false, llmRound: 0 },
  { type: 'tool_use', id: 'a1', name: 'grep_search', llmRound: 0 },
  { type: 'text', text: 'narrate1-EN', deliverable: false, llmRound: 1 },
  { type: 'tool_use', id: 'b1', name: 'apply_diff', llmRound: 1 },
  { type: 'text', text: 'THE ANSWER', deliverable: true, terminal: true },
];
const html = renderToolRoundsHTML(rounds, false, segments);
const translated = html.indexOf('第零轮译文');
const firstBatch = html.indexOf('data-llm-round="L0"');
const sourceOnly = html.indexOf('narrate1-EN');
const secondBatch = html.indexOf('data-llm-round="L1"');
check('translation_precedes_first_batch', translated >= 0 && translated < firstBatch);
check('translation_replaces_source', !html.includes('narrate0-EN'));
check('source_fallback_precedes_second_batch',
  sourceOnly >= 0 && sourceOnly < secondBatch && sourceOnly > firstBatch);
check('deliverable_is_outside_tool_panel', !html.includes('THE ANSWER'));
const noSegments = renderToolRoundsHTML(rounds, false);
check('no_segments_keeps_tool_rows_without_narration',
  fragment(noSegments).querySelectorAll('.ptool-line').length === 2
  && !noSegments.includes('seg-narration'));
report();
"""


def test_attempt_aware_grouping_contract() -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not installed')
    owner_source = Path(OWNER_JS).read_text(encoding='utf-8')
    process = subprocess.run(
        [node, '-e', _HARNESS],
        capture_output=True,
        text=True,
        env={**os.environ, 'OWNER_SRC': owner_source},
        timeout=30,
    )
    assert process.returncode == 0, (
        f'node eval failed:\nSTDOUT:{process.stdout}\nSTDERR:{process.stderr}'
    )
    failures = [
        line for line in process.stdout.splitlines() if line.startswith('FAIL ')
    ]
    assert not failures, process.stdout
    passes = [
        line for line in process.stdout.splitlines() if line.startswith('PASS ')
    ]
    assert len(passes) == 15, process.stdout


def test_public_renderer_places_grouped_narration() -> None:
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),
        body_js=_RENDER_HARNESS,
        extra_targets=[
            os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js'),
            os.path.join(JS_DIR, 'ui', 'tool_rounds_rich.js'),
        ],
        expect_pass=5,
        label='grouped narration public renderer',
    )
