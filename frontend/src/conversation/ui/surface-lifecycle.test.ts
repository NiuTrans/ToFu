// @vitest-environment jsdom
/** Renderer teardown must finish before a Surface gives back its first error. */
import { expect, it } from 'vitest';
import { createTurnState } from '../domain/turn-store';
import { selectConversationViewModel } from '../presentation/conversation-view-model';
import { createConversationSurface } from './conversation-surface';

function view(historical: boolean, withTool = false) {
  const state = createTurnState('conversation');
  state.turnsById.turn = { turnId: 'turn', presentationId: 'turn', runId: '',
    conversationId: 'conversation', laneId: 'main', ordinal: 1, actor: 'assistant',
    kind: 'reply', status: historical ? 'completed' : 'running', currentAttemptId: null,
    projectionRevision: 1, createdAt: 1, updatedAt: 1, settlement: {}, projection: {
      segments: [
        { type: 'thinking', blockId: 'reasoning', text: 'Consider the evidence' },
        { type: 'text', blockId: 'narration', text: 'Read the source' },
        ...(withTool ? [{ type: 'tool_use' as const, blockId: 'read', id: 'read', name: 'read_files',
          input: { path: 'source.ts' }, result: { status: 'done', content: 'source' } }] : []),
        { type: 'text' as const, blockId: 'answer', text: 'Answer', terminal: true, deliverable: true },
      ],
    } };
  state.laneOrder.main = ['turn'];
  return selectConversationViewModel(state);
}

for (const historical of [false, true]) it(`finishes all block cleanup after a failure (${historical ? 'opened settled panel' : 'live'})`, () => {
  const container = document.createElement('main'); document.body.append(container);
  const retired: string[] = [];
  const failure = new Error('renderer cleanup failed');
  const surface = createConversationSurface(container, {
    disposeBlock(node) {
      retired.push(node.dataset.blockId!);
      if (retired.length === 1) throw failure;
    },
  });
  surface.render(view(historical, true));
  const toggle = surface.root.querySelector<HTMLButtonElement>('[data-conversation-tool-panel]')!;
  // A live panel starts open; a settled one mounts its calls on demand.
  expect(toggle.getAttribute('aria-expanded')).toBe(String(!historical));
  if (historical) toggle.click();
  expect(surface.root.querySelector('[data-block-id="read"]')).not.toBeNull();
  const mounted = [...surface.root.querySelectorAll<HTMLElement>('[data-block-id]')]
    .map(node => node.dataset.blockId!).sort();
  expect(mounted.length).toBeGreaterThan(1);
  try {
    expect(() => surface.dispose()).toThrow(failure);
    expect(retired.slice().sort()).toEqual(mounted);
    expect(container.childElementCount).toBe(0);
    expect(surface.root.childElementCount).toBe(0);
    expect(surface.mountedTurnIds).toEqual([]);
    expect(() => surface.dispose()).not.toThrow();
    expect(retired).toHaveLength(mounted.length);
  } finally { container.remove(); }
});


it('a retired panel toggle cannot repaint a restored Turn with the same identity', () => {
  const container = document.createElement('main'); document.body.append(container);
  let paints = 0;
  const surface = createConversationSurface(container, {
    renderBlock(node, block) { paints += 1; node.textContent = block.blockId; },
  });
  try {
    surface.render(view(true, true));
    const retired = surface.root.querySelector<HTMLButtonElement>('[data-conversation-tool-panel]')!;
    retired.click();
    expect(retired.getAttribute('aria-expanded')).toBe('true');
    surface.render(selectConversationViewModel(createTurnState('conversation')));
    expect(retired.isConnected).toBe(false);
    surface.render(view(true, true));
    const restored = surface.root.querySelector<HTMLButtonElement>('[data-conversation-tool-panel]')!;
    expect(restored).not.toBe(retired);
    // The reader's choice belongs to the presentation, not the old node.
    expect(restored.getAttribute('aria-expanded')).toBe('true');
    const previousPaints = paints;
    retired.click();
    expect(paints).toBe(previousPaints);
    expect(restored.getAttribute('aria-expanded')).toBe('true');
    expect(surface.root.querySelector('[data-block-id="read"]')).not.toBeNull();
  } finally { surface.dispose(); container.remove(); }
});


for (const selector of [
  '[data-block-id="reasoning"]', '[data-presentation-id="turn"]',
  '[data-conversation-part="turn-content"]', '[data-conversation-part="lane-turns"]',
  '[data-conversation-part="main-lane"]',
]) it(`retires duplicate ${selector} through the renderer without disturbing the survivor`, () => {
  const container = document.createElement('main');
  const donorContainer = document.createElement('main');
  document.body.append(container, donorContainer);
  const resources = new WeakMap<HTMLElement, AbortController>();
  const retired: HTMLElement[] = [];
  const options = {
    renderBlock(node: HTMLElement) {
      resources.set(node, new AbortController());
      node.innerHTML = '<button class="test-evidence">Evidence</button>';
    },
    disposeBlock(node: HTMLElement) {
      retired.push(node);
      resources.get(node)?.abort();
    },
  };
  const surface = createConversationSurface(container, options);
  const donor = createConversationSurface(donorContainer, options);
  try {
    surface.render(view(false)); donor.render(view(false));
    const survivor = surface.root.querySelector<HTMLElement>(selector)!;
    const duplicate = donor.root.querySelector<HTMLElement>(selector)!;
    const leaves = [...duplicate.querySelectorAll<HTMLElement>('[data-block-id]')];
    if (duplicate.hasAttribute('data-block-id')) leaves.unshift(duplicate);
    expect(leaves.length).toBeGreaterThan(0);
    const active = survivor.querySelector<HTMLButtonElement>('.test-evidence')!;
    active.focus();
    expect(document.activeElement).toBe(active);
    survivor.parentElement!.append(duplicate);
    surface.render(view(false));
    expect(retired).toEqual(leaves);
    expect(leaves.every(node => resources.get(node)?.signal.aborted)).toBe(true);
    expect(duplicate.isConnected).toBe(false);
    expect(leaves.every(node => !('_tofuBlock' in node))).toBe(true);
    expect(surface.root.querySelector(selector)).toBe(survivor);
    expect(document.activeElement).toBe(active);
    expect(resources.get(active.closest<HTMLElement>('[data-block-id]')!)?.signal.aborted).toBe(false);
    surface.render(view(false));
    expect(retired).toEqual(leaves);
  } finally { surface.dispose(); donor.dispose(); container.remove(); donorContainer.remove(); }
});

it('retires every duplicate after a cleanup failure and preserves the first error', () => {
  const container = document.createElement('main'); document.body.append(container);
  const retired: HTMLElement[] = [];
  const failure = new Error('duplicate cleanup failed');
  let fail = true;
  const surface = createConversationSurface(container, {
    disposeBlock(node) { retired.push(node); if (fail) { fail = false; throw failure; } },
  });
  try {
    surface.render(view(false));
    const first = surface.root.querySelector<HTMLElement>('[data-presentation-id="turn"]')!;
    const duplicates = [first.cloneNode(true) as HTMLElement, first.cloneNode(true) as HTMLElement];
    const leaves = duplicates.flatMap(node => [...node.querySelectorAll<HTMLElement>('[data-block-id]')]);
    first.parentElement!.append(...duplicates);
    expect(() => surface.render(view(false))).toThrow(failure);
    expect(retired).toEqual(leaves);
    expect(duplicates.every(node => !node.isConnected)).toBe(true);
    expect(surface.root.querySelectorAll('[data-presentation-id="turn"]')).toHaveLength(1);
    expect(surface.root.querySelector('[data-presentation-id="turn"]')).toBe(first);
    expect(() => surface.render(view(false))).not.toThrow();
    expect(retired).toEqual(leaves);
  } finally { surface.dispose(); container.remove(); }
});
