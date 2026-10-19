// @vitest-environment jsdom
import { expect, it } from 'vitest';
import { RoundOutline } from './round-outline';
import type { ConversationTurnViewModel, ToolBlockViewModel } from '../presentation/conversation-view-model';

const block = (status: string): ToolBlockViewModel => ({ kind: 'tool', blockId: 'failed',
  identitySource: 'contract', source: {}, toolCallId: 'failed', name: 'mcp__llm__experiment_eval_jobs',
  input: {}, result: { status, content: 'original raw evidence' },
  round: { toolName: 'mcp__llm__experiment_eval_jobs', status } });
// Only the assistant role participates in this presentation controller.
const turn = { role: 'assistant' } as ConversationTurnViewModel;

it('keeps concise generic failure diagnostics visible and raw evidence folded on first paint', () => {
  const node = document.createElement('div');
  node.innerHTML = '<details data-tool-failure-evidence="collapsed"><summary class="ptool-result-header">experiment has not run yet</summary><pre>raw envelope</pre></details>';
  const outline = new RoundOutline(node);
  const value = block('error');
  outline.update(turn, [value]); outline.patchBlock(node, value); outline.completeBlock(node, value);
  const details = node.querySelector('details')!;
  expect(details.open).toBe(false);
  expect(node.dataset.actionState).toBe('attention');
  expect(node.querySelector('summary')?.textContent).toContain('experiment has not run yet');
  expect(node.querySelector('.conversation-action-state')?.textContent).toBe('Review details');
  for (const choice of [true, false]) {
    details.open = choice;
    outline.update(turn, [value]); outline.completeBlock(node, value);
    expect(details.open).toBe(choice);
  }
});

it('still opens structured actionable errors and active evidence on first paint', () => {
  for (const status of ['error', 'running']) {
    const node = document.createElement('div');
    node.innerHTML = '<details><summary class="ptool-result-header">Actionable evidence</summary></details>';
    const outline = new RoundOutline(node);
    const value = block(status);
    outline.update(turn, [value]); outline.completeBlock(node, value);
    expect(node.querySelector('details')?.open).toBe(true);
  }
});
