// @vitest-environment jsdom
import { expect, it } from 'vitest';
import { createConversationSurfaceController } from './conversation-surface-controller';
import { createTurnState } from '../domain/turn-store';

it('forced paints read each visible attempt once; explicit invalidation refreshes it', async () => {
  const container = document.createElement('main');
  document.body.append(container);
  let scheduled: (() => void) | undefined;
  let reads = 0;
  const controller = createConversationSurfaceController({
    isActive: () => true,
    getContainer: () => container,
    schedule: (render) => { scheduled = render; return () => { scheduled = undefined; }; },
    loadTaskTimePresentations: async () => { reads += 1; return { items: [], nextCursor: '' }; },
  });
  const state = createTurnState('conv');
  state.turnsById.turn = { turnId: 'turn', conversationId: 'conv', actor: 'assistant',
    laneId: 'main', ordinal: 1, status: 'completed', currentAttemptId: 'attempt',
    presentationId: 'turn', kind: 'message', runId: '', settlement: {}, createdAt: 1, updatedAt: 1,
    projectionRevision: 1, projection: { segments: [{ type: 'text', blockId: 'text', text: 'Answer' }] } };
  state.laneOrder.main = ['turn'];
  state.attemptsById.attempt = { attemptId: 'attempt', startedAt: 1 };
  const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
  try {
    for (let i = 0; i < 25; i += 1) {
      controller.render({ id: 'conv' }, state, { force: true });
      scheduled?.();
      await settle();
    }
    expect(container.querySelector('[data-turn-id="turn"]')).not.toBeNull();
    expect(reads).toBe(1);
    controller.invalidateTaskTimePresentations('other');
    await settle();
    expect(reads).toBe(1);
    controller.invalidateTaskTimePresentations('conv');
    await settle();
    expect(reads).toBe(2);
    controller.dispose();
    controller.invalidateTaskTimePresentations('conv');
    await settle();
    expect(reads).toBe(2);
  } finally { controller.dispose(); container.remove(); }
});
