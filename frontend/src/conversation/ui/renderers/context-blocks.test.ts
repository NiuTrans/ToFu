/** Origin blocks add facts the Turn header cannot carry; they never repeat its label. */
import { describe, expect, it, vi } from 'vitest';
import type { ConversationBlockViewModel } from '../../presentation/conversation-view-model';
import { renderOriginBlock } from './context-blocks';
import type { ClassicConversationRendererPorts } from './ports';

const { JSDOM } = await vi.importActual<{
  JSDOM: new (markup: string) => { window: Pick<Window, 'document' | 'close'> };
}>('jsdom');

type OriginBlock = Extract<ConversationBlockViewModel, { kind: 'origin' }>;

function render(value: OriginBlock['value']) {
  const dom = new JSDOM('<!doctype html><body><div></div></body>');
  const node = dom.window.document.querySelector('div')!;
  const block = { blockId: 'origin', kind: 'origin', identitySource: 'contract', source: value, value } as OriginBlock;
  renderOriginBlock(node, block, {} as ClassicConversationRendererPorts);
  return node;
}

describe('renderOriginBlock', () => {
  it.each(['brain', 'proactive', 'timer', 'swarm'] as const)(
    'renders no label-only banner for a %s origin', (initiator) => {
      const node = render({ blockId: 'origin', initiator });
      expect(node.childElementCount).toBe(0);
      expect(node.hidden).toBe(true);
    });

  it('keeps the peer source and Brain dispatch facts', () => {
    expect(render({ blockId: 'origin', initiator: 'peer', sourceConversationId: 'conv-1234567890' })
      .querySelector('.peer-msg-from')?.textContent).toBe('conv conv-123');
    const dispatch = render({
      blockId: 'origin', initiator: 'brain', brain: { epicTitle: 'Epic', method: 'route' },
    } as OriginBlock['value']);
    expect(dispatch.hidden).toBe(false);
    expect(dispatch.querySelector('.brain-dispatch-card .bdc-epic-title')?.textContent).toBe('Epic');
  });
});
