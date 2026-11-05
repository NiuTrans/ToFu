/** Automatic leases remain reusable after caller cancellation and terminal delivery. */
import { describe, expect, it, vi } from 'vitest';
import { createTurnAttemptLifecycle } from './turn-attempt-lifecycle';
import { createTurnStore } from './turn-state';

describe('attempt lifecycle transport leases', () => {
  it('rearms after a terminal frame whose caller already withdrew its callback', () => {
    const coordinator = { cursor: 'cursor', resume: vi.fn(async () => {}) };
    const owner = createTurnAttemptLifecycle(createTurnStore('conv'), {
      coordinator, terminalStatuses: new Set(['completed']), onTerminal: vi.fn(),
    });
    try {
      const first = owner.connectOnce('attempt');
      owner.connect('attempt', { onTerminal: vi.fn() }).close();

      owner.deliverTerminal({ conversationId: 'conv', turnId: 'turn', attemptId: 'attempt',
        seq: 1, projectionRevision: 1, type: 'terminal_settlement', payload: { status: 'completed' } });
      const next = owner.connectOnce('attempt');

      expect(next).not.toBe(first);
      expect(coordinator.resume).toHaveBeenCalledTimes(3);
    } finally { owner.dispose(); }
  });

  it('claims its automatic lease before a transport callback can reenter', () => {
    let nested: unknown;
    const coordinator = { cursor: 'cursor', resume: vi.fn(async () => {
      if (!nested) nested = owner.connectOnce('attempt');
    }) };
    const owner = createTurnAttemptLifecycle(createTurnStore('conv'), {
      coordinator, terminalStatuses: new Set(['completed']), onTerminal: vi.fn(),
    });
    try {
      const first = owner.connectOnce('attempt');
      expect(nested).toBe(first);
      expect(coordinator.resume).toHaveBeenCalledTimes(1);
    } finally { owner.dispose(); }
  });
});
