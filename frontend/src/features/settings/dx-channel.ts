/**
 * 大象 (Daxiang) channel Settings tab: the account (status, login MIS,
 * enable/disable), the command chat, the thread list and the 大象 outbox
 * drafts awaiting the owner's decision (docs/CHANNELS.md).
 * Entry points: populateDxTab / destroyDxTab, registered as the independent
 * Settings panel `dx` (`_populateDxTab` / `_destroyDxTab`).
 * Dependencies: the `Api.channels` endpoint facade (generated wire DTOs), the
 * account cards (dx-account.ts), the thread list (dx-thread-list.ts), the
 * shared channel view (channel-settings-view.ts), tofu-ui factories, i18n.
 * Server state is the authority: each card renders as soon as its own read
 * answers, every accepted command re-reads the server in place (open editors,
 * search and selection stay), and nothing is cached across openings.
 */
import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import { createAlert, createSpinner } from '../../ui';
import type { ChannelsEndpoints } from '../../api/endpoint-registry/channels';
import type { ChannelAccountView, ChannelThreadView, OutboxItemView } from '../../api/channels.generated';
import {
  element, feedback, outboxCard, refreshWhileConnecting, type CommandView,
} from './channel-settings-view';
import {
  createDxCardUi, renderAccountCard, renderCommandChatCard, replaceKeepingFocus, type DxCardPorts, type DxCardUi,
} from './dx-account';
import { createDxThreadList, type DxThreadList } from './dx-thread-list';

interface DxOpening {
  generation: number;
  scope: LifecycleScope;
  view: CommandView;
  ui: DxCardUi;
  threadList: DxThreadList;
  account: ChannelAccountView | null;
  threads: ChannelThreadView[] | null;
  slots: { account: HTMLElement; commandChat: HTMLElement; threads: HTMLElement; outbox: HTMLElement };
  accountRenders: number;
}

type DxBridge = {
  _populateDxTab?: () => void;
  _destroyDxTab?: () => void;
};

let opening: DxOpening | null = null;
let generation = 0;

function channelsApi(): ChannelsEndpoints {
  const api = (featureRegistry.Api as { channels?: ChannelsEndpoints } | undefined)?.channels;
  if (!api) throw new Error('Channel API is not ready');
  return api;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function loadFailure(error: unknown): HTMLElement {
  return createAlert({ variant: 'error', title: t('settings.dx.loadFailed', { error: errorText(error) }) }).el;
}

/** Every thread page: search, filter and counts cover all threads, not the first page. */
async function readAllThreads(api: ChannelsEndpoints, signal: AbortSignal): Promise<ChannelThreadView[]> {
  const threads: ChannelThreadView[] = [];
  let cursor = '';
  do {
    const page = await api.dxThreads(cursor, signal);
    threads.push(...page.items);
    cursor = page.nextCursor ?? '';
  } while (cursor);
  return threads;
}

function isCurrent(state: DxOpening): boolean {
  return opening === state && state.generation === generation;
}

function cardPorts(state: DxOpening): DxCardPorts {
  return { api: channelsApi, view: state.view, rerender: () => renderCards(state) };
}

function renderCommandChat(state: DxOpening): void {
  if (!isCurrent(state) || !state.account) return;
  replaceKeepingFocus(state.slots.commandChat,
                      renderCommandChatCard(state.account, state.threads, state.ui, cardPorts(state)));
}

/** The account and command chat cards; a connecting account is re-read until it moves. */
function renderCards(state: DxOpening): void {
  const { account } = state;
  if (!isCurrent(state) || !account) return;
  replaceKeepingFocus(state.slots.account, renderAccountCard(account, state.ui, cardPorts(state)));
  renderCommandChat(state);
  const rendered = ++state.accountRenders;
  refreshWhileConnecting(state.scope, account, (signal) => channelsApi().dxAccount(signal),
                         () => isCurrent(state) && rendered === state.accountRenders, state.view.reload);
}

function renderOutbox(state: DxOpening, items: OutboxItemView[]): void {
  const api = channelsApi();
  state.slots.outbox.replaceChildren(outboxCard(items, {
    title: t('settings.dx.outbox'), empty: t('settings.dx.outboxEmpty'),
    approve: t('settings.dx.approve'), discard: t('settings.dx.discard'),
    state: (value) => t(`settings.dx.outboxState.${value}` as const),
  }, state.view, { approve: (id) => api.approveOutbox(id), discard: (id) => api.discardOutbox(id) }));
}

/**
 * Read the account, every thread and the outbox; each part renders when its
 * read answers. A failed first read replaces its placeholder; a failed
 * re-read keeps the shown card and reports beside its title.
 */
async function load(state: DxOpening): Promise<void> {
  const { signal } = state.scope;
  const failed = (slot: HTMLElement) => (error: unknown) => {
    if (!isCurrent(state) || signal.aborted) return;
    const head = slot.querySelector('.channel-card-head');
    if (head) feedback(head, t('settings.dx.loadFailed', { error: errorText(error) }));
    else slot.replaceChildren(loadFailure(error));
  };
  let api: ChannelsEndpoints;
  try {
    api = channelsApi();
  } catch (error) {
    failed(state.slots.account)(error);
    return;
  }
  await Promise.all([
    api.dxAccount(signal).then((account) => {
      if (!isCurrent(state)) return;
      state.account = account;
      renderCards(state);
    }, failed(state.slots.account)),
    readAllThreads(api, signal).then((threads) => {
      if (!isCurrent(state)) return;
      state.threads = threads;
      state.threadList.setThreads(threads);
      renderCommandChat(state);
    }, (error: unknown) => {
      if (isCurrent(state) && !signal.aborted) {
        state.threadList.showError(t('settings.dx.loadFailed', { error: errorText(error) }));
      }
    }),
    api.outbox('dx', signal).then((page) => {
      if (isCurrent(state)) renderOutbox(state, page.items);
    }, failed(state.slots.outbox)),
  ]);
}

function slot(): HTMLElement {
  return element('div', 'dx-slot');
}

export function populateDxTab(): void {
  const root = document.getElementById('dxChannelRoot');
  if (!root) return;
  opening?.scope.destroy();
  const current = ++generation;
  const scope = createLifecycleScope();
  const view: CommandView = {
    isCurrent: () => opening === state && current === generation,
    reload: () => load(state),
    pendingLabel: t('settings.channels.working'),
    failure: (error) => t('settings.dx.actionFailed', { error: errorText(error) }),
  };
  const threadList = createDxThreadList({
    update: (key, update) => channelsApi().updateDxThread(key, update),
    isCurrent: view.isCurrent,
    failure: view.failure,
    pendingLabel: view.pendingLabel,
  });
  const state: DxOpening = {
    generation: current, scope, view, ui: createDxCardUi(), threadList, account: null, threads: null,
    slots: { account: slot(), commandChat: slot(), threads: slot(), outbox: slot() }, accountRenders: 0,
  };
  opening = state;
  state.slots.account.appendChild(createSpinner());
  state.slots.threads.appendChild(threadList.el);
  root.replaceChildren(state.slots.account, state.slots.commandChat, state.slots.threads, state.slots.outbox);
  void load(state);
}

export function destroyDxTab(): void {
  generation += 1;
  opening?.scope.destroy();
  opening = null;
}

const bridge = featureRegistry as unknown as DxBridge;
bridge._populateDxTab = populateDxTab;
bridge._destroyDxTab = destroyDxTab;
