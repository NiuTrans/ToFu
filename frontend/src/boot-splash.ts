/**
 * Responsibility: cold-start boot splash — while the server's background
 * boot runner is still working through its required phases, show a truthful
 * full-screen progress card (current phase, per-phase checklist, elapsed
 * time) instead of a dead app shell, and surface a latched boot failure
 * with an operator retry. Dismisses itself the instant boot reports ready
 * and never re-appears: post-ready storage blips stay owned by the storage
 * availability banner.
 * Entry point: startBootSplash (production wiring), createBootSplashController
 * (ports-injected core). Dependencies: injected DOM/transport/copy/scheduler
 * ports only — no runtime-module reach-through.
 */

import type { I18nKey, t } from './i18n';

export interface BootPhaseSnapshot {
  readonly index: number;
  readonly key: string;
  readonly label: string;
  readonly status: 'pending' | 'running' | 'done' | 'failed';
  readonly duration_s: number | null;
  readonly error: string | null;
  /** Auto-retry ordinal while a running phase rides out a transient failure. */
  readonly retry?: number;
}

export interface BootSnapshot {
  readonly status: 'pending' | 'running' | 'ready' | 'failed' | 'interrupted';
  readonly phaseTotal: number;
  readonly phases: readonly BootPhaseSnapshot[];
  readonly currentPhase: BootPhaseSnapshot | null;
  readonly failedPhase: BootPhaseSnapshot | null;
  readonly error: string | null;
  readonly retryCount: number;
  readonly elapsedS: number | null;
  readonly readyAt: number | null;
}

export type BootVerdict =
  | { readonly kind: 'ready' }
  | { readonly kind: 'starting'; readonly snapshot: BootSnapshot }
  | { readonly kind: 'failed'; readonly snapshot: BootSnapshot }
  | { readonly kind: 'unavailable' };

/** Pure verdict mapping — the unit-tested truth of the splash. */
export function verdictFromBootPayload(payload: unknown): BootVerdict {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return { kind: 'unavailable' };
  }
  const body = payload as Record<string, unknown>;
  const snapshot = body as unknown as BootSnapshot;
  switch (body.status) {
    case 'ready':
      return { kind: 'ready' };
    case 'failed':
      return { kind: 'failed', snapshot };
    case 'pending':
    case 'running':
    case 'interrupted':
      return { kind: 'starting', snapshot };
    default:
      return { kind: 'unavailable' };
  }
}

/** Legacy-server fallback: /api/v4/boot predates it, so storage.ready decides. */
export function verdictFromHealthPayload(payload: unknown): BootVerdict {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return { kind: 'unavailable' };
  }
  const storage = (payload as { storage?: unknown }).storage;
  const ready = !!storage && typeof storage === 'object'
    && (storage as { ready?: unknown }).ready === true;
  if (ready) return { kind: 'ready' };
  // The legacy projection has no per-phase truth; synthesize a one-row
  // snapshot so the card still renders honestly ('Storage sidecar' running).
  const syntheticPhase: BootPhaseSnapshot = {
    index: 3,
    key: 'storage_sidecar',
    label: 'Storage sidecar',
    status: 'running',
    duration_s: null,
    error: null,
  };
  return {
    kind: 'starting',
    snapshot: {
      status: 'running',
      phaseTotal: 9,
      phases: [syntheticPhase],
      currentPhase: syntheticPhase,
      failedPhase: null,
      error: null,
      retryCount: 0,
      elapsedS: null,
      readyAt: null,
    },
  };
}

export interface BootSplashCopy {
  splashTitle(): string;
  splashSubtitle(): string;
  connecting(): string;
  elapsed(seconds: number): string;
  failedTitle(): string;
  failedHint(): string;
  retry(): string;
  retrying(): string;
  phaseLabel(key: string, serverLabel: string): string;
}

export interface BootSplashSchedule {
  setTimeout(callback: () => void, delayMs: number): number;
  clearTimeout(handle: number): void;
}

export interface BootSplashPorts {
  readonly document: Document;
  readonly schedule: BootSplashSchedule;
  readonly copy: BootSplashCopy;
  readonly log: { warn(...args: readonly unknown[]): void };
  probeBoot(timeoutMs: number): Promise<BootVerdict>;
  requestRetry(): Promise<boolean>;
}

export interface BootSplashController {
  isActive(): boolean;
  destroy(): void;
}

export const BOOT_SPLASH_POLICY = Object.freeze({
  probeTimeoutMs: 3_000,
  pollMs: 800,
  retryResetMs: 2_500,
});

type BootPhaseLabelKey =
  | 'update.phase.frontend_assets'
  | 'update.phase.storage_boundary'
  | 'update.phase.storage_sidecar'
  | 'update.phase.storage_recovery'
  | 'update.phase.model_routing'
  | 'update.phase.critical_imports'
  | 'update.phase.background_workers'
  | 'update.phase.optional_services'
  | 'update.phase.readiness';

const PHASE_LABEL_KEYS: Readonly<Record<string, BootPhaseLabelKey>> = Object.freeze({
  frontend_assets: 'update.phase.frontend_assets',
  storage_boundary: 'update.phase.storage_boundary',
  storage_sidecar: 'update.phase.storage_sidecar',
  storage_recovery: 'update.phase.storage_recovery',
  model_routing: 'update.phase.model_routing',
  critical_imports: 'update.phase.critical_imports',
  background_workers: 'update.phase.background_workers',
  optional_services: 'update.phase.optional_services',
  readiness: 'update.phase.readiness',
});

function withFallback(
  read: () => string,
  key: string,
  fallback: string,
): string {
  try {
    const value = read();
    if (value && value !== key) return value;
  } catch {
    // i18n not loaded yet — the fallback copy below is the boot-language
    // default (zh), matching the storage-availability-monitor pattern.
  }
  return fallback;
}

export function createBootSplashController(
  ports: BootSplashPorts,
): BootSplashController {
  const doc = ports.document;
  let overlay: HTMLElement | null = null;
  let titleEl: HTMLElement | null = null;
  let subtitleEl: HTMLElement | null = null;
  let phaseListEl: HTMLElement | null = null;
  let progressEl: HTMLElement | null = null;
  let progressFillEl: HTMLElement | null = null;
  let statusLineEl: HTMLElement | null = null;
  let errorBoxEl: HTMLElement | null = null;
  let retryButton: HTMLButtonElement | null = null;
  let spinnerEl: HTMLElement | null = null;
  let visible = false;
  let destroyed = false;
  let finished = false;
  let pollTimer: number | null = null;
  let retryInFlight = false;
  let renderedPhases = '';

  // Reassigning identical text destroys native selection during boot polling.
  const setText = (element: HTMLElement, text: string): void => {
    if (element.textContent !== text) element.textContent = text;
  };

  const buildOverlay = (): void => {
    if (overlay) return;
    overlay = doc.createElement('div');
    overlay.className = 'boot-splash';
    overlay.id = 'boot-splash';
    const card = doc.createElement('div');
    card.className = 'boot-splash__card';
    card.setAttribute('role', 'status');
    card.setAttribute('aria-live', 'polite');

    spinnerEl = doc.createElement('span');
    spinnerEl.className = 'ui-spinner ui-spinner--lg boot-splash__spinner';
    spinnerEl.setAttribute('role', 'status');
    card.appendChild(spinnerEl);

    titleEl = doc.createElement('h1');
    titleEl.className = 'boot-splash__title';
    card.appendChild(titleEl);

    subtitleEl = doc.createElement('p');
    subtitleEl.className = 'boot-splash__subtitle';
    card.appendChild(subtitleEl);

    progressEl = doc.createElement('div');
    progressEl.className = 'boot-splash__progress';
    progressEl.setAttribute('role', 'progressbar');
    progressEl.setAttribute('aria-valuemin', '0');
    progressEl.setAttribute('aria-valuemax', '100');
    progressFillEl = doc.createElement('div');
    progressFillEl.className = 'boot-splash__progress-fill';
    progressEl.appendChild(progressFillEl);
    card.appendChild(progressEl);

    statusLineEl = doc.createElement('p');
    statusLineEl.className = 'boot-splash__status-line';
    card.appendChild(statusLineEl);

    phaseListEl = doc.createElement('ul');
    phaseListEl.className = 'boot-splash__phases';
    card.appendChild(phaseListEl);

    errorBoxEl = doc.createElement('div');
    errorBoxEl.className = 'boot-splash__error';
    errorBoxEl.hidden = true;
    card.appendChild(errorBoxEl);

    retryButton = doc.createElement('button');
    retryButton.type = 'button';
    retryButton.className = 'ui-btn ui-btn--primary boot-splash__retry';
    retryButton.hidden = true;
    retryButton.addEventListener('click', () => {
      void onRetryClicked();
    });
    card.appendChild(retryButton);

    overlay.appendChild(card);
    doc.body.appendChild(overlay);
  };

  const renderPhases = (snapshot: BootSnapshot): void => {
    if (!phaseListEl) return;
    const phases = snapshot.phases.map(phase => ({
      status: phase.status,
      label: ports.copy.phaseLabel(phase.key, phase.label),
    }));
    const signature = JSON.stringify(phases);
    if (signature === renderedPhases) return;
    renderedPhases = signature;
    phaseListEl.textContent = '';
    for (const phase of phases) {
      const row = doc.createElement('li');
      row.className = `boot-splash__phase boot-splash__phase--${phase.status}`;
      const marker = doc.createElement('span');
      marker.className = 'boot-splash__phase-marker';
      marker.setAttribute('aria-hidden', 'true');
      marker.textContent = phase.status === 'done'
        ? '✓'
        : phase.status === 'failed' ? '!' : '';
      if (phase.status === 'running') {
        const dotSpinner = doc.createElement('span');
        dotSpinner.className = 'ui-spinner ui-spinner--sm';
        marker.appendChild(dotSpinner);
      }
      const label = doc.createElement('span');
      label.className = 'boot-splash__phase-label';
      label.textContent = phase.label;
      row.appendChild(marker);
      row.appendChild(label);
      phaseListEl.appendChild(row);
    }
  };

  const renderStarting = (snapshot: BootSnapshot): void => {
    buildOverlay();
    if (!overlay || !titleEl || !subtitleEl || !progressEl || !progressFillEl
      || !statusLineEl || !errorBoxEl || !retryButton || !spinnerEl) return;
    const copy = ports.copy;
    overlay.classList.remove('boot-splash--failed');
    spinnerEl.hidden = false;
    retryButton.hidden = true;
    setText(titleEl, copy.splashTitle());
    setText(subtitleEl, copy.splashSubtitle());
    const total = Math.max(1, snapshot.phaseTotal || snapshot.phases.length || 1);
    const done = snapshot.phases.filter(p => p.status === 'done').length;
    const percent = Math.round((done / total) * 100);
    progressEl.hidden = false;
    progressEl.setAttribute('aria-valuenow', String(percent));
    progressFillEl.style.width = `${percent}%`;
    const currentLabel = snapshot.currentPhase
      ? ports.copy.phaseLabel(
        snapshot.currentPhase.key, snapshot.currentPhase.label)
      : copy.connecting();
    const elapsed = snapshot.elapsedS != null && snapshot.elapsedS >= 1
      ? ` · ${copy.elapsed(Math.round(snapshot.elapsedS))}`
      : '';
    statusLineEl.hidden = false;
    setText(statusLineEl, `${currentLabel} · ${done}/${total}${elapsed}`);
    // A running phase that is auto-retrying a transient storage failure
    // carries the reason; show it so a long recovery is never a silent timer.
    const transient = snapshot.currentPhase?.error;
    if (transient) {
      errorBoxEl.hidden = false;
      setText(errorBoxEl, `${copy.retrying()} ${transient}`);
    } else {
      errorBoxEl.hidden = true;
    }
    renderPhases(snapshot);
    if (!visible) {
      visible = true;
      overlay.classList.add('boot-splash--visible');
    }
  };

  const renderFailed = (snapshot: BootSnapshot): void => {
    buildOverlay();
    if (!overlay || !titleEl || !subtitleEl || !progressEl || !statusLineEl
      || !errorBoxEl || !retryButton || !spinnerEl) return;
    const copy = ports.copy;
    overlay.classList.add('boot-splash--failed');
    spinnerEl.hidden = true;
    progressEl.hidden = true;
    statusLineEl.hidden = true;
    setText(titleEl, copy.failedTitle());
    setText(subtitleEl, copy.failedHint());
    errorBoxEl.hidden = false;
    setText(errorBoxEl, snapshot.error
      || snapshot.failedPhase?.error
      || '');
    retryButton.hidden = false;
    retryButton.disabled = retryInFlight;
    retryButton.textContent = retryInFlight ? copy.retrying() : copy.retry();
    renderPhases(snapshot);
    if (!visible) {
      visible = true;
      overlay.classList.add('boot-splash--visible');
    }
  };

  const dismiss = (): void => {
    finished = true;
    if (pollTimer !== null) {
      ports.schedule.clearTimeout(pollTimer);
      pollTimer = null;
    }
    if (overlay) {
      overlay.classList.add('boot-splash--leaving');
      const node = overlay;
      overlay = null;
      // Keep the fade-out honest but bounded; then remove the node so no
      // fullscreen layer can intercept clicks after boot.
      ports.schedule.setTimeout(() => node.remove(), 400);
    }
    visible = false;
    try {
      window.dispatchEvent(new CustomEvent('tofu:boot-ready'));
    } catch {
      // A listener failure must never wedge boot completion.
    }
  };

  const poll = async (): Promise<void> => {
    if (destroyed || finished) return;
    let verdict: BootVerdict;
    try {
      verdict = await ports.probeBoot(BOOT_SPLASH_POLICY.probeTimeoutMs);
    } catch (error) {
      ports.log.warn('[boot-splash] probe failed', error);
      verdict = { kind: 'unavailable' };
    }
    if (destroyed || finished) return;
    if (verdict.kind === 'ready') {
      dismiss();
      return;
    }
    if (verdict.kind === 'starting') {
      retryInFlight = false;
      renderStarting(verdict.snapshot);
    } else if (verdict.kind === 'failed') {
      retryInFlight = false;
      renderFailed(verdict.snapshot);
    }
    // 'unavailable' keeps the previous truthful state; the connectivity
    // banners own the fully-unreachable case.
    pollTimer = ports.schedule.setTimeout(() => {
      void poll();
    }, BOOT_SPLASH_POLICY.pollMs);
  };

  const onRetryClicked = async (): Promise<void> => {
    if (retryInFlight) return;
    retryInFlight = true;
    if (retryButton) {
      retryButton.disabled = true;
      retryButton.textContent = ports.copy.retrying();
    }
    try {
      await ports.requestRetry();
    } catch (error) {
      ports.log.warn('[boot-splash] retry request failed', error);
    } finally {
      // The next poll renders the truthful state; the reset bound keeps the
      // button from sticking if the server never flips status.
      ports.schedule.setTimeout(() => {
        retryInFlight = false;
      }, BOOT_SPLASH_POLICY.retryResetMs);
    }
  };

  void poll();

  return {
    isActive: () => visible && !finished,
    destroy: () => {
      destroyed = true;
      if (pollTimer !== null) {
        ports.schedule.clearTimeout(pollTimer);
        pollTimer = null;
      }
      if (overlay) {
        overlay.remove();
        overlay = null;
      }
      visible = false;
    },
  };
}

interface BootSplashWindow {
  __tofuBootSplash?: { isActive(): boolean };
}

export interface BootSplashRuntimeDeps {
  /** api/transport request, injected so this module stays DOM/node-safe. */
  request: <T = unknown>(
    path: string,
    options?: Record<string, unknown>,
  ) => Promise<T>;
  /** HTTP status of a thrown transport error, else undefined. */
  errorStatus(error: unknown): number | undefined;
  translate: typeof t;
}

/** Production wiring: probe /api/v4/boot, fall back to /api/health on 404. */
export function startBootSplash(
  deps: BootSplashRuntimeDeps,
): BootSplashController {
  const { request, errorStatus, translate } = deps;
  let legacyMode = false;
  const controller = createBootSplashController({
    document,
    log: console,
    schedule: {
      setTimeout: (callback, delayMs) => window.setTimeout(callback, delayMs),
      clearTimeout: handle => window.clearTimeout(handle),
    },
    copy: {
      splashTitle: () => withFallback(
        () => translate('boot.splashTitle'), 'boot.splashTitle',
        '正在启动服务'),
      splashSubtitle: () => withFallback(
        () => translate('boot.splashSubtitle'), 'boot.splashSubtitle',
        '服务器已响应，正在完成启动；就绪后页面会自动继续。'),
      connecting: () => withFallback(
        () => translate('boot.connecting'), 'boot.connecting',
        '正在确认服务器状态…'),
      elapsed: seconds => withFallback(
        () => translate('boot.elapsed', { t: `${seconds}s` }),
        'boot.elapsed', `已等待 ${seconds} 秒`),
      failedTitle: () => withFallback(
        () => translate('boot.failedTitle'), 'boot.failedTitle',
        '启动在某个必需阶段失败'),
      failedHint: () => withFallback(
        () => translate('boot.failedHint'), 'boot.failedHint',
        '服务保持在线。修复原因后点击下方重试；重试会从失败的阶段继续。'),
      retry: () => withFallback(
        () => translate('boot.retry'), 'boot.retry', '重试'),
      retrying: () => withFallback(
        () => translate('boot.retrying'), 'boot.retrying', '重试中…'),
      phaseLabel: (key, serverLabel) => {
        const i18nKey = PHASE_LABEL_KEYS[key];
        if (!i18nKey) return serverLabel;
        return withFallback(
          () => translate(i18nKey), i18nKey, serverLabel);
      },
    },
    probeBoot: async (timeoutMs) => {
      const options = {
        timeout: timeoutMs,
        onError: 'throw' as const,
      };
      if (legacyMode) {
        const health = await request<unknown>('/api/health', options);
        return verdictFromHealthPayload(health);
      }
      try {
        const payload = await request<unknown>('/api/v4/boot', options);
        return verdictFromBootPayload(payload);
      } catch (error) {
        if (errorStatus(error) === 404) {
          // Server predates the boot projection: one mode switch, then the
          // storage.ready verdict carries the splash for this page lifetime.
          legacyMode = true;
          const health = await request<unknown>('/api/health', options);
          return verdictFromHealthPayload(health);
        }
        throw error;
      }
    },
    requestRetry: async () => {
      await request('/api/v4/boot/retry', {
        method: 'POST',
        timeout: BOOT_SPLASH_POLICY.probeTimeoutMs,
        onError: 'throw',
      });
      return true;
    },
  });
  (window as BootSplashWindow & Window).__tofuBootSplash = controller;
  return controller;
}
