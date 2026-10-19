// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { createConversationSurfaceController } from './conversation-surface-controller';
import { createTurnState } from '../domain/turn-store';

const cleanups: Array<() => void> = [];
afterEach(() => { cleanups.splice(0).reverse().forEach(dispose => dispose()); });

function controllerFixture(synchronous = false, cancel = () => {}) {
  let container = document.createElement('main'); document.body.append(container);
  let active = 'conv';
  const frames: Array<() => void> = [];
  const commits: Array<{ id: string; force: boolean }> = [];
  const mounts: string[] = [];
  const controller = createConversationSurfaceController({
    isActive: id => id === active, getContainer: () => container,
    schedule: render => { if (synchronous) render(); else frames.push(render); return cancel; },
    afterConversationCommit: (conv, _state, force) => { commits.push({ id: conv.id, force }); },
    onConversationMounted: conv => { mounts.push(conv.id); expect(controller.ownsConversation(conv.id)).toBe(true); },
  });
  cleanups.push(() => { controller.dispose(); container.remove(); });
  return { controller, frames, commits, mounts, get container() { return container; },
    activate: (id: string) => { active = id; },
    replaceContainer: () => {
      const previous = container;
      container = document.createElement('main'); document.body.append(container);
      cleanups.push(() => previous.remove());
    },
    paint: (id = active, force = false) => controller.render({ id }, createTurnState(id), { force }),
    flush: () => { frames.splice(0).forEach(render => render()); },
  };
}

it('does not lose forced invalidation when later graph frames coalesce', () => {
  const f = controllerFixture(); f.paint('conv', true); f.paint(); f.flush();
  expect(f.commits).toEqual([{ id: 'conv', force: true }]);
});

it('supports synchronous render scheduling across repeated updates', () => {
  const f = controllerFixture(true); f.paint(); f.paint(); f.paint();
  expect(f.commits).toHaveLength(3);
});

it('rejects a graph belonging to another conversation', () => {
  const f = controllerFixture();
  expect(f.controller.render({ id: 'conv' }, createTurnState('other'))).toBe(false);
  f.flush(); expect(f.commits).toHaveLength(0);
});

it('mounts into the current container even when the previous host stays connected', () => {
  const f = controllerFixture(); f.paint(); f.flush();
  const old = f.container;
  f.replaceContainer(); f.paint(); f.flush();
  expect(f.container.querySelector('.conversation-surface')).not.toBeNull();
  expect(old.querySelector('.conversation-surface')).toBeNull();
});

it('notifies artifact readers only after an active Surface mounts, including warm navigation', () => {
  const f = controllerFixture(); f.paint();
  expect(f.mounts).toEqual([]);
  f.flush(); f.paint(); f.flush();
  expect(f.mounts).toEqual(['conv']);
  f.activate('other'); f.paint(); f.flush();
  f.activate('conv'); f.paint(); f.flush();
  expect(f.mounts).toEqual(['conv', 'other', 'conv']);
  f.replaceContainer(); f.paint(); f.flush();
  expect(f.mounts).toEqual(['conv', 'other', 'conv', 'conv']);
});

it('ignores presentation updates from a conversation that is no longer active', () => {
  const f = controllerFixture(); f.paint(); f.flush(); f.activate('other');
  f.controller.setArtifacts('conv', new Map());
  f.controller.setExpandedBranchLane('conv', 'branch'); f.flush();
  expect(f.commits).toHaveLength(1);
});

it('a cancelled frame cannot consume a newer conversation pending paint', () => {
  const f = controllerFixture(); f.paint();
  const late = f.frames.shift()!;
  f.controller.disposeConversation('conv'); f.activate('other'); f.paint();
  late(); expect(f.commits).toHaveLength(0);
  f.flush(); expect(f.commits.map(c => c.id)).toEqual(['other']);
});

it('coalesces 100 presentation updates with the latest graph into one commit', () => {
  const f = controllerFixture(); f.paint(); f.flush();
  for (let i = 0; i < 100; i += 1) f.controller.setArtifacts('conv', new Map());
  f.paint();
  expect(f.commits).toHaveLength(1);
  expect(f.frames).toHaveLength(1);
  f.flush();
  expect(f.commits).toEqual([{ id: 'conv', force: false }, { id: 'conv', force: true }]);
});

it('explicit latest intent flushes the active pending graph and fences its old delivery', () => {
  const f = controllerFixture(); f.paint();
  f.controller.followLatest();
  expect(f.commits).toHaveLength(1);
  f.paint();
  f.frames.shift()!();
  expect(f.commits).toHaveLength(1);
  f.flush(); expect(f.commits).toHaveLength(2);
});

it('disposes the mounted surface even if the pending scheduler cancellation throws', () => {
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  try {
    const f = controllerFixture(false, () => { throw new Error('closed host'); });
    f.paint(); f.flush(); f.paint();
    expect(() => f.controller.dispose()).not.toThrow(); f.flush();
    expect(f.container.childElementCount).toBe(0);
    expect(f.commits).toHaveLength(1);
    expect(warning).toHaveBeenCalledTimes(1);
  } finally { warning.mockRestore(); }
});

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
