/**
 * 大象 (Daxiang) channel Settings tab: account status and enable/disable,
 * command chat selection or creation, thread modes/reply policies and the
 * 大象 outbox drafts awaiting the owner's decision (docs/CHANNELS.md).
 * Entry points: populateDxTab / destroyDxTab, registered as the independent
 * Settings panel `dx` (`_populateDxTab` / `_destroyDxTab`).
 * Dependencies: the `Api.channels` endpoint facade (generated wire DTOs), the
 * shared channel view (channel-settings-view.ts), tofu-ui factories, i18n.
 * Server state is the authority: every accepted command re-reads it; nothing
 * is cached across openings.
 */
import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import { createAlert, createButton, createDialog, createSpinner } from '../../ui';
import type { ChannelsEndpoints } from '../../api/endpoint-registry/channels';
import { CHANNEL_DEFAULT_MODE_BY_KIND, CHANNEL_DEFAULT_REPLY_POLICY } from '../../api/channels.generated';
import type {
  ChannelAccountView, ChannelThreadView, ConfigurableThreadMode, OutboxItemView, ReplyPolicy, ThreadMode,
} from '../../api/channels.generated';
import {
  card, cardSection, chip, element, empty, facts, feedback, hint, outboxCard, refreshWhileConnecting, routerLink,
  rowField, runCommand, selectCell, staticCell, statusLine, threadRow, threadTable, type CommandView,
} from './channel-settings-view';

interface DxState {
  account: ChannelAccountView;
  threads: ChannelThreadView[];
  nextCursor: string | null;
  outbox: OutboxItemView[];
}

type DxBridge = {
  _populateDxTab?: () => void;
  _destroyDxTab?: () => void;
};

const CONFIGURABLE_MODES: ConfigurableThreadMode[] = ['watch', 'mentions', 'ignore'];
const REPLY_POLICIES: ReplyPolicy[] = ['auto', 'draft', 'never'];

let scope: LifecycleScope | null = null;
let generation = 0;
let renders = 0;

function channelsApi(): ChannelsEndpoints {
  const api = (featureRegistry.Api as { channels?: ChannelsEndpoints } | undefined)?.channels;
  if (!api) throw new Error('Channel API is not ready');
  return api;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function modeLabel(mode: ThreadMode): string {
  return t(`settings.dx.mode.${mode}` as const);
}

function replyLabel(policy: ReplyPolicy): string {
  return t(`settings.dx.reply.${policy}` as const);
}

function defaultMode(thread: ChannelThreadView): ThreadMode {
  return CHANNEL_DEFAULT_MODE_BY_KIND[thread.kind];
}

function threadName(thread: ChannelThreadView): string {
  return `${thread.title || thread.key} (${thread.key})`;
}

function commandView(current: number): CommandView {
  return {
    isCurrent: () => current === generation,
    reload: () => load(current),
    pendingLabel: t('settings.channels.working'),
    failure: (error) => t('settings.dx.actionFailed', { error: errorText(error) }),
  };
}

function renderAccount(state: DxState, view: CommandView): HTMLElement {
  const { account } = state;
  const section = card(t('settings.dx.status'),
                       statusLine(account, t(`settings.dx.state.${account.status.state}` as const)));
  if (!account.skillInstalled) {
    section.appendChild(createAlert({ variant: 'warning', title: t('settings.dx.skillMissing') }).el);
  }
  const identity = account.identity;
  section.appendChild(facts([
    [t('settings.dx.account'), identity ? `${identity.name || identity.mis} (${identity.mis}) · ${identity.uid}`
      : t('settings.dx.noIdentity')],
    [t('settings.dx.router'), routerLink(account.routerConversationId, t('settings.channels.routerOpen'),
                                         t('settings.dx.routerMissing'))],
  ]));
  const actions = element('div', 'channel-actions');
  const toggle = createButton({
    label: account.enabled ? t('settings.dx.disable') : t('settings.dx.enable'),
    variant: account.enabled ? 'secondary' : 'primary',
    disabled: !account.enabled && !account.skillInstalled,
    onClick: () => { void runCommand(view, toggle, () => channelsApi().updateDxAccount({ enabled: !account.enabled })); },
  });
  actions.append(toggle, createButton({
    label: t('settings.dx.refresh'), variant: 'ghost',
    onClick: () => { void view.reload(); },
  }));
  const footer = cardSection(actions);
  if (!account.enabled && account.skillInstalled) footer.appendChild(hint(t('settings.dx.enableHint')));
  section.appendChild(footer);
  return section;
}

function confirmCreate(): Promise<string | null> {
  return createDialog({
    title: t('settings.dx.commandChatCreate'), body: t('settings.dx.commandChatCreateConfirm'),
    actions: [{ id: 'cancel', label: t('common.cancel') },
              { id: 'create', label: t('settings.dx.commandChatCreate'), variant: 'primary' }],
  }).open();
}

function renderCommandChat(state: DxState, view: CommandView): HTMLElement {
  const { account, threads } = state;
  const chosen = account.commandChat;
  const selected = threads.find((thread) => thread.key === chosen);
  const current = chosen
    ? chip(selected?.title || chosen, 'ok')
    : chip(t('settings.dx.commandChatNone'), 'idle');
  if (chosen) current.title = selected ? threadName(selected) : chosen;
  const section = card(t('settings.dx.commandChat'), current);
  section.appendChild(hint(t('settings.dx.commandChatHint')));
  if (!account.enabled || !account.identity) {
    section.appendChild(hint(t('settings.dx.commandChatNeedsAccount')));
    return section;
  }
  const choices = threads.filter((thread) => thread.kind !== 'pid')
    .map((thread) => ({ value: thread.key, label: threadName(thread) }));
  if (choices.length) {
    const field = rowField({ label: t('settings.dx.column.thread'), choices, value: chosen ?? choices[0].value });
    const select = field.control as HTMLSelectElement;
    const use = createButton({
      label: t('settings.dx.commandChatUse'),
      disabled: select.value === chosen,
      onClick: () => {
        const value = select.value;
        void runCommand(view, use, () => channelsApi().updateDxAccount({ commandChat: value }));
      },
    });
    select.addEventListener('change', () => { use.disabled = select.value === chosen; });
    const row = element('div', 'channel-actions');
    row.append(field.el, use);
    section.appendChild(cardSection(row));
  }
  if (!chosen) {
    const create = createButton({
      label: t('settings.dx.commandChatCreate'), variant: 'primary',
      onClick: () => {
        void confirmCreate().then((choice) => {
          if (choice === 'create' && view.isCurrent()) {
            void runCommand(view, create, () => channelsApi().createDxCommandChat());
          }
        });
      },
    });
    const row = element('div', 'channel-actions');
    row.appendChild(create);
    section.appendChild(cardSection(row));
  }
  return section;
}

function renderThread(thread: ChannelThreadView, view: CommandView): HTMLElement {
  const modeColumn = t('settings.dx.column.mode');
  const mode = thread.isCommandChat
    ? staticCell(modeColumn, modeLabel('command'))
    : selectCell(modeColumn, thread.modeOverride ?? '',
                 [{ value: '', label: t('settings.dx.default', { value: modeLabel(defaultMode(thread)) }) },
                  ...CONFIGURABLE_MODES.map((value) => ({ value, label: modeLabel(value) }))],
                 (select) => {
                   const value = select.value as ConfigurableThreadMode | '';
                   void runCommand(view, select, () => channelsApi().updateDxThread(thread.key, { mode: value || null }));
                 });
  const reply = selectCell(t('settings.dx.column.reply'), thread.replyPolicyOverride ?? '',
    [{ value: '', label: t('settings.dx.default', { value: replyLabel(CHANNEL_DEFAULT_REPLY_POLICY) }) },
     ...REPLY_POLICIES.map((value) => ({ value, label: replyLabel(value) }))],
    (select) => {
      const value = select.value as ReplyPolicy | '';
      void runCommand(view, select, () => channelsApi().updateDxThread(thread.key, { replyPolicy: value || null }));
    });
  return threadRow(thread.title || thread.key, thread.key, mode, reply);
}

function renderThreads(state: DxState, view: CommandView, current: number): HTMLElement {
  const section = card(t('settings.dx.threads'));
  if (!state.threads.length) {
    section.appendChild(empty(t('settings.dx.threadsEmpty')));
    return section;
  }
  section.appendChild(threadTable(
    { thread: t('settings.dx.column.thread'), mode: t('settings.dx.column.mode'), reply: t('settings.dx.column.reply') },
    state.threads.map((thread) => renderThread(thread, view)),
  ));
  if (state.nextCursor) {
    const cursor = state.nextCursor;
    const more = createButton({
      label: t('settings.dx.loadMore'), variant: 'ghost', size: 'sm',
      onClick: () => {
        more.disabled = true;
        void channelsApi().dxThreads(cursor, scope?.signal).then((page) => {
          if (!view.isCurrent()) return;
          state.threads.push(...page.items);
          state.nextCursor = page.nextCursor;
          render(state, current);
        }, (error) => {
          if (!view.isCurrent()) return;
          more.disabled = false;
          feedback(more, t('settings.dx.loadFailed', { error: errorText(error) }));
        });
      },
    });
    section.appendChild(more);
  }
  return section;
}

function render(state: DxState, current: number): void {
  const root = document.getElementById('dxChannelRoot');
  if (!root || current !== generation) return;
  const view = commandView(current);
  const rendered = ++renders;
  root.replaceChildren(
    renderAccount(state, view),
    renderCommandChat(state, view),
    renderThreads(state, view, current),
    outboxCard(state.outbox, {
      title: t('settings.dx.outbox'), empty: t('settings.dx.outboxEmpty'),
      approve: t('settings.dx.approve'), discard: t('settings.dx.discard'),
      state: (value) => t(`settings.dx.outboxState.${value}` as const),
    }, view, { approve: (id) => channelsApi().approveOutbox(id), discard: (id) => channelsApi().discardOutbox(id) }),
  );
  if (scope) {
    refreshWhileConnecting(scope, state.account, (signal) => channelsApi().dxAccount(signal),
                           () => current === generation && rendered === renders, view.reload);
  }
}

async function load(current: number): Promise<void> {
  const signal = scope?.signal;
  try {
    const api = channelsApi();
    const [account, page, outbox] = await Promise.all([
      api.dxAccount(signal), api.dxThreads('', signal), api.outbox('dx', signal),
    ]);
    if (current !== generation) return;
    render({ account, threads: page.items, nextCursor: page.nextCursor, outbox: outbox.items }, current);
  } catch (error) {
    const root = document.getElementById('dxChannelRoot');
    if (!root || current !== generation || signal?.aborted) return;
    root.replaceChildren(createAlert({
      variant: 'error', title: t('settings.dx.loadFailed', { error: errorText(error) }) }).el);
  }
}

export function populateDxTab(): void {
  const root = document.getElementById('dxChannelRoot');
  if (!root) return;
  scope?.destroy();
  scope = createLifecycleScope();
  const current = ++generation;
  root.replaceChildren(createSpinner());
  void load(current);
}

export function destroyDxTab(): void {
  generation += 1;
  scope?.destroy();
  scope = null;
}

const bridge = featureRegistry as unknown as DxBridge;
bridge._populateDxTab = populateDxTab;
bridge._destroyDxTab = destroyDxTab;
