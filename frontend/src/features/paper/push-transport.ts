/** Paper task replay and observation lifetimes; domain projections remain injected. */
import { featureRegistry } from '../../feature-registry';
export interface PaperPushEvent extends Record<string, unknown> {
  seq?: number;
  type?: string;
}

type PaperPushHandler = (event: PaperPushEvent) => void;

export interface PaperPushState {
  _seqSeen?: number;
  _pushTaskId?: string;
  _pushHandler?: PaperPushHandler;
  _pushChannel?: PaperPushChannel;
  _replayCursor?: number;
}

export type PaperPushChannel = string;

export interface PaperPushOptions {
  channel?: PaperPushChannel;
  isCurrent?: () => boolean;
  onEvent: PaperPushHandler;
  isTerminal?: (event: PaperPushEvent) => boolean;
}

type PushWindow = Window & {
  pushSubscribe?: (
    channel: string,
    taskId: string,
    handler: PaperPushHandler,
  ) => void;
  pushUnsubscribe?: (
    channel: string,
    taskId: string,
    handler?: PaperPushHandler,
  ) => void;
};

export interface TaskReplayPage {
  events?: unknown;
  next_cursor?: unknown;
  cursor?: unknown;
  cursorInfo?: unknown;
  status?: unknown;
  done?: unknown;
}

export interface TaskReplayIngestResult {
  nextCursor: number;
  status: string;
  done: boolean;
  cursorReset: boolean;
  accepted: number;
  changed: boolean;
}

function pushWindow(): PushWindow {
  return featureRegistry as unknown as PushWindow;
}

/** Ordered, exactly-once ingest gate shared by push and polling. */
export function paperIngestEvent<TState extends PaperPushState, TResult>(
  state: TState | null | undefined,
  event: PaperPushEvent | null | undefined,
  apply: ((state: TState, event: PaperPushEvent) => TResult) | null | undefined,
): TResult | false {
  if (!state || !event || typeof apply !== 'function') return false;
  if (typeof event.seq === 'number') {
    if (state._seqSeen == null) state._seqSeen = -1;
    if (event.seq <= state._seqSeen) return false;
    state._seqSeen = event.seq;
  }
  return apply(state, event);
}

function replayNumber(value: unknown, fallback: number): number {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? Math.floor(number) : fallback;
}

/** Fold one producer-owned tofu.task-replay/v1 page through the seq gate. */
export function taskReplayIngestPage<TState extends PaperPushState>(
  state: TState,
  page: TaskReplayPage | null | undefined,
  apply: (state: TState, event: PaperPushEvent) => unknown,
  requestedCursor = state._replayCursor ?? 0,
): TaskReplayIngestResult {
  const events = Array.isArray(page?.events)
    ? page.events.filter((event): event is PaperPushEvent => Boolean(
      event && typeof event === 'object'))
    : [];
  const cursorObject = page?.cursor && typeof page.cursor === 'object'
    ? page.cursor as Record<string, unknown>
    : page?.cursorInfo && typeof page.cursorInfo === 'object'
      ? page.cursorInfo as Record<string, unknown>
      : null;
  const fallbackNext = typeof page?.cursor === 'number'
    ? page.cursor
    : requestedCursor;
  const nextCursor = replayNumber(
    cursorObject?.next ?? page?.next_cursor,
    replayNumber(fallbackNext, requestedCursor),
  );
  const cursorReset = cursorObject?.reset === true;
  if (cursorReset) {
    const firstSequence = events.find(event => typeof event.seq === 'number')?.seq;
    state._seqSeen = Math.max(-1, (firstSequence ?? nextCursor) - 1);
  }

  let accepted = 0;
  let changed = false;
  for (const event of events) {
    const before = state._seqSeen;
    const result = paperIngestEvent(state, event, apply);
    if (typeof event.seq !== 'number' || state._seqSeen !== before) accepted += 1;
    if (result) changed = true;
  }
  state._replayCursor = nextCursor;
  const status = typeof page?.status === 'string' ? page.status : '';
  return {
    nextCursor,
    status,
    done: page?.done === true || defaultTerminal({ type: status }),
    cursorReset,
    accepted,
    changed,
  };
}

function defaultTerminal(event: PaperPushEvent): boolean {
  return event.type === 'done'
    || event.type === 'error'
    || event.type === 'aborted';
}

export interface PaperTaskSnapshot extends TaskReplayPage {
  ok?: boolean;
  [key: string]: any;
}

interface PaperTaskObserverPorts {
  readonly channel: PaperPushChannel;
  readonly pollIntervalMs: number;
  readonly state: () => { status: string; lastEventAt: number };
  readonly taskId: () => string;
  readonly read: (taskId: string, cursor: number) => Promise<Response | null | undefined>;
  readonly consume: (event: PaperPushEvent) => unknown;
  readonly snapshot: (page: PaperTaskSnapshot) => void;
  readonly terminal: (page: PaperTaskSnapshot) => void | Promise<void>;
  readonly lost: () => void;
  readonly presentation: {
    readonly render: () => void;
    readonly progress: () => void;
    readonly activity: () => void;
  };
  readonly warn: (error: unknown) => void;
}

// Existing Paper media observation contract: five failed reads expose `lost`;
// the visible activity clock advances each second independently of polling.
const MEDIA_POLL_FAILURE_LIMIT = 5;
const MEDIA_ACTIVITY_TICK_MS = 1000;

/**
 * One media task observation lifetime, shared by Podcast and Video.
 * Owns the cursor, sequence gate, subscription, read exclusion and timers.
 * Domain callbacks retain snapshot/terminal projection and error copy; stopping
 * observation never aborts the durable server task. No resources start on import.
 */
export function createPaperTaskObserver(ports: PaperTaskObserverPorts) {
  const replay: PaperPushState = {};
  let cursor = 0;
  let failures = 0;
  let generation = 0;
  let busy: { generation: number } | null = null;
  let pollTimer: number | null = null;
  let activityTimer: number | null = null;

  const stopPoll = (): void => {
    if (pollTimer != null) window.clearTimeout(pollTimer);
    pollTimer = null;
  };
  const startActivity = (): void => {
    if (activityTimer != null) return;
    activityTimer = window.setInterval(ports.presentation.activity, MEDIA_ACTIVITY_TICK_MS);
  };
  const stop = (): void => {
    generation += 1;
    stopPoll();
    if (activityTimer != null) window.clearInterval(activityTimer);
    activityTimer = null;
    paperDetachPush(replay);
  };
  const reset = (): void => {
    stop();
    cursor = failures = 0;
    replay._seqSeen = -1;
    replay._replayCursor = 0;
  };
  const paint = (changed: unknown): void => {
    if (changed) ports.presentation.render(); else ports.presentation.progress();
    ports.presentation.activity();
  };
  const attach = (): void => {
    const taskId = ports.taskId();
    const attachedGeneration = generation;
    if (!taskId) return;
    paperAttachPush(replay, taskId, {
      channel: ports.channel,
      isCurrent: () => attachedGeneration === generation && ports.taskId() === taskId,
      onEvent: (event) => {
        ports.state().lastEventAt = Date.now();
        paint(paperIngestEvent(replay, event, (_state, row) => ports.consume(row)));
        if (defaultTerminal(event)) void poll();
      },
    });
  };
  const schedule = (): void => {
    stopPoll();
    attach();
    pollTimer = window.setTimeout(() => void poll(), ports.pollIntervalMs);
  };
  const fail = (): void => {
    failures += 1;
    if (failures >= MEDIA_POLL_FAILURE_LIMIT) {
      stop();
      ports.lost();
    } else schedule();
  };
  const poll = async (): Promise<void> => {
    const taskId = ports.taskId();
    if (!taskId || busy?.generation === generation) return;
    stopPoll();
    const request = { generation };
    busy = request;
    const current = () => request.generation === generation && ports.taskId() === taskId;
    try {
      const transport = await ports.read(taskId, cursor);
      // The API transport owns v4 envelope decoding on Response.json().
      const response: PaperTaskSnapshot | null = transport?.ok ? await transport.json() : null;
      if (!current()) return;
      if (!response?.ok) { fail(); return; }
      failures = 0;
      const page = taskReplayIngestPage(replay, response,
        (_state, event) => ports.consume(event), cursor);
      if (page.accepted) ports.state().lastEventAt = Date.now();
      cursor = page.nextCursor;
      ports.snapshot(response);
      if (response.done) {
        stop();
        await ports.terminal(response);
        return;
      }
      if (page.changed) ports.presentation.render(); else ports.presentation.progress();
      if (ports.state().status === 'generating') startActivity();
      ports.presentation.activity();
      schedule();
    } catch (error: unknown) {
      ports.warn(error);
      if (current()) fail();
    } finally {
      // A detached read cannot release the exclusion owned by its replacement.
      if (busy === request) busy = null;
    }
  };
  return { reset, schedule, poll, startActivity, stop };
}

/** Bind the paper push channel idempotently for one state/task pair. */
export function paperAttachPush(
  state: PaperPushState | null | undefined,
  taskId: string,
  options: PaperPushOptions | null | undefined,
): void {
  if (!state || !taskId || !options || typeof options.onEvent !== 'function') {
    return;
  }
  const transport = pushWindow();
  if (typeof transport.pushSubscribe !== 'function') return;
  if (state._pushTaskId === taskId) return;
  paperDetachPush(state);

  const isCurrent = options.isCurrent ?? (() => true);
  const isTerminal = options.isTerminal ?? defaultTerminal;
  const channel = options.channel ?? 'paper';
  const handler: PaperPushHandler = (event) => {
    if (!isCurrent() || !event?.type) return;
    try {
      options.onEvent(event);
    } catch (error: unknown) {
      console.debug('[Paper:Push] handler failed:', error);
    }
    if (isTerminal(event)) paperDetachPush(state);
  };

  try {
    transport.pushSubscribe(channel, taskId, handler);
    state._pushTaskId = taskId;
    state._pushHandler = handler;
    state._pushChannel = channel;
  } catch (error: unknown) {
    console.debug('[Paper:Push] subscribe failed:', error);
  }
}

/** Release only the subscription owned by this state. */
export function paperDetachPush(
  state: PaperPushState | null | undefined,
): void {
  if (!state?._pushTaskId) return;
  const taskId = state._pushTaskId;
  const handler = state._pushHandler;
  const channel = state._pushChannel ?? 'paper';
  try {
    const transport = pushWindow();
    if (typeof transport.pushUnsubscribe === 'function') {
      transport.pushUnsubscribe(channel, taskId, handler);
    }
  } catch (error: unknown) {
    console.debug('[Paper:Push] unsubscribe failed:', error);
  }
  state._pushTaskId = '';
  state._pushHandler = undefined;
  state._pushChannel = undefined;
}
