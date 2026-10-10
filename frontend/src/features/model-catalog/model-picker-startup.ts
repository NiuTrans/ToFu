/**
 * Own config/catalog startup and recovery, not global transport policy.
 * Reads run together; config policy commits before routing. Transient failures
 * get six active-page retries with capped backoff, then wait for a real wake,
 * readiness signal or manual retry. Failed boot waits for explicit readiness;
 * permanent authorization/validation failures never auto-retry.
 */
export interface ModelPickerStartupPorts<Config, Routing> {
  loadConfig(signal: AbortSignal): Promise<Config>;
  loadRouting(signal: AbortSignal): Promise<Routing>;
  applyConfig(config: Config): void;
  applyRouting(routing: Routing): void;
  showFailure(error: unknown): void;
  onBootReady(callback: () => void): () => void;
  isActive?(): boolean;
  onActivityChange?(callback: () => void): () => void;
  probeBootReady?(): Promise<boolean>;
  scheduleRetry?(callback: () => void, delayMs: number): unknown;
  cancelRetry?(handle: unknown): void;
}

function isBootGateError(error: unknown): boolean {
  const failure = error as { status?: number; code?: string } | null;
  return failure?.status === 503 && failure.code === 'server_booting';
}

function isTransient(error: unknown): boolean {
  const failure = error as { status?: number; code?: string } | null;
  if ([401, 403].includes(failure?.status ?? -1)) return false;
  if (['aborted', 'server_boot_failed'].includes(failure?.code ?? '')) return false;
  // A response-body read can fail after receiving HTTP 200 headers.
  if (['network', 'timeout'].includes(failure?.code ?? '')) return true;
  if (failure?.status !== undefined) return [0, 408, 429, 502, 503, 504].includes(failure.status);
  return error instanceof TypeError;
}

export function createModelPickerStartup<Config, Routing>(
  ports: ModelPickerStartupPorts<Config, Routing>,
): { load(): Promise<void>; recover(): void; destroy(): void } {
  let disposed = false;
  let inFlight: Promise<void> | null = null;
  let controller: AbortController | null = null;
  let waitingForRecovery = false;
  let waitingForBootReady = false;
  let bootReadyDuringRequest = false;
  let loaded = false;
  let readyDuringRequest = false;
  let retryAttempt = 0;
  let retryTimer: unknown = null;
  let bootProbeUsed = false;
  const isActive = () => ports.isActive?.() !== false;
  const schedule = ports.scheduleRetry ?? ((callback, delayMs) => setTimeout(callback, delayMs));
  const cancel = ports.cancelRetry ?? (handle => clearTimeout(handle as ReturnType<typeof setTimeout>));

  function cancelScheduledRetry(): void {
    if (retryTimer === null) return;
    cancel(retryTimer);
    retryTimer = null;
  }

  function recover(): void {
    if (disposed || !isActive()) return;
    if (inFlight) {
      readyDuringRequest = true;
    } else if (waitingForRecovery || loaded) {
      void load();
    }
  }
  const offBootReady = ports.onBootReady(() => {
    bootReadyDuringRequest = true;
    if (waitingForBootReady) {
      waitingForBootReady = false;
      waitingForRecovery = true;
    }
    recover();
  });
  const offActivity = ports.onActivityChange?.(() => {
    if (!isActive()) cancelScheduledRetry();
    else recover();
  });

  function load(resetBudget = true): Promise<void> {
    if (disposed) return Promise.resolve();
    if (inFlight) return inFlight;
    cancelScheduledRetry();
    if (resetBudget) retryAttempt = 0;
    if (!isActive()) {
      waitingForRecovery = true;
      return Promise.resolve();
    }
    readyDuringRequest = false;
    bootReadyDuringRequest = false;
    waitingForBootReady = false;
    controller = new AbortController();
    const signal = controller.signal;
    const read = <T>(loader: (signal: AbortSignal) => Promise<T>): Promise<T> => {
      try { return loader(signal); } catch (error) { return Promise.reject(error); }
    };
    const config = read(ports.loadConfig).then(value => {
      if (!disposed) ports.applyConfig(value);
      return value;
    });
    inFlight = Promise.allSettled([config, read(ports.loadRouting)]).then(results => {
      if (disposed) return;
      const failed = results.filter(result => result.status === 'rejected');
      if (failed.length) {
        const failure = failed.find(result => [401, 403].includes(result.reason?.status))
          ?? failed.find(result => !isTransient(result.reason)) ?? failed[0];
        waitingForBootReady = failure.reason?.code === 'server_boot_failed';
        waitingForRecovery = isTransient(failure.reason);
        // Only explicit boot readiness can revive a latched boot failure.
        // Remember it even if the old failed response finishes afterward.
        if (waitingForBootReady && bootReadyDuringRequest) {
          waitingForBootReady = false;
          waitingForRecovery = true;
        }
        // Permanent failures cannot be revived by unrelated availability events.
        if (!waitingForRecovery) loaded = false;
        if (!isBootGateError(failure.reason)) ports.showFailure(failure.reason);
        if (isBootGateError(failure.reason) && !bootProbeUsed && ports.probeBootReady) {
          bootProbeUsed = true;
          void ports.probeBootReady().then(ready => {
            if (ready) recover();
          }).catch(() => { /* The bounded retry survives a failed probe. */ });
        }
        return;
      }
      const routing = results[1];
      if (routing.status === 'fulfilled') ports.applyRouting(routing.value);
      waitingForRecovery = false;
      loaded = true;
      retryAttempt = 0;
      bootProbeUsed = false;
    }).catch(error => {
      waitingForRecovery = false;
      waitingForBootReady = false;
      loaded = false;
      if (!disposed) ports.showFailure(error);
    }).finally(() => {
      inFlight = null;
      controller = null;
      if (disposed || !waitingForRecovery || !isActive()) return;
      if (readyDuringRequest) {
        void load(false);
      } else if (retryAttempt < 6) {
        const delayMs = Math.min(1000 * 2 ** retryAttempt++, 30_000);
        retryTimer = schedule(() => {
          retryTimer = null;
          void load(false);
        }, delayMs);
      }
    });
    return inFlight;
  }

  return {
    load,
    recover,
    destroy() {
      disposed = true;
      controller?.abort();
      cancelScheduledRetry();
      offBootReady();
      offActivity?.();
    },
  };
}
