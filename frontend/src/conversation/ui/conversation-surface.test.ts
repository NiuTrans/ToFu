// @vitest-environment jsdom
/** Semantic latest navigation and commit membership, independent of DOM paging. */
import { expect, it } from 'vitest';
import { createTurnState } from '../domain/turn-store';
import { selectConversationViewModel } from '../presentation/conversation-view-model';
import {
  createConversationSurface,
  type ConversationSurface,
  type ConversationSurfaceOptions,
} from './conversation-surface';

it('restores the logical tail, retains a bounded working set and avoids redundant commits', () => {
  const container = document.createElement('main'); document.body.append(container);
  const state = createTurnState('conv');
  for (let i = 0; i < 30; i += 1) {
    const id = `turn-${i}`;
    state.turnsById[id] = { turnId: id, presentationId: id, runId: '', conversationId: 'conv', laneId: 'main', ordinal: i,
      actor: 'assistant', kind: 'reply', status: 'completed', currentAttemptId: null,
      projectionRevision: 1, createdAt: i, updatedAt: i, projection: { content: id }, settlement: {} };
    (state.laneOrder.main ??= []).push(id);
  }
  const commits: Array<ReadonlyArray<string>> = [];
  let following = true;
  const surface = createConversationSurface(container, {
    windowing: { maxTurns: 8, batchSize: 3 },
    scrollAnchor: {
      get following() { return following; },
      capture: () => null, restore: () => {}, isNearBottom: () => true,
      suspendFollow: () => { following = false; },
      followLatest: () => { following = true; },
    },
    onCommit: ids => { commits.push(ids); },
  });
  try {
    surface.render(selectConversationViewModel(state));
    expect(surface.mountedTurnIds).toEqual(state.laneOrder.main!.slice(-8));
    for (let i = 0; i < 100; i += 1) surface.followLatest({ force: false });
    expect(commits).toHaveLength(1);
    surface.showEarlier();
    expect(surface.isNearLatest()).toBe(false); // Physical bottom is still true.
    const earlier = surface.mountedTurnIds;
    surface.followLatest({ force: false });
    expect(surface.mountedTurnIds).toBe(earlier);
    surface.followLatest();
    expect(surface.isNearLatest()).toBe(true);
    expect(surface.mountedTurnIds).toEqual(state.laneOrder.main!.slice(-8));
    expect(following).toBe(true);
    expect(commits.every(ids => ids.length <= 8)).toBe(true);
    expect(state.laneOrder.main).toHaveLength(30);
    surface.dispose(); surface.followLatest();
    expect(container.childElementCount).toBe(0);
  } finally { surface.dispose(); container.remove(); }
});

/** A pinned 700px viewport whose mounted Turns stack upward from its bottom. */
function stackedTail(count: number) {
  const viewport = document.createElement('main');
  const container = document.createElement('div');
  viewport.append(container); document.body.append(viewport);
  const frames: FrameRequestCallback[] = [];
  const view = viewport.ownerDocument.defaultView!;
  const request = view.requestAnimationFrame, cancel = view.cancelAnimationFrame;
  const rect = HTMLElement.prototype.getBoundingClientRect;
  view.requestAnimationFrame = callback => { frames.push(callback); return frames.length; };
  view.cancelAnimationFrame = () => {};
  Object.defineProperty(viewport, 'clientHeight', { value: 700 });
  viewport.getBoundingClientRect = () => ({ top: 0, bottom: 700 } as DOMRect);
  const geometry = { turnHeight: 1000 };
  HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
    const siblings = [...(this.parentElement?.children ?? [])]
      .filter(node => (node as HTMLElement).dataset.turnId !== undefined);
    const index = siblings.indexOf(this);
    if (this.dataset.turnId === undefined || index < 0) return rect.call(this);
    const top = 700 - (siblings.length - index) * geometry.turnHeight;
    return { top, bottom: top + geometry.turnHeight, width: 600, height: geometry.turnHeight } as DOMRect;
  };
  const state = createTurnState('conv');
  for (let i = 0; i < count; i += 1) {
    const id = `turn-${i}`;
    state.turnsById[id] = { turnId: id, presentationId: id, runId: '', conversationId: 'conv', laneId: 'main', ordinal: i,
      actor: 'assistant', kind: 'reply', status: 'completed', currentAttemptId: null,
      projectionRevision: 1, createdAt: i, updatedAt: i, projection: { content: id }, settlement: {} };
    (state.laneOrder.main ??= []).push(id);
  }
  const commits: Array<ReadonlyArray<string>> = [];
  const surfaces: ConversationSurface[] = [];
  const mount = (options: ConversationSurfaceOptions = {}): ConversationSurface => {
    const surface = createConversationSurface(container, {
      ...options,
      scrollViewport: viewport,
      scrollAnchor: {
        following: true, capture: () => null, restore: () => {},
        suspendFollow: () => {}, followLatest: () => {},
      },
      onCommit: ids => { commits.push([...ids]); },
    });
    surfaces.push(surface);
    return surface;
  };
  const flushFrames = (): void => { frames.splice(0).forEach(callback => callback(0)); };
  const close = (): void => {
    surfaces.forEach(surface => surface.dispose()); viewport.remove();
    HTMLElement.prototype.getBoundingClientRect = rect;
    view.requestAnimationFrame = request; view.cancelAnimationFrame = cancel;
  };
  return { state, geometry, commits, mount, flushFrames, close };
}

it('keeps a tail window that residency retired, refilling it only when too little remains', () => {
  const fixture = stackedTail(8);
  const { state, commits } = fixture;
  try {
    const surface = fixture.mount();
    surface.render(selectConversationViewModel(state));
    expect(surface.mountedTurnIds).toHaveLength(8);
    fixture.flushFrames();
    // Turns more than three viewports above the reader retire.
    const retired = [...surface.mountedTurnIds];
    expect(retired).toEqual(['turn-5', 'turn-6', 'turn-7']);
    // A later graph with the same tail neither remounts nor retires again.
    const before = commits.length;
    surface.render(selectConversationViewModel({ ...state, conversationRevision: 2 }));
    fixture.flushFrames();
    expect(commits.slice(before)).toEqual([retired]);
    // Content that shrank below the prefetch distance refills the budget.
    fixture.geometry.turnHeight = 100;
    surface.render(selectConversationViewModel({ ...state, conversationRevision: 3 }));
    expect(surface.mountedTurnIds).toHaveLength(8);
  } finally { fixture.close(); }
});

it('mounts a revisited conversation at the tail range its previous mount retired', () => {
  const fixture = stackedTail(8);
  const { state, commits } = fixture;
  try {
    const first = fixture.mount();
    first.render(selectConversationViewModel(state));
    fixture.flushFrames();
    const retainedTail = first.retainedTail;
    expect(retainedTail).toEqual({ firstTurnId: 'turn-5', lastTurnId: 'turn-7' });
    first.dispose();
    const before = commits.length;
    const revisit = fixture.mount({ retainedTail: retainedTail! });
    revisit.render(selectConversationViewModel(state));
    fixture.flushFrames();
    // One commit of the reachable range; nothing is mounted only to retire.
    expect(commits.slice(before)).toEqual([['turn-5', 'turn-6', 'turn-7']]);
    revisit.dispose();
    // A newer Turn invalidates the range: the budget mounts as on first visit.
    state.turnsById['turn-8'] = { ...state.turnsById['turn-7']!, turnId: 'turn-8', presentationId: 'turn-8', ordinal: 8 };
    state.laneOrder.main!.push('turn-8');
    const changed = fixture.mount({ retainedTail: retainedTail! });
    changed.render(selectConversationViewModel(state));
    expect(changed.mountedTurnIds).toHaveLength(9);
  } finally { fixture.close(); }
});

it('aligns mounted and windowed targets and fences already-delivered history demand', () => {
  const viewport = document.createElement('main');
  const container = document.createElement('div');
  viewport.append(container); document.body.append(viewport);
  const frames: FrameRequestCallback[] = [];
  const view = viewport.ownerDocument.defaultView!;
  const request = view.requestAnimationFrame, cancel = view.cancelAnimationFrame;
  view.requestAnimationFrame = callback => { frames.push(callback); return frames.length; };
  view.cancelAnimationFrame = () => {}; // Native cancellation may be too late.
  Object.defineProperty(viewport, 'clientHeight', { value: 700 });
  viewport.getBoundingClientRect = () => ({ top: 0, bottom: 700 } as DOMRect);
  const state = createTurnState('conv');
  for (let i = 0; i < 40; i += 1) {
    const id = `turn-${i}`;
    state.turnsById[id] = { turnId: id, presentationId: id, runId: '', conversationId: 'conv', laneId: 'main', ordinal: i,
      actor: 'human', kind: 'input', status: 'completed', currentAttemptId: null,
      projectionRevision: 1, createdAt: i, updatedAt: i, projection: { content: id }, settlement: {} };
    (state.laneOrder.main ??= []).push(id);
  }
  let following = true;
  const targets: string[] = [];
  const surface = createConversationSurface(container, {
    scrollViewport: viewport,
    windowing: { maxTurns: 8, batchSize: 3 },
    scrollAnchor: {
      get following() { return following; },
      capture: () => null, restore: () => {},
      suspendFollow: () => { following = false; },
      scrollToTurn(root, id, onScroll) {
        if (!root.querySelector(`[data-turn-id="${id}"]`)) return false;
        targets.push(id); viewport.scrollTop = 100; onScroll?.(); return true;
      },
    },
  });
  try {
    surface.render(selectConversationViewModel(state));
    expect(surface.mountedTurnIds).not.toContain('turn-20');
    // A previously queued scroll callback must not move the new target window.
    const boundary = surface.root.querySelector<HTMLElement>('[data-history-boundary="earlier"]')!;
    boundary.getBoundingClientRect = () => ({ top: 100, bottom: 101 } as DOMRect);
    viewport.scrollTop = 200; viewport.dispatchEvent(new Event('scroll'));
    expect(frames).toHaveLength(1);
    expect(surface.revealTurn('turn-20')).toBe(true);
    const mounted = [...surface.mountedTurnIds];
    frames.splice(0).forEach(callback => callback(0));
    expect(surface.mountedTurnIds).toEqual(mounted);
    expect(targets).toEqual(['turn-20']);
    // An already-mounted target must align too, without rebuilding the window.
    expect(surface.revealTurn('turn-22')).toBe(true);
    expect(targets).toEqual(['turn-20', 'turn-22']);
    expect(surface.mountedTurnIds).toEqual(mounted);
    viewport.dispatchEvent(new Event('scroll'));
    expect(frames).toHaveLength(0); // The jump's own event isn't lazy demand.
    expect(surface.revealTurn('missing')).toBe(false);
  } finally {
    surface.dispose(); viewport.remove();
    view.requestAnimationFrame = request; view.cancelAnimationFrame = cancel;
  }
});

it('renders replies as reasoning, one toggleable tool panel and prose; a reader choice beats the live default', () => {
  const container = document.createElement('main'); document.body.append(container);
  const scope = { attemptId: 'attempt-1', taskId: 'task-1' };
  const tool = (id: string, llmRound: number) => ({
    type: 'tool_use', blockId: id, id, name: 'read_files', llmRound, ...scope,
    input: { path: `${id}.ts` }, result: { content: id, status: 'done' },
  });
  const state = createTurnState('conv');
  const reply = (status: string, revision: number, attempt: string | null = null) => ({
    turnId: 'reply', presentationId: 'reply', runId: '', conversationId: 'conv', laneId: 'main', ordinal: 1,
    actor: 'assistant', kind: 'reply', status, currentAttemptId: attempt,
    projectionRevision: revision, createdAt: 1, updatedAt: revision, settlement: {},
    projection: {
      content: 'Done.',
      segments: [
        { type: 'thinking', blockId: 'think-0', llmRound: 0, ...scope, text: 'Plan.' },
        tool('tool-a', 0),
        { type: 'text', blockId: 'narrate-1', llmRound: 1, ...scope, text: 'Checking more.' },
        tool('tool-b', 1),
        { type: 'text', blockId: 'answer', llmRound: 2, ...scope, text: 'Done.', terminal: true, deliverable: true },
      ],
    },
  }) as never;
  state.turnsById.reply = reply('running', 1, 'attempt-1');
  state.laneOrder.main = ['reply'];
  let following = true;
  const surface = createConversationSurface(container, {
    scrollAnchor: {
      get following() { return following; },
      capture: () => null, restore: () => {},
      suspendFollow: () => { following = false; },
    },
  });
  const order = () => [...surface.root.querySelectorAll<HTMLElement>(
    '[data-turn-id="reply"] .conversation-turn-blocks > [data-block-id]:not([data-block-kind="live-status"]), '
    + '[data-turn-id="reply"] .conversation-turn-blocks > [data-narration-well] > [data-narration-well-body] > [data-block-id]',
  )].map(node => node.dataset.blockId);
  const toggle = () => surface.root.querySelector<HTMLButtonElement>('.conversation-tool-panel__toggle')!;
  const closed = ['think-0', 'layout:tool-panel', 'narrate-1', 'answer'];
  const open = ['think-0', 'layout:tool-panel', 'tool-a', 'tool-b', 'narrate-1', 'answer'];
  try {
    // Without a reader choice the panel is open while the Turn works and
    // folds once it settles, like reasoning.
    surface.render(selectConversationViewModel(state));
    expect(order()).toEqual(open);
    expect(surface.root.querySelector('[data-block-id="tool-a"]')?.getAttribute('data-turn-zone')).toBe('process');
    state.turnsById.reply = reply('completed', 2);
    surface.render(selectConversationViewModel(state));
    expect(order()).toEqual(closed);
    expect(toggle().getAttribute('aria-expanded')).toBe('false');
    expect(following).toBe(true);

    const button = toggle();
    button.focus(); button.click();
    expect(following).toBe(false);
    expect(order()).toEqual(open);
    expect(toggle()).toBe(button);
    expect(document.activeElement).toBe(button);
    expect(button.getAttribute('aria-expanded')).toBe('true');

    // The reader's choice survives a resumed attempt and its settlement.
    state.turnsById.reply = reply('running', 3, 'attempt-2');
    surface.render(selectConversationViewModel(state));
    state.turnsById.reply = reply('completed', 4);
    surface.render(selectConversationViewModel(state));
    expect(order()).toEqual(open);
    toggle().click();
    expect(order()).toEqual(closed);
    state.turnsById.reply = reply('running', 5, 'attempt-3');
    surface.render(selectConversationViewModel(state));
    expect(order()).toEqual(closed);
  } finally { surface.dispose(); container.remove(); }
});

it('keeps a live panel open after settlement once the reader engaged with a call', () => {
  const container = document.createElement('main'); document.body.append(container);
  const scope = { attemptId: 'attempt-1', taskId: 'task-1' };
  const state = createTurnState('conv');
  const reply = (status: string, revision: number) => ({
    turnId: 'reply', presentationId: 'reply', runId: '', conversationId: 'conv', laneId: 'main', ordinal: 1,
    actor: 'assistant', kind: 'reply', status, currentAttemptId: status === 'running' ? 'attempt-1' : null,
    projectionRevision: revision, createdAt: 1, updatedAt: revision, settlement: {},
    projection: { content: 'Done.', segments: [
      { type: 'tool_use', blockId: 'tool-a', id: 'tool-a', name: 'read_files', llmRound: 0, ...scope,
        input: { path: 'a.ts' }, result: { content: 'a', status: 'done' } },
      { type: 'text', blockId: 'answer', llmRound: 1, ...scope, text: 'Done.', terminal: true, deliverable: true },
    ] },
  }) as never;
  state.turnsById.reply = reply('running', 1);
  state.laneOrder.main = ['reply'];
  const surface = createConversationSurface(container);
  try {
    surface.render(selectConversationViewModel(state));
    const call = surface.root.querySelector<HTMLElement>('[data-block-id="tool-a"]')!;
    call.click();
    state.turnsById.reply = reply('completed', 2);
    surface.render(selectConversationViewModel(state));
    expect(surface.root.querySelector('[data-block-id="tool-a"]')).toBe(call);
    expect(surface.root.querySelector('.conversation-tool-panel__toggle')?.getAttribute('aria-expanded')).toBe('true');
  } finally { surface.dispose(); container.remove(); }
});

it('opens an oversized panel at its newest calls, pinning head and prose, with in-card boundaries', () => {
  const container = document.createElement('main'); document.body.append(container);
  const scope = { attemptId: 'attempt-1', taskId: 'task-1' };
  const reply = (calls: number) => {
    const state = createTurnState('conv');
    state.turnsById.reply = {
      turnId: 'reply', presentationId: 'reply', runId: '', conversationId: 'conv', laneId: 'main', ordinal: 1,
      actor: 'assistant', kind: 'reply', status: 'completed', currentAttemptId: null,
      projectionRevision: calls, createdAt: 1, updatedAt: calls, settlement: {},
      projection: { content: 'Done.', segments: [
        ...Array.from({ length: calls }, (_, index) => ({
          type: 'tool_use', blockId: `call-${index}`, id: `call-${index}`, name: 'read_files', llmRound: index, ...scope,
          input: { path: `${index}.ts` }, result: { content: String(index), status: 'done' },
        })),
        { type: 'text', blockId: 'answer', llmRound: calls, ...scope, text: 'Done.', terminal: true, deliverable: true },
      ] },
    } as never;
    state.laneOrder.main = ['reply'];
    return selectConversationViewModel(state);
  };
  const mounted = () => [...container.querySelectorAll<HTMLElement>('.conversation-turn-blocks > [data-block-id]')]
    .map(node => node.dataset.blockId);
  const calls = () => [...container.querySelectorAll<HTMLElement>('[data-block-region="process"]')]
    .map(node => node.dataset.blockId);
  const boundary = (direction: string) =>
    container.querySelector<HTMLElement>(`[data-region-boundary="process:${direction}"]`);
  const surface = createConversationSurface(container, { windowing: { maxBlocksPerTurn: 12 } });
  try {
    surface.render(reply(40));
    expect(mounted()).toEqual(['layout:tool-panel', 'answer']);
    container.querySelector<HTMLButtonElement>('.conversation-tool-panel__toggle')!.click();
    const opened = mounted();
    // The calls window is bounded on its own; the header and the answer are
    // outside it and stay mounted. The newest calls are the ones shown.
    expect(opened[0]).toBe('layout:tool-panel');
    expect(opened.at(-1)).toBe('answer');
    expect(calls()).toEqual(Array.from({ length: 12 }, (_, index) => `call-${28 + index}`));
    // Hidden calls are announced inside the card, before the first mounted call.
    const earlier = boundary('earlier')!;
    expect(earlier.nextElementSibling?.getAttribute('data-block-id')).toBe('call-28');
    expect(earlier.querySelector('button')!.dataset.turnKey).toBe('reply');
    expect(earlier.dataset.hiddenCount).toBe('28');
    expect(boundary('later')).toBeNull();
    expect(container.querySelector('[data-history-boundary]:not([hidden])')).toBeNull();
    // At the hard per-Turn cap a click pages; the far edge says so in place.
    earlier.querySelector('button')!.click();
    expect(calls()[0]).toBe('call-22');
    expect(boundary('later')?.dataset.hiddenCount).toBe('6');
    expect(mounted()[0]).toBe('layout:tool-panel');
    surface.dispose();

    const small = createConversationSurface(container, { windowing: { maxBlocksPerTurn: 12 } });
    small.render(reply(12));
    container.querySelector<HTMLButtonElement>('.conversation-tool-panel__toggle')!.click();
    expect(mounted()).toHaveLength(14);
    expect(container.querySelector('[data-region-boundary], [data-history-boundary]:not([hidden])')).toBeNull();
    small.dispose();
  } finally { container.remove(); }
});

it('reveals earlier calls on click without hiding the newest', () => {
  const container = document.createElement('main'); document.body.append(container);
  const viewport = document.createElement('div'); document.body.append(viewport);
  Object.defineProperty(viewport, 'clientHeight', { configurable: true, value: 320 });
  const scope = { attemptId: 'attempt-1', taskId: 'task-1' };
  const state = createTurnState('conv');
  state.turnsById.reply = {
    turnId: 'reply', presentationId: 'reply', runId: '', conversationId: 'conv', laneId: 'main', ordinal: 1,
    actor: 'assistant', kind: 'reply', status: 'completed', currentAttemptId: null,
    projectionRevision: 1, createdAt: 1, updatedAt: 1, settlement: {},
    projection: { content: 'Done.', segments: [
      ...Array.from({ length: 100 }, (_, index) => ({
        type: 'tool_use', blockId: `call-${index}`, id: `call-${index}`, name: 'read_files', llmRound: index, ...scope,
        input: { path: `${index}.ts` }, result: { content: String(index), status: 'done' },
      })),
      { type: 'text', blockId: 'answer', llmRound: 100, ...scope, text: 'Done.', terminal: true, deliverable: true },
    ] },
  } as never;
  state.laneOrder.main = ['reply'];
  const calls = () => [...container.querySelectorAll<HTMLElement>('[data-block-region="process"]')]
    .map(node => node.dataset.blockId);
  const boundary = (direction: string) =>
    container.querySelector<HTMLElement>(`[data-region-boundary="process:${direction}"]`);
  const surface = createConversationSurface(container, { scrollViewport: viewport, windowing: { maxBlocksPerTurn: 60 } });
  try {
    surface.render(selectConversationViewModel(state));
    container.querySelector<HTMLButtonElement>('.conversation-tool-panel__toggle')!.click();
    const opened = calls();
    expect(opened.at(-1)).toBe('call-99');
    expect(boundary('later')).toBeNull();
    boundary('earlier')!.querySelector('button')!.click();
    const grown = calls();
    expect(grown.length).toBeGreaterThan(opened.length);
    expect(grown.slice(-opened.length)).toEqual(opened);
    expect(boundary('later')).toBeNull();
    for (let index = 0; index < 20 && boundary('earlier'); index += 1) {
      boundary('earlier')!.querySelector('button')!.click();
    }
    expect(calls().length).toBeLessThanOrEqual(60);
    expect(calls()[0]).toBe('call-0');
    expect(boundary('later')?.dataset.hiddenCount).toBe('40');
  } finally { surface.dispose(); container.remove(); viewport.remove(); }
});

it('keeps the newest calls of a live panel mounted while the reader is away from the tail', () => {
  const container = document.createElement('main'); document.body.append(container);
  const viewport = document.createElement('div'); document.body.append(viewport);
  let height = 1280;
  Object.defineProperty(viewport, 'clientHeight', { configurable: true, get: () => height });
  const scope = { attemptId: 'attempt-1', taskId: 'task-1' };
  const state = createTurnState('conv');
  state.laneOrder.main = ['reply'];
  let following = true;
  const surface = createConversationSurface(container, {
    scrollViewport: viewport,
    windowing: { maxBlocksPerTurn: 60 },
    scrollAnchor: {
      get following() { return following; },
      capture: () => null, restore: () => {},
      suspendFollow: () => { following = false; },
    },
  });
  const render = (count: number) => {
    state.turnsById.reply = {
      turnId: 'reply', presentationId: 'reply', runId: '', conversationId: 'conv', laneId: 'main', ordinal: 1,
      actor: 'assistant', kind: 'reply', status: 'running', currentAttemptId: 'attempt-1',
      projectionRevision: count, createdAt: 1, updatedAt: count, settlement: {},
      projection: { content: '', segments: Array.from({ length: count }, (_, index) => ({
        type: 'tool_use', blockId: `call-${index}`, id: `call-${index}`, name: 'read_files', llmRound: index, ...scope,
        input: { path: `${index}.ts` }, result: { content: String(index), status: 'done' },
      })) },
    } as never;
    surface.render(selectConversationViewModel(state));
  };
  const calls = () => [...container.querySelectorAll<HTMLElement>('[data-block-region="process"]')]
    .map(node => node.dataset.blockId);
  const range = (first: number, last: number) =>
    Array.from({ length: last - first + 1 }, (_, index) => `call-${first + index}`);
  const boundary = (direction: string) =>
    container.querySelector<HTMLElement>(`[data-region-boundary="process:${direction}"]`);
  try {
    render(60);
    expect(calls()).toEqual(range(20, 59));
    // Owner report 2026-11-03: once the follow latch dropped (a scroll nudge,
    // or the pane settling while a conversation opens) the calls window froze,
    // so the newest calls sat behind a "N newer tool calls" row until a later
    // frame arrived while following again. The reader never asked to hide
    // them: a smaller viewport budget and appended calls both keep the tail
    // mounted, and the calls already on screen stay.
    following = false;
    height = 320;
    render(60);
    expect(calls()).toEqual(range(20, 59));
    expect(boundary('later')).toBeNull();
    render(72);
    expect(calls()).toEqual(range(20, 71));
    expect(boundary('later')).toBeNull();
    // Growth stays within the hard per-Turn cap by retiring the oldest calls.
    render(100);
    expect(calls()).toEqual(range(40, 99));
    expect(boundary('earlier')?.dataset.hiddenCount).toBe('40');
    expect(boundary('later')).toBeNull();
    // Back at the tail the window returns to the viewport budget.
    following = true;
    render(101);
    expect(calls()).toEqual(range(81, 100));
    expect(boundary('later')).toBeNull();
  } finally { surface.dispose(); container.remove(); viewport.remove(); }
});

it('never unmounts reasoning or the panel header while a long live reply follows its tail', () => {
  const container = document.createElement('main'); document.body.append(container);
  const scope = { attemptId: 'attempt-1', taskId: 'task-1' };
  const state = createTurnState('conv');
  const reply = (rounds: number, revision: number) => ({
    turnId: 'reply', presentationId: 'reply', runId: '', conversationId: 'conv', laneId: 'main', ordinal: 1,
    actor: 'assistant', kind: 'reply', status: 'running', currentAttemptId: 'attempt-1',
    projectionRevision: revision, createdAt: 1, updatedAt: revision, settlement: {},
    projection: { content: '', segments: Array.from({ length: rounds }, (_, round) => [
      { type: 'thinking', blockId: `think-${round}`, llmRound: round, ...scope, text: `Plan ${round}.` },
      { type: 'text', blockId: `say-${round}`, llmRound: round, ...scope, text: `Step ${round}.` },
      { type: 'tool_use', blockId: `call-${round}`, id: `call-${round}`, name: 'read_files', llmRound: round, ...scope,
        input: { path: `${round}.ts` }, result: { content: String(round), status: 'done' } },
    ]).flat() },
  }) as never;
  state.laneOrder.main = ['reply'];
  const surface = createConversationSurface(container, {
    windowing: { maxBlocksPerTurn: 12 },
    scrollAnchor: { following: true, capture: () => null, restore: () => {}, suspendFollow: () => {} },
  });
  const ids = (selector: string) => [...container.querySelectorAll<HTMLElement>(selector)]
    .map(node => node.dataset.blockId);
  try {
    let thinking: Element | null = null;
    let header: Element | null = null;
    for (let rounds = 4; rounds <= 40; rounds += 1) {
      state.turnsById.reply = reply(rounds, rounds);
      surface.render(selectConversationViewModel(state));
      const nowThinking = container.querySelector('[data-block-kind="thinking"]');
      const nowHeader = container.querySelector('[data-block-kind="tool-panel"]');
      expect(nowThinking).not.toBeNull();
      expect(nowHeader).not.toBeNull();
      // Same nodes every frame: no remount, so no flash and no reopened fold.
      if (thinking) expect(nowThinking).toBe(thinking);
      if (header) expect(nowHeader).toBe(header);
      thinking = nowThinking; header = nowHeader;
      const calls = ids('[data-block-region="process"]');
      const prose = ids('[data-block-region="prose"]');
      // Each region is bounded on its own (budget plus the small-overflow slack).
      expect(calls.length).toBeGreaterThan(0);
      expect(calls.length).toBeLessThanOrEqual(15);
      expect(prose.length).toBeLessThanOrEqual(15);
      expect(calls.at(-1)).toBe(`call-${rounds - 1}`);
      // A following reader sees the newest narration with the live status
      // closing the prose window.
      expect(prose.slice(-2)).toEqual([`say-${rounds - 1}`, 'live-status']);
      const hiddenCalls = container.querySelector<HTMLElement>('[data-region-boundary="process:earlier"]');
      if (rounds > 15) expect(hiddenCalls?.dataset.hiddenCount).toBe(String(rounds - calls.length));
    }
  } finally { surface.dispose(); container.remove(); }
});

it('gathers interim round prose into one scrolling well; the live tail and the answer stay outside', () => {
  const container = document.createElement('main'); document.body.append(container);
  const scope = { attemptId: 'attempt-1', taskId: 'task-1' };
  const state = createTurnState('conv');
  const say = (round: number) => ({ type: 'text', blockId: `say-${round}`, llmRound: round, ...scope, text: `Step ${round}.` });
  const call = (round: number) => ({
    type: 'tool_use', blockId: `call-${round}`, id: `call-${round}`, name: 'read_files', llmRound: round, ...scope,
    input: { path: `${round}.ts` }, result: { content: String(round), status: 'done' },
  });
  const reply = (rounds: number, revision: number, settled = false) => ({
    turnId: 'reply', presentationId: 'reply', runId: '', conversationId: 'conv', laneId: 'main', ordinal: 1,
    actor: 'assistant', kind: 'reply', status: settled ? 'completed' : 'running',
    currentAttemptId: settled ? null : 'attempt-1',
    projectionRevision: revision, createdAt: 1, updatedAt: revision, settlement: {},
    projection: { content: settled ? 'Done.' : '', segments: [
      ...Array.from({ length: rounds }, (_, round) => [say(round), call(round)]).flat(),
      ...(settled ? [{ type: 'text', blockId: 'answer', llmRound: rounds, ...scope, text: 'Done.', terminal: true, deliverable: true }] : []),
    ] },
  }) as never;
  state.laneOrder.main = ['reply'];
  const surface = createConversationSurface(container, {
    scrollAnchor: { following: true, capture: () => null, restore: () => {}, suspendFollow: () => {} },
  });
  const blocks = () => surface.root.querySelector<HTMLElement>('[data-turn-id="reply"] .conversation-turn-blocks')!;
  const well = () => blocks().querySelector<HTMLElement>(':scope > [data-narration-well]');
  const inWell = () => [...(well()?.querySelectorAll<HTMLElement>('[data-narration-well-body] > [data-block-id]') ?? [])]
    .map(node => node.dataset.blockId);
  try {
    // One streaming round: its prose may still grow, so there is no well yet.
    state.turnsById.reply = reply(1, 1);
    surface.render(selectConversationViewModel(state));
    expect(well()).toBeNull();
    const first = blocks().querySelector('[data-block-id="say-0"]');

    // A later round's text settles the earlier one into the well (same node).
    state.turnsById.reply = reply(3, 2);
    surface.render(selectConversationViewModel(state));
    expect(inWell()).toEqual(['say-0', 'say-1']);
    expect(blocks().querySelector('[data-block-id="say-0"]')).toBe(first);
    expect(blocks().querySelector(':scope > [data-block-id="say-2"]')).not.toBeNull();
    const body = well()!.querySelector<HTMLElement>('[data-narration-well-body]')!;
    expect(body.getAttribute('aria-label')).toBe(well()!.querySelector('.conversation-narration__label')!.textContent);
    expect(body.tabIndex).toBe(0);

    // Newest note first: the well rests at its end; a reader who scrolled up keeps their place.
    let top = 0;
    Object.defineProperty(body, 'scrollHeight', { configurable: true, get: () => 800 });
    Object.defineProperty(body, 'clientHeight', { configurable: true, get: () => 200 });
    Object.defineProperty(body, 'scrollTop', { configurable: true, get: () => top, set: value => { top = value; } });
    state.turnsById.reply = reply(3, 3);
    surface.render(selectConversationViewModel(state));
    expect(top).toBe(800);
    top = 50;
    body.dispatchEvent(new Event('scroll'));
    state.turnsById.reply = reply(3, 4);
    surface.render(selectConversationViewModel(state));
    expect(top).toBe(50);

    // Settled: every round's prose shares the well; the answer reads below it.
    state.turnsById.reply = reply(3, 5, true);
    surface.render(selectConversationViewModel(state));
    expect(inWell()).toEqual(['say-0', 'say-1', 'say-2']);
    expect(well()!.nextElementSibling?.getAttribute('data-block-id')).toBe('answer');
    expect(surface.root.querySelectorAll('[data-narration-well]')).toHaveLength(1);
  } finally { surface.dispose(); container.remove(); }
});
