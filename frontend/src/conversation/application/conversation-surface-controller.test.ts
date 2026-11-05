// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { createConversationSurfaceController } from './conversation-surface-controller';
import { createTurnState, createTurnStore } from '../domain/turn-store';
import { createAnimationFrameScheduler } from '../ui/animation-frame-scheduler';
import { createClassicConversationRenderers } from '../ui/classic-conversation-renderers';
import type { TurnRecord } from '../../api/conversation-sync.generated';

const cleanups: Array<() => void> = [];


function answer(revision: number, status: 'running' | 'completed'): TurnRecord {
  return { turnId: 'answer', presentationId: 'answer', conversationId: 'conv',
    laneId: 'main', ordinal: 1, actor: 'assistant', kind: 'reply', runId: '',
    status, currentAttemptId: 'attempt', projectionRevision: revision,
    createdAt: 1, updatedAt: revision, settlement: {},
    projection: { content: status === 'completed' ? 'Complete final answer' : '' } };
}
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
    onConversationMounted: conv => {
      mounts.push(conv.id);
      expect(container.querySelector('.conversation-surface')).not.toBeNull();
      expect(controller.ownsConversation(conv.id)).toBe(false);
    },
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

it('retains the newest canonical graph and retries a failed scheduled commit without new events', () => {
  const container = document.createElement('main'); document.body.append(container);
  const frames: Array<() => void> = [];
  const commits: string[] = [];
  let fail = false;
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  const controller = createConversationSurfaceController({
    isActive: () => true, getContainer: () => container,
    schedule: render => { frames.push(render); return () => {}; },
    nativeRenderers: createClassicConversationRenderers({ renderSafeMarkdownHtml: value => value }),
    costSignatureSnapshot: () => {
      if (fail) { fail = false; throw new Error('transient presentation failure'); }
      return new Map();
    },
    afterConversationCommit: (_conv, state) => { commits.push(state.turnsById.answer!.status); },
  });
  const store = createTurnStore('conv');
  const unsubscribe = store.subscribe((state, update) => controller.render({ id: 'conv' }, state, { update }));
  try {
    store.dispatch({ type: 'snapshot', snapshot: { conversationRevision: 1, turns: [answer(1, 'running')] } });
    frames.shift()!();
    expect(container.textContent).toContain('Waiting for an execution status update');
    fail = true;
    store.dispatch({ type: 'snapshot', snapshot: { conversationRevision: 2, turns: [answer(2, 'completed')] } });
    expect(() => frames.shift()!()).not.toThrow();
    expect(store.getState().turnsById.answer!.status).toBe('completed');
    expect(controller.ownsConversation('conv')).toBe(false);
    expect(frames).toHaveLength(1);
    frames.shift()!();
    expect(container.textContent).toContain('Complete final answer');
    expect(container.textContent).not.toContain('Waiting for an execution status update');
    expect(controller.ownsConversation('conv')).toBe(true);
    expect(commits).toEqual(['running', 'completed']);
    expect(warning).toHaveBeenCalledTimes(1);
  } finally { unsubscribe(); controller.dispose(); container.remove(); warning.mockRestore(); }
});

it('a withheld animation frame cannot strand the final graph or commit it twice', () => {
  vi.useFakeTimers();
  const container = document.createElement('main'); document.body.append(container);
  const frames: Array<FrameRequestCallback> = [];
  const commits = vi.fn();
  const scheduler = createAnimationFrameScheduler({
    requestAnimationFrame: callback => { frames.push(callback); return frames.length; },
    cancelAnimationFrame: () => {},
    setTimeout: (callback, delay) => window.setTimeout(callback, delay),
    clearTimeout: handle => window.clearTimeout(handle),
  });
  const controller = createConversationSurfaceController({
    isActive: () => true, getContainer: () => container,
    schedule: render => scheduler.schedule(render), afterConversationCommit: commits,
  });
  const state = createTurnState('conv'); state.turnsById.answer = answer(2, 'completed');
  state.laneOrder.main = ['answer'];
  try {
    controller.render({ id: 'conv' }, state);
    vi.advanceTimersByTime(250);
    expect(container.textContent).toContain('Complete final answer');
    expect(commits).toHaveBeenCalledTimes(1);
    frames[0](Date.now());
    expect(commits).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  } finally { controller.dispose(); container.remove(); vi.useRealTimers(); }
});

it('ownership is false while an accepted graph is uncommitted or its host was replaced', () => {
  const f = controllerFixture(); f.paint();
  expect(f.controller.ownsConversation('conv')).toBe(false);
  f.flush(); expect(f.controller.ownsConversation('conv')).toBe(true);
  f.paint(); expect(f.controller.ownsConversation('conv')).toBe(false);
  f.flush(); f.replaceContainer();
  expect(f.controller.ownsConversation('conv')).toBe(false);
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

it.each(['latest', 'reveal', 'disclosure'] as const)(
  '%s flush retries a transient failure through the same commit owner', action => {
    const container = document.createElement('main'); document.body.append(container);
    const frames: Array<() => void> = [];
    let fail = false;
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const controller = createConversationSurfaceController({
      isActive: id => id === 'conv', getContainer: () => container,
      schedule: render => { frames.push(render); return () => {}; },
      costSignatureSnapshot: () => {
        if (fail) { fail = false; throw new Error('one-shot explicit flush failure'); }
        return new Map();
      },
    });
    const running = createTurnState('conv');
    running.turnsById.answer = { ...answer(1, 'running'), projection: { translatedContent: 'Preview' } };
    running.laneOrder.main = ['answer'];
    const completed = createTurnState('conv');
    completed.turnsById.answer = answer(2, 'completed'); completed.laneOrder.main = ['answer'];
    try {
      controller.render({ id: 'conv' }, running); frames.shift()!();
      const button = document.createElement('button');
      button.dataset.conversationAction = 'translate'; button.dataset.operation = 'show-original';
      container.querySelector('[data-turn-id="answer"]')!.append(button);
      fail = true; controller.render({ id: 'conv' }, completed);
      const cancelled = frames.shift()!;
      if (action === 'latest') expect(() => controller.followLatest()).not.toThrow();
      else if (action === 'reveal') expect(controller.revealTurn('conv', 'answer')).toBe(false);
      else button.click();
      cancelled();
      expect(controller.ownsConversation('conv')).toBe(false);
      expect(frames).toHaveLength(1);
      frames.shift()!();
      expect(container.textContent).toContain('Complete final answer');
      expect(controller.ownsConversation('conv')).toBe(true);
    } finally { controller.dispose(); container.remove(); warning.mockRestore(); }
  },
);

it.each(['scroll', 'mount', 'receipt'] as const)(
  'does not acknowledge a commit before its required %s callback succeeds', stage => {
    const container = document.createElement('main'); document.body.append(container);
    const frames: Array<() => void> = [];
    const mounts = vi.fn();
    const receipts = vi.fn();
    let failed = false;
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const check = (part: typeof stage): void => {
      if (stage === part && !failed) { failed = true; throw new Error(`one-shot ${part} failure`); }
    };
    const controller = createConversationSurfaceController({
      isActive: () => true, getContainer: () => container,
      schedule: render => { frames.push(render); return () => {}; },
      restoreScroll: () => check('scroll'),
      onConversationMounted: () => { mounts(); check('mount'); },
      afterConversationCommit: () => { receipts(); check('receipt'); },
    });
    const state = createTurnState('conv');
    state.turnsById.answer = answer(2, 'completed'); state.laneOrder.main = ['answer'];
    try {
      controller.render({ id: 'conv' }, state); frames.shift()!();
      expect(controller.ownsConversation('conv')).toBe(false);
      expect(frames).toHaveLength(1);
      frames.shift()!();
      expect(controller.ownsConversation('conv')).toBe(true);
      expect(container.textContent).toContain('Complete final answer');
      expect(mounts).toHaveBeenCalledTimes(stage === 'mount' ? 2 : 1);
      expect(receipts).toHaveBeenCalledTimes(stage === 'receipt' ? 2 : 1);
      expect(frames).toHaveLength(0);
    } finally { controller.dispose(); container.remove(); warning.mockRestore(); }
  },
);

it.each([false, true])('new conversation owns its own failure repair budget (dispose=%s)', dispose => {
  const container = document.createElement('main'); document.body.append(container);
  const frames: Array<() => void> = [];
  let active = 'conv'; let secondFailed = false;
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  const controller = createConversationSurfaceController({
    isActive: id => id === active, getContainer: () => container,
    schedule: render => { frames.push(render); return () => {}; },
    costSignatureSnapshot: state => {
      if (state.conversationId === 'conv' || !secondFailed) {
        if (state.conversationId !== 'conv') secondFailed = true;
        throw new Error('conversation-local render failure');
      }
      return new Map();
    },
  });
  const flush = () => frames.splice(0).forEach(render => render());
  const first = createTurnState('conv');
  const second = createTurnState('other');
  second.turnsById.answer = { ...answer(2, 'completed'), conversationId: 'other' };
  second.laneOrder.main = ['answer'];
  try {
    controller.render({ id: 'conv' }, first); flush();
    expect(frames).toHaveLength(1);
    if (dispose) controller.disposeConversation('conv');
    active = 'other'; controller.render({ id: 'other' }, second); flush();
    expect(controller.ownsConversation('other')).toBe(false);
    expect(frames).toHaveLength(1);
    flush();
    expect(container.textContent).toContain('Complete final answer');
    expect(controller.ownsConversation('other')).toBe(true);
    expect(container.querySelector('[data-conversation-id="conv"]')).toBeNull();
  } finally { controller.dispose(); container.remove(); warning.mockRestore(); }
});

it.each([false, true])('a new canonical graph arriving during the budgeted failing repair is scheduled (switch=%s)', switchConversation => {
  const container = document.createElement('main'); document.body.append(container);
  const frames: Array<() => void> = [];
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  let active = 'conv'; let failures = 0;
  const first = createTurnState('conv');
  const nextId = switchConversation ? 'other' : 'conv';
  const next = createTurnState(nextId);
  next.turnsById.answer = { ...answer(2, 'completed'), conversationId: nextId };
  next.laneOrder.main = ['answer'];
  const controller = createConversationSurfaceController({
    isActive: id => id === active, getContainer: () => container,
    schedule: render => { frames.push(render); return () => {}; },
    costSignatureSnapshot: state => {
      if (state === first) {
        failures += 1;
        if (failures === 2) {
          active = nextId;
          expect(controller.render({ id: nextId }, next)).toBe(true);
        }
        throw new Error('failed old canonical graph');
      }
      return new Map();
    },
  });
  try {
    controller.render({ id: 'conv' }, first);
    frames.shift()!(); frames.shift()!();
    expect(failures).toBe(2);
    expect(frames).toHaveLength(1);
    frames.shift()!();
    expect(container.textContent).toContain('Complete final answer');
    expect(controller.ownsConversation(nextId)).toBe(true);
    expect(frames).toHaveLength(0);
  } finally { controller.dispose(); container.remove(); warning.mockRestore(); }
});

it('a persistent render failure retains demand without an unbounded callback loop', () => {
  const container = document.createElement('main'); document.body.append(container);
  const frames: Array<() => void> = [];
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  const controller = createConversationSurfaceController({
    isActive: () => true, getContainer: () => container,
    schedule: render => { frames.push(render); return () => {}; },
    costSignatureSnapshot: () => { throw new Error('persistent failure'); },
  });
  try {
    controller.render({ id: 'conv' }, createTurnState('conv'));
    frames.shift()!(); frames.shift()!();
    expect(frames).toHaveLength(0);
    expect(controller.ownsConversation('conv')).toBe(false);
    controller.render({ id: 'conv' }, createTurnState('conv')); frames.shift()!();
    expect(frames).toHaveLength(0);
  } finally { controller.dispose(); container.remove(); warning.mockRestore(); }
});

it.each(['reveal', 'latest'] as const)('a direct %s window commit shares the bounded failure owner', action => {
  const container = document.createElement('main'); document.body.append(container);
  const frames: Array<() => void> = [];
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  let failTurn = ''; let following = true;
  const renderers = createClassicConversationRenderers({ renderSafeMarkdownHtml: value => value });
  const original = renderers.renderBlock!;
  renderers.renderBlock = (node, block, context) => {
    if (context.turn.turnId === failTurn) {
      failTurn = ''; throw new Error('one-shot navigation renderer failure');
    }
    original(node, block, context);
  };
  const controller = createConversationSurfaceController({
    isActive: () => true, getContainer: () => container,
    schedule: render => { frames.push(render); return () => {}; },
    nativeRenderers: renderers, windowing: { maxTurns: 1 },
    scrollAnchor: {
      get following() { return following; }, capture: () => null, restore: () => {},
      suspendFollow: () => { following = false; }, followLatest: () => { following = true; },
    },
  });
  const state = createTurnState('conv');
  for (const [index, id] of ['older', 'latest'].entries()) {
    state.turnsById[id] = { ...answer(2, 'completed'), turnId: id, presentationId: id, ordinal: index + 1,
      projection: { content: `Complete ${id} answer` } };
  }
  state.laneOrder.main = ['older', 'latest'];
  try {
    controller.render({ id: 'conv' }, state); frames.shift()!();
    expect(controller.ownsConversation('conv')).toBe(true);
    if (action === 'latest') expect(controller.revealTurn('conv', 'older')).toBe(true);
    failTurn = action === 'reveal' ? 'older' : 'latest';
    if (action === 'reveal') expect(controller.revealTurn('conv', 'older')).toBe(false);
    else expect(() => controller.followLatest()).not.toThrow();
    expect(controller.ownsConversation('conv')).toBe(false);
    expect(frames).toHaveLength(1); frames.shift()!();
    expect(container.textContent).toContain(`Complete ${action === 'reveal' ? 'older' : 'latest'} answer`);
    expect(controller.ownsConversation('conv')).toBe(true);
    expect(frames).toHaveLength(0);
  } finally { controller.dispose(); container.remove(); warning.mockRestore(); }
});

it('required commit callbacks cannot recursively flush their own unacknowledged frame', () => {
  const container = document.createElement('main'); document.body.append(container);
  const frames: Array<() => void> = [];
  let calls = 0;
  const controller = createConversationSurfaceController({
    isActive: () => true, getContainer: () => container,
    schedule: render => { frames.push(render); return () => {}; },
    afterConversationCommit: () => {
      calls += 1;
      if (calls > 2) throw new Error('recursive commit');
      controller.followLatest();
    },
  });
  try {
    controller.render({ id: 'conv' }, createTurnState('conv')); frames.shift()!();
    expect(calls).toBe(1); expect(frames).toHaveLength(0);
    expect(controller.ownsConversation('conv')).toBe(true);
  } finally { controller.dispose(); container.remove(); }
});

it('presentation invalidation during a required callback survives this frame acknowledgement', () => {
  const container = document.createElement('main'); document.body.append(container);
  const frames: Array<() => void> = [];
  const receipts = vi.fn();
  const controller = createConversationSurfaceController({
    isActive: () => true, getContainer: () => container,
    schedule: render => { frames.push(render); return () => {}; },
    onConversationMounted: () => controller.setArtifacts('conv', new Map()),
    afterConversationCommit: receipts,
  });
  try {
    controller.render({ id: 'conv' }, createTurnState('conv')); frames.shift()!();
    expect(controller.ownsConversation('conv')).toBe(false);
    expect(frames).toHaveLength(1); frames.shift()!();
    expect(receipts).toHaveBeenCalledTimes(2);
    expect(controller.ownsConversation('conv')).toBe(true);
  } finally { controller.dispose(); container.remove(); }
});

it('an absent host retains demand without self-rescheduling until explicit remount', () => {
  const container = document.createElement('main'); document.body.append(container);
  let mounted = true;
  const frames: Array<() => void> = [];
  const controller = createConversationSurfaceController({
    isActive: () => true, getContainer: () => mounted ? container : null,
    schedule: render => { frames.push(render); return () => {}; },
  });
  try {
    controller.render({ id: 'conv' }, createTurnState('conv')); mounted = false;
    frames.shift()!();
    expect(frames).toHaveLength(0);
    expect(controller.ownsConversation('conv')).toBe(false);
    mounted = true; controller.render({ id: 'conv' }, createTurnState('conv')); frames.shift()!();
    expect(controller.ownsConversation('conv')).toBe(true);
    expect(frames).toHaveLength(0);
  } finally { controller.dispose(); container.remove(); }
});

it('persistent mount setup failure cannot reset the same host repair budget', () => {
  const container = document.createElement('main'); document.body.append(container);
  const frames: Array<() => void> = [];
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  const controller = createConversationSurfaceController({
    isActive: () => true, getContainer: () => container,
    schedule: render => { frames.push(render); return () => {}; },
    getScrollViewport: () => { throw new Error('persistent mount setup failure'); },
  });
  try {
    controller.render({ id: 'conv' }, createTurnState('conv'));
    frames.shift()!(); frames.shift()!();
    expect(frames).toHaveLength(0);
    expect(controller.ownsConversation('conv')).toBe(false);
    controller.render({ id: 'conv' }, createTurnState('conv')); frames.shift()!();
    expect(frames).toHaveLength(0);
  } finally { controller.dispose(); container.remove(); warning.mockRestore(); }
});

it('keeps translation previews of unmounted conversations until they settle or are disposed', () => {
  const f = controllerFixture(true);
  const preview = { status: 'pending' as const, partialByRound: { 'text:llm-1': '正在检查日志' } };
  const running = createTurnState('conv');
  running.turnsById.answer = { ...answer(1, 'running'), projection: {
    segments: [{ type: 'text', blockId: 'text:llm-1', text: 'Checking the logs' }] } };
  running.laneOrder.main = ['answer'];
  const open = () => { f.activate('conv'); f.controller.render({ id: 'conv' }, running); };
  const away = () => { f.activate('other'); f.paint('other'); };

  open(); away();
  const commits = f.commits.length;
  f.controller.setTranslationActivity('conv', 'answer', preview);
  expect(f.commits).toHaveLength(commits);
  open();
  expect(f.container.textContent).toContain('正在检查日志');

  away(); open();
  expect(f.container.textContent).toContain('正在检查日志');

  away();
  f.controller.setTranslationActivity('conv', 'answer', null);
  open();
  expect(f.container.textContent).not.toContain('正在检查日志');

  f.controller.setTranslationActivity('conv', 'answer', preview);
  f.controller.disposeConversation('conv'); open();
  expect(f.container.textContent).toContain('Checking the logs');
  expect(f.container.textContent).not.toContain('正在检查日志');
});
