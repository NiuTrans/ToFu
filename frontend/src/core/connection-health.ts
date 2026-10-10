/** Declared state owner for conversation and background-task transport health. */

import type { ConnectionHealth, ConnectionHealthState } from
  '../api/conversation-sync.generated';

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

  get(conversationId: string): ConnectionHealth | undefined {
    return this.states.get(conversationId);
  }

  set(conversationId: string, health: ConnectionHealth): void {
    const previous = this.states.get(conversationId);
    if (previous && previous.state === health.state
        && previous.generation === health.generation
        && previous.reason === health.reason
        && previous.lastFrameAt === health.lastFrameAt
        && previous.retryCount === health.retryCount) return;
    this.states.set(conversationId, Object.freeze({ ...health }));
    for (const listener of this.listeners.get(conversationId) ?? []) {
      listener(health);
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
    if (!this.states.delete(conversationId)) return;
    this.emitAggregate();
  }

  subscribe(conversationId: string, listener: HealthListener): () => void {
    const bucket = this.listeners.get(conversationId) ?? new Set<HealthListener>();
    bucket.add(listener);
    this.listeners.set(conversationId, bucket);
    const current = this.states.get(conversationId);
    if (current) listener(current);
    return () => {
      bucket.delete(listener);
      if (!bucket.size) this.listeners.delete(conversationId);
    };
  }

  aggregate(): AggregateConnectionHealth {
    const count = Array.from(this.states.values())
      .filter((health) => DEGRADED_STATES.has(health.state)).length;
    return { degraded: count > 0, count, at: Date.now() };
  }

  subscribeAggregate(listener: AggregateListener): () => void {
    this.aggregateListeners.add(listener);
    listener(this.aggregate());
    return () => this.aggregateListeners.delete(listener);
  }

  private emitAggregate(): void {
    const aggregate = this.aggregate();
    for (const listener of this.aggregateListeners) listener(aggregate);
  }
}

export const conversationConnectionHealth = new ConnectionHealthStore();
