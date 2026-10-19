/** Shell invalidation must match full projection, with local event work. */
import {describe, expect, it} from 'vitest';
import {applyTurnStateProjection, createTurnProjectionReconciler, type ProjectedConversation} from './turn-projection';
import {createTurnState, createTurnStore, reduceTurnState, type TurnAction, type TurnState, type TurnStateUpdate} from './turn-state';

const turn = (turnId: string, ordinal: number, laneId = 'main') => ({
  turnId, presentationId: turnId, conversationId: 'projection', ordinal, laneId,
  actor: 'assistant' as const, kind: 'reply', runId: '', status: 'running' as const,
  currentAttemptId: `${turnId}-attempt`, projectionRevision: 1,
  projection: {content: 'before'}, settlement: {}, createdAt: 1, updatedAt: 1,
});
const event = (seq: number, extra = {}): TurnAction => ({type: 'event', event: {
  conversationId: 'projection', turnId: 'live', attemptId: 'live-attempt', seq, projectionRevision: seq,
  type: 'projection_updated', payload: {projection: {content: `after ${seq}`}, ...extra},
}});
const initial = (): TurnState => reduceTurnState(createTurnState('projection'), {type: 'snapshot', snapshot: {
  conversationRevision: 1, turns: [turn('live', 1), turn('other', 2), turn('branch', 3, 'branch')],
  attempts: [{attemptId: 'live-attempt', turnId: 'live', status: 'running', lastSeq: 1}],
}});
function ports(conversation: ProjectedConversation, state: TurnState, effects: string[]) {
  return {conversation, state, active: true, persist: () => effects.push('persist'),
    replaceAll: () => effects.push('render'), buildNavigation: () => effects.push('navigation'),
    renderConversationList: () => effects.push('catalog'), updateSendButton: () => effects.push('send')};
}

describe('incremental conversation shell projection', () => {
  it('matches the full projection across real reducer transitions and revision-only updates', () => {
    const reconciler = createTurnProjectionReconciler();
    const fast: ProjectedConversation = {}, full: ProjectedConversation = {};
    const store = createTurnStore('projection'); let compared = 0;
    store.subscribe((state, update) => {
      const expected: string[] = [], actual: string[] = [];
      expect(reconciler.apply(ports(fast, state, actual), update)).toBe(applyTurnStateProjection(ports(full, state, expected)));
      expect(actual).toEqual(expected);
      for (const key of ['_serverRev', '_serverTurnCount', '_turnTransport', '_turnSnapshotRequired']) expect(fast[key]).toEqual(full[key]);
      compared++;
    });
    store.dispatch({type: 'snapshot', snapshot: {conversationRevision: 1,
      turns: Object.values(initial().turnsById).filter(value => value !== undefined), attempts: Object.values(initial().attemptsById).filter(value => value !== undefined)}});
    store.dispatch(event(2)); store.dispatch(event(2));
    store.dispatch({type: 'command_pending', turnId: 'live', operation: 'stop'});
    store.dispatch({type: 'command_failed', turnId: 'live'});
    store.dispatch({type: 'transport', status: 'recovering'});
    store.dispatch({type: 'snapshot', snapshot: {conversationRevision: 5}});
    store.dispatch(event(3, {phase: {phase: 'stream_stalled', seq: 1}}));
    store.dispatch(event(4, {turns: [turn('new', 4)], phase: null}));
    store.dispatch({type: 'snapshot', snapshot: {conversationRevision: 6, deletedTurnIds: ['other']}});
    store.dispatch({type: 'snapshot', snapshot: {conversationRevision: 7, authoritativeFull: true,
      turns: [{...turn('live', 1), projectionRevision: 8, status: 'completed'}], attempts: []}});
    expect(compared).toBe(11);
  });

  it('falls back on missed/reset receipts and preserves explicit invalidation and stale DOM repair', () => {
    const reconciler = createTurnProjectionReconciler(), conversation: ProjectedConversation = {};
    const before = initial(); reconciler.apply({conversation, state: before});
    const middle = reduceTurnState(before, event(2));
    const next = reduceTurnState(middle, event(3));
    expect(reconciler.apply({conversation, state: next}, {previousState: middle, action: event(3), changes: {turnIds: [], attemptIds: [], reset: false}})).toBe(true);
    expect(reconciler.apply({conversation, state: next})).toBe(false);
    let renders = 0;
    expect(reconciler.apply({conversation, state: next, active: true, domStale: true, replaceAll: () => renders++})).toBe(false);
    expect(renders).toBe(1);
    conversation._turnProjectionFingerprint = undefined;
    expect(reconciler.apply({conversation, state: next})).toBe(true);
    reconciler.forget(conversation);
    expect(reconciler.apply({conversation, state: next})).toBe(false);
  });

  it('does not read unchanged historical Turns for a valid local receipt', () => {
    const reconciler = createTurnProjectionReconciler(), conversation: ProjectedConversation = {};
    const before = initial(); reconciler.apply({conversation, state: before});
    let changes: TurnStateUpdate['changes'];
    const next = reduceTurnState(before, event(2), {onChanges: value => changes = value});
    const guarded = {...next, turnsById: new Proxy(next.turnsById, {get(target, key) {
      if (key === 'other' || key === 'branch') throw Error('Read unrelated historical Turn');
      return Reflect.get(target, key);
    }})};
    expect(reconciler.apply({conversation, state: guarded}, {previousState: before, action: event(2), changes})).toBe(true);
  });

  it('compares usage, phase and commands exactly, ignoring unlisted cached Turns', () => {
    const reconciler = createTurnProjectionReconciler(); const fast: ProjectedConversation = {}, full: ProjectedConversation = {};
    let state = initial();
    const apply = (next: TurnState, ids: string[]) => {
      const action: TurnAction = {type: 'snapshot', snapshot: {}};
      expect(reconciler.apply({conversation: fast, state: next}, {previousState: state, action, changes: {turnIds: ids, attemptIds: [], reset: false}}))
        .toBe(applyTurnStateProjection({conversation: full, state: next}));
      state = next;
    };
    apply(state, []);
    apply({...state, liveRoundUsageByTurn: {live: {attemptId: 'live-attempt', value: {round: 2, tokensOut: 50, model: 'fixture'}}}}, ['live']);
    apply({...state, livePhase: {phase: 'waiting_model', seq: 2, emittedAt: 1}}, []);
    apply({...state, livePhase: {phase: 'waiting_model', seq: 2, emittedAt: 2}}, []);
    apply({...state, turnsById: {...state.turnsById, unlisted: turn('unlisted', 100)}}, ['unlisted']);
  });

  it('publishes memo ownership before reentrant rendering and isolates conversations', () => {
    const reconciler = createTurnProjectionReconciler(), conversation: ProjectedConversation = {}, peer: ProjectedConversation = {};
    const before = initial(); const next = reduceTurnState(before, event(2));
    reconciler.apply({conversation, state: before, active: true, replaceAll: () => {
      expect(reconciler.apply({conversation, state: next})).toBe(true);
    }});
    expect(reconciler.apply({conversation, state: next})).toBe(false);
    expect(reconciler.apply({conversation: peer, state: next})).toBe(true);
    expect(reconciler.apply({conversation, state: before})).toBe(true);
  });
});
