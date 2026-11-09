// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  AuditPage, ChannelAccountView, ChannelThreadPage, ChannelThreadUpdate, ChannelThreadView, DxRobotView, OutboxPage,
} from '../../api/channels.generated';

const runtime = vi.hoisted(() => ({ Api: undefined as unknown, closeSettings: vi.fn(), loadConversation: vi.fn() }));
vi.mock('../../feature-registry', () => ({ featureRegistry: runtime }));
vi.mock('../../i18n', () => ({
  t: (key: string, params?: Record<string, string>) => (params ? `${key}:${JSON.stringify(params)}` : key),
}));

import { destroyDxTab, populateDxTab } from './dx-channel';

const CONNECTED = { state: 'connected', detail: '', updatedAtMs: 1, connectedAtMs: 1 } as const;
const DISABLED = { state: 'disabled', detail: '', updatedAtMs: 1, connectedAtMs: null } as const;

function status(state: ChannelAccountView['status']['state'], detail = '') {
  return { state, detail, updatedAtMs: 2, connectedAtMs: null };
}

function account(overrides: Partial<ChannelAccountView> = {}): ChannelAccountView {
  return {
    channel: 'dx', enabled: true, loginMis: 'owner', identity: { uid: '1001', mis: 'owner', name: 'Owner' },
    commandChat: 'dx:gid:900', skillInstalled: true, routerConversationId: 'router-conv', robot: null,
    assistantNote: '', status: CONNECTED, ...overrides,
  };
}

function thread(key: string, title: string, overrides: Partial<ChannelThreadView> = {}): ChannelThreadView {
  const kind = key.split(':')[1] as ChannelThreadView['kind'];
  const mode = kind === 'uid' ? 'mentions' : 'ignore';
  return { key, kind, title, mode, modeOverride: null, replyPolicy: 'notify', replyPolicyOverride: null,
           isCommandChat: false, subscribers: [], lastEventAtMs: null, watermark: null, note: '', ...overrides };
}

function threads(): ChannelThreadView[] {
  return [
    thread('dx:gid:500', 'Team', { lastEventAtMs: 1_000 }),
    thread('dx:gid:600', 'Ops', { mode: 'watch', modeOverride: 'watch', lastEventAtMs: 3_000 }),
    thread('dx:gid:900', 'Tofu Router', { mode: 'command', isCommandChat: true }),
    thread('dx:uid:2002', 'Ann', { lastEventAtMs: 2_000 }),
  ];
}

const DEFAULT_MODE = { gid: 'ignore', uid: 'mentions', pid: 'ignore', issue: 'mentions' } as const;

const outbox: OutboxPage = {
  items: [{ id: 'ob1', thread: 'dx:uid:2002', threadTitle: 'Ann', text: '[Tofu 代发] hello', replyTo: null,
            state: 'draft', origin: { kind: 'tool', conversationId: 'router-conv', turnId: 't' },
            createdAtMs: 1, updatedAtMs: 1, externalId: null, error: null, attachments: [],
            approvalCode: '1A2B3C', authority: 'third_party', decision: null }],
};

const EMPTY_AUDIT: AuditPage = { items: [], nextBefore: null };

const SHA = 'a'.repeat(64);
const withAttachments: OutboxPage = {
  items: [{ ...outbox.items[0], attachments: [
    { index: 0, name: 'chart.png', type: 'image/png', size: 2048, sha256: SHA, kind: 'image', renditionOf: null,
      state: 'pending', error: null },
    { index: 1, name: 'deck.pptx', type: 'application/vnd.ms-powerpoint', size: 3 * 1024 * 1024, sha256: SHA,
      kind: 'file', renditionOf: null, state: 'sent', error: null },
    { index: 2, name: 'deck.pdf', type: 'application/pdf', size: 700, sha256: SHA, kind: 'file', renditionOf: 1,
      state: 'failed', error: 'not_delivered' },
    { index: 3, name: 'deck-1.png', type: 'image/png', size: 900, sha256: SHA, kind: 'image', renditionOf: 1,
      state: 'pending', error: null },
  ] }],
};

function fakeApi(view = account(), items = threads()) {
  const byKey = new Map(items.map((item) => [item.key, item]));
  return {
    dxAccount: vi.fn(async (_signal?: AbortSignal) => view),
    updateDxAccount: vi.fn(async (_update: unknown) => view),
    createDxCommandChat: vi.fn(async () => view),
    dxThreads: vi.fn(async (_cursor?: string, _signal?: AbortSignal): Promise<ChannelThreadPage> => (
      { items: [...byKey.values()], nextCursor: null })),
    updateDxThread: vi.fn(async (key: string, update: ChannelThreadUpdate) => {
      const next = { ...byKey.get(key)! };
      if ('mode' in update) {
        next.modeOverride = update.mode ?? null;
        next.mode = update.mode ?? DEFAULT_MODE[next.kind];
      }
      if ('replyPolicy' in update) {
        next.replyPolicyOverride = update.replyPolicy ?? null;
        next.replyPolicy = update.replyPolicy ?? 'notify';
      }
      if ('note' in update) next.note = update.note ?? '';
      byKey.set(key, next);
      return next;
    }),
    outbox: vi.fn(async (_channel: string, _signal?: AbortSignal) => outbox),
    approveOutbox: vi.fn(async () => outbox.items[0]),
    discardOutbox: vi.fn(async () => outbox.items[0]),
    outboxAttachmentUrl: vi.fn((id: string, index: number) => `/api/v4/channels/outbox/${id}/attachments/${index}`),
    audit: vi.fn(async (_channel: string, _before?: string, _signal?: AbortSignal): Promise<AuditPage> => EMPTY_AUDIT),
  };
}

async function settle() {
  for (let index = 0; index < 8; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

async function flush() {
  for (let index = 0; index < 40; index += 1) await Promise.resolve();
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

function card(index: number): HTMLElement {
  return document.querySelectorAll<HTMLElement>('#dxChannelRoot .channel-card')[index];
}

function rows(): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>('.dx-thread-row')];
}

function visibleTitles(): string[] {
  return rows().filter((row) => !row.hidden)
    .map((row) => row.querySelector('.dx-thread-name')!.textContent ?? '');
}

function row(title: string): HTMLElement {
  const found = rows().find((node) => node.querySelector('.dx-thread-name')!.textContent === title);
  if (!found) throw new Error(`missing row ${title}`);
  return found;
}

function radio(scope: ParentNode, value: string): HTMLInputElement {
  return scope.querySelector<HTMLInputElement>(`input[type="radio"][value="${value}"]`)!;
}

function type(input: HTMLInputElement, value: string) {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

/** jsdom lacks the native modal: showModal refuses a detached dialog and close fires `close`, as in browsers. */
function emulateNativeModal(): () => void {
  const proto = HTMLDialogElement.prototype as unknown as Record<string, unknown>;
  const saved = { showModal: proto.showModal, close: proto.close };
  proto.showModal = function showModal(this: HTMLDialogElement) {
    if (!this.isConnected) throw new DOMException('The element is not in a Document.', 'InvalidStateError');
    this.setAttribute('open', '');
  };
  proto.close = function close(this: HTMLDialogElement, result?: string) {
    if (!this.open) return;
    this.removeAttribute('open');
    this.returnValue = result ?? '';
    this.dispatchEvent(new Event('close'));
  };
  return () => Object.assign(proto, saved);
}

describe('大象 settings tab', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="settingsTab_dx"><div id="dxChannelRoot"></div></div>';
  });
  afterEach(() => destroyDxTab());

  it('renders status, account, command chat, threads (pinned chat, then most recent) and outbox', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    expect(card(0).querySelector('.channel-card-head .channel-status')!.textContent)
      .toBe('settings.dx.state.connected');
    expect(text()).toContain('Owner (owner) · 1001');
    const chosen = card(1).querySelector<HTMLElement>('.channel-card-head .channel-chip')!;
    expect(chosen.textContent).toBe('Tofu Router');
    expect(chosen.title).toBe('Tofu Router (dx:gid:900)');
    expect(findButton('settings.dx.commandChatCreate')).toBeUndefined();  // already chosen
    expect(visibleTitles()).toEqual(['Tofu Router', 'Ops', 'Ann', 'Team']);
    // The command chat is fixed; every other thread shows its mode as one click away.
    expect(row('Tofu Router').querySelector('input')).toBeNull();
    expect(radio(row('Ops'), 'watch').checked).toBe(true);
    expect(radio(row('Team'), 'ignore').checked).toBe(true);
    expect(row('Ann').querySelector('select')!.value).toBe('notify');
    expect(text()).toContain('[Tofu 代发] hello');
    expect(api.outbox).toHaveBeenCalledWith('dx', expect.anything());
    expect(text()).not.toContain('router-conv');
    button('settings.channels.routerOpen').click();
    expect(runtime.closeSettings).toHaveBeenCalled();
    expect(runtime.loadConversation).toHaveBeenCalledWith('router-conv');
  });

  it('shows the account as soon as it is read and reads every thread page', async () => {
    const api = fakeApi();
    const pages: Array<(value: ChannelThreadPage) => void> = [];
    api.dxThreads.mockImplementation(() => new Promise<ChannelThreadPage>((resolve) => { pages.push(resolve); }));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    expect(card(0).querySelector('.channel-status')!.textContent).toBe('settings.dx.state.connected');
    expect(document.querySelector('.dx-threads .ui-spinner')).not.toBeNull();
    const [team, ops, router, ann] = threads();
    pages[0]({ items: [team, ops], nextCursor: 'thread/dx:gid:600' });
    await settle();
    expect(api.dxThreads).toHaveBeenLastCalledWith('thread/dx:gid:600', expect.anything());
    expect(rows()).toHaveLength(0);
    pages[1]({ items: [router, ann], nextCursor: null });
    await settle();
    expect(visibleTitles()).toEqual(['Tofu Router', 'Ops', 'Ann', 'Team']);
    expect(api.dxThreads).toHaveBeenCalledTimes(2);
  });

  it('enables at once and follows the account through authorizing until connected', async () => {
    vi.useFakeTimers();
    try {
      const api = fakeApi(account({ enabled: false, identity: null, loginMis: null, commandChat: null,
                                    status: DISABLED }));
      runtime.Api = { channels: api };
      populateDxTab();
      await flush();
      expect(text()).toContain('settings.dx.noIdentity');
      expect(text()).toContain('settings.dx.enableHint');
      expect(text()).toContain('settings.dx.commandChatNeedsAccount');
      const starting = account({ identity: null, commandChat: null, status: status('starting') });
      api.updateDxAccount.mockResolvedValueOnce(starting);
      api.dxAccount.mockResolvedValue(starting);
      button('settings.dx.enable').click();
      await flush();
      expect(api.updateDxAccount).toHaveBeenCalledWith({ enabled: true });
      expect(card(0).querySelector('.channel-status')!.textContent).toBe('settings.dx.state.starting');
      expect(card(0).querySelector('.channel-status .ui-spinner')).not.toBeNull();
      expect(text()).toContain('settings.dx.hint.starting');
      expect(text()).toContain('settings.dx.identityPending:{"mis":"owner"}');

      api.dxAccount.mockResolvedValue(account({ identity: null, commandChat: null, status: status('authorizing') }));
      await vi.advanceTimersByTimeAsync(3000);
      await flush();
      expect(card(0).querySelector('.channel-status')!.textContent).toBe('settings.dx.state.authorizing');
      expect(text()).toContain('settings.dx.hint.authorizing:{"mis":"owner"}');

      api.dxAccount.mockResolvedValue(account({ commandChat: null }));
      await vi.advanceTimersByTimeAsync(3000);
      await flush();
      expect(card(0).querySelector('.channel-status')!.textContent).toBe('settings.dx.state.connected');
      expect(text()).not.toContain('settings.dx.commandChatNeedsAccount');
      const reads = api.dxAccount.mock.calls.length;
      await vi.advanceTimersByTimeAsync(30_000);
      await flush();
      expect(api.dxAccount).toHaveBeenCalledTimes(reads);
    } finally {
      vi.useRealTimers();
    }
  });

  it('switches the login account from an inline form that checks the MIS first', async () => {
    const api = fakeApi();
    api.updateDxAccount.mockResolvedValue(account({ loginMis: 'alice', identity: null, commandChat: null,
                                                    status: status('starting') }));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const toggle = button('settings.dx.switchAccount');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    toggle.click();
    const input = document.querySelector<HTMLInputElement>('.dx-account-switch input')!;
    expect(input.value).toBe('owner');
    expect(document.querySelector('.dx-account-switch')!.textContent).toContain('settings.dx.misHint');
    type(input, 'not a mis');
    button('settings.dx.misSubmit').click();
    await settle();
    expect(api.updateDxAccount).not.toHaveBeenCalled();
    // The problem shows under the form's row, as a refused command would.
    const problem = document.querySelector('.dx-account-switch .channel-feedback')!;
    expect(problem.textContent).toContain('settings.dx.misInvalid');
    expect(problem.previousElementSibling).toBe(input.closest('.channel-actions'));
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(document.activeElement).toBe(input);
    type(input, '  Alice ');
    expect(document.querySelector('.channel-feedback')).toBeNull();
    expect(input.hasAttribute('aria-invalid')).toBe(false);
    api.dxAccount.mockResolvedValue(account({ loginMis: 'alice', identity: null, commandChat: null,
                                              status: status('starting') }));
    button('settings.dx.misSubmit').click();
    await settle();
    expect(api.updateDxAccount).toHaveBeenCalledWith({ mis: 'alice', enabled: true });
    expect(document.querySelector('.dx-account-switch')).toBeNull();
    expect(text()).toContain('settings.dx.identityPending:{"mis":"alice"}');
    button('settings.dx.switchAccount').click();
    button('common.cancel').click();
    expect(document.querySelector('.dx-account-switch')).toBeNull();
  });

  it('asks for a MIS when the server has no 大象 login to use, and offers no reconnect', async () => {
    const api = fakeApi(account({ loginMis: null, identity: null, commandChat: null,
                                  status: status('needs_login', 'No 大象 account to log in with') }));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    expect(document.querySelector('.dx-account-switch')).not.toBeNull();
    expect(text()).toContain('settings.dx.hint.needsMis');
    expect(findButton('settings.dx.reconnect')).toBeUndefined();
    expect(findButton('settings.dx.switchAccount')).toBeUndefined();
    expect(findButton('common.cancel')).toBeUndefined();
  });

  it('says a runtime refusal is not a login problem and offers no login or reconnect', async () => {
    const api = fakeApi(account({ status: status('runtime_unavailable', 'bridge exited (1): SSO_NODE_UNAVAILABLE: no Node') }));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    expect(text()).toContain('settings.dx.state.runtime_unavailable — bridge exited (1): SSO_NODE_UNAVAILABLE: no Node');
    expect(text()).toContain('settings.dx.hint.runtimeUnavailable');
    expect(text()).not.toContain('settings.dx.hint.needsLogin');
    expect(findButton('settings.dx.reconnect')).toBeUndefined();
  });

  it('reconnects an account that needs the owner to act', async () => {
    const api = fakeApi(account({ status: status('kicked') }));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    expect(text()).toContain('settings.dx.hint.kicked');
    button('settings.dx.reconnect').click();
    await settle();
    expect(api.updateDxAccount).toHaveBeenCalledWith({ enabled: true });
  });

  it('keeps what the owner typed, and its focus, when a status change re-renders the account', async () => {
    vi.useFakeTimers();
    try {
      const api = fakeApi(account({ identity: null, status: status('authorizing') }));
      runtime.Api = { channels: api };
      populateDxTab();
      await flush();
      button('settings.dx.switchAccount').click();
      const input = document.querySelector<HTMLInputElement>('.dx-account-switch input')!;
      input.focus();
      type(input, 'bob');
      input.setSelectionRange(1, 2);
      api.dxAccount.mockResolvedValue(account({ identity: null, status: status('starting') }));
      await vi.advanceTimersByTimeAsync(3000);
      await flush();
      expect(card(0).querySelector('.channel-status')!.textContent).toBe('settings.dx.state.starting');
      const successor = document.querySelector<HTMLInputElement>('.dx-account-switch input')!;
      expect(successor).not.toBe(input);
      expect(successor.value).toBe('bob');
      expect(document.activeElement).toBe(successor);
      expect([successor.selectionStart, successor.selectionEnd]).toEqual([1, 2]);
    } finally {
      vi.useRealTimers();
    }
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

  it('confirms and creates the Tofu Router group through a dialog it mounts itself', async () => {
    const restore = emulateNativeModal();
    try {
      const api = fakeApi(account({ commandChat: null }));
      runtime.Api = { channels: api };
      populateDxTab();
      await settle();
      expect(card(1).querySelector('.channel-card-head .channel-chip')!.textContent)
        .toBe('settings.dx.commandChatNone');
      button('settings.dx.commandChatCreate').click();
      const dialog = document.querySelector('dialog')!;
      expect(dialog.open).toBe(true);
      expect(dialog.textContent).toContain('settings.dx.commandChatCreateConfirm');
      [...dialog.querySelectorAll('button')].find((node) => node.textContent === 'common.cancel')!.click();
      await settle();
      expect(document.querySelector('dialog')).toBeNull();
      expect(api.createDxCommandChat).not.toHaveBeenCalled();

      api.dxAccount.mockResolvedValue(account());
      button('settings.dx.commandChatCreate').click();
      [...document.querySelector('dialog')!.querySelectorAll('button')]
        .find((node) => node.textContent === 'settings.dx.commandChatCreate')!.click();
      await settle();
      expect(api.createDxCommandChat).toHaveBeenCalledTimes(1);
      expect(document.querySelector('dialog')).toBeNull();
      expect(api.dxAccount).toHaveBeenCalledTimes(2);
      expect(findButton('settings.dx.commandChatCreate')).toBeUndefined();
      expect(card(1).querySelector('.channel-card-head .channel-chip')!.textContent).toBe('Tofu Router');
    } finally {
      restore();
    }
  });

  it('keeps a failed group creation beside its button', async () => {
    const restore = emulateNativeModal();
    try {
      const api = fakeApi(account({ commandChat: null }));
      api.createDxCommandChat.mockRejectedValueOnce(new Error('group_http_failed'));
      runtime.Api = { channels: api };
      populateDxTab();
      await settle();
      button('settings.dx.commandChatCreate').click();
      [...document.querySelector('dialog')!.querySelectorAll('button')]
        .find((node) => node.textContent === 'settings.dx.commandChatCreate')!.click();
      await settle();
      const alert = card(1).querySelector('.channel-feedback')!;
      expect(alert.textContent).toContain('settings.dx.actionFailed:{"error":"group_http_failed"}');
      expect(button('settings.dx.commandChatCreate').disabled).toBe(false);
    } finally {
      restore();
    }
  });

  it('picks an existing chat as the command chat from a searchable list', async () => {
    const api = fakeApi(account({ commandChat: 'dx:gid:600' }));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    expect(document.querySelector('.dx-chat-picker')).toBeNull();
    button('settings.dx.commandChatChange').click();
    const picker = document.querySelector<HTMLElement>('.dx-chat-picker')!;
    const options = () => [...picker.querySelectorAll<HTMLElement>('.dx-chat-option')]
      .filter((node) => !node.hidden).map((node) => node.querySelector('.channel-row-name')!.textContent);
    // Groups and direct chats other than the current one, most recent first.
    expect(options()).toEqual(['Tofu Router', 'Ann', 'Team']);
    type(picker.querySelector('input')!, 'ANN');
    expect(options()).toEqual(['Ann']);
    type(picker.querySelector('input')!, 'nothing');
    expect(picker.querySelector<HTMLElement>('.channel-empty')!.hidden).toBe(false);
    type(picker.querySelector('input')!, '2002');
    picker.querySelector<HTMLButtonElement>('.dx-chat-option:not([hidden]) button')!.click();
    await settle();
    expect(api.updateDxAccount).toHaveBeenCalledWith({ commandChat: 'dx:uid:2002' });
    expect(document.querySelector('.dx-chat-picker')).toBeNull();
  });

  it('sets a thread mode in one click and stores a kind default as null', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    radio(row('Team'), 'watch').click();
    await settle();
    expect(api.updateDxThread).toHaveBeenLastCalledWith('dx:gid:500', { mode: 'watch' });
    expect(radio(row('Team'), 'watch').checked).toBe(true);
    radio(row('Team'), 'ignore').click();
    await settle();
    expect(api.updateDxThread).toHaveBeenLastCalledWith('dx:gid:500', { mode: null });
    radio(row('Ann'), 'watch').click();
    await settle();
    expect(api.updateDxThread).toHaveBeenLastCalledWith('dx:uid:2002', { mode: 'watch' });
    const reply = row('Ann').querySelector('select')!;
    reply.value = 'auto';
    reply.dispatchEvent(new Event('change'));
    await settle();
    expect(api.updateDxThread).toHaveBeenLastCalledWith('dx:uid:2002', { replyPolicy: 'auto' });
    reply.value = 'notify';
    reply.dispatchEvent(new Event('change'));
    await settle();
    expect(api.updateDxThread).toHaveBeenLastCalledWith('dx:uid:2002', { replyPolicy: null });
    // A thread write changes that row only: no re-read of the whole tab.
    expect(api.dxThreads).toHaveBeenCalledTimes(1);
    expect(api.dxAccount).toHaveBeenCalledTimes(1);
  });

  it('restores the acknowledged mode beside its row when a write fails', async () => {
    const api = fakeApi();
    api.updateDxThread.mockRejectedValueOnce(new Error('store down'));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    radio(row('Team'), 'watch').click();
    await settle();
    expect(radio(row('Team'), 'ignore').checked).toBe(true);
    const alert = document.querySelector('.channel-feedback')!;
    expect(alert.textContent).toContain('settings.dx.actionFailed:{"error":"store down"}');
    expect(alert.previousElementSibling).toBe(row('Team'));
  });

  it('applies a thread\'s writes in order and shows the last acknowledged view', async () => {
    const api = fakeApi();
    let release: () => void = () => undefined;
    const first = api.updateDxThread.getMockImplementation()!;
    api.updateDxThread.mockImplementationOnce((key, update) => new Promise((resolve) => {
      release = () => resolve(first(key, update));
    }));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    radio(row('Team'), 'watch').click();
    radio(row('Team'), 'mentions').click();
    await settle();
    expect(api.updateDxThread).toHaveBeenCalledTimes(1);  // the second waits for the first
    expect(radio(row('Team'), 'mentions').checked).toBe(true);
    release();
    await settle();
    expect(api.updateDxThread.mock.calls.map((call) => call[1])).toEqual([{ mode: 'watch' }, { mode: 'mentions' }]);
    expect(radio(row('Team'), 'mentions').checked).toBe(true);
  });

  it('searches by name or key and filters by mode with counts', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const filters = [...document.querySelectorAll<HTMLLabelElement>('.dx-thread-filter .ui-choice')]
      .map((label) => label.textContent);
    expect(filters).toEqual([
      'settings.dx.filterCount:{"label":"settings.dx.filter.all","count":"4"}',
      'settings.dx.filterCount:{"label":"settings.dx.mode.watch","count":"1"}',
      'settings.dx.filterCount:{"label":"settings.dx.mode.mentions","count":"1"}',
      'settings.dx.filterCount:{"label":"settings.dx.mode.ignore","count":"1"}',
    ]);
    const search = document.querySelector<HTMLInputElement>('.dx-threads .dx-thread-search input')!;
    type(search, 'tea');
    expect(visibleTitles()).toEqual(['Team']);
    type(search, 'UID:2002');
    expect(visibleTitles()).toEqual(['Ann']);
    type(search, '');
    radio(document.querySelector('.dx-thread-filter')!, 'watch').click();
    expect(visibleTitles()).toEqual(['Ops']);
    type(search, 'team');
    expect(visibleTitles()).toEqual([]);
    expect(document.querySelector<HTMLElement>('.dx-threads .channel-empty')!.hidden).toBe(false);
    radio(document.querySelector('.dx-thread-filter')!, 'all').click();
    expect(visibleTitles()).toEqual(['Team']);
    // A changed mode updates the counts, but the row stays until the view changes.
    radio(document.querySelector('.dx-thread-filter')!, 'ignore').click();
    type(search, '');
    radio(row('Team'), 'watch').click();
    await settle();
    expect(visibleTitles()).toEqual(['Team']);
    expect(document.querySelectorAll<HTMLLabelElement>('.dx-thread-filter .ui-choice')[1].textContent)
      .toBe('settings.dx.filterCount:{"label":"settings.dx.mode.watch","count":"2"}');
  });

  it('bulk-sets selected threads and reports the ones that failed', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const bulk = document.querySelector<HTMLElement>('.dx-thread-bulk-actions')!;
    expect(bulk.hidden).toBe(true);
    row('Team').querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
    row('Ann').querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
    expect(bulk.hidden).toBe(false);
    expect(bulk.textContent).toContain('settings.dx.selected:{"count":"2"}');
    const all = document.querySelector<HTMLInputElement>('.dx-thread-bulk > .ui-choice input')!;
    expect(all.indeterminate).toBe(true);
    api.updateDxThread.mockRejectedValueOnce(new Error('store down'));
    button('settings.dx.bulk').click();
    button('settings.dx.modeValue:{"value":"settings.dx.mode.watch"}').click();
    await settle();
    const calls = api.updateDxThread.mock.calls.map(([key, update]) => [key, update]);
    expect(calls).toEqual(expect.arrayContaining([['dx:gid:500', { mode: 'watch' }],
                                                  ['dx:uid:2002', { mode: 'watch' }]]));
    expect(calls).toHaveLength(2);
    const alert = document.querySelector('.channel-feedback')!;
    expect(alert.textContent).toContain('settings.dx.bulkFailed:{"count":"1"');
    expect(button('settings.dx.bulk').disabled).toBe(false);
    // The default of each kind is stored as null.
    button('settings.dx.bulk').click();
    button('settings.dx.modeValue:{"value":"settings.dx.useDefault"}').click();
    await settle();
    expect(api.updateDxThread.mock.calls.slice(2).map((call) => call[1])).toEqual([{ mode: null }, { mode: null }]);
    button('settings.dx.bulk').click();
    button('settings.dx.replyValue:{"value":"settings.dx.reply.auto"}').click();
    await settle();
    expect(api.updateDxThread.mock.calls.slice(4).map((call) => call[1]))
      .toEqual([{ replyPolicy: 'auto' }, { replyPolicy: 'auto' }]);
    button('settings.dx.clearSelection').click();
    expect(bulk.hidden).toBe(true);
  });

  it('selects all visible threads only, and a new search clears the selection', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const search = document.querySelector<HTMLInputElement>('.dx-threads .dx-thread-search input')!;
    type(search, 'gid');
    const all = document.querySelector<HTMLInputElement>('.dx-thread-bulk > .ui-choice input')!;
    all.click();
    expect(document.querySelector('.dx-thread-bulk-actions')!.textContent).toContain('settings.dx.selected:{"count":"2"}');
    expect(row('Ann').querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(false);
    type(search, '');
    expect(document.querySelector<HTMLElement>('.dx-thread-bulk-actions')!.hidden).toBe(true);
    expect(row('Team').querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked).toBe(false);
  });

  it('lists a draft\'s attachments with name, type and size, and thumbnails only original images', async () => {
    const api = fakeApi();
    api.outbox.mockImplementation(async () => withAttachments);
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const entries = [...document.querySelectorAll<HTMLElement>('.channel-outbox-attachment')];
    expect(entries.map((entry) => entry.querySelector('.channel-outbox-attachment-name')!.textContent))
      .toEqual(['chart.png', 'deck.pptx', 'deck.pdf', 'deck-1.png']);
    expect(entries[0].querySelector('.channel-outbox-attachment-meta')!.textContent).toBe('image/png · 2.0 KB');
    expect(entries[1].querySelector('.channel-outbox-attachment-meta')!.textContent)
      .toBe('application/vnd.ms-powerpoint · 3.0 MB');
    expect(entries[2].querySelector('.channel-outbox-attachment-meta')!.textContent)
      .toBe('settings.dx.attachmentPreviewOf:{"name":"deck.pptx"} · application/pdf · 700 B');
    const thumbnails = [...document.querySelectorAll<HTMLImageElement>('.channel-outbox-thumbnail')];
    expect(thumbnails.map((image) => image.getAttribute('src'))).toEqual(['/api/v4/channels/outbox/ob1/attachments/0']);
    expect(thumbnails[0].alt).toBe('chart.png');
    expect(api.outboxAttachmentUrl).toHaveBeenCalledWith('ob1', 0);
    expect(entries.map((entry) => entry.querySelector('.channel-chip-text')!.textContent)).toEqual([
      'settings.dx.attachmentPending', 'settings.dx.outboxState.sent', 'settings.dx.outboxState.failed',
      'settings.dx.attachmentPending']);
    expect(entries[2].textContent).toContain('not_delivered');
    expect(document.querySelector('.channel-outbox-attachments')!.getAttribute('aria-label'))
      .toBe('settings.dx.attachments');
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

  it('shows the needs-login state and disables enabling without the skill', async () => {
    const api = fakeApi(account({ enabled: false, skillInstalled: false, identity: null,
                                  status: { state: 'needs_login', detail: 'exit 42', updatedAtMs: 1 } }));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    expect(text()).toContain('settings.dx.state.needs_login — exit 42');
    expect(text()).toContain('settings.dx.skillMissing');
    expect(button('settings.dx.enable').disabled).toBe(true);
  });

  it('reports a failed read in its own card', async () => {
    const api = fakeApi();
    api.dxAccount.mockRejectedValueOnce(new Error('boom'));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const failure = document.querySelector('#dxChannelRoot .ui-alert')!;
    expect(failure.textContent).toContain('settings.dx.loadFailed:{"error":"boom"}');
    expect(findButton('settings.dx.enable')).toBeUndefined();
    expect(findButton('settings.dx.disable')).toBeUndefined();
    expect(visibleTitles()).toEqual(['Tofu Router', 'Ops', 'Ann', 'Team']);
  });
});

// Synthetic, never a real credential: the card must never render it back.
const SECRET = 'fake-robot-secret-never-real-42';

function robotView(overrides: Partial<DxRobotView> = {}): DxRobotView {
  return { clientId: 'fake-robot-client', credentialName: 'channel.u1.dx.robot', credentialHint: 'fa…42',
           managed: true, builtIn: false, error: null, ...overrides };
}

function robotCard(): HTMLElement {
  const found = [...document.querySelectorAll<HTMLElement>('#dxChannelRoot .channel-card')]
    .find((node) => node.querySelector('.channel-card-title')!.textContent === 'settings.dx.robot.title');
  if (!found) throw new Error('missing robot card');
  return found;
}

function robotChip(): string {
  return robotCard().querySelector('.channel-card-head .channel-chip')!.textContent ?? '';
}

/** The client ID and secret inputs of the robot card's form. */
function robotInputs(): [HTMLInputElement, HTMLInputElement] {
  const inputs = robotCard().querySelectorAll<HTMLInputElement>('.dx-robot-form input');
  return [inputs[0], inputs[1]];
}

function secretShown(): boolean {
  return document.body.innerHTML.includes(SECRET)
    || [...document.querySelectorAll('input')].some((input) => input.value === SECRET);
}

function dialogButton(label: string): HTMLButtonElement {
  return [...document.querySelector('dialog')!.querySelectorAll('button')].find((node) => node.textContent === label)!;
}

describe('大象 notification robot card', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="settingsTab_dx"><div id="dxChannelRoot"></div></div>';
  });
  afterEach(() => destroyDxTab());

  it('renders an unconfigured robot after the command chat, with a write-only secret and its setup steps', async () => {
    runtime.Api = { channels: fakeApi() };
    populateDxTab();
    await settle();
    expect(card(2)).toBe(robotCard());
    expect(robotChip()).toBe('settings.dx.robot.state.none');
    const [client, secret] = robotInputs();
    expect(client.value).toBe('');
    expect([secret.type, secret.value, secret.autocomplete]).toEqual(['password', '', 'new-password']);
    expect(secret.placeholder).toBe('');
    expect(findButton('settings.dx.robot.remove')).toBeUndefined();
    expect(robotCard().querySelector('.dx-robot-vault')).toBeNull();
    const steps = robotCard().querySelector<HTMLDetailsElement>('.dx-robot-setup')!;
    expect(steps.open).toBe(true);
    expect(steps.querySelectorAll('.dx-robot-steps li')).toHaveLength(4);
    expect([...steps.querySelectorAll<HTMLAnchorElement>('a')].map((link) => [link.href, link.target, link.rel]))
      .toEqual([['https://open.internal.example.com/developer/app/apply', '_blank', 'noopener noreferrer'],
                ['https://xopen.internal.example.com/home', '_blank', 'noopener noreferrer']]);
    expect(steps.textContent).toContain('settings.dx.robot.stepsSource');
  });

  it('saves a pasted secret, empties its input before the request and never shows it back', async () => {
    const api = fakeApi();
    const configured = account({ robot: robotView() });
    let inputWhenSent: string | null = null;
    api.updateDxAccount.mockImplementationOnce(async () => {
      inputWhenSent = robotInputs()[1].value;
      return configured;
    });
    api.dxAccount.mockResolvedValueOnce(account()).mockResolvedValue(configured);
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const [client, secret] = robotInputs();
    type(client, '  fake-robot-client ');
    type(secret, SECRET);
    button('settings.dx.robot.save').click();
    await settle();
    expect(api.updateDxAccount).toHaveBeenCalledWith({ robot: { clientId: 'fake-robot-client', secret: SECRET } });
    expect(inputWhenSent).toBe('');
    expect(api.dxAccount).toHaveBeenCalledTimes(2);
    expect(robotChip()).toBe('settings.dx.robot.state.configured');
    const text = robotCard().textContent ?? '';
    expect(text).toContain('fake-robot-client');
    expect(text).toContain('settings.dx.robot.secretValue:{"hint":"fa…42","name":"channel.u1.dx.robot"}');
    expect(text).not.toContain('settings.dx.robot.groupOnly');  // the command chat is a group
    const [savedClient, savedSecret] = robotInputs();
    expect(savedClient.value).toBe('fake-robot-client');
    expect([savedSecret.value, savedSecret.placeholder]).toEqual(['', 'settings.dx.robot.secretKeep']);
    expect(robotCard().querySelector<HTMLDetailsElement>('.dx-robot-setup')!.open).toBe(false);
    expect(secretShown()).toBe(false);
  });

  it('keeps the saved secret when only the client ID changes', async () => {
    const api = fakeApi(account({ robot: robotView() }));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    type(robotInputs()[0], 'other-client');
    button('settings.dx.robot.save').click();
    await settle();
    expect(api.updateDxAccount).toHaveBeenCalledWith({ robot: { clientId: 'other-client' } });
  });

  it('asks for the client ID, then the secret, before sending anything', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const [client, secret] = robotInputs();
    button('settings.dx.robot.save').click();
    await settle();
    const problem = robotCard().querySelector('.channel-feedback')!;
    expect(problem.textContent).toContain('settings.dx.robot.needsClientId');
    expect(problem.previousElementSibling).toBe(client.closest('.channel-actions'));
    expect(document.activeElement).toBe(client);
    type(client, 'fake-robot-client');
    expect(robotCard().querySelector('.channel-feedback')).toBeNull();
    button('settings.dx.robot.save').click();
    await settle();
    expect(robotCard().querySelector('.channel-feedback')!.textContent).toContain('settings.dx.robot.needsSecret');
    expect(document.activeElement).toBe(secret);
    expect(api.updateDxAccount).not.toHaveBeenCalled();
  });

  it('uses an existing vault entry by name and never offers entries reserved for other owners', async () => {
    const api = fakeApi();
    const list = vi.fn(async () => ({ credentials: [
      { name: 'dx-robot-secret' }, { name: 'channel.u1.dx.robot' }, { name: 'skill.dx-pura-api.sso' },
      { name: 'other-token' }] }));
    const chosen = account({ robot: robotView({ credentialName: 'dx-robot-secret', managed: false }) });
    api.updateDxAccount.mockResolvedValueOnce(chosen);
    api.dxAccount.mockResolvedValueOnce(account()).mockResolvedValue(chosen);
    runtime.Api = { channels: api, credentials: { list } };
    populateDxTab();
    await settle();
    const vault = robotCard().querySelector<HTMLDetailsElement>('.dx-robot-vault')!;
    expect(vault.open).toBe(false);
    const select = vault.querySelector('select')!;
    expect([...select.options].map((option) => option.value)).toEqual(['dx-robot-secret', 'other-token']);
    button('settings.dx.robot.vaultUse').click();
    await settle();
    expect(vault.querySelector('.channel-feedback')!.textContent).toContain('settings.dx.robot.needsClientId');
    expect(api.updateDxAccount).not.toHaveBeenCalled();
    type(robotInputs()[0], 'fake-robot-client');
    select.value = 'dx-robot-secret';
    button('settings.dx.robot.vaultUse').click();
    await settle();
    expect(api.updateDxAccount).toHaveBeenCalledWith(
      { robot: { clientId: 'fake-robot-client', credentialName: 'dx-robot-secret' } });
    const reopened = robotCard().querySelector<HTMLDetailsElement>('.dx-robot-vault')!;
    expect(reopened.open).toBe(true);
    expect(reopened.querySelector('select')!.value).toBe('dx-robot-secret');
    expect(robotCard().textContent).toContain('"name":"dx-robot-secret"');
  });

  it('removes the robot only after the owner confirms', async () => {
    const restore = emulateNativeModal();
    try {
      const api = fakeApi(account({ robot: robotView() }));
      runtime.Api = { channels: api };
      populateDxTab();
      await settle();
      button('settings.dx.robot.remove').click();
      expect(document.querySelector('dialog')!.open).toBe(true);
      expect(document.querySelector('dialog')!.textContent).toContain('settings.dx.robot.removeConfirm');
      dialogButton('common.cancel').click();
      await settle();
      expect(document.querySelector('dialog')).toBeNull();
      expect(api.updateDxAccount).not.toHaveBeenCalled();

      api.updateDxAccount.mockResolvedValueOnce(account());
      api.dxAccount.mockResolvedValue(account());
      button('settings.dx.robot.remove').click();
      dialogButton('settings.dx.robot.remove').click();
      await settle();
      expect(api.updateDxAccount).toHaveBeenCalledWith({ robot: null });
      expect(robotChip()).toBe('settings.dx.robot.state.none');
      expect(findButton('settings.dx.robot.remove')).toBeUndefined();
    } finally {
      restore();
    }
  });

  it.each([
    ['robot_70003', 'settings.dx.robot.fix.group'],
    ['robot_30002', 'settings.dx.robot.fix.permission'],
    ['robot_70001', 'settings.dx.robot.fix.app'],
    ['token_rejected', 'settings.dx.robot.fix.credentials'],
    ['robot_credential_missing', 'settings.dx.robot.fix.secretMissing'],
    ['token_unavailable', 'settings.dx.robot.fix.network'],
    ['robot_99999', null],
  ])('shows the last refusal %s with the setup step that fixes it', async (code, fix) => {
    const error = { code, detail: 'The open platform refused the robot send: 机器人不在该群', atMs: 1_700_000_000_000 };
    runtime.Api = { channels: fakeApi(account({ robot: robotView({ error }) })) };
    populateDxTab();
    await settle();
    expect(robotChip()).toBe('settings.dx.robot.state.refused');
    const refusal = robotCard().querySelector('.dx-robot-refusal')!;
    const time = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
      .format(new Date(error.atMs));
    expect(refusal.textContent).toContain(`settings.dx.robot.refused:{"time":"${time}"}`);
    expect(refusal.textContent).toContain(`机器人不在该群 (${code})`);
    expect(refusal.querySelector('.dx-robot-fix')?.textContent ?? null).toBe(fix);
    expect(robotCard().querySelector<HTMLDetailsElement>('.dx-robot-setup')!.open).toBe(true);
  });

  it('warns that only a group command chat uses the robot', async () => {
    runtime.Api = { channels: fakeApi(account({ commandChat: 'dx:uid:2002', robot: robotView() })) };
    populateDxTab();
    await settle();
    expect(robotChip()).toBe('settings.dx.robot.state.configured');
    expect(robotCard().querySelector('.ui-alert--warning')!.textContent).toBe('settings.dx.robot.groupOnly');
  });

  it('keeps a refused save beside its row with the fix, and does not keep the secret', async () => {
    const api = fakeApi();
    api.updateDxAccount.mockRejectedValueOnce(Object.assign(
      new Error('The SSO token endpoint refused the robot credentials (HTTP 400 invalid_client)'),
      { status: 409, code: 'token_rejected' }));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    type(robotInputs()[0], 'fake-robot-client');
    type(robotInputs()[1], SECRET);
    button('settings.dx.robot.save').click();
    await settle();
    const alert = robotCard().querySelector('.channel-feedback')!;
    expect(alert.textContent).toContain('settings.dx.actionFailed:{"error":"The SSO token endpoint refused the robot '
                                        + 'credentials (HTTP 400 invalid_client)"} settings.dx.robot.fix.credentials');
    expect(alert.previousElementSibling).toBe(button('settings.dx.robot.save').closest('.channel-actions'));
    expect(button('settings.dx.robot.save').disabled).toBe(false);
    expect(button('settings.dx.robot.save').hasAttribute('aria-busy')).toBe(false);
    expect(robotInputs()[0].value).toBe('fake-robot-client');
    expect(robotInputs()[1].value).toBe('');
    expect(secretShown()).toBe(false);
    expect(api.dxAccount).toHaveBeenCalledTimes(1);
  });

  it('keeps what the owner typed when the other cards re-render', async () => {
    vi.useFakeTimers();
    try {
      const api = fakeApi(account({ status: status('disconnected') }));
      runtime.Api = { channels: api };
      populateDxTab();
      await flush();
      const robot = robotCard();
      const [client, secret] = robotInputs();
      type(client, 'fake-robot-client');
      type(secret, SECRET);
      secret.focus();
      api.dxAccount.mockResolvedValue(account());
      await vi.advanceTimersByTimeAsync(3000);
      await flush();
      expect(card(0).querySelector('.channel-status')!.textContent).toBe('settings.dx.state.connected');
      expect(api.dxAccount).toHaveBeenCalledTimes(3);  // first read, the tick, the reload
      button('settings.dx.switchAccount').click();
      expect(robotCard()).toBe(robot);
      expect([client.value, secret.value]).toEqual(['fake-robot-client', SECRET]);
      expect(document.activeElement).toBe(secret);
    } finally {
      vi.useRealTimers();
    }
  });
});

function assistantCard(): HTMLElement {
  const found = document.querySelector<HTMLElement>('#dxChannelRoot .dx-assistant');
  if (!found) throw new Error('missing assistant card');
  return found;
}

function auditCard(): HTMLElement {
  const found = document.querySelector<HTMLElement>('#dxChannelRoot .channel-audit');
  if (!found) throw new Error('missing audit card');
  return found;
}

function typeText(input: HTMLTextAreaElement, value: string) {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

function auditItem(id: string, overrides: Partial<OutboxPage['items'][number]> = {}): OutboxPage['items'][number] {
  return { ...outbox.items[0], id, state: 'sent', ...overrides };
}

describe('大象 assistant for other people\'s messages (C11)', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="settingsTab_dx"><div id="dxChannelRoot"></div></div>';
  });
  afterEach(() => destroyDxTab());

  it('explains the assistant after the robot card and saves the owner\'s account note', async () => {
    let view = account({ assistantNote: 'I lead the search team.' });
    const api = fakeApi(view);
    api.dxAccount.mockImplementation(async () => view);
    api.updateDxAccount.mockImplementation(async (update) => {
      view = { ...view, assistantNote: (update as { assistantNote: string | null }).assistantNote ?? '' };
      return view;
    });
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const titles = [...document.querySelectorAll('#dxChannelRoot .channel-card-title')].map((node) => node.textContent);
    expect(titles).toEqual(['settings.dx.status', 'settings.dx.commandChat', 'settings.dx.robot.title',
                            'settings.dx.assistant.title', 'settings.dx.threads', 'settings.dx.outbox',
                            'settings.channels.audit.title']);
    expect(card(3)).toBe(assistantCard());
    expect(assistantCard().textContent).toContain('settings.dx.assistant.how:{"label":"[Tofu 代发]"}');
    expect(assistantCard().textContent).toContain('settings.dx.assistant.approval');
    const note = assistantCard().querySelector('textarea')!;
    expect(note.value).toBe('I lead the search team.');
    expect(note.maxLength).toBe(2000);
    typeText(note, '  Keep replies short.  ');
    button('settings.dx.noteSave').click();
    await settle();
    expect(api.updateDxAccount).toHaveBeenLastCalledWith({ assistantNote: 'Keep replies short.' });
    expect(api.dxAccount).toHaveBeenCalledTimes(2);  // the save re-reads the server
    expect(assistantCard().querySelector('textarea')!.value).toBe('Keep replies short.');
    typeText(assistantCard().querySelector('textarea')!, '   ');
    button('settings.dx.noteSave').click();
    await settle();
    expect(api.updateDxAccount).toHaveBeenLastCalledWith({ assistantNote: null });  // blank clears it
  });

  it('keeps a typed account note when another command re-reads the tab', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const note = assistantCard().querySelector('textarea')!;
    typeText(note, 'unsaved words');
    button('settings.dx.refresh').click();
    await settle();
    expect(api.dxAccount).toHaveBeenCalledTimes(2);
    expect(assistantCard().querySelector('textarea')).toBe(note);
    expect(note.value).toBe('unsaved words');
  });

  it('offers notify-only first, the default, and stores choosing it as null', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const reply = row('Ann').querySelector('select')!;
    expect([...reply.options].map((option) => option.value)).toEqual(['notify', 'draft', 'auto', 'never']);
    expect(reply.value).toBe('notify');
    reply.value = 'draft';
    reply.dispatchEvent(new Event('change'));
    await settle();
    expect(api.updateDxThread).toHaveBeenLastCalledWith('dx:uid:2002', { replyPolicy: 'draft' });
    reply.value = 'notify';
    reply.dispatchEvent(new Event('change'));
    await settle();
    expect(api.updateDxThread).toHaveBeenLastCalledWith('dx:uid:2002', { replyPolicy: null });
  });

  it('edits a thread\'s note in place and shows it under the thread\'s controls', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const ann = row('Ann');
    const toggle = [...ann.querySelectorAll<HTMLButtonElement>('button')]
      .find((node) => node.textContent === 'settings.dx.note.edit')!;
    expect(toggle.getAttribute('aria-label')).toBe('settings.dx.note.edit · Ann');
    expect(ann.querySelector<HTMLElement>('.dx-thread-note')!.hidden).toBe(true);  // no note yet
    toggle.click();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const input = ann.querySelector('textarea')!;
    expect(document.activeElement).toBe(input);
    typeText(input, ' My mentor: reply formally. ');
    [...ann.querySelectorAll('button')].find((node) => node.textContent === 'settings.dx.noteSave')!.click();
    await settle();
    expect(api.updateDxThread).toHaveBeenLastCalledWith('dx:uid:2002', { note: 'My mentor: reply formally.' });
    expect(ann.querySelector('textarea')).toBeNull();
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    const shown = ann.querySelector<HTMLElement>('.dx-thread-note')!;
    expect(shown.hidden).toBe(false);
    expect(shown.textContent).toBe('settings.dx.note.value:{"note":"My mentor: reply formally."}');
    toggle.click();
    expect(ann.querySelector('textarea')!.value).toBe('My mentor: reply formally.');
    typeText(ann.querySelector('textarea')!, '');
    [...ann.querySelectorAll('button')].find((node) => node.textContent === 'settings.dx.noteSave')!.click();
    await settle();
    expect(api.updateDxThread).toHaveBeenLastCalledWith('dx:uid:2002', { note: null });  // blank removes it
    expect(ann.querySelector<HTMLElement>('.dx-thread-note')!.hidden).toBe(true);
    // An ignored thread gets no replies, so it offers no note; the command chat is the owner's own.
    expect(row('Team').querySelector<HTMLElement>('.dx-thread-controls button')!.hidden).toBe(true);
    expect(row('Tofu Router').querySelector('button')).toBeNull();
  });

  it('keeps a refused note beside its editor with what the owner typed', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    api.updateDxThread.mockRejectedValueOnce(new Error('A note is text of at most 2000 characters'));
    const ann = row('Ann');
    [...ann.querySelectorAll('button')].find((node) => node.textContent === 'settings.dx.note.edit')!.click();
    typeText(ann.querySelector('textarea')!, 'too long');
    [...ann.querySelectorAll('button')].find((node) => node.textContent === 'settings.dx.noteSave')!.click();
    await settle();
    expect(ann.querySelector('textarea')!.value).toBe('too long');
    const alert = ann.querySelector('.dx-thread-note-editor .channel-feedback')!;
    expect(alert.textContent).toContain('A note is text of at most 2000 characters');
    expect(ann.querySelector<HTMLElement>('.dx-thread-note')!.hidden).toBe(true);
  });

  it('shows a draft\'s approval code beside its state', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    const item = document.querySelector('.channel-outbox-item')!;
    expect(item.querySelector('.channel-outbox-code')!.textContent)
      .toBe('settings.channels.approvalCode:{"code":"1A2B3C"}');
    api.outbox.mockResolvedValue({ items: [{ ...outbox.items[0], state: 'queued' }] });
    button('settings.dx.refresh').click();
    await settle();
    expect(document.querySelector('.channel-outbox-item .channel-outbox-code')).toBeNull();
  });

  it('audits what was recorded for other people with whose request and the decision, page by page', async () => {
    const api = fakeApi();
    const approved = auditItem('ob3', {
      text: '[Tofu 代发] 开的，下午三点见。', createdAtMs: Date.UTC(2026, 10, 6, 8), authority: 'third_party',
      decision: { kind: 'approved', by: 'owner_message', atMs: Date.UTC(2026, 10, 6, 9), turnId: 'turn-9' },
      attachments: [{ index: 0, name: 'agenda.pdf', type: 'application/pdf', size: 10, sha256: 'a'.repeat(64),
                      kind: 'file', renditionOf: null, state: 'sent', error: null }],
    });
    const discarded = auditItem('ob2', { state: 'discarded', authority: 'merged',
                                         decision: { kind: 'discarded', by: 'owner_settings', atMs: 3, turnId: '' } });
    const older = auditItem('ob1', { state: 'draft', decision: null });
    api.audit.mockImplementation(async (_channel, before) => (before === 'cursor-2'
      ? { items: [older], nextBefore: null } : { items: [approved, discarded], nextBefore: 'cursor-2' }));
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    expect(api.audit).toHaveBeenCalledWith('dx', '', expect.anything());
    const items = () => [...auditCard().querySelectorAll<HTMLElement>('.channel-audit-item')];
    expect(items()).toHaveLength(2);
    const first = items()[0].textContent ?? '';
    expect(first).toContain('[Tofu 代发] 开的，下午三点见。');
    expect(first).toContain('settings.dx.outboxState.sent');
    expect(first).toContain('settings.channels.audit.attachments:{"names":"agenda.pdf"}');
    expect(first).toContain('settings.channels.audit.authority.third_party');
    expect(first).toContain('"decision":"settings.channels.audit.kind.approved"');
    expect(first).toContain('"by":"settings.channels.audit.by.owner_message"');
    expect(items()[1].textContent).toContain('settings.channels.audit.authority.merged');
    // Read-only: drafts are decided in the outbox card, never here.
    expect(auditCard().querySelector('.channel-outbox-code, .channel-outbox-actions')).toBeNull();
    const more = button('settings.channels.audit.more');
    more.click();
    await settle();
    expect(api.audit).toHaveBeenLastCalledWith('dx', 'cursor-2', expect.anything());
    expect(items()).toHaveLength(3);
    expect(items()[2].textContent).not.toContain('settings.channels.audit.kind');  // an undecided draft
    expect(more.hidden).toBe(true);
    // A re-read shows the newest page again.
    button('settings.dx.refresh').click();
    await settle();
    expect(items()).toHaveLength(2);
    expect(more.isConnected && !more.hidden).toBe(true);
  });

  it('says when nothing was recorded, and reports a failed audit read in its own card', async () => {
    const api = fakeApi();
    runtime.Api = { channels: api };
    populateDxTab();
    await settle();
    expect(auditCard().textContent).toContain('settings.channels.audit.empty');
    api.audit.mockRejectedValueOnce(new Error('store down'));
    destroyDxTab();
    populateDxTab();
    await settle();
    expect(auditCard().querySelector('.ui-alert')!.textContent)
      .toContain('settings.channels.audit.loadFailed:{"error":"store down"}');
    expect(card(0).querySelector('.channel-status')!.textContent).toBe('settings.dx.state.connected');
    expect(visibleTitles()).toEqual(['Tofu Router', 'Ops', 'Ann', 'Team']);
  });
});
