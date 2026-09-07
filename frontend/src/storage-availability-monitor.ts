/**
 * Responsibility: probe Sidecar readiness and own the persistent storage
 * warning plus its bounded, visibility-aware recovery poll.
 * Entry point: createStorageAvailabilityMonitor. Dependencies: injected
 * health, copy, DOM, scheduler, visibility, and logging ports only.
 */

import type {
  AvailabilityHealthProbeResponse,
  AvailabilityLogger,
  AvailabilitySchedule,
} from './availability-monitor-ports';

export interface StorageAvailabilityCopy {
  unavailableTitle(): string;
  unavailableDescription(): string;
  dismiss(): string;
}

export interface StorageAvailabilityPorts {
  readonly document: Document;
  readonly schedule: AvailabilitySchedule;
  readonly copy: StorageAvailabilityCopy;
  readonly log: AvailabilityLogger;
  warningIconHtml(): string;
  isVisible(): boolean;
  probeHealth(
    timeoutMs: number,
  ): Promise<AvailabilityHealthProbeResponse | null>;
}

export interface StorageAvailabilityMonitor {
  check(): Promise<void>;
  destroy(): void;
}

export const STORAGE_AVAILABILITY_POLICY = Object.freeze({
  probeTimeoutMs: 3_000,
  recoveryPollMs: 15_000,
});

const STORAGE_WARNING_ID = 'storage-warning-banner';
const COPY_KEYS = Object.freeze({
  unavailableTitle: 'conn.storageUnavailableTitle',
  unavailableDescription: 'conn.storageUnavailableDesc',
  dismiss: 'conn.dismiss',
});

const FALLBACK_COPY = Object.freeze({
  unavailableTitle: '存储服务暂时不可用',
  unavailableDescription: '持久化操作已安全暂停，服务器正在自动恢复存储连接。',
  dismiss: '关闭',
});

function isStorageReady(payload: unknown): boolean {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return false;
  }
  const storage = (payload as { storage?: unknown }).storage;
  return !!storage && typeof storage === 'object' && !Array.isArray(storage)
    && (storage as { ready?: unknown }).ready === true;
}

export function createStorageAvailabilityMonitor(
  ports: StorageAvailabilityPorts,
): StorageAvailabilityMonitor {
  const browserDocument = ports.document;
  let recoveryPollTimer: number | null = null;
  let destroyed = false;
  let generation = 0;
  let probeInFlight: Promise<boolean | null> | null = null;

  const copyOrFallback = (
    read: () => string,
    key: string,
    fallback: string,
  ): string => {
    try {
      const value = read();
      if (value && value !== key) return value;
    } catch (error: unknown) {
      ports.log.debug('[StorageAvailability] copy lookup failed', error);
    }
    return fallback;
  };

  const stopRecoveryPoll = (): void => {
    if (recoveryPollTimer === null) return;
    ports.schedule.clearInterval(recoveryPollTimer);
    recoveryPollTimer = null;
  };

  const clearWarning = (recovered: boolean): void => {
    const banner = browserDocument.getElementById(STORAGE_WARNING_ID);
    if (banner) {
      banner.remove();
      if (recovered) {
        ports.log.info(
          '[StorageAvailability] Sidecar ready again; warning cleared',
        );
      }
    }
    stopRecoveryPoll();
  };

  const showWarning = (): void => {
    if (destroyed || browserDocument.getElementById(STORAGE_WARNING_ID)) return;
    const banner = browserDocument.createElement('div');
    banner.id = STORAGE_WARNING_ID;
    banner.setAttribute('role', 'alert');
    banner.setAttribute('aria-live', 'assertive');
    banner.style.cssText =
      'position:fixed;top:0;left:0;right:0;z-index:10000;' +
      'background:#dc2626;color:#fff;padding:10px 16px;font-size:14px;' +
      'text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.3);' +
      'display:flex;align-items:center;justify-content:center;gap:8px;';

    const icon = browserDocument.createElement('span');
    icon.style.display = 'inline-flex';
    icon.innerHTML = ports.warningIconHtml();
    const message = browserDocument.createElement('span');
    const title = browserDocument.createElement('strong');
    title.textContent = copyOrFallback(
      ports.copy.unavailableTitle,
      COPY_KEYS.unavailableTitle,
      FALLBACK_COPY.unavailableTitle,
    );
    message.append(
      title,
      ` — ${copyOrFallback(
        ports.copy.unavailableDescription,
        COPY_KEYS.unavailableDescription,
        FALLBACK_COPY.unavailableDescription,
      )}`,
    );
    const dismiss = browserDocument.createElement('button');
    dismiss.type = 'button';
    dismiss.textContent = copyOrFallback(
      ports.copy.dismiss,
      COPY_KEYS.dismiss,
      FALLBACK_COPY.dismiss,
    );
    dismiss.style.cssText =
      'background:rgba(255,255,255,.2);border:none;color:#fff;padding:4px 10px;' +
      'border-radius:4px;cursor:pointer;font-size:13px;margin-left:12px;' +
      'white-space:nowrap;';
    dismiss.addEventListener('click', () => clearWarning(false));
    banner.append(icon, message, dismiss);
    browserDocument.body.prepend(banner);
  };

  const readStorageReady = (
    expectedGeneration: number,
    context: string,
  ): Promise<boolean | null> => {
    if (probeInFlight) return probeInFlight;
    const request = (async (): Promise<boolean | null> => {
      try {
        const response = await ports.probeHealth(
          STORAGE_AVAILABILITY_POLICY.probeTimeoutMs,
        );
        if (destroyed || generation !== expectedGeneration) return null;
        if (!response?.ok) return null;
        if (typeof response.json !== 'function') {
          throw new Error('health response has no JSON reader');
        }
        return isStorageReady(await response.json());
      } catch (error: unknown) {
        ports.log.debug(
          `[StorageAvailability] ${context} failed (unreachable or bad payload)`,
          error,
        );
        return null;
      }
    })();
    probeInFlight = request;
    void request.then(() => {
      if (probeInFlight === request) probeInFlight = null;
    });
    return request;
  };

  const startRecoveryPoll = (): void => {
    if (destroyed || recoveryPollTimer !== null) return;
    recoveryPollTimer = ports.schedule.setInterval(() => {
      if (!browserDocument.getElementById(STORAGE_WARNING_ID)) {
        stopRecoveryPoll();
        return;
      }
      if (!ports.isVisible()) return;
      const expectedGeneration = generation;
      void readStorageReady(expectedGeneration, 'recovery poll').then((ready) => {
        if (ready === true && !destroyed && generation === expectedGeneration) {
          clearWarning(true);
        }
      });
    }, STORAGE_AVAILABILITY_POLICY.recoveryPollMs);
  };

  const check = async (): Promise<void> => {
    if (destroyed) return;
    const expectedGeneration = generation;
    const ready = await readStorageReady(expectedGeneration, 'startup probe');
    if (destroyed || generation !== expectedGeneration || ready === null) return;
    if (ready) {
      clearWarning(true);
      return;
    }
    showWarning();
    startRecoveryPoll();
  };

  const destroy = (): void => {
    if (destroyed) return;
    destroyed = true;
    generation += 1;
    clearWarning(false);
  };

  return Object.freeze({ check, destroy });
}
