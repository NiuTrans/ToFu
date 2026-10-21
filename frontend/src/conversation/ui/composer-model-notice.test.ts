import { expect, it, vi } from 'vitest';
import type { ConversationTurnViewModel, ConversationViewModel } from '../presentation/conversation-view-model';
import { renderComposerModelNotice } from './composer-model-notice';
const { JSDOM } = await vi.importActual<{ JSDOM: new (html: string) => {
  window: Pick<Window, 'document' | 'close'>;
} }>('jsdom');

it('keeps a live switch visible before route settlement and after reload', () => {
  const dom = new JSDOM('<!doctype html><body></body>');
  try {
    const node = dom.window.document.createElement('header');
    const turn = { actor: 'assistant', status: 'running', source: {
      projection: { lastRoundUsage: { model: 'gpt-6.1-sol', resolvedModel: 'gpt-6.1-sol' } },
    }, metadata: { fallback: { from: 'gpt-6.1-sol', model: 'kimi-k3',
      reason: 'generic: SSE error: cyber_policy <script>literal</script>' }, fallbackInTimeline: true },
    } as unknown as ConversationTurnViewModel;
    const view = (value: ConversationTurnViewModel) => ({ conversationId: 'A', mainLane: { turns: [value] } }) as unknown as ConversationViewModel;
    renderComposerModelNotice(node, view(turn));
    const stable = node.firstChild;
    renderComposerModelNotice(node, view(turn));
    expect(node.firstChild).toBe(stable);
    expect(node.textContent).toContain('Automatically switched: gpt-6.1-sol → kimi-k3');
    expect(node.textContent).toContain('cyber_policy <script>literal</script>');
    expect(node.querySelector('script')).toBeNull();
    expect(node.querySelector('[role="status"]')).not.toBeNull();
    node.replaceChildren();
    const replay = JSON.parse(JSON.stringify(turn));
    replay.status = 'completed';
    replay.source.projection.lastRoundUsage = { model: 'kimi-k3', resolvedModel: 'kimi-k3', providerId: 'sankuai' };
    renderComposerModelNotice(node, view(replay));
    expect(node.textContent).toContain('Automatically switched: gpt-6.1-sol → kimi-k3');
    expect(node.textContent).toContain('Model: kimi-k3 · sankuai');
    node.replaceChildren();
    replay.metadata.fallback = undefined;
    renderComposerModelNotice(node, view(replay));
    expect(node.textContent).toBe('Model: kimi-k3');
    expect(node.querySelector('[role="status"]')).toBeNull();
    node.replaceChildren();
    replay.source.projection = { model: 'gpt-6.1-sol' };
    renderComposerModelNotice(node, view(replay));
    expect(node.childElementCount).toBe(0);
    replay.actor = 'human';
    renderComposerModelNotice(node, view(replay));
    expect(node.childElementCount).toBe(0);
    expect(node.hidden).toBe(true);
    renderComposerModelNotice(node, null);
    expect(node.dataset.conversationId).toBe('');
  } finally { dom.window.close(); }
});
