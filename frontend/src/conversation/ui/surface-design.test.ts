// @vitest-environment jsdom
/** Appearance changes must not add redundant style work to streamed content. */
import { expect, it } from 'vitest';
import { createTurnState } from '../domain/turn-store';
import { selectConversationViewModel } from '../presentation/conversation-view-model';
import { createConversationSurface } from './conversation-surface';
import { createConversationSurfaceEngine } from './surface-engine';
import { createConversationSurfaceDesign } from './surface-design';
import type { ConversationSurfaceOptions } from './surface-contracts';

it('streams every content update without rewriting unchanged shell classes or control labels', () => {
  const container = document.createElement('main');
  document.body.append(container);
  const surface = createConversationSurface(container);
  const state = createTurnState('conv');
  state.turnsById.answer = {
    turnId: 'answer', presentationId: 'answer', runId: '', conversationId: 'conv',
    laneId: 'main', ordinal: 1, actor: 'assistant', kind: 'reply', status: 'running',
    currentAttemptId: 'attempt', projectionRevision: 1, createdAt: 1, updatedAt: 1,
    projection: { segments: [{ type: 'text', blockId: 'answer-text', text: 'initial' }] }, settlement: {},
  };
  state.laneOrder.main = ['answer'];
  surface.render(selectConversationViewModel(state));
  const article = container.querySelector('[data-turn-id="answer"]');
  const controls = [...container.querySelectorAll('[data-history-boundary] button')];
  const labels = controls.map(button => button.firstChild);
  const observer = new MutationObserver(() => {});
  observer.observe(container, { subtree: true, attributes: true, attributeFilter: ['class'] });
  try {
    for (let revision = 2; revision <= 65; revision++) {
      state.turnsById.answer = { ...state.turnsById.answer!, projectionRevision: revision,
        projection: { segments: [{ type: 'text', blockId: 'answer-text', text: `content-${revision}` }] } };
      surface.render(selectConversationViewModel(state));
      expect(container.textContent).toContain(`content-${revision}`);
      expect(container.querySelector('[data-turn-id="answer"]')).toBe(article);
    }
    const mutations = observer.takeRecords();
    expect(mutations, `${mutations.length} redundant class writes for 64 content frames`).toHaveLength(0);
    controls.forEach((button, index) => expect(button.firstChild).toBe(labels[index]));
    state.turnsById.answer = { ...state.turnsById.answer!, status: 'failed', projectionRevision: 66 };
    surface.render(selectConversationViewModel(state));
    expect(article?.classList.contains('turn-failed')).toBe(true);
  } finally {
    observer.disconnect(); surface.dispose(); container.remove();
  }
});

// The anatomy the context rail's sticky travel depends on (and that
// tests/support/context_rail_probe.py mirrors): track › card › content.
it('places turn context in a rail track whose one card holds the rendered facts', () => {
  const container = document.createElement('main');
  document.body.append(container);
  const surface = createConversationSurface(container, { renderTurnContextRail(node, block) {
    node.replaceChildren(); node.textContent = String(block?.value.snapshot.model || ''); node.hidden = !block;
  } });
  const state = createTurnState('conv');
  state.turnsById.input = {
    turnId: 'input', presentationId: 'input', runId: '', conversationId: 'conv',
    laneId: 'main', ordinal: 1, actor: 'human', kind: 'input', status: 'completed',
    currentAttemptId: null, projectionRevision: 1, createdAt: 1, updatedAt: 1,
    projection: { content: 'question', contextSnapshot: { blockId: 'ctx', snapshot: { model: 'opus' } } },
    settlement: {},
  };
  state.laneOrder.main = ['input'];
  try {
    surface.render(selectConversationViewModel(state));
    const rail = container.querySelector<HTMLElement>(
      '[data-turn-id="input"] > [data-conversation-part="turn-context-rail"]')!;
    expect([rail.className, rail.hidden]).toEqual(['conversation-turn-context-rail turn-ctx', false]);
    const card = rail.firstElementChild as HTMLElement;
    expect(rail.children).toHaveLength(1);
    expect([card.dataset.conversationPart, card.className])
      .toEqual(['turn-context-card', 'conversation-turn-context-card tctx-card']);
    const content = card.firstElementChild as HTMLElement;
    expect([content.dataset.conversationPart, content.className, content.textContent])
      .toEqual(['turn-context-content', 'turn-context-content', 'opus']);
  } finally {
    surface.dispose(); container.remove();
  }
});

it('uses the same paging, focus and keyed lifetime with a different design', () => {
  const container = document.createElement('main'); document.body.append(container);
  const options: ConversationSurfaceOptions = { windowing: { maxTurns: 2, batchSize: 1 } };
  const original = createConversationSurfaceDesign(options);
  const surface = createConversationSurfaceEngine(container, options, {
    ...original,
    decorate(node, part) { node.className = `alternate-${part}`; },
    turnShell(node) { node.className = 'alternate-turn'; },
    windowBoundary(node, direction, count, status) {
      const button = original.windowBoundary(node, direction, count, status);
      button.className = 'alternate-history';
      return button;
    },
  });
  const state = createTurnState('conv');
  state.laneOrder.main = [];
  for (let i = 0; i < 4; i++) {
    const id = `turn-${i}`;
    state.turnsById[id] = { turnId: id, presentationId: id, runId: '', conversationId: 'conv',
      laneId: 'main', ordinal: i, actor: 'assistant', kind: 'reply', status: 'completed',
      currentAttemptId: null, projectionRevision: 1, createdAt: 1, updatedAt: 1,
      projection: { content: id }, settlement: {} };
    state.laneOrder.main.push(id);
  }
  try {
    surface.render(selectConversationViewModel(state));
    expect(surface.root.className).toBe('alternate-root');
    expect(surface.mountedTurnIds).toEqual(['turn-2', 'turn-3']);
    const retained = container.querySelector('[data-turn-id="turn-2"]');
    const button = container.querySelector<HTMLButtonElement>('[data-conversation-window-action="earlier"]')!;
    button.focus(); button.click();
    expect(surface.mountedTurnIds).toEqual(['turn-1', 'turn-2']);
    expect(container.querySelector('[data-turn-id="turn-2"]')).toBe(retained);
    expect(document.activeElement).toBe(button);
    expect(surface.revealTurn('turn-0')).toBe(true);
    expect(surface.mountedTurnIds).toContain('turn-0');
    expect(state.laneOrder.main).toHaveLength(4);
    surface.dispose();
    expect(container.childElementCount).toBe(0);
    button.click();
    expect(container.childElementCount).toBe(0);
  } finally { surface.dispose(); container.remove(); }
});
