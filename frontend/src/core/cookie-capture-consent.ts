/**
 * Responsibility: own the cookie-capture completion subscription and toast
 * projection. Entry points: handleCookieCaptureFrame and
 * createCookieCaptureConsentController. Dependencies: injected ports only.
 */

export interface CookieCaptureFrame {
  type?: string;
  domain?: unknown;
}

export interface CookieCaptureConsentDependencies {
  subscribe?(channel: string, taskId: string, handler: (frame: unknown) => void): void;
  unsubscribe?(channel: string, taskId: string, handler: (frame: unknown) => void): void;
  showToast?(message: string, kind: 'success'): void;
  translate?(key: string): string;
}

export interface CookieCaptureConsentController {
  readonly source: 'typed';
  handleFrame(frame: unknown): boolean;
  destroy(): void;
}

export function handleCookieCaptureFrame(
  frame: unknown,
  dependencies: CookieCaptureConsentDependencies,
): boolean {
  if (!frame || typeof frame !== 'object') return false;
  const captured = frame as CookieCaptureFrame;
  if (captured.type !== 'captured') return false;
  if (!dependencies.showToast) return false;
  const domain = typeof captured.domain === 'string' ? captured.domain : '';
  const template = dependencies.translate?.('cc.captured') ?? 'cc.captured {domain}';
  dependencies.showToast(template.replace('{domain}', domain), 'success');
  return true;
}

export function createCookieCaptureConsentController(
  dependencies: CookieCaptureConsentDependencies,
): CookieCaptureConsentController {
  let destroyed = false;
  const handler = (frame: unknown): void => {
    if (!destroyed) handleCookieCaptureFrame(frame, dependencies);
  };
  dependencies.subscribe?.('cookie_capture', 'consent', handler);
  return Object.freeze({
    source: 'typed' as const,
    handleFrame(frame: unknown): boolean {
      return destroyed ? false : handleCookieCaptureFrame(frame, dependencies);
    },
    destroy(): void {
      if (destroyed) return;
      destroyed = true;
      dependencies.unsubscribe?.('cookie_capture', 'consent', handler);
    },
  });
}
