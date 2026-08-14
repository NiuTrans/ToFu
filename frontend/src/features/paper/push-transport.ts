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
  paperIngestEvent?: typeof paperIngestEvent;
  paperAttachPush?: typeof paperAttachPush;
  paperDetachPush?: typeof paperDetachPush;
  taskReplayIngestPage?: typeof taskReplayIngestPage;
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

/** Install compatibility names consumed by the remaining classic scripts. */
export function installPaperPushGlobals(): void {
  const target = pushWindow();
  target.paperIngestEvent = paperIngestEvent;
  target.paperAttachPush = paperAttachPush;
  target.paperDetachPush = paperDetachPush;
  target.taskReplayIngestPage = taskReplayIngestPage;
}

installPaperPushGlobals();
