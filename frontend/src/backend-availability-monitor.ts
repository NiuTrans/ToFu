/**
 * Responsibility: own the page-lifetime backend-liveness verdict, prominent
 * offline presentation, confirmation/recovery timers, and wake callbacks.
 * Entry point: createBackendAvailabilityMonitor. Dependencies: injected
 * health, push, recovery, copy, DOM, clock, and logging ports only.
 */

import type {
  AvailabilityHealthProbeResponse,
  AvailabilityLogger,
  AvailabilitySchedule,
} from './availability-monitor-ports';

export type BackendAvailabilityPhase = 'online' | 'suspect' | 'offline';

export interface BackendPushReading {
  readonly connected?: boolean;
}

export interface BackendAvailabilityCopy {
  backendOfflineTitle(): string;
  backendOfflineDescription(retrySeconds: number): string;
  networkOfflineTitle(): string;
  networkOfflineDescription(): string;
  offlineElapsed(duration: string): string;
  retryNow(): string;
  snooze(): string;
  restoredTitle(): string;
  restoredDescription(): string;
  backendTitlePrefix(): string;
  networkTitlePrefix(): string;
}

export interface BackendAvailabilityRecoveryNotice {
  readonly title: string;
  readonly description: string;
  readonly durationMs: number;
}

export interface BackendAvailabilityPorts {
  readonly document: Document;
  readonly browserEvents: EventTarget;
  readonly schedule: AvailabilitySchedule;
  readonly copy: BackendAvailabilityCopy;
  readonly log: AvailabilityLogger;
  offlineIconHtml(): string;
  isVisible(): boolean;
  isNetworkOnline(): boolean;
  probeHealth(
    timeoutMs: number,
  ): Promise<AvailabilityHealthProbeResponse | null>;
  subscribePushReading(
    listener: (reading: BackendPushReading | null) => void,
  ): (() => void) | void;
  subscribePushReconnect(listener: () => void): (() => void) | void;
  nudgePushConnection(): unknown;
  probeStuckStreams(reason: string): unknown;
  recoverOfflineConversations(reason: string): unknown;
  revalidateOnResume(reason: string): unknown;
  notifyRecovery(notice: BackendAvailabilityRecoveryNotice): unknown;
}

export interface BackendAvailabilitySnapshot {
  readonly phase: BackendAvailabilityPhase;
  readonly consecutiveFailures: number;
  readonly probing: boolean;
  readonly offlineSince: number;
  readonly snoozedUntil: number;
  readonly started: boolean;
  readonly plannedInterruption: boolean;
}

export interface BackendAvailabilityMonitor {
  start(): void;
  probeNow(): void;
  snooze(): void;
  beginPlannedInterruption(): void;
  endPlannedInterruption(backendReachable: boolean): void;
  snapshot(): BackendAvailabilitySnapshot;
  destroy(): void;
}

export const BACKEND_AVAILABILITY_POLICY = Object.freeze({
  confirmationFailures: 2,
  confirmationGapMs: 4_000,
  recoveryPollMs: 5_000,
  snoozeMs: 60_000,
  probeTimeoutMs: 4_000,
});

const OFFLINE_BANNER_ID = 'backend-offline-banner';
const COPY_KEYS = Object.freeze({
  backendOfflineTitle: 'conn.backendOfflineTitle',
  backendOfflineDescription: 'conn.backendOfflineDesc',
  networkOfflineTitle: 'conn.networkOfflineTitle',
  networkOfflineDescription: 'conn.networkOfflineDesc',
  offlineElapsed: 'conn.backendOfflineElapsed',
  retryNow: 'conn.backendRetryNow',
  snooze: 'conn.backendSnooze',
  restoredTitle: 'conn.backendRestored',
  restoredDescription: 'conn.backendRestoredDesc',
  backendTitlePrefix: 'conn.backendOfflineTitlePrefix',
  networkTitlePrefix: 'conn.networkOfflineTitlePrefix',
});

const FALLBACK_COPY = Object.freeze({
  backendOfflineTitle: '后端服务器已离线',
  networkOfflineTitle: '本机网络已断开',
  networkOfflineDescription: '浏览器报告网络已断开。检查网络连接；恢复后页面会自动重连。',
  retryNow: '立即重试',
  snooze: '暂时隐藏',
  restoredTitle: '后端已恢复',
  restoredDescription: '正在重新连接并同步进行中的对话…',
  backendTitlePrefix: '【后端离线】',
  networkTitlePrefix: '【网络断开】',
});

function formatDuration(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1_000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes < 60) {
    return `${minutes}m${remainingSeconds > 0
      ? `${String(remainingSeconds).padStart(2, '0')}s`
      : ''}`;
  }
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return `${hours}h${remainingMinutes > 0
    ? `${String(remainingMinutes).padStart(2, '0')}m`
    : ''}`;
}

export function createBackendAvailabilityMonitor(
  ports: BackendAvailabilityPorts,
): BackendAvailabilityMonitor {
  const browserDocument = ports.document;
  const state = {
    phase: 'online' as BackendAvailabilityPhase,
    consecutiveFailures: 0,
    probing: false,
    offlineSince: 0,
    snoozedUntil: 0,
    banner: null as HTMLDivElement | null,
    elapsedElement: null as HTMLSpanElement | null,
    originalTitle: null as string | null,
    confirmationTimer: null as number | null,
    recoveryPollTimer: null as number | null,
    elapsedTimer: null as number | null,
    started: false,
    generation: 0,
    plannedInterruption: false,
  };
  let cleanups: Array<() => void> = [];

  const copyOrFallback = (
    read: () => string,
    key: string,
    fallback: string,
  ): string => {
    try {
      const value = read();
      if (value && value !== key) return value;
    } catch (error: unknown) {
      ports.log.debug('[BackendAvailability] copy lookup failed', error);
    }
    return fallback;
  };

  const networkIsDown = (): boolean => !ports.isNetworkOnline();

  const backendOfflineDescription = (): string => copyOrFallback(
    () => ports.copy.backendOfflineDescription(
      Math.round(BACKEND_AVAILABILITY_POLICY.recoveryPollMs / 1_000),
    ),
    COPY_KEYS.backendOfflineDescription,
    `所有进行中的回复已暂停。每 ${Math.round(
      BACKEND_AVAILABILITY_POLICY.recoveryPollMs / 1_000,
    )} 秒自动重试，恢复后会自动重连并同步结果。`,
  );

  const clearConfirmationTimer = (): void => {
    if (state.confirmationTimer === null) return;
    ports.schedule.clearTimeout(state.confirmationTimer);
    state.confirmationTimer = null;
  };

  const clearRecoveryPoll = (): void => {
    if (state.recoveryPollTimer === null) return;
    ports.schedule.clearInterval(state.recoveryPollTimer);
    state.recoveryPollTimer = null;
  };

  const clearElapsedTimer = (): void => {
    if (state.elapsedTimer === null) return;
    ports.schedule.clearInterval(state.elapsedTimer);
    state.elapsedTimer = null;
  };

  const hideBanner = (): void => {
    state.banner?.remove();
    state.banner = null;
    state.elapsedElement = null;
  };

  const restoreTitle = (): void => {
    if (state.originalTitle === null) return;
    try {
      browserDocument.title = state.originalTitle;
      state.originalTitle = null;
    } catch (error: unknown) {
      ports.log.debug('[BackendAvailability] title restore failed', error);
    }
  };

  const paintElapsed = (): void => {
    if (!state.elapsedElement) return;
    const duration = formatDuration(
      ports.schedule.now() - state.offlineSince,
    );
    state.elapsedElement.textContent = copyOrFallback(
      () => ports.copy.offlineElapsed(duration),
      COPY_KEYS.offlineElapsed,
      `已离线 ${duration}`,
    );
  };

  const createActionButton = (
    label: string,
    onClick: () => void,
  ): HTMLButtonElement => {
    const button = browserDocument.createElement('button');
    button.type = 'button';
    button.textContent = label;
    button.style.cssText =
      'background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.35);' +
      'color:#fff;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:13px;' +
      'white-space:nowrap;';
    button.addEventListener('click', onClick);
    return button;
  };

  const showBanner = (): void => {
    if (state.banner?.isConnected) return;
    browserDocument.getElementById(OFFLINE_BANNER_ID)?.remove();
    const banner = browserDocument.createElement('div');
    banner.id = OFFLINE_BANNER_ID;
    banner.setAttribute('role', 'alert');
    banner.setAttribute('aria-live', 'assertive');
    banner.style.cssText =
      'position:fixed;top:0;left:0;right:0;z-index:10001;' +
      'background:linear-gradient(90deg,#991b1b,#b91c1c);color:#fff;padding:10px 16px;' +
      'font-size:14px;text-align:center;box-shadow:0 2px 10px rgba(0,0,0,.4);' +
      'display:flex;align-items:center;justify-content:center;gap:10px;flex-wrap:wrap;';

    const icon = browserDocument.createElement('span');
    icon.style.display = 'inline-flex';
    icon.innerHTML = ports.offlineIconHtml();
    const message = browserDocument.createElement('span');
    const title = browserDocument.createElement('strong');
    const down = networkIsDown();
    title.textContent = down
      ? copyOrFallback(
        ports.copy.networkOfflineTitle,
        COPY_KEYS.networkOfflineTitle,
        FALLBACK_COPY.networkOfflineTitle,
      )
      : copyOrFallback(
        ports.copy.backendOfflineTitle,
        COPY_KEYS.backendOfflineTitle,
        FALLBACK_COPY.backendOfflineTitle,
      );
    const elapsed = browserDocument.createElement('span');
    elapsed.className = 'bom-elapsed';
    elapsed.style.opacity = '.9';
    const description = browserDocument.createElement('span');
    description.className = 'bom-desc';
    description.style.opacity = '.92';
    description.textContent = ` — ${down
      ? copyOrFallback(
        ports.copy.networkOfflineDescription,
        COPY_KEYS.networkOfflineDescription,
        FALLBACK_COPY.networkOfflineDescription,
      )
      : backendOfflineDescription()}`;
    message.append(title, ' ', elapsed, description);
    banner.append(
      icon,
      message,
      createActionButton(
        copyOrFallback(
          ports.copy.retryNow,
          COPY_KEYS.retryNow,
          FALLBACK_COPY.retryNow,
        ),
        () => probe('manual'),
      ),
      createActionButton(
        copyOrFallback(
          ports.copy.snooze,
          COPY_KEYS.snooze,
          FALLBACK_COPY.snooze,
        ),
        snooze,
      ),
    );
    browserDocument.body.prepend(banner);
    state.banner = banner;
    state.elapsedElement = elapsed;
    paintElapsed();
  };

  const prefixTitle = (): void => {
    try {
      if (state.originalTitle === null) {
        state.originalTitle = browserDocument.title || '';
      }
      const prefix = networkIsDown()
        ? copyOrFallback(
          ports.copy.networkTitlePrefix,
          COPY_KEYS.networkTitlePrefix,
          FALLBACK_COPY.networkTitlePrefix,
        )
        : copyOrFallback(
          ports.copy.backendTitlePrefix,
          COPY_KEYS.backendTitlePrefix,
          FALLBACK_COPY.backendTitlePrefix,
        );
      browserDocument.title = `${prefix} ${state.originalTitle}`;
    } catch (error: unknown) {
      ports.log.debug('[BackendAvailability] title prefix failed', error);
    }
  };

  const runRecoveryCallback = (
    name: string,
    callback: () => unknown,
  ): void => {
    try {
      const result = callback();
      if (result && typeof (result as PromiseLike<unknown>).then === 'function') {
        void Promise.resolve(result).catch((error: unknown) => {
          ports.log.error(`[BackendAvailability] ${name} failed`, error);
        });
      }
    } catch (error: unknown) {
      ports.log.error(`[BackendAvailability] ${name} failed`, error);
    }
  };

  const fireRecovery = (reason: string): void => {
    runRecoveryCallback('push reconnect nudge', ports.nudgePushConnection);
    runRecoveryCallback(
      'stuck-stream probe',
      () => ports.probeStuckStreams(reason),
    );
    runRecoveryCallback(
      'offline-conversation recovery',
      () => ports.recoverOfflineConversations(reason),
    );
    runRecoveryCallback(
      'catalog revalidation',
      () => ports.revalidateOnResume(reason),
    );
  };

  const recover = (reason: string): void => {
    const downMilliseconds = ports.schedule.now()
      - (state.offlineSince || ports.schedule.now());
    state.phase = 'online';
    state.consecutiveFailures = 0;
    clearConfirmationTimer();
    clearRecoveryPoll();
    clearElapsedTimer();
    hideBanner();
    restoreTitle();
    ports.log.info(
      `[BackendAvailability] backend recovered (${reason}) after ${formatDuration(
        downMilliseconds,
      )}`,
    );
    runRecoveryCallback('recovery notice', () => ports.notifyRecovery({
      title: copyOrFallback(
        ports.copy.restoredTitle,
        COPY_KEYS.restoredTitle,
        FALLBACK_COPY.restoredTitle,
      ),
      description: copyOrFallback(
        ports.copy.restoredDescription,
        COPY_KEYS.restoredDescription,
        FALLBACK_COPY.restoredDescription,
      ),
      durationMs: 6_000,
    }));
    fireRecovery(reason);
  };

  const armRecoveryPoll = (): void => {
    if (state.recoveryPollTimer !== null) return;
    state.recoveryPollTimer = ports.schedule.setInterval(() => {
      if (!ports.isVisible()) return;
      void probe('poll');
    }, BACKEND_AVAILABILITY_POLICY.recoveryPollMs);
  };

  const armElapsedTimer = (): void => {
    if (state.elapsedTimer !== null) return;
    paintElapsed();
    state.elapsedTimer = ports.schedule.setInterval(() => {
      if (state.phase !== 'offline') return;
      if (!state.banner && state.snoozedUntil > 0
          && ports.schedule.now() >= state.snoozedUntil) {
        state.snoozedUntil = 0;
        showBanner();
      }
      paintElapsed();
    }, 1_000);
  };

  const goOffline = (reason: string): void => {
    state.phase = 'offline';
    state.offlineSince = ports.schedule.now();
    state.snoozedUntil = 0;
    clearConfirmationTimer();
    ports.log.error(
      `[BackendAvailability] backend offline confirmed (${reason}) after ` +
      `${state.consecutiveFailures} failed probes`,
    );
    showBanner();
    prefixTitle();
    armElapsedTimer();
    armRecoveryPoll();
  };

  const armConfirmationProbe = (): void => {
    clearConfirmationTimer();
    state.confirmationTimer = ports.schedule.setTimeout(() => {
      state.confirmationTimer = null;
      void probe('confirm');
    }, BACKEND_AVAILABILITY_POLICY.confirmationGapMs);
  };

  const handleDead = (reason: string): void => {
    state.consecutiveFailures += 1;
    if (state.phase === 'offline') return;
    if (state.phase === 'online') state.phase = 'suspect';
    if (state.consecutiveFailures
        >= BACKEND_AVAILABILITY_POLICY.confirmationFailures) {
      goOffline(reason);
      return;
    }
    ports.log.warn(
      `[BackendAvailability] probe ${state.consecutiveFailures}/` +
      `${BACKEND_AVAILABILITY_POLICY.confirmationFailures} failed (${reason}); ` +
      'confirming before alarm',
    );
    armConfirmationProbe();
  };

  const handleAlive = (reason: string): void => {
    state.consecutiveFailures = 0;
    if (state.phase === 'offline') {
      recover(reason);
      return;
    }
    if (state.phase === 'suspect') {
      state.phase = 'online';
      clearConfirmationTimer();
      ports.log.info(
        `[BackendAvailability] probe OK (${reason}); no alarm raised`,
      );
    }
  };

  async function probe(reason: string): Promise<void> {
    if (!state.started || state.probing || state.plannedInterruption) return;
    const generation = state.generation;
    state.probing = true;
    let alive = false;
    let verdictReason = reason;
    try {
      const response = await ports.probeHealth(
        BACKEND_AVAILABILITY_POLICY.probeTimeoutMs,
      );
      const status = Number(response?.status) || 0;
      if (status === 401 || status === 403) {
        alive = true;
        verdictReason = `proxy_auth_${status}`;
        ports.log.warn(
          `[BackendAvailability] health probe reached an outer proxy but was ` +
          `denied (HTTP ${status}); backend remains online`,
        );
      } else {
        alive = response?.ok === true;
      }
    } catch (error: unknown) {
      ports.log.debug(
        `[BackendAvailability] health probe failed (${reason})`,
        error,
      );
    } finally {
      if (generation === state.generation) state.probing = false;
    }
    if (!state.started || generation !== state.generation) return;
    if (alive) handleAlive(verdictReason);
    else handleDead(reason);
  }

  const markSuspect = (trigger: string): void => {
    if (!state.started || state.plannedInterruption
        || state.phase === 'offline') return;
    if (state.phase === 'suspect') {
      if (!state.probing && state.confirmationTimer === null) {
        void probe(`re_${trigger}`);
      }
      return;
    }
    state.phase = 'suspect';
    state.consecutiveFailures = 0;
    ports.log.warn(
      `[BackendAvailability] connection suspect (${trigger}); probing health`,
    );
    void probe(trigger);
  };

  const beginPlannedInterruption = (): void => {
    if (state.plannedInterruption) return;
    state.plannedInterruption = true;
    state.generation += 1;
    state.probing = false;
    state.phase = 'online';
    state.consecutiveFailures = 0;
    state.offlineSince = 0;
    state.snoozedUntil = 0;
    clearConfirmationTimer();
    clearRecoveryPoll();
    clearElapsedTimer();
    hideBanner();
    restoreTitle();
    browserDocument.documentElement.setAttribute(
      'data-tofu-planned-interruption',
      'true',
    );
    ports.log.info(
      '[BackendAvailability] planned interruption began; generic alarms paused',
    );
  };

  const endPlannedInterruption = (backendReachable: boolean): void => {
    if (!state.plannedInterruption) return;
    state.plannedInterruption = false;
    state.generation += 1;
    state.probing = false;
    state.consecutiveFailures = 0;
    state.offlineSince = 0;
    state.snoozedUntil = 0;
    clearConfirmationTimer();
    clearRecoveryPoll();
    clearElapsedTimer();
    hideBanner();
    restoreTitle();
    browserDocument.documentElement.removeAttribute(
      'data-tofu-planned-interruption',
    );
    if (backendReachable) {
      state.phase = 'online';
      ports.log.info(
        '[BackendAvailability] planned interruption ended; backend is reachable',
      );
      return;
    }
    state.phase = 'suspect';
    ports.log.warn(
      '[BackendAvailability] planned interruption ended without a liveness ' +
      'verdict; probing before raising an alarm',
    );
    void probe('planned_interruption_ended');
  };

  function snooze(): void {
    if (state.phase !== 'offline') return;
    state.snoozedUntil = ports.schedule.now()
      + BACKEND_AVAILABILITY_POLICY.snoozeMs;
    hideBanner();
    ports.log.info(
      `[BackendAvailability] banner snoozed for ` +
      `${BACKEND_AVAILABILITY_POLICY.snoozeMs / 1_000}s; polling continues`,
    );
  }

  const addListener = (
    target: EventTarget,
    type: string,
    listener: EventListener,
  ): void => {
    target.addEventListener(type, listener);
    cleanups.push(() => target.removeEventListener(type, listener));
  };

  const ownSubscription = (subscribe: () => (() => void) | void): void => {
    try {
      const unsubscribe = subscribe();
      if (typeof unsubscribe === 'function') cleanups.push(unsubscribe);
    } catch (error: unknown) {
      ports.log.error('[BackendAvailability] signal subscription failed', error);
    }
  };

  const start = (): void => {
    if (state.started) return;
    state.started = true;
    state.generation += 1;
    cleanups = [];
    ownSubscription(() => ports.subscribePushReading((reading) => {
      if (!reading) return;
      if (reading.connected === false) markSuspect('push_drop');
      else if (state.phase !== 'online') void probe('push_reconnected');
    }));
    ownSubscription(() => ports.subscribePushReconnect(() => {
      if (state.phase !== 'online') void probe('push_reopen');
    }));
    addListener(ports.browserEvents, 'offline', () => {
      markSuspect('browser_offline');
    });
    addListener(ports.browserEvents, 'online', () => {
      if (state.phase !== 'online') void probe('browser_online');
    });
    addListener(browserDocument, 'visibilitychange', () => {
      if (ports.isVisible() && state.phase !== 'online') void probe('visible');
    });
  };

  const probeNow = (): void => {
    ports.log.info('[BackendAvailability] manual retry requested');
    void probe('manual');
  };

  const snapshot = (): BackendAvailabilitySnapshot => Object.freeze({
    phase: state.phase,
    consecutiveFailures: state.consecutiveFailures,
    probing: state.probing,
    offlineSince: state.offlineSince,
    snoozedUntil: state.snoozedUntil,
    started: state.started,
    plannedInterruption: state.plannedInterruption,
  });

  const destroy = (): void => {
    if (!state.started && cleanups.length === 0
        && !state.plannedInterruption) return;
    state.started = false;
    state.generation += 1;
    for (const cleanup of cleanups.splice(0).reverse()) {
      try {
        cleanup();
      } catch (error: unknown) {
        ports.log.warn('[BackendAvailability] cleanup failed', error);
      }
    }
    clearConfirmationTimer();
    clearRecoveryPoll();
    clearElapsedTimer();
    hideBanner();
    restoreTitle();
    state.phase = 'online';
    state.consecutiveFailures = 0;
    state.probing = false;
    state.offlineSince = 0;
    state.snoozedUntil = 0;
    state.plannedInterruption = false;
    browserDocument.documentElement.removeAttribute(
      'data-tofu-planned-interruption',
    );
  };

  return Object.freeze({
    start,
    probeNow,
    snooze,
    beginPlannedInterruption,
    endPlannedInterruption,
    snapshot,
    destroy,
  });
}
