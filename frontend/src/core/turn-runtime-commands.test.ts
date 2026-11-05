// @vitest-environment jsdom
/** Command outcomes belong to the server; UI adoption belongs to a live store. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createConversationTurnRuntime, type RuntimeConversation, type TurnRuntime } from './turn-runtime';
import type { ConversationSyncApi, ConversationSyncSnapshot, TurnRecord } from '../api/conversation-sync.generated';
import { assertConversationSyncSchema } from '../api/conversation-sync.generated';

const PLAN_ID = 'plan_0123456789abcdef01234567';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function turn(revision = 1): TurnRecord {
  return assertConversationSyncSchema<TurnRecord>('TurnRecord', {
    turnId: 'turn', presentationId: 'turn', conversationId: 'conv',
    laneId: 'main', ordinal: 1, actor: 'assistant', kind: 'reply', runId: '',
    status: 'completed', currentAttemptId: null, projectionRevision: revision,
    createdAt: 1, updatedAt: revision, settlement: {}, projection: {
      content: `authority ${revision}`,
      proposedPlan: { blockId: 'proposed-plan', planId: PLAN_ID, revision: 1,
        format: 'markdown', text: 'An exact accepted plan' },
    } });
}

function snapshot(): ConversationSyncSnapshot {
  return { ok: true, contract: 'tofu.conversation-sync.snapshot/v1',
    scope: { kind: 'conversation', ownerId: 7, threadId: 'conv' }, conversationId: 'conv',
    conversationRevision: 1, syncSeq: 1, cursor: 'cursor-1', serverBootId: 'test',
    heartbeatIntervalMs: 15000, settings: {}, turns: [turn()], attempts: [], queueItems: [] };
}

function acknowledgement(withAttempt = false) {
  const acceptedTurn = turn(2);
  if (withAttempt) {
    acceptedTurn.status = 'running';
    acceptedTurn.currentAttemptId = 'accepted-attempt';
  }
  return { ok: true, conversationId: 'conv', conversationRevision: 2, turn: acceptedTurn,
    ...(withAttempt ? { attempt: { attemptId: 'accepted-attempt', turnId: 'turn',
      conversationId: 'conv', commandId: 'accepted-command', taskId: 'accepted-task',
      operation: 'generate', status: 'running', baseProjectionRevision: 2,
      resumeAnchor: {}, createdAt: 2 } } : {}) };
}

const commandCases = [
  { name: 'submit', api: 'createTurn', withAttempt: true,
    invoke: (r: TurnRuntime, c: RuntimeConversation) => r.submitConversation(c, 'question', {}) },
  { name: 'branch submit', api: 'createTurn', withAttempt: true,
    invoke: (r: TurnRuntime, c: RuntimeConversation) => r.submitBranch(c, { laneId: 'branch' }, 'turn', 'question', {}) },
  { name: 'attempt operation', api: 'createAttempt', withAttempt: true,
    invoke: (r: TurnRuntime, c: RuntimeConversation) => r.operateConversation(c, 'turn', 'retry') },
  { name: 'projection edit', api: 'updateTurn', withAttempt: false,
    invoke: (r: TurnRuntime, c: RuntimeConversation) => r.updateConversationTurn(c, 'turn', { content: 'edit' }) },
  { name: 'plan execution', api: 'executePlan', withAttempt: true,
    invoke: (r: TurnRuntime, c: RuntimeConversation) => r.executeConversationPlan(c, 'turn', PLAN_ID, 1, 'current') },
  { name: 'settled append', api: 'appendSettledTurn', withAttempt: false,
    invoke: (r: TurnRuntime, c: RuntimeConversation) => r.appendSettledConversationTurn(c, 'assistant', { content: 'append' }) },
  { name: 'file undo', api: 'undoFileChanges', withAttempt: false,
    invoke: (r: TurnRuntime, c: RuntimeConversation) => r.mutateConversationFileChanges(c, 'turn', 'undo') },
  { name: 'file redo', api: 'redoFileChanges', withAttempt: false,
    invoke: (r: TurnRuntime, c: RuntimeConversation) => r.mutateConversationFileChanges(c, 'turn', 'redo') },
  { name: 'lane create', api: 'createLane', withAttempt: false,
    invoke: (r: TurnRuntime, c: RuntimeConversation) => r.createBranchLane(c, 'turn') },
  { name: 'lane delete', api: 'deleteLane', withAttempt: false,
    invoke: (r: TurnRuntime, c: RuntimeConversation) => r.deleteBranchLane(c, 'turn', 'branch') },
  { name: 'Turn delete', api: 'deleteTurns', withAttempt: false,
    invoke: (r: TurnRuntime, c: RuntimeConversation) => r.deleteConversationTurns(c, ['turn']) },
  { name: 'queue cancel', api: 'cancelQueue', withAttempt: false,
    invoke: (r: TurnRuntime, c: RuntimeConversation) => r.cancelQueuedTurn(c, 'queue') },
] as const;

class Source extends EventTarget {
  readyState = 1;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  close() { this.readyState = 2; }
}

const cleanups: Array<() => void> = [];
beforeEach(() => vi.useFakeTimers());
afterEach(() => { cleanups.splice(0).forEach(dispose => dispose()); vi.useRealTimers(); });

function fixture() {
  const api = { snapshot: vi.fn(async () => snapshot()),
    createTurn: vi.fn(), createAttempt: vi.fn(), updateTurn: vi.fn(), executePlan: vi.fn(),
    appendSettledTurn: vi.fn(), undoFileChanges: vi.fn(), redoFileChanges: vi.fn(),
    createLane: vi.fn(), deleteLane: vi.fn(), deleteTurns: vi.fn(), cancelQueue: vi.fn(),
    turn: vi.fn(), abortAttempt: vi.fn() };
  const applied = vi.fn();
  const settings = vi.fn();
  const sources: Source[] = [];
  const runtime = createConversationTurnRuntime({ api: api as unknown as ConversationSyncApi,
    isActive: () => false, onStateApplied: applied, applySettings: settings,
    onProtocolError: vi.fn(), eventSourceFactory: () => {
      const source = new Source(); sources.push(source); return source as unknown as EventSource;
    } });
  cleanups.push(() => runtime.disposeConversation('conv'));
  const conversation: RuntimeConversation = { id: 'conv' };
  return { runtime, conversation, api, applied, settings, sources };
}

describe.each(commandCases)('$name command ownership', command => {
  it('keeps a newer hydration claimant when an older command is acknowledged', async () => {
    const f = fixture();
    const store = await f.runtime.hydrateConversation(f.conversation);
    const response = acknowledgement(command.withAttempt);
    const commandGate = deferred<typeof response>();
    const commandStarted = deferred<void>();
    f.api[command.api].mockImplementation(() => { commandStarted.resolve(); return commandGate.promise; });
    const pending = command.invoke(f.runtime, f.conversation);
    await commandStarted.promise;
    const readGate = deferred<ConversationSyncSnapshot>();
    const readStarted = deferred<void>();
    f.api.snapshot.mockImplementationOnce(() => { readStarted.resolve(); return readGate.promise; });
    const replacement = { id: 'conv', title: 'latest hydration claimant' };
    const loading = f.runtime.hydrateConversation(replacement);
    await readStarted.promise;

    commandGate.resolve(response);
    expect(await pending).toBe(response);
    readGate.resolve({ ...snapshot(), conversationRevision: 3, syncSeq: 3,
      cursor: 'cursor-3', turns: [turn(3)] });
    await loading;
    f.applied.mockClear();
    store.dispatch({ type: 'command_response', response: { conversationRevision: 4, turn: turn(4) } });

    expect(f.applied.mock.calls.map(([conversation]) => conversation)).toEqual([replacement]);
    expect(f.api.snapshot).toHaveBeenCalledTimes(2);
  });

  it('adopts the accepted response while its store remains owned', async () => {
    const f = fixture();
    const store = await f.runtime.hydrateConversation(f.conversation);
    const response = acknowledgement(command.withAttempt);
    f.api[command.api].mockResolvedValue(response);

    expect(await command.invoke(f.runtime, f.conversation)).toBe(response);
    expect(store.getState().turnsById.turn?.projection.content).toBe('authority 2');
    expect(f.api[command.api]).toHaveBeenCalledTimes(1);
  });

  it('binds the caller shell when no newer binding overtakes the command', async () => {
    const f = fixture();
    const store = await f.runtime.hydrateConversation(f.conversation);
    const caller = { id: 'conv', title: 'new caller shell' };
    f.api[command.api].mockResolvedValue(acknowledgement(command.withAttempt));
    await command.invoke(f.runtime, caller);
    f.applied.mockClear();

    store.dispatch({ type: 'command_response', response: { conversationRevision: 3, turn: turn(3) } });

    expect(f.applied.mock.calls.map(([conversation]) => conversation)).toEqual([caller]);
  });

  it.each([false, true])('preserves an accepted ACK after disposal (reopened=%s)', async reopen => {
    const f = fixture();
    const store = await f.runtime.hydrateConversation(f.conversation);
    const response = acknowledgement(command.withAttempt);
    const gate = deferred<typeof response>();
    const started = deferred<void>();
    f.api[command.api].mockImplementation(() => { started.resolve(); return gate.promise; });
    const pending = command.invoke(f.runtime, f.conversation);
    await started.promise;
    f.runtime.disposeConversation('conv');
    const staleDispatch = vi.fn();
    const unsubscribe = store.subscribe(staleDispatch);
    const replacement = reopen ? await f.runtime.hydrateConversation({ id: 'conv' }) : null;
    const current = replacement?.getState();
    const sourceCount = f.sources.length;
    f.applied.mockClear(); f.settings.mockClear();
    gate.resolve(response);
    try {
      expect(await pending).toBe(response);
      expect(staleDispatch).not.toHaveBeenCalled();
      expect(f.applied).not.toHaveBeenCalled();
      expect(f.settings).not.toHaveBeenCalled();
      expect(f.runtime.readRuntimeState('conv')).toBe(current ?? null);
      expect(f.sources).toHaveLength(sourceCount);
    } finally { unsubscribe(); }
  });

  it('preserves a rejected command without publishing conflict state to a retired store', async () => {
    const f = fixture();
    const store = await f.runtime.hydrateConversation(f.conversation);
    const gate = deferred<never>();
    const started = deferred<void>();
    f.api[command.api].mockImplementation(() => { started.resolve(); return gate.promise; });
    const error = { status: 409, body: { latestTurn: turn(4) } };
    const pending = command.invoke(f.runtime, f.conversation);
    const rejected = expect(pending).rejects.toBe(error);
    await started.promise;
    f.runtime.disposeConversation('conv');
    const staleDispatch = vi.fn();
    const unsubscribe = store.subscribe(staleDispatch);
    f.applied.mockClear();
    gate.reject(error);
    try {
      await rejected;
      expect(staleDispatch).not.toHaveBeenCalled();
      expect(f.applied).not.toHaveBeenCalled();
      expect(f.runtime.readRuntimeState('conv')).toBeNull();
    } finally { unsubscribe(); }
  });

  it('applies to the current shell without reclaiming a replaced shell', async () => {
    const f = fixture();
    const store = await f.runtime.hydrateConversation(f.conversation);
    const response = acknowledgement(command.withAttempt);
    const gate = deferred<typeof response>();
    const started = deferred<void>();
    f.api[command.api].mockImplementation(() => { started.resolve(); return gate.promise; });
    const pending = command.invoke(f.runtime, f.conversation);
    await started.promise;
    const replacement = { id: 'conv', title: 'current shell' };
    await f.runtime.wakeConversation(replacement);
    f.applied.mockClear(); f.settings.mockClear();
    gate.resolve(response);
    expect(await pending).toBe(response);
    expect(f.applied.mock.calls.every(([conversation]) => conversation === replacement)).toBe(true);
    expect(f.settings.mock.calls.every(([conversation]) => conversation === replacement)).toBe(true);
    f.applied.mockClear();
    store.dispatch({ type: 'command_response', response: { conversationRevision: 3, turn: turn(3) } });
    expect(f.applied.mock.calls.map(([conversation]) => conversation)).toEqual([replacement]);
  });
});

it.each(commandCases.filter(command => [
  'attempt operation', 'projection edit', 'plan execution', 'file undo', 'file redo',
  'lane create', 'lane delete',
].includes(command.name)))('does not dispatch $name after its preparation loses ownership', async command => {
  const f = fixture();
  const store = await f.runtime.hydrateConversation(f.conversation);
  f.api[command.api].mockResolvedValue(acknowledgement(command.withAttempt));
  const pending = command.invoke(f.runtime, f.conversation);
  const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  f.runtime.disposeConversation('conv');
  const staleDispatch = vi.fn();
  const unsubscribe = store.subscribe(staleDispatch);
  try {
    await rejected;
    expect(staleDispatch).not.toHaveBeenCalled();
  } finally { unsubscribe(); }
  expect(f.api[command.api]).not.toHaveBeenCalled();
  expect(f.runtime.readRuntimeState('conv')).toBeNull();
});

it.each(['submit', 'runOperation'] as const)('does not publish or dispatch %s through a retired managed store', async command => {
  const f = fixture();
  const store = await f.runtime.hydrateConversation(f.conversation);
  f.runtime.disposeConversation('conv');
  const staleDispatch = vi.fn();
  const unsubscribe = store.subscribe(staleDispatch);
  try {
    const pending = command === 'submit'
      ? f.runtime.submit(store, null, {})
      : f.runtime.runOperation(store, 'turn', 'retry');
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    expect(staleDispatch).not.toHaveBeenCalled();
    expect(f.api.createTurn).not.toHaveBeenCalled();
    expect(f.api.createAttempt).not.toHaveBeenCalled();
  } finally { unsubscribe(); }
});

it('does not write a restored projection after disposal during the preparatory read', async () => {
  const f = fixture();
  await f.runtime.hydrateConversation(f.conversation);
  const gate = deferred<{ turn: TurnRecord }>();
  const started = deferred<void>();
  f.api.turn.mockImplementation(() => { started.resolve(); return gate.promise; });
  const pending = f.runtime.updateConversationTurn(f.conversation, 'turn', {
    content: 'edit', toolRounds: [{ _snapshotToolResult: {} }],
  });
  const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  await started.promise;
  const readOptions = f.api.turn.mock.calls[0]?.[2];
  f.runtime.disposeConversation('conv');
  gate.resolve({ turn: turn() });
  await rejected;
  expect(f.api.updateTurn).not.toHaveBeenCalled();
  expect(readOptions?.signal?.aborted).toBe(true);
});

it.each(['conversationId', 'turnId'] as const)('rejects a preparatory read with a foreign %s', async field => {
  const f = fixture();
  await f.runtime.hydrateConversation(f.conversation);
  f.api.turn.mockResolvedValue({ turn: { ...turn(), [field]: 'foreign' } });
  f.api.updateTurn.mockResolvedValue(acknowledgement());

  await expect(f.runtime.updateConversationTurn(f.conversation, 'turn', {
    content: 'edit', toolRounds: [{ _snapshotToolResult: {} }],
  })).rejects.toThrow(/mismatched/);

  expect(f.api.updateTurn).not.toHaveBeenCalled();
  expect(f.runtime.readRuntimeState('conv')?.turnsById.turn?.projectionRevision).toBe(1);
});

describe.each(commandCases.filter(command => [
  'attempt operation', 'projection edit', 'plan execution', 'file undo', 'file redo',
].includes(command.name)))('$name conflict identity', command => {
  it.each(['conversationId', 'turnId'] as const)('does not adopt a conflict with a foreign %s', async field => {
    const f = fixture();
    const store = await f.runtime.hydrateConversation(f.conversation);
    const error = { status: 409, body: { latestTurn: { ...turn(4), [field]: 'foreign' } } };
    f.api[command.api].mockRejectedValue(error);

    await expect(command.invoke(f.runtime, f.conversation)).rejects.toBe(error);

    expect(store.getState().turnsById.turn?.projectionRevision).toBe(1);
    expect(store.getState().turnsById.foreign).toBeUndefined();
    expect(store.getState().commandPending.turn).toBeUndefined();
  });
});

it('does not adopt a foreign-conversation conflict during submission', async () => {
  const f = fixture();
  const store = await f.runtime.hydrateConversation(f.conversation);
  const error = { status: 409, body: { latestTurn: { ...turn(4), conversationId: 'foreign' } } };
  f.api.createTurn.mockRejectedValue(error);
  await expect(f.runtime.submitConversation(f.conversation, 'question', {})).rejects.toBe(error);
  expect(store.getState().turnsById.turn?.projectionRevision).toBe(1);
});

it('retains standalone store operation semantics and the original conflict error', async () => {
  const f = fixture();
  const store = f.runtime.createStore('conv');
  store.dispatch({ type: 'snapshot', snapshot: snapshot() });
  const error = { status: 409, body: { latestTurn: turn(4) } };
  f.api.createAttempt.mockRejectedValue(error);
  await expect(f.runtime.runOperation(store, 'turn', 'retry')).rejects.toBe(error);
  expect(store.getState().turnsById.turn?.projection.content).toBe('authority 4');
  expect(store.getState().commandPending.turn).toBeUndefined();
});

it('routes terminal command hooks to the replacement shell', async () => {
  const f = fixture();
  await f.runtime.hydrateConversation(f.conversation);
  f.api.createAttempt.mockResolvedValue(acknowledgement(true));
  await f.runtime.operateConversation(f.conversation, 'turn', 'retry');
  const replacement = { id: 'conv', title: 'replacement' };
  await f.runtime.wakeConversation(replacement);
  f.applied.mockClear();

  f.sources[0].onmessage?.(new MessageEvent('message', { lastEventId: 'cursor-2', data: JSON.stringify({
    contract: 'tofu.conversation-sync.event/v1', conversationId: 'conv',
    type: 'attempt.event', syncSeq: 2, occurredAt: 2,
    payload: { event: { conversationId: 'conv', turnId: 'turn', attemptId: 'accepted-attempt',
      seq: 2, projectionRevision: 3, type: 'terminal_settlement',
      payload: { status: 'completed', settlement: {}, projection: { content: 'authority 3' } } } },
  }) }));

  expect(f.runtime.readRuntimeState('conv')?.turnsById.turn?.status).toBe('completed');
  expect(f.applied).toHaveBeenCalled();
  expect(f.applied.mock.calls.every(([conversation]) => conversation === replacement)).toBe(true);
});

it('does not allocate a new runtime for late failure cleanup or an ownerless abort', async () => {
  const f = fixture();
  await f.runtime.hydrateConversation(f.conversation);
  f.runtime.disposeConversation('conv');

  f.runtime.markCommandFailed(f.conversation, 'turn');
  expect(f.runtime.readRuntimeState('conv')).toBeNull();
  await expect(f.runtime.abortConversation(f.conversation)).resolves.toBeNull();
  expect(f.runtime.readRuntimeState('conv')).toBeNull();
  expect(f.api.abortAttempt).not.toHaveBeenCalled();
});
