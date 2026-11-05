/**
 * One page-wide Server-Sent Events response for every live conversation
 * stream (tofu.conversation-sync.multiplexed-events/v1).
 *
 * An HTTP/1.x browser allows six connections per origin and an EventSource
 * holds one for its whole life, so one stream per conversation starved every
 * other request once six conversations were live. Each coordinator still owns
 * its conversation's cursor, recovery and silence deadline through a source
 * that behaves like that conversation's own EventSource; this owner only
 * shares the transport.
 *
 * Entry: createConversationEventMultiplexer(...).open(request).
 * Membership owns every resource: a source joins on open and leaves on close
 * or stream.closed. A membership change replaces the response once per
 * microtask; no member means no response, timer or listener.
 */
import {
  CONVERSATION_SYNC_STREAM_POLICY,
  assertConversationSyncSchema,
  type ConversationEventSubscription,
  type ConversationStreamClosed,
  type ConversationSyncApi,
} from '../api/conversation-sync.generated';
import { createLifecycleScope, type LifecycleScope } from '../lifecycle';
import { notifyObserver } from './observer';

export const CONVERSATION_STREAM_EVENT_NAMES = [
  'turn.upsert',
  'turn.patch',
  'turn.deleted',
  'attempt.event',
  'conversation.activity',
  'sync.heartbeat',
  'sync.reset_required',
] as const;

const STREAM_CLOSED_EVENT = CONVERSATION_SYNC_STREAM_POLICY.streamClosedEvent;
const CONNECTING = 0;
const OPEN = 1;
const CLOSED = 2;

/** The EventSource surface one conversation's stream consumer uses. */
export interface ConversationEventSource extends EventTarget {
  readonly readyState: number;
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  close(): void;
}

/** One conversation's replay request. Stream generations belong to the transport. */
export interface ConversationStreamRequest {
  conversationId: string;
  after: string;
  windowBytes?: number;
  cursorSource?: 'snapshot';
}

export type ConversationEventSourceFactory =
  (request: ConversationStreamRequest) => ConversationEventSource;

/** Reconnect backoff shared by every conversation stream recovery path. */
export function conversationStreamRetryDelayMs(failures: number): number {
  return Math.min(500 * (2 ** Math.min(failures, 5)), 15_000);
}

export interface ConversationEventMultiplexerOptions {
  api: Pick<ConversationSyncApi, 'multiplexedEventsUrl'>;
  streamClientId: string;
  eventSourceFactory?: (url: string) => EventSource;
  onProtocolError?(error: Error): void;
}

interface Connection {
  readonly native: EventSource;
  readonly members: readonly MultiplexedConversationSource[];
  readonly scope: LifecycleScope;
}

class MultiplexedConversationSource extends EventTarget implements ConversationEventSource {
  readyState = CONNECTING;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  /** Newest id received on this stream: a resumed response replays after it. */
  after: string;
  cursorSource: 'snapshot' | undefined;
  servedBy: Connection | null = null;

  constructor(
    readonly request: ConversationStreamRequest,
    private readonly release: (source: MultiplexedConversationSource) => void,
  ) {
    super();
    this.after = request.after;
    this.cursorSource = request.cursorSource;
    this.addEventListener('open', event => this.onopen?.(event));
    this.addEventListener('error', event => this.onerror?.(event));
    this.addEventListener('message', event => this.onmessage?.(event as MessageEvent));
  }

  close(): void {
    if (this.readyState === CLOSED) return;
    this.readyState = CLOSED;
    this.release(this);
  }

  subscription(streamGeneration: number): ConversationEventSubscription {
    return {
      conversationId: this.request.conversationId,
      streamGeneration,
      ...(this.after ? { after: this.after } : {}),
      ...(this.request.windowBytes ? { windowBytes: this.request.windowBytes } : {}),
      // A stream that received nothing yet repeats its snapshot bootstrap,
      // exactly as a native reconnect without Last-Event-ID would.
      ...(this.cursorSource ? { cursorSource: this.cursorSource } : {}),
    };
  }
}

export function createConversationEventMultiplexer(
  options: ConversationEventMultiplexerOptions,
): { open: ConversationEventSourceFactory } {
  const members = new Set<MultiplexedConversationSource>();
  let connection: Connection | null = null;
  let connectQueued = false;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let failures = 0;
  // Every response supersedes the previous one's subscriptions server-side; a
  // late-arriving older request is then refused as a stale generation.
  let streamGeneration = 0;

  const protocolError = (error: unknown): void => {
    notifyObserver('conversation-stream-protocol-error', options.onProtocolError,
      error instanceof Error ? error : new Error(String(error)));
  };

  const closeConnection = (): void => {
    const current = connection;
    connection = null;
    if (!current) return;
    for (const member of current.members) {
      if (member.servedBy === current) member.servedBy = null;
    }
    current.scope.destroy();
  };

  const stop = (): void => {
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = null;
    failures = 0;
    closeConnection();
  };

  const scheduleConnect = (): void => {
    if (connectQueued || retryTimer) return;
    connectQueued = true;
    queueMicrotask(() => {
      connectQueued = false;
      // A failure after queueing claimed a backed-off retry, which connects
      // the membership current at that time.
      if (!retryTimer) connect();
    });
  };

  /** Members that lost their response: refused ones end like their own
   * stream would have; the rest wait for one backed-off replacement. */
  const fail = (affected: MultiplexedConversationSource[], refused: boolean): void => {
    if (refused) {
      for (const member of affected) {
        member.readyState = CLOSED;
        members.delete(member);
      }
      if (members.size) scheduleConnect();
      else stop();
    } else {
      failures += 1;
      for (const member of affected) member.readyState = CONNECTING;
      // Claim the retry before observers run: a reentrant open joins it.
      if (members.size && !retryTimer) {
        retryTimer = setTimeout(() => {
          retryTimer = null;
          connect();
        }, conversationStreamRetryDelayMs(failures));
      }
    }
    for (const member of affected) member.dispatchEvent(new Event('error'));
  };

  const endMember = (member: MultiplexedConversationSource, body: string): void => {
    try {
      const closed = assertConversationSyncSchema<ConversationStreamClosed>(
        'ConversationStreamClosed', JSON.parse(body) as unknown);
      if (closed.conversationId !== member.request.conversationId) {
        throw new Error('Conversation stream closure identity mismatch');
      }
    } catch (error) {
      // The event type alone ends the stream; a malformed body is reported.
      protocolError(error);
    }
    member.servedBy = null;
    member.readyState = CLOSED;
    members.delete(member);
    if (!members.size) stop();
    member.dispatchEvent(new Event('error'));
  };

  const deliver = (current: Connection, event: MessageEvent<string>): void => {
    const data = typeof event.data === 'string' ? event.data : '';
    const framing = /^(\d+)\n/.exec(data);
    const member = framing ? current.members[Number(framing[1])] : undefined;
    if (!framing || !member) {
      protocolError(new Error('Multiplexed conversation frame has no valid stream index'));
      const affected = current.members.filter(item => item.servedBy === current);
      closeConnection();
      fail(affected, false);
      return;
    }
    failures = 0;
    // A member that left keeps streaming until the replacement supersedes it.
    if (member.servedBy !== current) return;
    const body = data.slice(framing[0].length);
    if (event.type === STREAM_CLOSED_EVENT) {
      endMember(member, body);
      return;
    }
    if (event.lastEventId) {
      member.after = event.lastEventId;
      member.cursorSource = undefined;
    }
    member.dispatchEvent(new MessageEvent(event.type, {
      data: body, lastEventId: event.lastEventId,
    }));
  };

  function connect(): void {
    closeConnection();
    if (!members.size) return;
    const streams = [...members];
    streamGeneration += 1;
    let native: EventSource;
    try {
      const url = options.api.multiplexedEventsUrl(
        options.streamClientId,
        streams.map(member => member.subscription(streamGeneration)),
      );
      native = (options.eventSourceFactory ?? (value => new EventSource(value)))(url);
    } catch (error) {
      protocolError(error);
      fail(streams, false);
      return;
    }
    const scope = createLifecycleScope();
    const current: Connection = { native, members: streams, scope };
    connection = current;
    for (const member of streams) member.servedBy = current;
    scope.add(() => native.close());
    scope.listen(native, 'open', () => {
      if (connection !== current) return;
      for (const member of streams) {
        if (member.servedBy !== current || member.readyState !== CONNECTING) continue;
        member.readyState = OPEN;
        member.dispatchEvent(new Event('open'));
      }
    });
    scope.listen(native, 'error', () => {
      if (connection !== current) return;
      // A refused response (HTTP error status) is CLOSED. Otherwise the native
      // retry would replay every stream from its bootstrap cursor, so the
      // response is replaced from each stream's newest id instead.
      const refused = native.readyState === CLOSED;
      const affected = streams.filter(member => member.servedBy === current);
      closeConnection();
      if (!refused && !affected.length) {
        // The response ended after its last stream closed.
        scheduleConnect();
        return;
      }
      fail(affected, refused);
    });
    for (const name of [...CONVERSATION_STREAM_EVENT_NAMES, STREAM_CLOSED_EVENT]) {
      scope.listen(native, name, event => {
        if (connection === current) deliver(current, event as MessageEvent<string>);
      });
    }
  }

  const release = (member: MultiplexedConversationSource): void => {
    if (!members.delete(member)) return;
    const served = member.servedBy !== null;
    member.servedBy = null;
    if (!members.size) stop();
    else if (served) scheduleConnect();
  };

  return {
    open(request) {
      if (!options.streamClientId) {
        throw new Error('Multiplexed conversation streams require a stream client id');
      }
      // The response admits each conversation once; a newer request
      // supersedes the older stream as the server would.
      const superseded = [...members].filter(
        existing => existing.request.conversationId === request.conversationId);
      for (const existing of superseded) {
        existing.servedBy = null;
        existing.readyState = CLOSED;
        members.delete(existing);
      }
      const member = new MultiplexedConversationSource(request, release);
      members.add(member);
      scheduleConnect();
      for (const existing of superseded) existing.dispatchEvent(new Event('error'));
      return member;
    },
  };
}
