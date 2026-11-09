// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { BotAccountView, ChannelThreadPage, OutboxPage } from '../../api/channels.generated';

const runtime = vi.hoisted(() => ({ Api: undefined as unknown }));
vi.mock('../../feature-registry', () => ({ featureRegistry: runtime }));
vi.mock('../../i18n', () => ({
  t: (key: string, params?: Record<string, string>) => (params ? `${key}:${JSON.stringify(params)}` : key),
}));

import { destroyBotTab, populateBotTab } from './bot-channel';

const SECRET = 'wecom-fake-secret-for-the-panel-test-000';
const CONNECTED = { state: 'connected', detail: '', updatedAtMs: 1, connectedAtMs: 1 } as const;
const DISABLED = { state: 'disabled', detail: '', updatedAtMs: 1, connectedAtMs: null } as const;
const CHAT = 'wecom:uid:mfrgg';

function account(overrides: Partial<BotAccountView> = {}): BotAccountView {
  return {
    channel: 'wecom', enabled: true, appId: 'aibot-1', identity: { uid: 'zhangsan', name: '' },
    commandChat: CHAT, status: CONNECTED,
    credential: { name: 'channel.u7.wecom.secret', hint: 'wec…0000', managed: true },
    pairingCode: null, login: null, routerConversationId: 'router-conv', ...overrides,
  };
}

const threads: ChannelThreadPage = {
  nextCursor: null,
  items: [
    { key: CHAT, kind: 'uid', title: 'zhangsan', mode: 'command', modeOverride: null, replyPolicy: 'draft',
      replyPolicyOverride: null, isCommandChat: true, subscribers: [], lastEventAtMs: 1, watermark: null, note: '' },
    { key: 'wecom:gid:mzxw6', kind: 'gid', title: '项目群', mode: 'ignore', modeOverride: null,
      replyPolicy: 'never', replyPolicyOverride: 'never', isCommandChat: false, subscribers: [],
      lastEventAtMs: 1, watermark: null, note: '' },
  ],
};

const outbox: OutboxPage = {
  items: [
    { id: 'ob1', thread: 'wecom:gid:mzxw6', threadTitle: '项目群', text: '[Tofu 代发] 周报已发出。', replyTo: null,
      state: 'draft', origin: { kind: 'tool', conversationId: 'router-conv', turnId: 't' },
      createdAtMs: 1, updatedAtMs: 1, externalId: null, error: null, attachments: [], approvalCode: '0A1B2C',
      authority: 'third_party', decision: null },
    { id: 'ob2', thread: 'qq:uid:gezdg', threadTitle: 'QQ', text: '[Tofu 代发] qq only', replyTo: null,
      state: 'draft', origin: { kind: 'tool', conversationId: 'router-conv', turnId: 't' },
      createdAtMs: 1, updatedAtMs: 1, externalId: null, error: null, attachments: [], approvalCode: '3D4E5F',
      authority: 'third_party', decision: null },
  ],
};

function fakeApi(view: BotAccountView = account()) {
  return {
    channels: {
      botAccount: vi.fn(async () => view),
      updateBotAccount: vi.fn(async () => view),
      botThreads: vi.fn(async () => threads),
      updateBotThread: vi.fn(async () => threads.items[1]),
      outbox: vi.fn(async (channel: string) => ({
        items: outbox.items.filter((item) => item.thread.startsWith(`${channel}:`)),
      })),
      approveOutbox: vi.fn(async () => outbox.items[0]),
      discardOutbox: vi.fn(async () => outbox.items[0]),
    },
    credentials: {
      list: vi.fn(async () => ({ credentials: [{ name: 'wecom_bot' }, { name: 'channel.u7.wecom.secret' }] })),
    },
  };
}

async function settle() {
  for (let index = 0; index < 5; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

function root(channel = 'wecom'): HTMLElement {
  return document.getElementById(`${channel}ChannelRoot`)!;
}

function button(label: string, scope: ParentNode = document): HTMLButtonElement {
  const found = [...scope.querySelectorAll('button')].find((node) => node.textContent === label);
  if (!found) throw new Error(`missing button ${label}`);
  return found as HTMLButtonElement;
}

function input(label: string): HTMLInputElement {
  const field = [...root().querySelectorAll('label')].find((node) => node.textContent === label);
  if (!field) throw new Error(`missing field ${label}`);
  const control = document.getElementById(field.htmlFor) ?? field.parentElement!.querySelector('input');
  return control as HTMLInputElement;
}

describe('bot platform settings panels', () => {
  beforeEach(() => {
    document.body.innerHTML = ['wecom', 'qq', 'wechat']
      .map((channel) => `<div id="settingsTab_${channel}"><div id="${channel}ChannelRoot"></div></div>`).join('');
  });
  afterEach(() => {
    for (const channel of ['wecom', 'qq', 'wechat'] as const) destroyBotTab(channel);
  });

  it('renders status, bot, owner, credential hint, setup steps, chats and only its own outbox', async () => {
    const api = fakeApi();
    runtime.Api = api;
    populateBotTab('wecom');
    await settle();
    const text = root().textContent ?? '';
    expect(text).toContain('settings.bots.state.connected');
    expect(text).toContain('aibot-1');
    expect(text).toContain('zhangsan');
    expect(text).toContain('wec…0000');
    expect([...root().querySelectorAll('.channel-steps li')].map((node) => node.textContent))
      .toEqual(['settings.bots.wecom.step1', 'settings.bots.wecom.step2', 'settings.bots.wecom.step3']);
    expect(text).toContain('项目群');
    expect(text).toContain('[Tofu 代发] 周报已发出。');
    expect(text).not.toContain('qq only');
    expect(api.channels.botAccount).toHaveBeenCalledWith('wecom', expect.anything());
    expect(api.channels.outbox).toHaveBeenCalledWith('wecom', expect.anything());
    // The command chat's mode is fixed; other chats get a mode and a reply policy select.
    const rows = [...root().querySelectorAll('.channel-row')];
    expect(rows).toHaveLength(2);
    expect(rows[0].querySelectorAll('select')).toHaveLength(1);
    expect(rows[0].textContent).toContain('settings.bots.commandChat');
    expect(rows[1].querySelectorAll('select')).toHaveLength(2);
    // The vault picker offers entries the owner made, never the reserved channel entries.
    const options = [...root().querySelectorAll('option')].map((node) => node.value);
    expect(options).toContain('wecom_bot');
    expect(options).not.toContain('channel.u7.wecom.secret');
  });

  it('submits a pasted secret once, clears it at once and never renders it back', async () => {
    const api = fakeApi();
    runtime.Api = api;
    populateBotTab('wecom');
    await settle();
    const secret = root().querySelector('input[type="password"]') as HTMLInputElement;
    expect(secret.value).toBe('');
    secret.value = `  ${SECRET} `;
    button('settings.bots.save').click();
    expect(secret.value).toBe('');
    await settle();
    expect(api.channels.updateBotAccount).toHaveBeenCalledWith('wecom', { secret: SECRET });
    expect(api.channels.botAccount).toHaveBeenCalledTimes(2);
    expect(document.body.innerHTML).not.toContain(SECRET);
  });

  it('first setup sends the BotID, secret and enable together, and refuses an incomplete form', async () => {
    const api = fakeApi(account({ enabled: false, appId: null, identity: null, commandChat: null,
                                  credential: null, status: DISABLED }));
    runtime.Api = api;
    populateBotTab('wecom');
    await settle();
    expect(button('settings.github.enable').disabled).toBe(true);
    expect(root().textContent).toContain('settings.bots.needsCredential');
    expect(button('settings.bots.pairNew').disabled).toBe(true);
    const save = button('settings.bots.saveEnable');
    save.click();
    const secret = root().querySelector('input[type="password"]') as HTMLInputElement;
    secret.value = SECRET;
    save.click();
    await settle();
    expect(api.channels.updateBotAccount).not.toHaveBeenCalled();
    input('settings.bots.wecom.appId').value = ' aibot-2 ';
    secret.value = SECRET;
    save.click();
    await settle();
    expect(api.channels.updateBotAccount).toHaveBeenCalledWith('wecom', { enabled: true, appId: 'aibot-2', secret: SECRET });
  });

  it('shows a pending pairing code and lets the owner cancel it or ask for a new one', async () => {
    const api = fakeApi(account({ identity: null, commandChat: null, pairingCode: '482913' }));
    runtime.Api = api;
    populateBotTab('qq');
    await settle();
    expect(root('qq').querySelector('.channel-pairing-code')!.textContent).toBe('482913');
    expect(root('qq').textContent).toContain('settings.bots.pairingCode:{"platform":"settings.tabQq"}');
    button('settings.bots.pairCancel', root('qq')).click();
    await settle();
    expect(api.channels.updateBotAccount).toHaveBeenCalledWith('qq', { pair: false });

    const paired = fakeApi(account());
    runtime.Api = paired;
    populateBotTab('qq');
    await settle();
    expect(root('qq').querySelector('.channel-pairing-code')).toBeNull();
    expect(root('qq').textContent).toContain('settings.bots.paired:{"owner":"zhangsan"}');
    button('settings.bots.pairNew', root('qq')).click();
    await settle();
    expect(paired.channels.updateBotAccount).toHaveBeenCalledWith('qq', { pair: true });
  });

  it('微信 binds by QR login: no secret field, the code while waiting, cancel and start', async () => {
    const waiting = account({ channel: 'wechat', appId: null, identity: null, commandChat: null, credential: null,
                              enabled: false, status: DISABLED,
                              login: { state: 'waiting', qrDataUri: 'data:image/png;base64,iVBORw0KGgo=', detail: '' } });
    const api = fakeApi(waiting);
    runtime.Api = api;
    populateBotTab('wechat');
    await settle();
    expect(root('wechat').querySelector('input[type="password"]')).toBeNull();
    expect(api.credentials.list).not.toHaveBeenCalled();
    const code = root('wechat').querySelector('img.channel-login-qr') as HTMLImageElement;
    expect(code.src).toBe('data:image/png;base64,iVBORw0KGgo=');
    button('settings.bots.wechat.loginCancel', root('wechat')).click();
    await settle();
    expect(api.channels.updateBotAccount).toHaveBeenCalledWith('wechat', { login: false });

    const failed = fakeApi({ ...waiting, login: { state: 'failed', qrDataUri: null, detail: 'expired' } });
    runtime.Api = failed;
    populateBotTab('wechat');
    await settle();
    expect(root('wechat').textContent).toContain('settings.bots.wechat.loginFailed:{"detail":"expired"}');
    expect(root('wechat').querySelector('img')).toBeNull();
    button('settings.bots.wechat.loginRefresh', root('wechat')).click();
    await settle();
    expect(failed.channels.updateBotAccount).toHaveBeenCalledWith('wechat', { login: true });
  });

  it('refreshes a waiting QR directly and keeps a failed request retryable', async () => {
    const waiting = account({ channel: 'wechat', credential: null,
      status: { ...DISABLED, state: 'authorizing' },
      login: { state: 'waiting', qrDataUri: 'data:image/png;base64,old', detail: '' } });
    const api = fakeApi(waiting);
    api.channels.updateBotAccount.mockRejectedValueOnce(
      Object.assign(new Error('upstream details'), { code: 'wechat_unavailable' }));
    runtime.Api = api;
    populateBotTab('wechat');
    await settle();
    expect(root('wechat').textContent).toContain('settings.bots.wechat.loginExpiryHint');
    button('settings.bots.wechat.loginRefresh', root('wechat')).click();
    await settle();
    expect(api.channels.updateBotAccount).toHaveBeenCalledWith('wechat', { login: true });
    expect(root('wechat').textContent).toContain('settings.bots.wechat.loginUnavailable');
    expect(root('wechat').textContent).not.toContain('upstream details');
    expect(button('settings.bots.wechat.loginRefresh', root('wechat')).disabled).toBe(false);
    expect(button('settings.bots.wechat.loginCancel', root('wechat')).disabled).toBe(false);
    expect(root('wechat').querySelector('img')?.getAttribute('src')).toBe(waiting.login!.qrDataUri);
  });

  it('observes scan and expiry changes even while the account stays authorizing', async () => {
    vi.useFakeTimers();
    try {
      const waiting = account({ channel: 'wechat', credential: null,
        status: { ...DISABLED, state: 'authorizing' },
        login: { state: 'waiting', qrDataUri: 'data:image/png;base64,old', detail: '' } });
      const api = fakeApi(waiting);
      runtime.Api = api;
      populateBotTab('wechat');
      await vi.advanceTimersByTimeAsync(0);
      api.channels.botAccount.mockResolvedValue({ ...waiting,
        login: { state: 'scanned', qrDataUri: null, detail: '' } });
      await vi.advanceTimersByTimeAsync(3000);
      expect(root('wechat').textContent).toContain('settings.bots.wechat.loginScanned');
      expect(root('wechat').querySelector('img')).toBeNull();
      api.channels.botAccount.mockResolvedValue({ ...waiting,
        login: { state: 'expired', qrDataUri: null, detail: '' } });
      await vi.advanceTimersByTimeAsync(3000);
      expect(root('wechat').textContent).toContain('settings.bots.wechat.loginExpired');
      expect([...root('wechat').querySelectorAll('[role="status"]')]
        .some((node) => node.textContent?.includes('settings.bots.wechat.loginExpired'))).toBe(true);
      button('settings.bots.wechat.loginRefresh', root('wechat')).click();
      await vi.advanceTimersByTimeAsync(0);
      expect(api.channels.updateBotAccount).toHaveBeenCalledWith('wechat', { login: true });
    } finally {
      vi.useRealTimers();
    }
  });

  it('a chat select sets or clears its override', async () => {
    const api = fakeApi();
    runtime.Api = api;
    populateBotTab('wecom');
    await settle();
    const [mode, reply] = [...root().querySelectorAll('.channel-row')[1].querySelectorAll('select')];
    expect([mode.value, reply.value]).toEqual(['', 'never']);
    expect([...(reply as HTMLSelectElement).options].map((option) => option.value))
      .toEqual(['', 'notify', 'draft', 'auto', 'never']);
    mode.value = 'watch';
    mode.dispatchEvent(new Event('change'));
    await settle();
    expect(api.channels.updateBotThread).toHaveBeenCalledWith('wecom', 'wecom:gid:mzxw6', { mode: 'watch' });
    const [, again] = [...root().querySelectorAll('.channel-row')[1].querySelectorAll('select')];
    again.value = '';
    again.dispatchEvent(new Event('change'));
    await settle();
    expect(api.channels.updateBotThread).toHaveBeenCalledWith('wecom', 'wecom:gid:mzxw6', { replyPolicy: null });
  });

  it('shows a load failure, and a destroyed panel ignores a late answer', async () => {
    const api = fakeApi();
    api.channels.botAccount = vi.fn(async () => { throw new Error('channel store offline'); });
    runtime.Api = api;
    populateBotTab('wecom');
    await settle();
    expect(root().querySelector('.ui-alert--error')!.textContent)
      .toContain('settings.bots.loadFailed:{"error":"channel store offline"}');

    let answer!: (value: BotAccountView) => void;
    const late = fakeApi();
    late.channels.botAccount = vi.fn(() => new Promise<BotAccountView>((resolve) => { answer = resolve; }));
    runtime.Api = late;
    populateBotTab('wecom');
    destroyBotTab('wecom');
    answer(account());
    await settle();
    expect(root().querySelector('.channel-card')).toBeNull();
  });
});
