import { installActionRegistry, type ActionCallable } from './action-registry';
import { apiTransport } from './api/transport';
import { setLanguage, t } from './i18n';
import * as relayAdmin from './admin/relay-admin.js';

const adminActions = relayAdmin as unknown as Record<string, unknown>;

function resolveAdminAction(name: string): ActionCallable | undefined {
  const action = adminActions[name];
  return typeof action === 'function' ? action as ActionCallable : undefined;
}

const publicWindow = window as unknown as Record<string, unknown>;
publicWindow.Api = apiTransport;
publicWindow.TofuModules = Object.freeze({
  version: 3 as const,
  apiTransport,
  resolveAction: resolveAdminAction,
  t,
  setLanguage,
});

installActionRegistry(resolveAdminAction);

async function start(): Promise<void> {
  if (document.readyState === 'loading') {
    await new Promise<void>((resolve) => {
      document.addEventListener('DOMContentLoaded', () => resolve(), { once: true });
    });
  }
  await relayAdmin.bootAdmin();
  window.dispatchEvent(new CustomEvent('tofu:app-ready', {
    detail: { version: 3, entry: 'admin' },
  }));
}

void start().catch((error: unknown) => {
  console.error('[admin] application initialization failed', error);
  window.dispatchEvent(new CustomEvent('tofu:app-failed', { detail: { error } }));
});
