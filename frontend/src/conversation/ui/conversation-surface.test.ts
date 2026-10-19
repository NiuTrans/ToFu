// @vitest-environment jsdom
/** Semantic latest navigation and commit membership, independent of DOM paging. */
import { expect, it } from 'vitest';
import { createTurnState } from '../domain/turn-store';
import { selectConversationViewModel } from '../presentation/conversation-view-model';
import { createConversationSurface } from './conversation-surface';

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
