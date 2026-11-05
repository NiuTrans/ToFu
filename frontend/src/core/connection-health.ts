/** Declared state owner for conversation and background-task transport health. */

import type { ConnectionHealth, ConnectionHealthState } from
  '../api/conversation-sync.generated';
import { notifyObserver } from './observer';

export interface AggregateConnectionHealth {
  degraded: boolean;
  count: number;
  at: number;
}

type HealthListener = (health: ConnectionHealth) => void;
type AggregateListener = (health: AggregateConnectionHealth) => void;

const DEGRADED_STATES = new Set<ConnectionHealthState>(['degraded', 'offline']);

export class ConnectionHealthStore {
  private readonly states = new Map<string, ConnectionHealth>();
  private readonly listeners = new Map<string, Set<HealthListener>>();
  private readonly aggregateListeners = new Set<AggregateListener>();
  private degradedCount = 0;
  private emittedAggregate: AggregateConnectionHealth | null = null;

  get(conversationId: string): ConnectionHealth | undefined {
    return this.states.get(conversationId);
  }

  set(conversationId: string, health: ConnectionHealth): void {
    const previous = this.states.get(conversationId);
    if (previous && previous.state === health.state
        && previous.transport === health.transport
        && previous.observedAt === health.observedAt
        && previous.generation === health.generation
        && previous.reason === health.reason
        && previous.lastFrameAt === health.lastFrameAt
        && previous.retryCount === health.retryCount) return;
    const published = Object.freeze({ ...health });
    this.states.set(conversationId, published);
    this.degradedCount += Number(DEGRADED_STATES.has(published.state))
      - Number(Boolean(previous && DEGRADED_STATES.has(previous.state)));
    for (const listener of [...this.listeners.get(conversationId) ?? []]) {
      // A listener can synchronously replace/clear this record. Later listeners
      // must not see older evidence after the nested publication completed.
      if (this.states.get(conversationId) !== published) break;
      notifyObserver('connection-health', listener, published);
    }
    this.emitAggregate();
  }

  setTaskStreamDegraded(conversationId: string, degraded: boolean): void {
    const previous = this.states.get(conversationId);
    // A conversation coordinator is the declared v3 transport owner. The old
    // per-bubble timer still renders elapsed time, but it must never replace a
    // heartbeat-backed state with a guessed "reconnecting" verdict during a
    // quiet model/tool phase.
    if (previous?.transport === 'conversation-sse') return;
    if (!degraded) {
      this.clear(conversationId);
      return;
    }
    const now = Date.now();
    this.set(conversationId, {
      state: 'degraded',
      transport: 'task-sse',
      observedAt: now,
      generation: previous?.generation ?? 0,
      lastFrameAt: previous?.lastFrameAt,
      retryCount: previous?.retryCount ?? 0,
      reason: 'task-stream-silence',
    });
  }

  clear(conversationId: string): void {
    const previous = this.states.get(conversationId);
    if (!previous) return;
    this.states.delete(conversationId);
    this.degradedCount -= Number(DEGRADED_STATES.has(previous.state));
    this.emitAggregate();
  }

  subscribe(conversationId: string, listener: HealthListener): () => void {
    const bucket = this.listeners.get(conversationId) ?? new Set<HealthListener>();
    bucket.add(listener);
    this.listeners.set(conversationId, bucket);
    const current = this.states.get(conversationId);
    if (current) notifyObserver('connection-health', listener, current);
    return () => {
      bucket.delete(listener);
      if (!bucket.size) this.listeners.delete(conversationId);
    };
  }

  aggregate(): AggregateConnectionHealth {
    const count = this.degradedCount;
    return { degraded: count > 0, count, at: Date.now() };
  }

  subscribeAggregate(listener: AggregateListener): () => void {
    this.aggregateListeners.add(listener);
    notifyObserver('connection-health-aggregate', listener, this.aggregate());
    return () => this.aggregateListeners.delete(listener);
  }

  private emitAggregate(): void {
    // Commit the counter before callbacks, then derive from the current value:
    // nested set/clear may have superseded the caller's original transition.
    if ((this.emittedAggregate?.count ?? 0) === this.degradedCount) return;
    const aggregate = Object.freeze(this.aggregate());
    this.emittedAggregate = aggregate;
    for (const listener of [...this.aggregateListeners]) {
      // Count can return to the same value during nested clear/set (ABA).
      // Publication identity also fences older timestamps after that reentry.
      if (this.emittedAggregate !== aggregate) break;
      notifyObserver('connection-health-aggregate', listener, aggregate);
    }
  }
}

export const conversationConnectionHealth = new ConnectionHealthStore();
