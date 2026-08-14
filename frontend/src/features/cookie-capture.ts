export interface CookieCaptureFrame {
  type?: string;
  domain?: unknown;
}

export interface CookieCaptureConsentController {
  readonly source: 'vite';
  handleFrame(frame: unknown): boolean;
  destroy(): void;
}

type PushHandler = (frame: unknown) => void;

/** Apply one push frame. Returns true only when a completion was rendered. */
export function handleCookieCaptureFrame(frame: unknown): boolean {
  if (!frame || typeof frame !== 'object') return false;
  const captured = frame as CookieCaptureFrame;
  if (captured.type !== 'captured') return false;
  const showToast = resolveRuntimeAction('showToast');
  if (!showToast) return false;
  const domain = typeof captured.domain === 'string' ? captured.domain : '';
  showToast(
    t('cc.captured').replace('{domain}', domain),
    'success',
  );
  return true;
}

/**
 * Own the cookie-capture push subscription. The returned destroy contract
 * prevents duplicate toasts when a shell is remounted or upgraded in place.
 */
export function attachCookieCaptureConsent(): CookieCaptureConsentController {
  let destroyed = false;
  const handler: PushHandler = (frame) => {
    if (!destroyed) handleCookieCaptureFrame(frame);
  };
  const subscribe = resolveRuntimeAction('pushSubscribe');
  subscribe?.('cookie_capture', 'consent', handler);
  return Object.freeze({
    source: 'vite' as const,
    handleFrame: handleCookieCaptureFrame,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      resolveRuntimeAction('pushUnsubscribe')?.('cookie_capture', 'consent', handler);
    },
  });
}
import { t } from '../i18n';
import { resolveRuntimeAction } from '../runtime/app-runtime.js';
