"""Typed completion evidence is a safe projection, never browser delivery state."""
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_background_completion_native_surfaces():
    target = native_module_path(
        '.native/background-completion-renderer.js',
        ROOT / 'frontend/src/conversation/ui/classic-conversation-renderers.ts')
    view_model = native_module_path(
        '.native/background-completion-view-model.js',
        ROOT / 'frontend/src/conversation/presentation/conversation-view-model.ts')
    run_harness(target_js=str(target), extra_targets=[str(view_model)],
                label='background completion UI', expect_pass=6,
                body_js=r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({root: process.argv[3],
  targets: [process.argv[2], process.argv[4]]});
const renderers = createClassicConversationRenderers({renderSafeMarkdownHtml: () => {throw Error('raw XML must not reach markdown');}});
const completion = {commandId: 'bg_1', status: 'completed', command: '<script>cmd</script>',
  resultPreview: '<img src=x onerror=bad>' + 'X'.repeat(2200), resultTruncated: true};
const item = {queueId: 'q1', kind: 'workflow_step', position: 1, text: 'RAW_XML', source: {backgroundCommand: completion}};
const node = document.createElement('div');
renderers.renderQueueItem(node, item);
check('queue_is_finished_but_not_confirmed', node.textContent.includes('finished · delivery unconfirmed')
  && node.textContent.includes('Cancel delivery') && !node.textContent.includes('RAW_XML'));
check('output_is_expandable_bounded_and_escaped', !!node.querySelector('details')
  && !node.querySelector('script,img') && node.textContent.includes('truncated')
  && node.querySelector('.sw-card-raw-pre').textContent.length === 2000);
const input = document.createElement('div');
renderers.renderBlock(input, {kind: 'text', markdown: 'RAW_XML'},
  {turn: {actor: 'system', source: {projection: {backgroundCommand: completion}}}});
check('fresh_turn_renders_durable_evidence', input.textContent.includes('submitted as turn input')
  && !!input.querySelector('details') && !input.textContent.includes('RAW_XML') && !input.querySelector('script,img'));
check('fresh_turn_dom_carries_bgcmd_classes',
  !!input.querySelector('p.bgcmd-label')
  && !!input.querySelector('details.bgcmd-details > summary.bgcmd-summary > code.bgcmd-id')
  && !!input.querySelector('details.bgcmd-details > pre.bgcmd-command'));
const mkTurn = (turnId, projection) => ({
  turnId, conversationId: 'conv-1', laneId: 'main', ordinal: 1,
  actor: 'human', kind: 'input', runId: '', status: 'completed',
  currentAttemptId: null, projectionRevision: 1, settlement: {},
  createdAt: 1, updatedAt: 1, projection,
});
const vm = selectConversationViewModel({
  conversationId: 'conv-1', conversationRevision: 1, transport: {},
  turnsById: {
    't-bg': mkTurn('t-bg', {
      segments: [{type: 'text', blockId: 'text:0', text: 'done'}],
      backgroundCommand: completion,
    }),
    't-plain': mkTurn('t-plain', {
      segments: [{type: 'text', blockId: 'text:0', text: 'hi'}],
    }),
  },
  laneOrder: {main: ['t-bg', 't-plain']},
  queueItems: [], attemptsById: {}, commandPending: {},
});
const actionsOf = (id) => vm.mainLane.turns
  .find((turn) => turn.turnId === id).actions.map((a) => a.action);
const bgActions = actionsOf('t-bg');
check('bgcmd_turn_actions_drop_edit_regenerate',
  !bgActions.includes('edit') && !bgActions.includes('regenerate')
  && bgActions.includes('copy') && bgActions.includes('delete'));
const plainActions = actionsOf('t-plain');
check('plain_human_turn_keeps_edit_regenerate',
  plainActions.includes('edit') && plainActions.includes('regenerate'));
report();
""")
