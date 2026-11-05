// @vitest-environment jsdom
import { expect, it } from 'vitest';
import { RoundOutline } from './round-outline';
import type { ConversationTurnViewModel, ToolBlockViewModel } from '../presentation/conversation-view-model';

const block = (status: string): ToolBlockViewModel => ({ kind: 'tool', blockId: 'failed',
  identitySource: 'contract', source: {}, toolCallId: 'failed', name: 'mcp__llm__experiment_eval_jobs',
  input: {}, result: { status, content: 'original raw evidence' },
  round: { toolName: 'mcp__llm__experiment_eval_jobs', status } });
// Only the assistant role participates in this presentation controller; it
// reads the Turn's ordered blocks to find large rounds.
const turnWith = (value: ToolBlockViewModel) => ({ role: 'assistant', blocks: [value] }) as unknown as ConversationTurnViewModel;

it('keeps concise generic failure diagnostics visible and raw evidence folded on first paint', () => {
  const node = document.createElement('div');
  node.innerHTML = '<details data-tool-failure-evidence="collapsed"><summary class="ptool-result-header">experiment has not run yet</summary><pre>raw envelope</pre></details>';
  const outline = new RoundOutline(node);
  const value = block('error');
  const turn = turnWith(value);
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

it('lets a swarm panel verdict chip stand as the only state in its header', () => {
  const node = document.createElement('div');
  node.innerHTML = '<div class="sw-panel"><div class="sw-header"><div class="sw-header-left">'
    + '<span class="sw-header-title">Parallel execution</span></div><div class="sw-header-right">'
    + '<span class="sw-status-pill sw-pill-running">Working…</span></div></div></div>';
  const outline = new RoundOutline(node);
  const value: ToolBlockViewModel = { ...block('running'), blockId: 'swarm', toolCallId: 'swarm',
    name: 'spawn_agents', round: { toolName: 'spawn_agents', status: 'searching', _swarmActive: true } };
  outline.update(turnWith(value), [value]); outline.patchBlock(node, value); outline.completeBlock(node, value);
  expect(node.dataset.actionState).toBe('active');
  expect(node.querySelector<HTMLElement>('.sw-header')?.dataset.actionSummary).toBe('true');
  expect(node.querySelector('.conversation-action-state')).toBeNull();
});

it('still opens structured actionable errors and active evidence on first paint', () => {
  for (const status of ['error', 'running']) {
    const node = document.createElement('div');
    node.innerHTML = '<details><summary class="ptool-result-header">Actionable evidence</summary></details>';
    const outline = new RoundOutline(node);
    const value = block(status);
    outline.update(turnWith(value), [value]); outline.completeBlock(node, value);
    expect(node.querySelector('details')?.open).toBe(true);
  }
});
