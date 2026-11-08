/**
 * 大象 (Daxiang) channel Settings tab: the account (status, login MIS,
 * enable/disable), the command chat, the notification robot, the assistant
 * card (how Tofu replies for the owner, and the owner's account note), the
 * thread list (mode, reply policy and note per thread), the 大象 outbox drafts
 * awaiting the owner's decision with their approval codes, and the audit of
 * what Tofu recorded for other people (docs/CHANNELS.md, C11).
 * Entry points: populateDxTab / destroyDxTab, registered as the independent
 * Settings panel `dx` (`_populateDxTab` / `_destroyDxTab`).
 * Dependencies: the `Api.channels` endpoint facade (generated wire DTOs), the
 * account cards (dx-account.ts), the robot card (dx-robot.ts), the assistant
 * card (dx-assistant.ts), the thread list (dx-thread-list.ts), the shared
 * channel view (channel-settings-view.ts, also the audit list), tofu-ui
 * factories, i18n.
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
  auditCopy, createAuditList, element, feedback, outboxCard, refreshWhileConnecting, vaultEntryNames,
  type AuditList, type CommandView,
} from './channel-settings-view';
import {
  createDxCardUi, renderAccountCard, renderCommandChatCard, replaceKeepingFocus, type DxCardPorts, type DxCardUi,
} from './dx-account';
import { renderAssistantCard } from './dx-assistant';
import { renderRobotCard } from './dx-robot';
import { createDxThreadList, type DxThreadList } from './dx-thread-list';

interface DxOpening {
  generation: number;
  scope: LifecycleScope;
  view: CommandView;
  ui: DxCardUi;
  threadList: DxThreadList;
  audit: AuditList;
  account: ChannelAccountView | null;
  threads: ChannelThreadView[] | null;
  vaultEntries: string[] | null;
  /** What the robot card last rendered: other re-renders leave its typed fields alone. */
  robotKey: string | null;
  /** The account note the assistant card last rendered, for the same reason. */
  assistantKey: string | null;
  slots: {
    account: HTMLElement; commandChat: HTMLElement; robot: HTMLElement; assistant: HTMLElement;
    threads: HTMLElement; outbox: HTMLElement; audit: HTMLElement;
  };
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

/** The robot card, only when its part of the account or the vault changed, or after its own command. */
function renderRobot(state: DxOpening): void {
  const { account } = state;
  if (!isCurrent(state) || !account) return;
  const key = JSON.stringify([account.robot ?? null, account.commandChat, state.vaultEntries ?? []]);
  if (key === state.robotKey) return;
  state.robotKey = key;
  const view: CommandView = {
    ...state.view,
    reload: () => {
      state.robotKey = null;
      return state.view.reload();
    },
  };
  replaceKeepingFocus(state.slots.robot,
                      renderRobotCard(account, state.vaultEntries ?? [], { api: channelsApi, view }));
}

/** The assistant card, only when the saved account note changed, or after its own save. */
function renderAssistant(state: DxOpening): void {
  const { account } = state;
  if (!isCurrent(state) || !account || account.assistantNote === state.assistantKey) return;
  state.assistantKey = account.assistantNote;
  const view: CommandView = {
    ...state.view,
    reload: () => {
      state.assistantKey = null;
      return state.view.reload();
    },
  };
  state.slots.assistant.replaceChildren(renderAssistantCard(account, { api: channelsApi, view }));
}

/** The account, command chat, robot and assistant cards; a connecting account is re-read until it moves. */
function renderCards(state: DxOpening): void {
  const { account } = state;
  if (!isCurrent(state) || !account) return;
  replaceKeepingFocus(state.slots.account, renderAccountCard(account, state.ui, cardPorts(state)));
  renderCommandChat(state);
  renderRobot(state);
  renderAssistant(state);
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
    code: (code) => t('settings.channels.approvalCode', { code }),
    attachments: {
      label: t('settings.dx.attachments'), pending: t('settings.dx.attachmentPending'),
      previewOf: (name) => t('settings.dx.attachmentPreviewOf', { name }),
      imageUrl: (id, index) => api.outboxAttachmentUrl(id, index),
    },
  }, state.view, { approve: (id) => api.approveOutbox(id), discard: (id) => api.discardOutbox(id) }));
}

/**
 * Read the account, every thread, the outbox, the audit's newest page and the
 * vault entry names; each part renders when its read answers. A failed first read replaces its
 * placeholder; a failed re-read keeps the shown card and reports beside its title.
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
    api.audit('dx', '', signal).then((page) => {
      if (isCurrent(state)) state.audit.setPage(page);
    }, (error: unknown) => {
      if (isCurrent(state) && !signal.aborted) {
        state.audit.showError(t('settings.channels.audit.loadFailed', { error: errorText(error) }));
      }
    }),
    vaultEntryNames().then((names) => {
      if (!isCurrent(state)) return;
      state.vaultEntries = names;
      renderRobot(state);
    }),
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
  const audit = createAuditList(auditCopy((value) => t(`settings.dx.outboxState.${value}` as const)), {
    more: (before) => channelsApi().audit('dx', before, scope.signal),
    isCurrent: view.isCurrent,
  });
  const state: DxOpening = {
    generation: current, scope, view, ui: createDxCardUi(), threadList, audit, account: null, threads: null,
    vaultEntries: null, robotKey: null, assistantKey: null,
    slots: { account: slot(), commandChat: slot(), robot: slot(), assistant: slot(), threads: slot(),
             outbox: slot(), audit: slot() },
    accountRenders: 0,
  };
  opening = state;
  state.slots.account.appendChild(createSpinner());
  state.slots.threads.appendChild(threadList.el);
  state.slots.audit.appendChild(audit.el);
  root.replaceChildren(state.slots.account, state.slots.commandChat, state.slots.robot, state.slots.assistant,
                       state.slots.threads, state.slots.outbox, state.slots.audit);
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
