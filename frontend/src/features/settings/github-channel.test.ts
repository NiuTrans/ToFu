// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  AuditPage, ChannelThreadPage, GitHubAccountView, OutboxPage,
} from '../../api/channels.generated';

const runtime = vi.hoisted(() => ({ Api: undefined as unknown }));
vi.mock('../../feature-registry', () => ({ featureRegistry: runtime }));
vi.mock('../../i18n', () => ({
  t: (key: string, params?: Record<string, string>) => (params ? `${key}:${JSON.stringify(params)}` : key),
}));

import { destroyGitHubTab, populateGitHubTab } from './github-channel';

const TOKEN = 'ghp_fakeSecretTokenForThePanelTest000';
const DISABLED = { state: 'disabled', detail: '', updatedAtMs: 1, connectedAtMs: null } as const;

function account(overrides: Partial<GitHubAccountView> = {}): GitHubAccountView {
  return {
    channel: 'github', enabled: true,
    identity: { uid: '583231', mis: 'octo-owner', name: 'Octo Owner' },
    status: { state: 'connected', detail: '', updatedAtMs: 1, connectedAtMs: 1 },
    credential: { name: 'channel.u7.github.token', hint: 'ghp_…0001', managed: true },
    apiBaseUrl: 'https://api.github.com', webBaseUrl: 'https://github.com',
    poll: { intervalS: 60, lastPollAtMs: 1, nextPollAtMs: 61_000 },
    routerConversationId: 'router-conv', ...overrides,
  };
}

const threads: ChannelThreadPage = {
  nextCursor: null,
  items: [
    { key: 'github:octo/repo#12', kind: 'issue', title: 'Fix the poller', mode: 'watch', modeOverride: 'watch',
      replyPolicy: 'draft', replyPolicyOverride: 'draft', isCommandChat: false, subscribers: [],
      lastEventAtMs: 1, watermark: null, note: '' },
    { key: 'github:octo/other#3', kind: 'issue', title: 'Bug', mode: 'mentions', modeOverride: null,
      replyPolicy: 'notify', replyPolicyOverride: null, isCommandChat: false, subscribers: [],
      lastEventAtMs: 1, watermark: null, note: '' },
  ],
};

const outbox: OutboxPage = {
  items: [
    { id: 'ob1', thread: 'github:octo/repo#12', threadTitle: 'Fix the poller', text: '[Tofu 代发] Plan B is in.',
      replyTo: null, state: 'draft', origin: { kind: 'tool', conversationId: 'router-conv', turnId: 't' },
      createdAtMs: 1, updatedAtMs: 1, externalId: null, error: null, attachments: [], approvalCode: '0A1B2C',
      authority: 'third_party', decision: null },
    { id: 'ob2', thread: 'dx:uid:2002', threadTitle: 'Ann', text: '[Tofu 代发] hello', replyTo: null,
      state: 'draft', origin: { kind: 'tool', conversationId: 'router-conv', turnId: 't' },
      createdAtMs: 1, updatedAtMs: 1, externalId: null, error: null, attachments: [], approvalCode: '3D4E5F',
      authority: 'third_party', decision: null },
  ],
};

const audit: AuditPage = {
  nextBefore: null,
  items: [{ ...outbox.items[0], id: 'ob0', text: '[Tofu 代发] Fixed in #13.', state: 'sent', authority: 'owner',
            decision: { kind: 'approved', by: 'owner_settings', atMs: 5, turnId: '' } }],
};

function fakeApi(view = account()) {
  return {
    channels: {
      githubAccount: vi.fn(async () => view),
      updateGitHubAccount: vi.fn(async () => view),
      githubThreads: vi.fn(async () => threads),
      updateGitHubThread: vi.fn(async () => threads.items[0]),
      outbox: vi.fn(async (channel: string) => ({
        items: outbox.items.filter((item) => item.thread.startsWith(`${channel}:`)),
      })),
      approveOutbox: vi.fn(async () => outbox.items[0]),
      discardOutbox: vi.fn(async () => outbox.items[0]),
      audit: vi.fn(async (_channel: string, _before?: string, _signal?: AbortSignal) => audit),
    },
    credentials: {
      list: vi.fn(async () => ({ credentials: [{ name: 'github_token' }, { name: 'skill.u7.x.key' },
                                               { name: 'channel.u7.github.token' }] })),
    },
  };
}

async function settle() {
  for (let index = 0; index < 5; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

function button(label: string): HTMLButtonElement {
  const found = [...document.querySelectorAll('button')].find((node) => node.textContent === label);
  if (!found) throw new Error(`missing button ${label}`);
  return found as HTMLButtonElement;
}

function root(): HTMLElement {
  return document.getElementById('githubChannelRoot')!;
}

describe('GitHub settings tab', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="settingsTab_github"><div id="githubChannelRoot"></div></div>';
  });
  afterEach(() => destroyGitHubTab());

  it('renders status, identity, token hint, polling, threads and only GitHub drafts', async () => {
    const api = fakeApi();
    runtime.Api = api;
    populateGitHubTab();
    await settle();
    const text = root().textContent ?? '';
    expect(text).toContain('settings.github.state.connected');
    expect(text).toContain('Octo Owner (octo-owner) · 583231');
    expect(text).toContain('ghp_…0001');
    expect(text).toContain('"seconds":"60"');
    expect(text).toContain('Fix the poller');
    expect(text).toContain('octo/repo, octo/other');
    expect(text).toContain('[Tofu 代发] Plan B is in.');
    expect(text).not.toContain('[Tofu 代发] hello');
    expect(api.channels.outbox).toHaveBeenCalledWith('github', expect.anything());
    expect(root().querySelector('.channel-card-head .channel-status')!.textContent)
      .toBe('settings.github.state.connected');
    // One header row names the columns; every cell keeps its own label.
    expect([...root().querySelectorAll('.channel-table-head span')].map((node) => node.textContent))
      .toEqual(['settings.github.column.thread', 'settings.github.column.mode', 'settings.github.column.reply']);
    expect(root().querySelectorAll('.channel-row .channel-cell label')).toHaveLength(4);
    const link = [...root().querySelectorAll('a')].find((node) => node.textContent === 'github:octo/repo#12')!;
    expect(link.href).toBe('https://github.com/octo/repo/issues/12');
    expect(link.rel).toContain('noopener');
    const options = [...root().querySelectorAll('option')].map((node) => node.value);
    expect(options).toContain('github_token');
    expect(options).not.toContain('skill.u7.x.key');
    expect(options).not.toContain('channel.u7.github.token');
  });

  it('submits a pasted token once and never renders it back', async () => {
    const api = fakeApi();
    runtime.Api = api;
    populateGitHubTab();
    await settle();
    const secret = root().querySelector('input[type="password"]') as HTMLInputElement;
    secret.value = `  ${TOKEN} `;
    button('settings.github.tokenSave').click();
    expect(secret.value).toBe('');
    await settle();
    expect(api.channels.updateGitHubAccount).toHaveBeenCalledWith({ token: TOKEN });
    expect(api.channels.githubAccount).toHaveBeenCalledTimes(2);
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it('verifies and enables a first token in one step', async () => {
    const api = fakeApi(account({ enabled: false, credential: null, identity: null, status: DISABLED }));
    runtime.Api = api;
    populateGitHubTab();
    await settle();
    expect(root().textContent).toContain('settings.github.needsToken');
    const create = [...root().querySelectorAll('a')].find((node) => node.textContent === 'settings.github.tokenCreate')!;
    expect(create.href).toBe('https://github.com/settings/tokens/new?scopes=repo&description=Tofu');
    expect(create.rel).toContain('noopener');
    const secret = root().querySelector('input[type="password"]') as HTMLInputElement;
    secret.value = TOKEN;
    secret.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    await settle();
    expect(api.channels.updateGitHubAccount).toHaveBeenCalledWith({ token: TOKEN, enabled: true });
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it('shows a refused token beside its input and keeps the controls usable', async () => {
    const api = fakeApi(account({ enabled: false, credential: null, identity: null, status: DISABLED }));
    api.channels.updateGitHubAccount.mockRejectedValueOnce(
      Object.assign(new Error('GitHub rejected the token'), { status: 400 }));
    runtime.Api = api;
    populateGitHubTab();
    await settle();
    (root().querySelector('input[type="password"]') as HTMLInputElement).value = TOKEN;
    button('settings.github.tokenSaveEnable').click();
    await settle();
    const alert = root().querySelector('.channel-feedback')!;
    expect(alert.textContent).toContain('settings.github.tokenFailed:{"error":"GitHub rejected the token"}');
    expect(alert.previousElementSibling).toBe(button('settings.github.tokenSaveEnable').closest('.channel-actions'));
    expect(button('settings.github.tokenSaveEnable').disabled).toBe(false);
    expect(api.channels.githubAccount).toHaveBeenCalledTimes(1);
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it('watches a thread by owner/repo#number and rejects other input', async () => {
    const api = fakeApi();
    runtime.Api = api;
    populateGitHubTab();
    await settle();
    const target = [...root().querySelectorAll('input.ui-input')]
      .find((node) => (node as HTMLInputElement).type === 'text' && !(node as HTMLInputElement).value) as HTMLInputElement;
    target.value = 'not a thread';
    button('settings.github.watchAdd').click();
    await settle();
    expect(api.channels.updateGitHubThread).not.toHaveBeenCalled();
    expect(root().textContent).toContain('settings.github.watchInvalid');
    const retry = [...root().querySelectorAll('input.ui-input')]
      .find((node) => (node as HTMLInputElement).value === 'not a thread') as HTMLInputElement;
    retry.value = 'Octo/Repo#7';
    button('settings.github.watchAdd').click();
    await settle();
    expect(api.channels.updateGitHubThread).toHaveBeenCalledWith('github:Octo/Repo#7', { mode: 'watch' });
  });

  it('changes a thread mode with null meaning the default and approves a draft', async () => {
    const api = fakeApi();
    runtime.Api = api;
    populateGitHubTab();
    await settle();
    const mode = [...root().querySelectorAll('select')]
      .find((select) => [...select.options].some((option) => option.value === 'mentions'))!;
    mode.value = '';
    mode.dispatchEvent(new Event('change'));
    await settle();
    expect(api.channels.updateGitHubThread).toHaveBeenCalledWith('github:octo/repo#12', { mode: null });
    button('settings.github.approve').click();
    await settle();
    expect(api.channels.approveOutbox).toHaveBeenCalledWith('ob1');
  });

  it('offers notify-only first as the default, shows a draft\'s approval code and the GitHub audit', async () => {
    const api = fakeApi();
    runtime.Api = api;
    populateGitHubTab();
    await settle();
    const reply = [...root().querySelectorAll('select')]
      .find((select) => [...select.options].some((option) => option.value === 'notify'))!;
    expect([...reply.options].map((option) => option.value)).toEqual(['', 'notify', 'draft', 'auto', 'never']);
    expect(reply.options[0].textContent).toBe('settings.github.default:{"value":"settings.github.reply.notify"}');
    reply.value = 'notify';
    reply.dispatchEvent(new Event('change'));
    await settle();
    expect(api.channels.updateGitHubThread).toHaveBeenCalledWith('github:octo/repo#12', { replyPolicy: 'notify' });
    const code = root().querySelector('.channel-outbox-item .channel-outbox-code')!;
    expect(code.textContent).toBe('settings.channels.approvalCode:{"code":"0A1B2C"}');
    expect(api.channels.audit).toHaveBeenCalledWith('github', '', expect.anything());
    const record = root().querySelector('.channel-audit')!;
    expect(record.querySelector('.channel-card-title')!.textContent).toBe('settings.channels.audit.title');
    expect(record.textContent).toContain('[Tofu 代发] Fixed in #13.');
    expect(record.textContent).toContain('settings.channels.audit.authority.owner');
    expect(record.textContent).toContain('"decision":"settings.channels.audit.kind.approved"');
    expect(record.querySelector('.channel-outbox-code')).toBeNull();  // the audit decides nothing
  });

  it('keeps enabling unavailable without a token and the API base fixed while enabled', async () => {
    const api = fakeApi(account({ enabled: false, credential: null, identity: null,
                                  status: { state: 'needs_login', detail: 'GitHub rejected the token (401)', updatedAtMs: 1 } }));
    runtime.Api = api;
    populateGitHubTab();
    await settle();
    expect(root().textContent).toContain('settings.github.state.needs_login — GitHub rejected the token (401)');
    expect(root().textContent).toContain('settings.github.tokenNone');
    expect(button('settings.github.enable').disabled).toBe(true);
    expect(button('settings.github.apiBaseSave').disabled).toBe(false);
  });

  it('reports a failed read without stale controls', async () => {
    const api = fakeApi();
    api.channels.githubAccount.mockRejectedValueOnce(new Error('boom'));
    runtime.Api = api;
    populateGitHubTab();
    await settle();
    expect(root().textContent).toContain('settings.github.loadFailed');
    expect(document.querySelectorAll('select')).toHaveLength(0);
  });
});
