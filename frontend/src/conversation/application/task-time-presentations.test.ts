import { expect, it } from 'vitest';
import { createTaskTimePresentations } from './task-time-presentations';
import type { AutomationManagementPresentationPage } from '../../api/automation.generated';

it('coalesces mount reads, preserves a pending wake and cancels a previous visit', async () => {
  const calls: Array<{ signal: AbortSignal; resolve(page: AutomationManagementPresentationPage): void }> = [];
  let paints = 0;
  const loader = createTaskTimePresentations({
    load: (_conversation, _attempt, signal) => new Promise((resolve) => calls.push({ signal, resolve })),
    changed: () => { paints += 1; },
  });
  loader.refresh('conv', ['attempt']);
  loader.refresh('conv', ['attempt']);
  expect(calls).toHaveLength(1);
  loader.refresh('conv', ['attempt'], true);
  calls[0].resolve({ items: [], nextCursor: '' });
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(calls).toHaveLength(2);
  loader.refresh('next', ['next-attempt']);
  expect(calls[1].signal.aborted).toBe(true);
  calls[1].resolve({ items: [{ id:'a'.repeat(64),runId:'run',stepId:'show',conversationId:'conv',
    attemptId:'attempt',turnId:'turn',output:{text:'old',startedAt:1,timezone:'UTC'} }],nextCursor:'' });
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(loader.values.size).toBe(0);
  expect(paints).toBe(0);
  loader.refresh('next', []);
  expect(calls[2].signal.aborted).toBe(true);
  loader.dispose();
});
