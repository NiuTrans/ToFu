/** Shared speech capability reads for the composer and Settings. Entry points:
 * refresh (coalesced read), invalidate/routingChanged (retire older reads),
 * subscribe and destroy. The audio API owns availability; routing is a wake hint.
 * No polling, import-time requests, or cached provider/credential policy.
 */
import {featureRegistry} from '../../feature-registry';

export interface AudioCapabilities {
  available: boolean;
  models: readonly {model?: string; mode?: string}[];
}

function capabilitiesFrom(value: unknown): AudioCapabilities {
  const response = value as Partial<AudioCapabilities> | null;
  return {
    available: response?.available === true,
    models: Array.isArray(response?.models) ? response.models : [],
  };
}

export function needsWaveEncoding(capabilities: AudioCapabilities | null): boolean {
  return !capabilities?.models.length
    || capabilities.models.some(model => model?.mode !== 'endpoint');
}

export function createAudioCapabilitiesReader(ports: {
  load(signal: AbortSignal): Promise<unknown>;
  report(error: unknown): void;
}) {
  let current: AudioCapabilities | null = null;
  let generation = 0;
  let routingRevision: number | null = null;
  let pending: Promise<AudioCapabilities | null> | null = null;
  let disposed = false;
  const abort = new AbortController();
  const listeners = new Set<(value: AudioCapabilities | null) => void>();
  function publish(listener: (value: AudioCapabilities | null) => void): void {
    try { listener(current); } catch (error) { ports.report(error); }
  }

  async function read(): Promise<AudioCapabilities | null> {
    try {
      while (!disposed) {
        const attempt = generation;
        let value: AudioCapabilities | null = null;
        let failure: unknown;
        try { value = capabilitiesFrom(await ports.load(abort.signal)); }
        catch (error) { failure = error; }
        if (disposed) return null;
        if (attempt !== generation) continue;
        if (failure) ports.report(failure);
        current = value;
        for (const listener of listeners) publish(listener);
        if (attempt === generation) return current;
      }
      return null;
    } finally {
      // Release before resolving the outer promise: a queued routing hint
      // must not join an already completed read during promise settlement.
      pending = null;
    }
  }

  function refresh(): Promise<AudioCapabilities | null> {
    if (disposed) return Promise.resolve(null);
    // Defer the read until pending is assigned, including synchronous failures.
    pending ??= Promise.resolve().then(read);
    return pending;
  }

  function invalidate(): Promise<AudioCapabilities | null> {
    generation++;
    return refresh();
  }

  function routingAccepted(revision: unknown): Promise<AudioCapabilities | null> {
    if (typeof revision === 'number' && Number.isSafeInteger(revision) && revision >= 0) {
      if (routingRevision !== null && revision < routingRevision) return pending ?? Promise.resolve(current);
      routingRevision = revision;
    }
    // A fresh accepted response can carry changed health at the same revision.
    return invalidate();
  }

  return {
    get current() { return current; },
    refresh, invalidate, routingAccepted,
    routingChanged(revision: unknown): Promise<AudioCapabilities | null> {
      if (typeof revision === 'number' && Number.isSafeInteger(revision) && revision >= 0) {
        if (routingRevision !== null && revision <= routingRevision) return pending ?? Promise.resolve(current);
        routingRevision = revision;
      }
      return routingAccepted(revision);
    },
    subscribe(listener: (value: AudioCapabilities | null) => void): () => void {
      if (disposed) return () => {};
      listeners.add(listener);
      publish(listener);
      return () => { listeners.delete(listener); };
    },
    destroy(): void {
      if (disposed) return;
      disposed = true;
      generation++;
      listeners.clear();
      current = null;
      abort.abort();
    },
  };
}

export const audioCapabilities = createAudioCapabilitiesReader({
  load: signal => (featureRegistry.Api as {
    audio: {capabilities(options?: {signal: AbortSignal}): Promise<unknown>};
  }).audio.capabilities({signal}),
  report: error => console.warn('[Voice] capabilities probe failed:', error),
});
