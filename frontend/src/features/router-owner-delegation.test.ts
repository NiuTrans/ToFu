// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RouterOwnerDelegationPorts } from './router-owner-delegation';
import type { RouterOwnerDelegationState } from '../api/router.generated';

const revokedGrant = {
  version: 1 as const, owner: 1, state: 'revoked' as const, revision: 0, scopes: ['restart', 'conversation_control'] as ('restart' | 'conversation_control')[],
  grantee: null, grantedAt: null, grantedBy: null, assurance: null, revokedAt: null, revokedBy: null, updatedAt: null,
};
const router = { conversationId: 'coordinator-router', revision: 1, availability: 'available' as const };
const off: RouterOwnerDelegationState = { grant: revokedGrant, active: false, router, actions: [] };
const grantee = { kind: 'global_router' as const, conversationId: 'coordinator-router', bindingRevision: 1, tenant: null };
const on: RouterOwnerDelegationState = {
  grant: { ...revokedGrant, state: 'granted', revision: 1, grantee, grantedAt: 1, assurance: 'authenticated',
    grantedBy: { kind: 'human-ui', credential: 'session' } },
  active: true, router, actions: [],
};
const used: RouterOwnerDelegationState = {
  ...on,
  actions: [{
    id: 'od-1', owner: 1, scope: 'restart', requester: { ...grantee, turnId: 't', attemptId: 'a' },
    reason: 'Load the new release', trigger: { kind: 'owner_message', turnId: 'o', text: '重启一下' },
    target: { kind: 'server' }, grantRevision: 1, grantedBy: null, requestedAt: 1_700_000_000,
    status: 'restarting', code: null, detail: null, updatedAt: 1_700_000_000,
  }],
};
const catalog = {
  'routerDelegation.summaryOff': '代我操作：未开启', 'routerDelegation.summaryOn': '代我操作：已开启',
  'routerDelegation.hint': '允许总负责人替你重启 Tofu', 'routerDelegation.switch': '代我操作',
  'routerDelegation.advisory': '仅供参考', 'routerDelegation.noUse': '尚未使用。',
  'routerDelegation.lastUse': '最近一次：{operation}，{status}（{time}）— {reason}',
  'routerDelegation.failed': '未更改：{error}', 'routerDelegation.op.restart': '重启',
  'routerDelegation.op.conversation_control': '会话控制', 'routerDelegation.status.restarting': '正在重启',
};

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => catalog })));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function setup(read: RouterOwnerDelegationState, set: (granted: boolean) => Promise<RouterOwnerDelegationState>) {
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  const i18n = await import('../i18n');
  await i18n.ready?.();
  const { createRouterOwnerDelegation } = await import('./router-owner-delegation');
  const ports: RouterOwnerDelegationPorts = { read: vi.fn(async () => read), set: vi.fn(set) };
  const entry = createRouterOwnerDelegation(ports);
  document.body.replaceChildren(entry.settings);
  await vi.waitFor(() => expect(ports.read).toHaveBeenCalled());
  await Promise.resolve();
  const input = entry.settings.querySelector('input') as HTMLInputElement;
  const summary = entry.settings.querySelector('summary') as HTMLElement;
  const lastUse = entry.settings.querySelector('.router-owner-delegation-last') as HTMLElement;
  const error = entry.settings.querySelector('.router-owner-delegation-error') as HTMLElement;
  return { entry, ports, input, summary, lastUse, error };
}

describe('Act for me (the owner delegation switch)', () => {
  it('shows the server state and grants only through the owner request', async () => {
    const view = await setup(off, async () => on);
    expect(view.input.checked).toBe(false);
    expect(view.summary.dataset.i18n).toBe('routerDelegation.summaryOff');
    expect(view.lastUse.dataset.i18n).toBe('routerDelegation.noUse');
    view.input.checked = true;
    view.input.dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(view.summary.dataset.i18n).toBe('routerDelegation.summaryOn'));
    expect(view.ports.set).toHaveBeenCalledWith(true);
    expect(view.input.checked).toBe(true);
    view.entry.destroy();
  });

  it('keeps the server state and says why when the server refuses', async () => {
    const view = await setup(off, async () => { throw new Error('human_approval_required'); });
    view.input.checked = true;
    view.input.dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(view.error.hidden).toBe(false));
    expect(view.error.textContent).toContain('human_approval_required');
    expect(view.input.checked).toBe(false);
    expect(view.summary.dataset.i18n).toBe('routerDelegation.summaryOff');
    view.entry.destroy();
  });

  it('revokes at once and shows the last audited use', async () => {
    const view = await setup(used, async () => off);
    await vi.waitFor(() => expect(view.input.checked).toBe(true));
    expect(view.lastUse.dataset.i18n).toBeUndefined();
    expect(view.lastUse.textContent).toContain('Load the new release');
    view.input.checked = false;
    view.input.dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(view.input.checked).toBe(false));
    expect(view.ports.set).toHaveBeenCalledWith(false);
    expect(view.lastUse.dataset.i18n).toBe('routerDelegation.noUse');
    view.entry.destroy();
  });

  it('marks an open-mode grant as advisory and rereads only when opened', async () => {
    const view = await setup({ ...on, grant: { ...on.grant, assurance: 'advisory' } }, async () => on);
    const advisory = view.entry.settings.querySelector('.router-owner-delegation-advisory') as HTMLElement;
    await vi.waitFor(() => expect(advisory.hidden).toBe(false));
    expect(view.ports.read).toHaveBeenCalledTimes(1);
    view.entry.settings.open = true;
    view.entry.settings.dispatchEvent(new Event('toggle'));
    await vi.waitFor(() => expect(view.ports.read).toHaveBeenCalledTimes(2));
    view.entry.destroy();
  });
});
