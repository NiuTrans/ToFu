// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  ChannelAccountView, ChannelThreadPage, OutboxPage,
} from '../../api/channels.generated';

const runtime = vi.hoisted(() => ({ Api: undefined as unknown, closeSettings: vi.fn(), loadConversation: vi.fn() }));
vi.mock('../../feature-registry', () => ({ featureRegistry: runtime }));
vi.mock('../../i18n', () => ({
  t: (key: string, params?: Record<string, string>) => (params ? `${key}:${JSON.stringify(params)}` : key),
}));

import { destroyDxTab, populateDxTab } from './dx-channel';

function account(overrides: Partial<ChannelAccountView> = {}): ChannelAccountView {
  return {
    channel: 'dx', enabled: true, identity: { uid: '1001', mis: 'owner', name: 'Owner' },
    commandChat: 'dx:gid:900', skillInstalled: true, routerConversationId: 'router-conv',
    status: { state: 'connected', detail: '', updatedAtMs: 1, connectedAtMs: 1 }, ...overrides,
  };
}

const threads: ChannelThreadPage = {
  nextCursor: null,
  items: [
    { key: 'dx:gid:900', kind: 'gid', title: 'Tofu Router', mode: 'command', modeOverride: null,
      replyPolicy: 'draft', replyPolicyOverride: null, isCommandChat: true, subscribers: [],
      lastEventAtMs: null, watermark: null },
    { key: 'dx:gid:500', kind: 'gid', title: 'Team', mode: 'ignore', modeOverride: null,
      replyPolicy: 'draft', replyPolicyOverride: null, isCommandChat: false, subscribers: [],
      lastEventAtMs: null, watermark: null },
  ],
};

const outbox: OutboxPage = {
  items: [{ id: 'ob1', thread: 'dx:uid:2002', threadTitle: 'Ann', text: '[Tofu 代发] hello', replyTo: null,
            state: 'draft', origin: { kind: 'tool', conversationId: 'router-conv', turnId: 't' },
            createdAtMs: 1, updatedAtMs: 1, externalId: null, error: null }],
};

function fakeApi(view = account()) {
  return {
    dxAccount: vi.fn(async () => view),
    updateDxAccount: vi.fn(async () => view),
    createDxCommandChat: vi.fn(async () => view),
    dxThreads: vi.fn(async () => threads),
    updateDxThread: vi.fn(async () => threads.items[1]),
    outbox: vi.fn(async () => outbox),
    approveOutbox: vi.fn(async () => outbox.items[0]),
    discardOutbox: vi.fn(async () => outbox.items[0]),
  };
}

const DISABLED = { state: 'disabled', detail: '', updatedAtMs: 1, connectedAtMs: null } as const;

async function settle() {
  for (let index = 0; index < 5; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

async function flush() {
  for (let index = 0; index < 20; index += 1) await Promise.resolve();
}

function findButton(label: string): HTMLButtonElement | undefined {
  return [...document.querySelectorAll('button')].find((node) => node.textContent === label);
}

function button(label: string): HTMLButtonElement {
  const found = findButton(label);
  if (!found) throw new Error(`missing button ${label}`);
  return found;
}

function text(): string {
  return document.getElementById('dxChannelRoot')!.textContent ?? '';
}

describe('大象 settings tab', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="settingsTab_dx"><div id="dxChannelRoot"></div></div>';
  });
  afterEach(() => destroyDxTab());

  it('renders status, command chat, thread modes and outbox drafts from the server', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const text = document.getElementById('dxChannelRoot')!.textContent ?? '';
    expect(text).toContain('settings.dx.state.connected');
    expect(text).toContain('Owner (owner) · 1001');
    expect(text).toContain('Tofu Router (dx:gid:900)');
    expect(text).toContain('[Tofu 代发] hello');
    expect(findButton('settings.dx.commandChatCreate')).toBeUndefined();  // already chosen
    expect(text).not.toContain('router-conv');
    button('settings.channels.routerOpen').click();
    expect(runtime.closeSettings).toHaveBeenCalled();
    expect(runtime.loadConversation).toHaveBeenCalledWith('router-conv');
  });

  it('explains what a disabled account still needs and shows progress while enabling', async () => {
    const api = fakeApi(account({ enabled: false, identity: null, commandChat: null, status: DISABLED }));
    let finish: (value: ChannelAccountView) => void = () => undefined;
    api.updateDxAccount.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    expect(text()).toContain('settings.dx.noIdentity');
    expect(text()).toContain('settings.dx.enableHint');
    expect(text()).toContain('settings.dx.commandChatNeedsAccount');
    expect(findButton('settings.dx.commandChatCreate')).toBeUndefined();
    button('settings.dx.enable').click();
    const pending = button('settings.channels.working');
    expect(pending.getAttribute('aria-busy')).toBe('true');
    expect(pending.querySelector('.ui-spinner')).not.toBeNull();
    expect(button('settings.dx.refresh').disabled).toBe(true);
    expect(api.updateDxAccount).toHaveBeenCalledWith({ enabled: true });
    api.dxAccount.mockResolvedValue(account());
    finish(account());
    await settle();
    expect(text()).toContain('settings.dx.state.connected');
    expect(text()).not.toContain('settings.dx.commandChatNeedsAccount');
  });

  it('keeps a failed command beside its control and restores the control', async () => {
    const api = fakeApi(account({ enabled: false, identity: null, commandChat: null, status: DISABLED }));
    api.updateDxAccount.mockRejectedValueOnce(new Error('bridge down'));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    button('settings.dx.enable').click();
    await settle();
    const alert = document.querySelector('.channel-feedback')!;
    expect(alert.textContent).toContain('settings.dx.actionFailed:{"error":"bridge down"}');
    expect(alert.previousElementSibling).toBe(button('settings.dx.enable').closest('.channel-actions'));
    expect(button('settings.dx.enable').disabled).toBe(false);
    expect(button('settings.dx.enable').hasAttribute('aria-busy')).toBe(false);
    expect(api.dxAccount).toHaveBeenCalledTimes(1);
  });

  it('re-reads a connecting account until its session reports, then stops', async () => {
    vi.useFakeTimers();
    try {
      const api = fakeApi(account({ status: { state: 'starting', detail: '', updatedAtMs: 1, connectedAtMs: null } }));
      runtime.Api = { channels: api };
      populateDxTab();
      await flush();
      expect(text()).toContain('settings.dx.state.starting');
      expect(document.querySelector('.channel-status .ui-spinner')).not.toBeNull();
      await vi.advanceTimersByTimeAsync(3000);
      await flush();
      expect(api.dxAccount).toHaveBeenCalledTimes(2);
      api.dxAccount.mockResolvedValue(account());
      await vi.advanceTimersByTimeAsync(3000);
      await flush();
      expect(text()).toContain('settings.dx.state.connected');
      const reads = api.dxAccount.mock.calls.length;
      await vi.advanceTimersByTimeAsync(30_000);
      await flush();
      expect(api.dxAccount).toHaveBeenCalledTimes(reads);
    } finally {
      vi.useRealTimers();
    }
  });

  it('approves a draft and re-reads authoritative state', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    button('settings.dx.approve').click();
    await settle();
    expect(api.approveOutbox).toHaveBeenCalledWith('ob1');
    expect(api.outbox).toHaveBeenCalledTimes(2);
  });

  it('changes a thread mode with null meaning the default', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const selects = [...document.querySelectorAll('select')];
    const mode = selects.find((select) => [...select.options].some((option) => option.value === 'mentions'))!;
    mode.value = 'watch';
    mode.dispatchEvent(new Event('change'));
    await settle();
    expect(api.updateDxThread).toHaveBeenCalledWith('dx:gid:500', { mode: 'watch' });
    mode.value = '';
    mode.dispatchEvent(new Event('change'));
    await settle();
    expect(api.updateDxThread).toHaveBeenLastCalledWith('dx:gid:500', { mode: null });
  });

  it('shows the needs-login state and disables enabling without the skill', async () => {
    const api = fakeApi(account({ enabled: false, skillInstalled: false, identity: null,
                                  status: { state: 'needs_login', detail: 'exit 42', updatedAtMs: 1 } }));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const text = document.getElementById('dxChannelRoot')!.textContent ?? '';
    expect(text).toContain('settings.dx.state.needs_login — exit 42');
    expect(text).toContain('settings.dx.skillMissing');
    expect(button('settings.dx.enable').disabled).toBe(true);
  });

  it('reports a failed read without stale controls', async () => {
    const api = fakeApi();
    api.dxAccount.mockRejectedValueOnce(new Error('boom'));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    expect(document.getElementById('dxChannelRoot')!.textContent).toContain('settings.dx.loadFailed');
    expect(document.querySelectorAll('select')).toHaveLength(0);
  });
});
