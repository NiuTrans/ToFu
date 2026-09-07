/**
 * Bounded browser-side timing receipts for the durable attempt timing trace.
 *
 * The conversation projection remains the only state authority. This module
 * observes already-authoritative attempt/transport changes, waits until the
 * subscribing renderer has had a browser paint opportunity, and appends only
 * content-free timing metadata through the generated conversation API.
 */

import type {
  AttemptEvent,
  ConnectionHealth,
  ConversationSyncApi,
  RecordPerceptionRequest,
} from '../api/conversation-sync.generated';
import { scheduleAnimationFrame } from '../conversation/ui/animation-frame-scheduler';

type UnknownRecord = Record<string, unknown>;

export interface PerceptionAttemptIdentity {
  turnId: string;
  attemptId: string;
  projectionRevision: number;
}

export interface PerceptionRecorderOptions {
  api: ConversationSyncApi;
  clientId?: string;
  now?: () => number;
  scheduleAfterPaint?: (
    callback: (paintedAt: number | null) => void,
  ) => () => void;
}

type PendingObservation = {
  conversationId: string;
  turnId: string;
  body: RecordPerceptionRequest;
  attempts: number;
};

type PendingPaint = {
  conversationId: string;
  cancel: () => void;
};

type DegradedInterval = {
  startedAt: number;
  lastSignature: string;
  identity: PerceptionAttemptIdentity | null;
};

const MAX_PENDING_OBSERVATIONS = 32;
const MAX_PENDING_PAINTS = 64;
const MAX_TRACKED_PHASES = 128;
const MAX_SEND_ATTEMPTS = 5;
const MAX_RETRY_DELAY_MS = 15_000;

function record(value: unknown): UnknownRecord | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : null;
}

function text(value: unknown, maximum: number): string {
  return typeof value === 'string' ? value.slice(0, maximum) : '';
}

function nonnegative(value: unknown, fallback = 0): number {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.trunc(number)) : fallback;
}

function primitiveRecordFingerprint(value: unknown): string {
  const source = record(value);
  if (!source) return '';
  return Object.keys(source).sort().slice(0, 16).map((key) => {
    const child = source[key];
    if (child === null || typeof child === 'boolean'
        || typeof child === 'number' || typeof child === 'string') {
      return `${key.slice(0, 80)}=${String(child).slice(0, 200)}`;
    }
    return '';
  }).filter(Boolean).join('&');
}

function safeClientId(value: string | undefined): string {
  const normalized = String(value || '').trim().slice(0, 64);
  return normalized || 'browser';
}

function randomNonce(): string {
  try {
    const values = new Uint32Array(2);
    globalThis.crypto?.getRandomValues(values);
    if (values.some((value) => value !== 0)) {
      return Array.from(values, (value) => value.toString(36)).join('');
    }
  } catch {
    // A deterministic counter below still keeps this queue internally unique.
  }
  return Math.floor(Math.random() * 0xffffffff).toString(36);
}

function pageVisibility(): 'visible' | 'hidden' {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden'
    ? 'hidden' : 'visible';
}

function defaultScheduleAfterPaint(
  callback: (paintedAt: number | null) => void,
): () => void {
  let finished = false;
  let cancelFirstFrame: (() => void) | null = null;
  let cancelSecondFrame: (() => void) | null = null;
  let timeout: ReturnType<typeof setTimeout> | null = null;
  const finish = (paintedAt: number | null): void => {
    if (finished) return;
    finished = true;
    if (timeout !== null) clearTimeout(timeout);
    callback(paintedAt);
  };
  cancelFirstFrame = scheduleAnimationFrame(globalThis, () => {
    cancelSecondFrame = scheduleAnimationFrame(globalThis, () => {
      finish(pageVisibility() === 'visible' ? Date.now() : null);
    });
  });
  // A hidden/throttled or blocked page has no confirmed paint. Retire the
  // bounded pending slot and report the evidence gap on a later receipt;
  // never label a timeout as something the user actually saw.
  timeout = setTimeout(() => finish(null), 2_000);
  return () => {
    if (finished) return;
    finished = true;
    if (timeout !== null) clearTimeout(timeout);
    cancelFirstFrame?.();
    cancelSecondFrame?.();
  };
}

function retryableFailure(error: unknown): boolean {
  const status = nonnegative(record(error)?.status);
  return status === 0 || status === 408 || status === 429 || status >= 500;
}

export class TurnPerceptionRecorder {
  private readonly api: ConversationSyncApi;
  private readonly clientId: string;
  private readonly enabled: boolean;
  private readonly now: () => number;
  private readonly scheduleAfterPaint: PerceptionRecorderOptions['scheduleAfterPaint'];
  private readonly nonce = randomNonce();
  private readonly pending: PendingObservation[] = [];
  private readonly pendingPaints = new Map<string, PendingPaint>();
  private readonly phaseFingerprints = new Map<string, string>();
  private readonly degradedIntervals = new Map<string, DegradedInterval>();
  private readonly droppedByConversation = new Map<string, number>();
  private sequence = 0;
  private paintSequence = 0;
  private draining = false;
  private disposed = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(options: PerceptionRecorderOptions) {
    this.api = options.api;
    this.enabled = typeof options.api.recordPerception === 'function';
    this.clientId = safeClientId(options.clientId);
    this.now = options.now ?? (() => Date.now());
    this.scheduleAfterPaint = options.scheduleAfterPaint
      ?? defaultScheduleAfterPaint;
  }

  observeAttemptEvent(
    event: AttemptEvent,
    receivedAt: number,
    serverPublishedAt?: number,
  ): void {
    if (this.disposed || !this.enabled) return;
    const phase = record(event.payload.phase);
    const phaseName = text(phase?.phase, 80);
    if (phaseName) {
      const key = `${event.conversationId}\u0000${event.attemptId}`;
      const detailArgs = record(phase?.detailArgs);
      const fingerprint = [
        phaseName,
        text(phase?.detailKey, 160),
        text(phase?.detail, 400),
        primitiveRecordFingerprint(detailArgs),
        String(phase?.attempt ?? ''),
        String(phase?.statusCode ?? ''),
        String(phase?.roundNum ?? ''),
      ].join('\u001f');
      if (this.phaseFingerprints.get(key) !== fingerprint) {
        this.rememberPhase(key, fingerprint);
        this.afterPaint(event.conversationId, (paintedAt) => {
          this.enqueue(event.conversationId, event.turnId, {
            observationId: this.nextObservationId(),
            attemptId: event.attemptId,
            kind: 'phase_painted',
            clientId: this.clientId,
            phase: phaseName,
            detailKey: text(phase?.detailKey, 160) || undefined,
            serverEmittedAt: nonnegative(
              phase?.emittedAt,
              nonnegative(serverPublishedAt),
            ) || undefined,
            receivedAt: nonnegative(receivedAt),
            paintedAt,
            projectionRevision: nonnegative(event.projectionRevision),
            visibility: pageVisibility(),
          });
        });
      }
    }

    if (event.type === 'terminal_settlement') {
      this.afterPaint(event.conversationId, (paintedAt) => {
        this.enqueue(event.conversationId, event.turnId, {
          observationId: this.nextObservationId(),
          attemptId: event.attemptId,
          kind: 'terminal_painted',
          clientId: this.clientId,
          serverEmittedAt: nonnegative(serverPublishedAt) || undefined,
          receivedAt: nonnegative(receivedAt),
          paintedAt,
          projectionRevision: nonnegative(event.projectionRevision),
          visibility: pageVisibility(),
        });
      });
      this.phaseFingerprints.delete(
        `${event.conversationId}\u0000${event.attemptId}`,
      );
    }
  }

  observeHealth(
    conversationId: string,
    health: ConnectionHealth,
    identity: PerceptionAttemptIdentity | null,
  ): void {
    if (this.disposed || !this.enabled) return;
    const trouble = health.state === 'recovering'
      || health.state === 'degraded' || health.state === 'offline';
    if (trouble) {
      const observedAt = nonnegative(health.observedAt, this.now());
      const signature = `${health.state}\u001f${health.reason || ''}`;
      const current = this.degradedIntervals.get(conversationId) ?? {
        startedAt: observedAt,
        lastSignature: '',
        identity,
      };
      if (identity) current.identity = identity;
      this.degradedIntervals.set(conversationId, current);
      if (identity && current.lastSignature !== signature) {
        current.lastSignature = signature;
        this.afterPaint(conversationId, (paintedAt) => {
          this.enqueue(conversationId, identity.turnId, {
            observationId: this.nextObservationId(),
            attemptId: identity.attemptId,
            kind: 'transport_degraded',
            clientId: this.clientId,
            reason: text(health.reason, 160) || undefined,
            healthState: health.state,
            receivedAt: observedAt,
            paintedAt,
            observedAt,
            generation: nonnegative(health.generation),
            projectionRevision: nonnegative(identity.projectionRevision),
            retryCount: nonnegative(health.retryCount),
            visibility: pageVisibility(),
          });
        });
      }
      return;
    }

    if (health.state !== 'live') return;
    const degraded = this.degradedIntervals.get(conversationId);
    if (degraded) {
      this.degradedIntervals.delete(conversationId);
      const recoveredIdentity = identity ?? degraded.identity;
      if (recoveredIdentity) {
        const observedAt = nonnegative(health.observedAt, this.now());
        this.afterPaint(conversationId, (paintedAt) => {
          this.enqueue(conversationId, recoveredIdentity.turnId, {
            observationId: this.nextObservationId(),
            attemptId: recoveredIdentity.attemptId,
            kind: 'transport_recovered',
            clientId: this.clientId,
            healthState: health.state,
            receivedAt: observedAt,
            paintedAt,
            observedAt,
            durationMs: Math.max(0, observedAt - degraded.startedAt),
            generation: nonnegative(health.generation),
            projectionRevision: nonnegative(
              recoveredIdentity.projectionRevision,
            ),
            retryCount: nonnegative(health.retryCount),
            visibility: pageVisibility(),
          });
        });
      }
    }
    this.flush();
  }

  flush(): void {
    if (this.disposed || !this.enabled) return;
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    void this.drain();
  }

  disposeConversation(conversationId: string): void {
    for (const [key, pending] of this.pendingPaints) {
      if (pending.conversationId !== conversationId) continue;
      pending.cancel();
      this.pendingPaints.delete(key);
    }
    for (let index = this.pending.length - 1; index >= 0; index -= 1) {
      if (this.pending[index]?.conversationId === conversationId) {
        this.pending.splice(index, 1);
      }
    }
    for (const key of this.phaseFingerprints.keys()) {
      if (key.startsWith(`${conversationId}\u0000`)) {
        this.phaseFingerprints.delete(key);
      }
    }
    this.degradedIntervals.delete(conversationId);
    this.droppedByConversation.delete(conversationId);
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    for (const pending of this.pendingPaints.values()) pending.cancel();
    this.pendingPaints.clear();
    this.pending.length = 0;
    this.phaseFingerprints.clear();
    this.degradedIntervals.clear();
    this.droppedByConversation.clear();
  }

  private nextObservationId(): string {
    this.sequence += 1;
    return `p${this.nonce}:${this.now().toString(36)}:${this.sequence.toString(36)}`;
  }

  private rememberPhase(key: string, fingerprint: string): void {
    this.phaseFingerprints.delete(key);
    this.phaseFingerprints.set(key, fingerprint);
    while (this.phaseFingerprints.size > MAX_TRACKED_PHASES) {
      const oldest = this.phaseFingerprints.keys().next().value as string | undefined;
      if (!oldest) break;
      this.phaseFingerprints.delete(oldest);
    }
  }

  private afterPaint(
    conversationId: string,
    callback: (paintedAt: number) => void,
  ): void {
    while (this.pendingPaints.size >= MAX_PENDING_PAINTS) {
      const oldestKey = this.pendingPaints.keys().next().value as string | undefined;
      if (!oldestKey) break;
      const oldest = this.pendingPaints.get(oldestKey);
      oldest?.cancel();
      this.pendingPaints.delete(oldestKey);
      if (oldest) this.noteDrop(oldest.conversationId);
    }
    this.paintSequence += 1;
    const key = `${conversationId}:${this.paintSequence}`;
    let fired = false;
    const cancel = this.scheduleAfterPaint?.((paintedAt) => {
      fired = true;
      this.pendingPaints.delete(key);
      if (this.disposed) return;
      if (paintedAt === null) {
        this.noteDrop(conversationId);
        return;
      }
      callback(nonnegative(paintedAt, this.now()));
    }) ?? (() => undefined);
    if (!fired) this.pendingPaints.set(key, { conversationId, cancel });
  }

  private enqueue(
    conversationId: string,
    turnId: string,
    body: RecordPerceptionRequest,
  ): void {
    if (this.disposed) return;
    if (this.pending.length >= MAX_PENDING_OBSERVATIONS) {
      const dropIndex = this.draining && this.pending.length > 1 ? 1 : 0;
      const [dropped] = this.pending.splice(dropIndex, 1);
      if (dropped) this.noteDrop(dropped.conversationId);
    }
    const droppedBefore = this.droppedByConversation.get(conversationId) ?? 0;
    this.pending.push({
      conversationId,
      turnId,
      body: droppedBefore > 0 ? { ...body, clientDroppedBefore: droppedBefore } : body,
      attempts: 0,
    });
    void this.drain();
  }

  private async drain(): Promise<void> {
    if (this.disposed || this.draining || this.retryTimer !== null) return;
    this.draining = true;
    try {
      while (!this.disposed && this.pending.length > 0) {
        const current = this.pending[0];
        if (!current) break;
        try {
          await this.api.recordPerception(
            current.conversationId,
            current.turnId,
            current.body,
            { priority: 'background', timeout: 5_000 },
          );
          this.removePending(current);
        } catch (error) {
          if (!this.pending.includes(current)) continue;
          current.attempts += 1;
          if (retryableFailure(error) && current.attempts < MAX_SEND_ATTEMPTS) {
            this.scheduleRetry(current.attempts);
            return;
          }
          this.removePending(current);
          this.noteDrop(current.conversationId);
        }
      }
    } finally {
      this.draining = false;
    }
  }

  private scheduleRetry(attempt: number): void {
    if (this.retryTimer !== null || this.disposed) return;
    const delay = Math.min(MAX_RETRY_DELAY_MS, 1_000 * (2 ** (attempt - 1)));
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.drain();
    }, delay);
  }

  private removePending(observation: PendingObservation): void {
    const index = this.pending.indexOf(observation);
    if (index >= 0) this.pending.splice(index, 1);
  }

  private noteDrop(conversationId: string): void {
    this.droppedByConversation.set(
      conversationId,
      Math.min(
        2_147_483_647,
        (this.droppedByConversation.get(conversationId) ?? 0) + 1,
      ),
    );
  }
}

export function createTurnPerceptionRecorder(
  options: PerceptionRecorderOptions,
): TurnPerceptionRecorder {
  return new TurnPerceptionRecorder(options);
}
