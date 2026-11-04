// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  ChannelAccountView, ChannelThreadPage, ChannelThreadUpdate, ChannelThreadView, OutboxPage,
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
    commandChat: 'dx:gid:900', skillInstalled: true, routerConversationId: 'router-conv',
    status: CONNECTED, ...overrides,
  };
}

function thread(key: string, title: string, overrides: Partial<ChannelThreadView> = {}): ChannelThreadView {
  const kind = key.split(':')[1] as ChannelThreadView['kind'];
  const mode = kind === 'uid' ? 'mentions' : 'ignore';
  return { key, kind, title, mode, modeOverride: null, replyPolicy: 'draft', replyPolicyOverride: null,
           isCommandChat: false, subscribers: [], lastEventAtMs: null, watermark: null, ...overrides };
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
            createdAtMs: 1, updatedAtMs: 1, externalId: null, error: null }],
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
        next.replyPolicy = update.replyPolicy ?? 'draft';
      }
      byKey.set(key, next);
      return next;
    }),
    outbox: vi.fn(async (_channel: string, _signal?: AbortSignal) => outbox),
    approveOutbox: vi.fn(async () => outbox.items[0]),
    discardOutbox: vi.fn(async () => outbox.items[0]),
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
    expect(row('Ann').querySelector('select')!.value).toBe('draft');
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
    reply.value = 'draft';
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
