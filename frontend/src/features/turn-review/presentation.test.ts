import { expect, it, vi } from 'vitest';
import { createTurnReviewPresentation, type TurnReviewPresentationPorts } from './presentation';
import type { TurnReviewPorts } from '../../rich-response/artifact-review';

it('fences late imports and retires mounts on navigation, deletion and shutdown', async () => {
  let active = 'one'; let resolve!: (value: { openTurnReviewDialog: typeof open }) => void;
  const close = vi.fn(); const open = vi.fn(() => close);
  const loadDialog = vi.fn().mockImplementationOnce(() => new Promise(done => { resolve = done; }))
    .mockResolvedValue({ openTurnReviewDialog: open });
  const ports = { activeConversationId: () => active, loadDialog, reportError: vi.fn(), text: (_key: string, fallback: string) => fallback } as unknown as TurnReviewPresentationPorts;
  const owner = createTurnReviewPresentation(ports);
  const pending = owner.open('one', 'turn'); active = 'two'; owner.activate(active);
  resolve({ openTurnReviewDialog: open }); await pending; expect(open).not.toHaveBeenCalled();
  await owner.open('two', 'turn'); expect(open).toHaveBeenCalledOnce();
  owner.observe('two', { turn: {} }); expect(close).not.toHaveBeenCalled();
  owner.observe('two', {}); expect(close).toHaveBeenCalledOnce();
  await owner.open('two', 'turn'); owner.disposeConversation('one'); expect(close).toHaveBeenCalledOnce();
  owner.activate(null); expect(close).toHaveBeenCalledTimes(2);
  owner.destroy(); expect(close).toHaveBeenCalledTimes(2);
  await owner.open('two', 'turn'); expect(open).toHaveBeenCalledTimes(2);
});

it('invalidates only the mounted execution identity and ignores metadata, other Turns and retired listeners', async () => {
  const notifications = [vi.fn(), vi.fn()];
  const releases: Array<() => void> = [];
  let opened = 0;
  const open = vi.fn((ports: TurnReviewPorts) => {
    const release = ports.subscribeInvalidation!(notifications[opened++]); releases.push(release);
    return release;
  });
  const owner = createTurnReviewPresentation({ activeConversationId: () => 'conv',
    loadDialog: async () => ({ openTurnReviewDialog: open }), text: (_key: string, fallback: string) => fallback,
  } as unknown as TurnReviewPresentationPorts);
  await owner.open('conv', 'one');
  const turn = { currentAttemptId: 'attempt', modelContextRevision: 1, projectionRevision: 2, status: 'running' };
  owner.observe('conv', { one: turn }); expect(notifications[0]).toHaveBeenCalledOnce();
  for (let n = 0; n < 1000; n++) owner.observe('conv', {
    one: { ...turn, projectionRevision: n + 3 }, unrelated: { modelContextRevision: n },
  });
  owner.observe('foreign', { one: { ...turn, modelContextRevision: 2 } });
  expect(notifications[0]).toHaveBeenCalledOnce();
  owner.observe('conv', { one: { ...turn, modelContextRevision: 2 } });
  owner.observe('conv', { one: { ...turn, currentAttemptId: 'retry' } });
  owner.observe('conv', { one: { ...turn, status: 'completed' } });
  expect(notifications[0]).toHaveBeenCalledTimes(4);
  await owner.open('conv', 'two'); releases[0]();
  owner.observe('conv', { two: turn }); expect(notifications[1]).toHaveBeenCalledOnce();
  owner.destroy(); owner.observe('conv', { two: { ...turn, modelContextRevision: 5 } });
  expect(notifications[1]).toHaveBeenCalledOnce();
});
