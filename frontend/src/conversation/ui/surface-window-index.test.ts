// @vitest-environment jsdom
/** Window preparation should follow the canonical graph, independent of history size. */
import { expect, it } from 'vitest';
import { createTurnState } from '../domain/turn-store';
import { selectConversationViewModel } from '../presentation/conversation-view-model';
import { createConversationSurface } from './conversation-surface';
import { prepareSurfaceWindows, type SurfaceLaneWindow } from './surface-window-preparation';
import type { TurnRecord } from '../../api/conversation-sync.generated';

function record(id: string, ordinal: number, extra: Partial<TurnRecord> = {}): TurnRecord {
  return { turnId: id, presentationId: id, runId: '', conversationId: 'window-index', laneId: 'main',
    ordinal, actor: 'assistant', kind: 'reply', status: 'completed', currentAttemptId: null,
    projectionRevision: 1, createdAt: ordinal, updatedAt: ordinal, settlement: {},
    projection: { content: id, segments: [{ type: 'text', blockId: 'answer', text: id,
      terminal: true, deliverable: true }] }, ...extra };
}

function stateWith(turns: TurnRecord[]) {
  const state = createTurnState('window-index');
  for (const turn of turns) {
    state.turnsById[turn.turnId] = turn;
    (state.laneOrder[turn.laneId] ??= []).push(turn.turnId);
  }
  return state;
}

it('reads the same bounded Turn working set as loaded history grows', () => {
  const observations = [512, 4096].map(count => {
    const container = document.createElement('main'); document.body.append(container);
    const state = stateWith(Array.from({ length: count }, (_, i) => record(`t-${i}`, i)));
    let paints = 0;
    const surface = createConversationSurface(container, {
      windowing: { maxTurns: 8, batchSize: 2 },
      renderBlock(node, block) { paints++; node.innerHTML = '<button></button>';
        node.firstElementChild!.textContent = block.kind === 'text' ? block.displayMarkdown : block.kind; },
    });
    try {
      surface.render(selectConversationViewModel(state));
      const first = surface.root.querySelector('[data-presentation-id]')!;
      const focused = first.querySelector<HTMLButtonElement>('[data-block-id="answer"] button')!;
      focused.focus();
      const tail = state.turnsById[`t-${count - 1}`]!;
      const updated = record(tail.turnId, tail.ordinal, { projectionRevision: 2,
        projection: { content: 'updated', segments: [{ type: 'text', blockId: 'answer', text: 'updated', terminal: true, deliverable: true }] } });
      const next = selectConversationViewModel({ ...state, turnsById: { ...state.turnsById, [tail.turnId]: updated } });
      let reads = 0;
      const original = next.mainLane.turns;
      // Transparent observation of the same lane/Turn identities and values.
      next.mainLane.turns = new Proxy(original, { get(target, key, receiver) {
        if (typeof key === 'string' && /^\d+$/.test(key)) reads++;
        return Reflect.get(target, key, receiver);
      } });
      try { surface.render(next); } finally { next.mainLane.turns = original; }
      expect(surface.root.querySelector('[data-presentation-id]')).toBe(first);
      expect(document.activeElement).toBe(focused);
      expect(surface.mountedTurnIds).toEqual(state.laneOrder.main!.slice(-8));
      expect(surface.root.querySelector(`[data-turn-id="${tail.turnId}"] [data-block-id="answer"]`)?.textContent).toBe('updated');
      expect(paints).toBe(9);
      return { reads, mounted: surface.mountedTurnIds.length };
    } finally { surface.dispose(); container.remove(); }
  });
  expect(observations[0].reads).toBeGreaterThan(0);
  expect(observations[1]).toEqual(observations[0]);
});

it('publishes stable ordered branch identities and leaves earlier indexes unchanged', () => {
  const rootA = record('root-a', 1, { projection: { _branchLanes: [{ laneId: 'branch-b' }, { laneId: 'branch-a' }] } });
  const rootB = record('root-b', 2, { projection: { _branchLanes: [{ laneId: 'branch-c' }] } });
  const state = stateWith([rootA, rootB,
    record('b', 1, { laneId: 'branch-b', parentTurnId: 'root-a' }),
    record('a', 1, { laneId: 'branch-a', parentTurnId: 'root-a' }),
    record('c', 1, { laneId: 'branch-c', parentTurnId: 'root-b' })]);
  const before = selectConversationViewModel(state);
  const after = selectConversationViewModel({ ...state, laneOrder: { ...state.laneOrder, main: ['root-b', 'root-a'] } });
  expect(before.mainLane.childLaneIds).toEqual(['branch-b', 'branch-a', 'branch-c']);
  expect(after.mainLane.childLaneIds).toEqual(['branch-c', 'branch-b', 'branch-a']);
  expect(before.navigation?.turnLocations.get('root-a')).toEqual({ laneId: 'main', index: 0 });
  expect(after.navigation?.turnLocations.get('root-a')).toEqual({ laneId: 'main', index: 1 });
  expect(before.navigation?.lanes.get('main')).toBe(before.mainLane);
  expect(after.navigation?.lanes.get('main')).toBe(after.mainLane);
});

it('preserves the explicit graph after JSON round-trip or external lane replacement', () => {
  const state = stateWith([record('old', 1)]);
  const view = selectConversationViewModel(state);
  const replacement = selectConversationViewModel(stateWith([
    record('new', 2, { projection: { _branchLanes: [{ laneId: 'new-branch' }] } }),
    record('child', 1, { laneId: 'new-branch', parentTurnId: 'new' }),
  ]), {}, { expandedBranchLaneId: 'new-branch' });
  const container = document.createElement('main');
  const surface = createConversationSurface(container);
  try {
    surface.render(JSON.parse(JSON.stringify(view)));
    expect(surface.mountedTurnIds).toEqual(['old']);
    surface.render({ ...view, mainLane: replacement.mainLane });
    expect(surface.revealTurn('new')).toBe(true);
    expect(surface.mountedTurnIds).toEqual(['new', 'child']);
  } finally { surface.dispose(); }
});

it('keeps branch reachability, history anchors and budgets equal to explicit graph traversal', () => {
  const roots = Array.from({ length: 12 }, (_, i) => record(`root-${i}`, i));
  roots[3] = record('root-3', 3, { projection: { _branchLanes: [{ laneId: 'branch' }] } });
  const branch = record('child', 1, { laneId: 'branch', parentTurnId: 'root-3',
    projection: { _branchLanes: [{ laneId: 'nested' }] } });
  const nested = record('leaf', 1, { laneId: 'nested', parentTurnId: 'child' });
  const orphan = record('orphan-turn', 1, { laneId: 'orphan' });
  const previous = new Map<string, SurfaceLaneWindow>([['main', {
    start: 0, end: 4, atTail: false, firstMountedTurnId: 'root-4',
    lastMountedTurnId: 'root-7', loadedTurns: 8,
  }]]);
  for (const rootTurns of [roots, roots.filter(t => t.turnId !== 'root-4'), [...roots].reverse()]) {
    for (const expandedBranchLaneId of [undefined, 'branch', 'nested', 'orphan']) {
      const view = selectConversationViewModel(stateWith([...rootTurns, branch, nested, orphan]), {},
        { expandedBranchLaneId });
      for (const following of [true, false, undefined]) {
        const options = { maxTurns: 8, batchSize: 2, maxBlocksPerTurn: 10, blockBatchSize: 2 };
        const measurement = { viewportHeight: 700, geometric: true, following, reset: false };
        const actual = prepareSurfaceWindows(view, previous, options, measurement);
        const explicit = prepareSurfaceWindows({ ...view, navigation: undefined }, previous, options, measurement);
        expect(actual).toEqual(explicit);
        expect([...actual.lanesById.keys()]).toEqual(expandedBranchLaneId === 'branch'
          ? ['main', 'branch', 'nested', 'orphan'] : ['main', 'branch', 'orphan']);
      }
    }
  }
  expect(previous.get('main')).toEqual({ start: 0, end: 4, atTail: false,
    firstMountedTurnId: 'root-4', lastMountedTurnId: 'root-7', loadedTurns: 8 });
});

it('keeps cycle-rejected branches as explicit diagnostic placeholders', () => {
  const cycles: string[] = [];
  const view = selectConversationViewModel(stateWith([
    record('root', 1, { projection: { _branchLanes: [{ laneId: 'branch' }] } }),
    record('child', 1, { laneId: 'branch', parentTurnId: 'root',
      projection: { _branchLanes: [{ laneId: 'main' }] } }),
  ]), { onLaneCycle: id => cycles.push(id) }, { expandedBranchLaneId: 'branch' });
  expect(cycles).toEqual(['main']);
  expect(view.navigation).toBeUndefined();
  const rejected = view.mainLane.turns[0].branches[0].turns[0].branches[0];
  expect(rejected).toMatchObject({ laneId: 'main', kind: 'cycle-rejected', turns: [] });
  const surface = createConversationSurface(document.createElement('main'));
  try { surface.render(view); expect(surface.mountedTurnIds).toEqual(['root', 'child']); }
  finally { surface.dispose(); }
});
