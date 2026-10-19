// @vitest-environment jsdom
/** Program fallback retains the concurrent evidence-first presentation contract. */
import { expect, it, vi } from 'vitest';
import type { ConversationBlockViewModel, ConversationTurnViewModel } from '../../presentation/conversation-view-model';
import { renderProgramBlock } from './tool-blocks';

const { JSDOM } = await vi.importActual<{
  JSDOM: new (markup: string) => { window: Pick<Window, 'document' | 'close'> };
}>('jsdom');

it.each(['completed', 'error'] as const)('keeps %s program evidence safe and distinguishes model handoff', status => {
  const dom = new JSDOM('<!doctype html><body></body>');
  try {
    const document = dom.window.document;
    const node = document.createElement('div');
    const block = {
      kind: 'program', blockId: 'program',
      round: { programStatus: status, programCode: 'tools.observe("choose");',

        programStats: { outcome: 'needs_model' },
        programResult: { kind: 'observation', reason: 'Choose evidence', data: '<script>unsafe()</script>' } },
    } as unknown as Extract<ConversationBlockViewModel, { kind: 'program' }>;
    const before = JSON.stringify(block);
    renderProgramBlock(node, block, {} as ConversationTurnViewModel, {
      renderSafeMarkdownHtml: () => { throw new Error('Program evidence must remain code'); },
    });
    const result = node.querySelector<HTMLDetailsElement>('.ptc-result')!;
    expect(result.querySelector('summary')?.textContent)
      .toBe(status === 'completed' ? 'Evidence for judgment' : 'Program result');
    expect(result.open).toBe(true);
    expect(node.querySelector<HTMLDetailsElement>('.ptc-code')?.open).toBe(false);
    expect(node.textContent).toContain(status === 'completed' ? 'Returned for model judgment' : 'Failed');
    expect(node.querySelector('script')).toBeNull();
    expect(node.textContent).toContain('<script>unsafe()</script>');
    expect(JSON.stringify(block)).toBe(before);
  } finally { dom.window.close(); }
});
