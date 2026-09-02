/** Lazy background feature owners that should not tax the first screen. */

import { t } from '../i18n';
import { resolveBrowserLocalStorage } from '../core/browser-storage';
import {
  createSubscriptionResetNoticeController,
  type SubscriptionResetNoticeController,
} from './subscription-reset-notice';

type OAuthStatusApi = {
  status?(): Promise<unknown>;
};

type RuntimeToast = (
  icon: string,
  title: string,
  detail: string,
  durationMs: number,
  options: { hint: string; onClick(): void },
) => void;

let resetNoticeController: SubscriptionResetNoticeController | null = null;
let visibilityListenerInstalled = false;

function runtimeFunction<T extends (...args: never[]) => unknown>(name: string): T | null {
  const modules = (window as Window & {
    TofuModules?: { resolveAction(action: string): unknown };
  }).TofuModules;
  const value = modules?.resolveAction(name);
  return typeof value === 'function' ? value as unknown as T : null;
}

function startSubscriptionResetNotice(): void {
  if (resetNoticeController) return;
  const translate = t as unknown as (
    key: string,
    values?: Record<string, unknown>,
  ) => string;
  resetNoticeController = createSubscriptionResetNoticeController({
    async readStatus(): Promise<unknown> {
      const api = (window as Window & {
        Api?: { oauth?: OAuthStatusApi };
      }).Api;
      return typeof api?.oauth?.status === 'function'
        ? await api.oauth.status()
        : null;
    },
    notify(notice): boolean {
      const showToast = runtimeFunction<RuntimeToast>('showToast');
      if (!showToast) return false;
      showToast('', notice.title, notice.detail, 10_000, {
        hint: notice.hint,
        onClick: notice.onClick,
      });
      return true;
    },
    translate,
    openSettings(): unknown {
      return runtimeFunction<() => unknown>('openSettings')?.();
    },
    switchSettingsTab(tabId: string): unknown {
      return runtimeFunction<(value: string) => unknown>('switchSettingsTab')?.(tabId);
    },
    storage: resolveBrowserLocalStorage(),
    isVisible: () => document.visibilityState !== 'hidden',
  });
  resetNoticeController.start();

  if (!visibilityListenerInstalled) {
    visibilityListenerInstalled = true;
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState !== 'hidden') {
        void resetNoticeController?.checkIfDue();
      }
    });
    window.addEventListener('beforeunload', () => {
      resetNoticeController?.destroy();
      resetNoticeController = null;
    }, { once: true });
  }
}

export async function preload(): Promise<void> {
  startSubscriptionResetNotice();
  document.dispatchEvent(new CustomEvent('tofu:feature-domain-loaded', {
    detail: { domain: 'background' },
  }));
}
