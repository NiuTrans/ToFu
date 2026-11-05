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
  routingRevision?(routing: Routing): number | undefined;
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
): {
  load(): Promise<void>;
  refreshConfig(): Promise<void>;
  adoptRouting(routing: Routing): boolean;
  recover(): void;
  destroy(): void;
} {
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
  let refreshController: AbortController | null = null;
  let routingGeneration = 0;
  let acceptedRevision: number | undefined;
  let configApplied = false;
  let pendingRouting: { value: Routing } | null = null;
  let configGeneration = 0;
  let latestConfigRead: { generation: number; promise: Promise<void> } | null = null;
  const isActive = () => ports.isActive?.() !== false;
  const schedule = ports.scheduleRetry ?? ((callback, delayMs) => setTimeout(callback, delayMs));
  const cancel = ports.cancelRetry ?? (handle => clearTimeout(handle as ReturnType<typeof setTimeout>));

  const read = <T>(loader: (signal: AbortSignal) => Promise<T>, signal: AbortSignal): Promise<T> => {
    try { return loader(signal); } catch (error) { return Promise.reject(error); }
  };

  function projectAcceptedRouting(): void {
    if (disposed || !configApplied || !pendingRouting) return;
    const pending = pendingRouting;
    pendingRouting = null;
    ports.applyRouting(pending.value);
  }

  function readConfig(signal: AbortSignal) {
    const generation = ++configGeneration;
    const promise = read(ports.loadConfig, signal).then(value => {
      if (disposed || generation !== configGeneration) return;
      ports.applyConfig(value);
      if (disposed || generation !== configGeneration) return;
      configApplied = true;
      projectAcceptedRouting();
      // Coordination needs settlement, not a retained config response.
    });
    const request = { generation, promise };
    if (generation === configGeneration) latestConfigRead = request;
    return request;
  }

  function revisionOf(routing: Routing): number | undefined {
    const revision = ports.routingRevision?.(routing);
    return typeof revision === 'number' && Number.isSafeInteger(revision) && revision >= 0
      ? revision : undefined;
  }

  // Settings reads and commits enter the same projection as startup. A
  // committed response supersedes older in-flight reads, including errors;
  // equal revisions may still carry fresh availability/health controls.
  function adoptRouting(routing: Routing): boolean {
    if (disposed) return false;
    const revision = revisionOf(routing);
    if (revision !== undefined && acceptedRevision !== undefined && revision < acceptedRevision) return false;
    routingGeneration++;
    if (revision !== undefined) acceptedRevision = revision;
    pendingRouting = { value: routing };
    projectAcceptedRouting();
    return true;
  }

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
    const routingAtStart = routingGeneration;
    const initialConfig = readConfig(signal);
    inFlight = Promise.allSettled([initialConfig.promise, read(ports.loadRouting, signal)]).then(async results => {
      // A post-save config refresh can supersede startup while its routing
      // read is pending. Join the current policy before painting that catalog.
      let configRead = initialConfig;
      let configResult = results[0];
      while (!disposed && latestConfigRead && configRead !== latestConfigRead) {
        configRead = latestConfigRead;
        [configResult] = await Promise.allSettled([configRead.promise]);
      }
      if (disposed) return;
      const routing = results[1];
      const currentRoutingRead = routingAtStart === routingGeneration;
      const failed = [configResult, ...(currentRoutingRead ? [routing] : [])]
        .filter(result => result.status === 'rejected');
      if (failed.length) {
        const failure = failed.find(result => [401, 403].includes(result.reason?.status))
          ?? failed.find(result => !isTransient(result.reason)) ?? failed[0];
        waitingForBootReady = failure.reason?.code === 'server_boot_failed';
        waitingForRecovery = isTransient(failure.reason);
        if (!waitingForRecovery) pendingRouting = null;
        if (configResult.status === 'rejected' && [401, 403].includes(configResult.reason?.status)) {
          configApplied = false;
        }
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
      if (routing.status === 'fulfilled') {
        const revision = revisionOf(routing.value);
        if (currentRoutingRead || (revision !== undefined
            && acceptedRevision !== undefined && revision > acceptedRevision)) {
          adoptRouting(routing.value);
        }
      }
      waitingForRecovery = false;
      loaded = true;
      retryAttempt = 0;
      bootProbeUsed = false;
    }).catch(error => {
      pendingRouting = null;
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

  /* Settings Save already committed the routing authority, so only the misc
   * config projection can have changed. Going through load() would refetch the
   * whole ~550KB aggregate and re-apply an authority the caller holds, adding
   * a round trip plus main-thread parse cost to the save the user is watching.
   * A failed refresh falls back to the full startup pass so the established
   * retry/readiness ladder keeps owning recovery. */
  async function refreshConfig(): Promise<void> {
    if (disposed) return;
    refreshController?.abort();
    refreshController = new AbortController();
    const current = refreshController;
    const request = readConfig(current.signal);
    try {
      await request.promise;
    } catch (error) {
      if (!disposed && refreshController === current && request.generation === configGeneration) void load();
      throw error;
    } finally {
      if (refreshController === current) refreshController = null;
    }
  }

  return {
    load,
    refreshConfig,
    adoptRouting,
    recover,
    destroy() {
      if (disposed) return;
      disposed = true;
      latestConfigRead = null;
      pendingRouting = null;
      controller?.abort();
      refreshController?.abort();
      cancelScheduledRetry();
      offBootReady();
      offActivity?.();
    },
  };
}
