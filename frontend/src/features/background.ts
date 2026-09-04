/** Lazy background feature owners that should not tax the first screen. */

import { t } from '../i18n';
import { resolveBrowserLocalStorage } from '../core/browser-storage';
import { featureRegistry } from '../feature-registry';
import {
  CODEX_RESET_NOTICE_PUSH_CHANNEL,
  CODEX_RESET_NOTICE_PUSH_TASK_ID,
  createSubscriptionResetNoticeController,
  type SubscriptionResetNoticeController,
} from './subscription-reset-notice';
import {
  createMyDayBackgroundController,
  type MyDayBackgroundController,
} from './myday/background-controller';
import {
  browserMyDayPersistentCache,
  createMyDayReportRepository,
} from './myday/report-cache';

type OAuthStatusApi = {
  status?(): Promise<unknown>;
};

type MyDayBackgroundApi = {
  status?(date: string): Promise<{ status?: unknown; report?: unknown } | null>;
  convCount?(date: string): Promise<{ count?: unknown } | null>;
};

type RuntimeToast = (
  icon: string,
  title: string,
  detail: string,
  durationMs: number,
  options: { hint: string; onClick(): void },
) => void;

type MyDayRuntimeToast = (
  icon: string,
  title: string,
  detail: string,
  durationMs: number,
) => void;

type RuntimePushHandler = (frame: unknown) => void;
type RuntimePushSubscribe = (
  channel: string,
  taskId: string,
  handler: RuntimePushHandler,
) => void;
type RuntimePushUnsubscribe = RuntimePushSubscribe;

let resetNoticeController: SubscriptionResetNoticeController | null = null;
let myDayBackgroundController: MyDayBackgroundController | null = null;
let myDayBackgroundStart: Promise<void> | null = null;
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
    subscribeOfferUpdates(listener): (() => void) | null {
      const subscribe = runtimeFunction<RuntimePushSubscribe>('pushSubscribe');
      const unsubscribe = runtimeFunction<RuntimePushUnsubscribe>('pushUnsubscribe');
      if (!subscribe || !unsubscribe) return null;
      const handler: RuntimePushHandler = (frame) => listener(frame);
      subscribe(
        CODEX_RESET_NOTICE_PUSH_CHANNEL,
        CODEX_RESET_NOTICE_PUSH_TASK_ID,
        handler,
      );
      return () => unsubscribe(
        CODEX_RESET_NOTICE_PUSH_CHANNEL,
        CODEX_RESET_NOTICE_PUSH_TASK_ID,
        handler,
      );
    },
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
      myDayBackgroundController?.destroy();
      myDayBackgroundController = null;
    }, { once: true });
  }
}

export function prepareMyDayBackground(): Promise<void> {
  if (myDayBackgroundController) return Promise.resolve();
  if (myDayBackgroundStart) return myDayBackgroundStart;
  const start = (async (): Promise<void> => {
    const resolveOwner = runtimeFunction<() => Promise<number | null>>(
      'initCurrentUserId',
    );
    const ownerId = resolveOwner ? await resolveOwner() : null;
    const repository = createMyDayReportRepository({
      cache: browserMyDayPersistentCache(),
      ownerId: () => ownerId,
      publishDigest: (digest) => {
        document.dispatchEvent(new CustomEvent('tofu:day', { detail: digest }));
      },
    });
    featureRegistry.MyDayReportRepository = repository;
    const dailyApi = (): MyDayBackgroundApi | undefined => (
      window as Window & { Api?: { daily?: MyDayBackgroundApi } }
    ).Api?.daily;
    myDayBackgroundController = createMyDayBackgroundController({
      ownerId: () => ownerId,
      repository,
      readStatus: async (date) => {
        const status = dailyApi()?.status;
        return typeof status === 'function' ? await status(date) : null;
      },
      readConversationCount: async (date) => {
        const convCount = dailyApi()?.convCount;
        return typeof convCount === 'function' ? await convCount(date) : null;
      },
      notify: (notice): boolean => {
        const showToast = runtimeFunction<MyDayRuntimeToast>('showToast');
        if (!showToast) return false;
        showToast(notice.icon, notice.title, notice.body, notice.durationMs);
        return true;
      },
      translate: t as unknown as (
        key: string,
        values?: Record<string, unknown>,
      ) => string,
      storage: resolveBrowserLocalStorage(),
      reportIsOpen: () => document.getElementById('dailyReportModal')
        ?.classList.contains('open') === true,
      warn: (message, detail) => console.warn(message, detail),
    });
    myDayBackgroundController.start();
  })();
  myDayBackgroundStart = start;
  void start.finally(() => {
    if (myDayBackgroundStart === start) myDayBackgroundStart = null;
  });
  return start;
}

export async function preload(): Promise<void> {
  startSubscriptionResetNotice();
  await prepareMyDayBackground();
  document.dispatchEvent(new CustomEvent('tofu:feature-domain-loaded', {
    detail: { domain: 'background' },
  }));
}
