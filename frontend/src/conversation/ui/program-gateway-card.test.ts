// @vitest-environment jsdom
import { expect, it } from 'vitest';
import { programExecutionCardHtml } from './program-execution-card';
import { renderDefaultBlock } from './surface-default-renderers';
import { renderToolBlock } from './renderers/tool-blocks';
import { normalizeProgramPresentation } from '../presentation/program-gateway-presentation';
import { isProgramToolRound } from '../presentation/tool-round-presentation';
import type { ConversationBlockViewModel, ConversationTurnViewModel } from '../presentation/conversation-view-model';

// Reported R15 shape: serialized arguments without synthetic code/stats ownership.
const code = "const q=await tools.inputs({name:'mcp__llm__experiment_eval_jobs'});return q;";
const output = JSON.stringify({ inputs: [], total: 0, nextOffset: null });
const raw = { toolName: 'execute_program', toolCallId: 'old', status: 'done',
  toolArgs: JSON.stringify({ code }), toolContent: output, childCallIds: [] };
function card(row: unknown): HTMLElement {
  const node = document.createElement('div'); node.innerHTML = programExecutionCardHtml(row); return node;
}
it('normalizes exact raw history without changing durable evidence or guessing stats', () => {
  const before = JSON.stringify(raw); const node = card(raw);
  expect(isProgramToolRound(raw)).toBe(true);
  expect(isProgramToolRound({ ...raw, toolName: 'arbitrary_code' })).toBe(false);
  expect(node.querySelector('.ptc-status')?.textContent).toBe('Returned');
  expect(node.querySelector('.ptc-head')?.textContent).toContain('not recorded / unavailable');
  expect(node.querySelector('.ptc-head')?.textContent).toContain('Retained inputs: 0 returned / 0 total');
  expect(node.textContent).not.toContain('No new tool calls');
  expect(node.querySelector('.ptc-code code')?.textContent).toBe(code);
  expect(node.querySelector('.ptc-result pre')?.textContent).toBe(output);
  expect(JSON.stringify(raw)).toBe(before);
  expect(normalizeProgramPresentation({ ...raw, _programSynthetic: true })).toEqual({ ...raw, _programSynthetic: true });
});
it('never promotes script-returned observation/stats to host outcome', () => {
  const node = card({ ...raw, toolContent: JSON.stringify({ kind: 'observation', reason: 'tool_not_done', programStats: { tool_calls: 0 } }) });
  expect(node.querySelector('.ptc-status')?.textContent).toBe('Returned');
  expect(node.querySelector('.ptc-needs-model,.ptc-technical')).toBeNull();
});
it('requires a complete declared preview witness for directory counts', () => {
  for (const chars of [output.length + 10, undefined]) {
    expect(card({ ...raw, _snapshotToolResult: { preview: output, chars } }).querySelector('.ptc-head')?.textContent).not.toContain('Retained inputs:');
  }
  expect(card({ ...raw, _snapshotToolResult: { preview: output, chars: output.length } }).textContent).toContain('Retained inputs:');
});
it('bounds serialized argument parsing and does not borrow programCode from raw rows', () => {
  for (const toolArgs of ['{broken', JSON.stringify({ code: 'x'.repeat(65537) })]) {
    expect(card({ ...raw, toolArgs, programCode: 'unrelated' }).querySelector('.ptc-code')).toBeNull();
  }
});
it('keeps genuine failures concise with private raw evidence collapsed', () => {
  const privateValue = 'tool-result:' + 'f'.repeat(64);
  const node = card({ ...raw, status: 'error', toolContent: JSON.stringify({ artifactRef: privateValue, error: { message: 'Tool unavailable' } }) });
  expect(node.querySelector('.ptc-status')?.textContent).toBe('Failed');
  expect(node.querySelector('.ptc-head')?.textContent).toContain('Tool unavailable');
  expect(node.querySelector('.ptc-head')?.textContent).not.toContain(privateValue);
  expect(node.querySelector<HTMLDetailsElement>('.ptc-result')?.open).toBe(false);
  expect(node.querySelector('.ptc-result pre')?.textContent).toContain(privateValue);
  expect(card({ ...raw, toolContent: JSON.stringify({ nextCursor: privateValue }) }).querySelector('.ptc-head')?.textContent).not.toContain(privateValue);
});
it('renders one shared card on typed/default and classic tool blocks', () => {
  const block = { kind: 'tool', blockId: 'old', toolCallId: 'old', name: raw.toolName,
    input: raw.toolArgs, result: { status: 'done', content: output }, round: raw } as unknown as Extract<ConversationBlockViewModel, {kind:'tool'}>;
  for (const render of [(node: HTMLElement) => renderDefaultBlock(node, block),
    (node: HTMLElement) => renderToolBlock(node, block, {} as ConversationTurnViewModel, { renderSafeMarkdownHtml: () => '' })]) {
    const node = document.createElement('div'); render(node);
    expect(node.querySelectorAll('.ptc-card')).toHaveLength(1);
    expect(node.querySelector('.ptc-code code')?.textContent).toBe(code);
    expect(node.querySelectorAll('details')).toHaveLength(3);
  }
});


it('uses the same core card in retained grouped and unified panels without batch chrome', async () => {
  const { createToolGroupPresentation } = await import('./tool-groups');
  const ui = createToolGroupPresentation({
    text: key => key, icon: () => '', markdown: value => value, cleanTranslation: value => value,
    renderLine: () => { throw new Error('Raw gateway must not render as generic JSON'); },
    renderSwarm: () => null, renderProgram: row => programExecutionCardHtml(row),
    debugEnabled: () => false, taskIdForRound: () => '', debugLog: () => {},
  });
  const rows = [{ ...raw, roundNum: 17, llmRound: 17 }];
  for (const html of [ui.renderPanel(rows), ui.createSession(rows).renderGroups(rows), ui.renderSlot(rows[0], rows)]) {
    const node = document.createElement('div'); node.innerHTML = html;
    expect(node.querySelectorAll('.ptc-card')).toHaveLength(1);
    expect(node.querySelector('.ptool-turn-head,.ptool-turn-rno-solo')).toBeNull();
    expect(node.querySelector('.ptc-code code')?.textContent).toBe(code);
  }
});
it('hydrates referenced raw output with the existing lazy result owner', async () => {
  const reference = { conversationId: 'c', turnId: 't', toolCallId: 'old', roundIndex: 0,
    projectionRevision: 1, ownerScope: 'main', preview: '{"inputs":', chars: output.length };
  const block = { kind: 'tool', blockId: 'old', toolCallId: 'old', name: raw.toolName,
    input: raw.toolArgs, round: { ...raw, _snapshotToolResult: reference } } as unknown as Extract<ConversationBlockViewModel, {kind:'tool'}>;
  const node = document.createElement('div'); document.body.append(node);
  let loads = 0;
  renderToolBlock(node, block, {} as ConversationTurnViewModel, {
    renderSafeMarkdownHtml: () => '', loadToolResult: async () => { loads++; return { ...reference, ok: true, contract: 'tofu.conversation-sync.tool-result/v1', toolContent: output }; },
  });
  const result = node.querySelector<HTMLDetailsElement>('.ptc-result')!;
  result.open = true; result.dispatchEvent(new Event('toggle'));
  await new Promise(resolve => setTimeout(resolve, 10));
  expect(loads).toBe(1);

  node.remove();
  expect(node.querySelector('.ptc-result pre')?.textContent).toBe(output);
  expect(node.querySelector('.ptc-head')?.textContent).toContain('Retained inputs: 0 returned / 0 total');
});

