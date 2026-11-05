/**
 * 大象 (Daxiang) channel Settings tab: account status and enable/disable,
 * command chat selection or creation, thread modes/reply policies and the
 * outbox drafts awaiting the owner's decision (docs/CHANNELS.md).
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
import { createAlert, createButton, createDialog, createEmptyState, createField, createSpinner } from '../../ui';
import type { ChannelsEndpoints } from '../../api/endpoint-registry/channels';
import { CHANNEL_DEFAULT_MODE_BY_KIND, CHANNEL_DEFAULT_REPLY_POLICY } from '../../api/channels.generated';
import type {
  ChannelAccountView, ChannelThreadView, ConfigurableThreadMode, OutboxItemView, ReplyPolicy, ThreadMode,
} from '../../api/channels.generated';
import {
  card, element, facts, feedback, hint, refreshWhileConnecting, routerLink, rowField, runCommand, statusLine,
  type CommandView,
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
const DISCARDABLE = new Set(['draft', 'queued', 'failed']);

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
  const section = card(t('settings.dx.status'));
  section.appendChild(statusLine(account, t(`settings.dx.state.${account.status.state}` as const)));
  const identity = account.identity;
  section.appendChild(facts([
    [t('settings.dx.account'), identity ? `${identity.name || identity.mis} (${identity.mis}) · ${identity.uid}`
      : t('settings.dx.noIdentity')],
    [t('settings.dx.router'), routerLink(account.routerConversationId, t('settings.channels.routerOpen'),
                                         t('settings.dx.routerMissing'))],
  ]));
  if (!account.skillInstalled) {
    section.appendChild(createAlert({ variant: 'warning', title: t('settings.dx.skillMissing') }).el);
  }
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
  section.appendChild(actions);
  if (!account.enabled && account.skillInstalled) section.appendChild(hint(t('settings.dx.enableHint')));
  return section;
}

function renderCommandChat(state: DxState, view: CommandView): HTMLElement {
  const { account, threads } = state;
  const section = card(t('settings.dx.commandChat'));
  const selected = threads.find((thread) => thread.key === account.commandChat);
  section.appendChild(facts([[t('settings.dx.commandChat'), account.commandChat
    ? `${selected?.title || account.commandChat} (${account.commandChat})` : t('settings.dx.commandChatNone')]]));
  section.appendChild(hint(t('settings.dx.commandChatHint')));
  if (!account.enabled || !account.identity) {
    section.appendChild(hint(t('settings.dx.commandChatNeedsAccount')));
    return section;
  }
  const choices = threads.filter((thread) => thread.kind !== 'pid')
    .map((thread) => ({ value: thread.key, label: `${thread.title || thread.key} (${thread.key})` }));
  const actions = element('div', 'channel-actions');
  if (choices.length) {
    const field = rowField({ label: t('settings.dx.column.thread'), choices,
      value: account.commandChat ?? choices[0].value });
    const use = createButton({
      label: t('settings.dx.commandChatUse'),
      onClick: () => {
        const value = (field.control as HTMLSelectElement).value;
        void runCommand(view, use, () => channelsApi().updateDxAccount({ commandChat: value }));
      },
    });
    actions.append(field.el, use);
  }
  if (!account.commandChat) {
    const create = createButton({
      label: t('settings.dx.commandChatCreate'), variant: 'primary',
      onClick: () => {
        const dialog = createDialog({
          title: t('settings.dx.commandChatCreate'), body: t('settings.dx.commandChatCreateConfirm'),
          actions: [{ id: 'cancel', label: t('common.cancel') },
                    { id: 'create', label: t('settings.dx.commandChatCreate'), variant: 'primary' }],
        });
        void dialog.open().then((choice) => {
          if (choice === 'create' && view.isCurrent()) {
            void runCommand(view, create, () => channelsApi().createDxCommandChat());
          }
        });
      },
    });
    actions.appendChild(create);
  }
  if (actions.childElementCount) section.appendChild(actions);
  return section;
}

function renderThread(thread: ChannelThreadView, view: CommandView): HTMLElement {
  const row = element('div', 'channel-row');
  const title = element('div', 'channel-row-title');
  title.append(element('div', '', thread.title || thread.key), element('div', 'channel-key', thread.key));
  row.appendChild(title);
  if (thread.isCommandChat) {
    row.appendChild(facts([[t('settings.dx.column.mode'), modeLabel('command')]]));
  } else {
    const mode = createField({
      label: t('settings.dx.column.mode'), value: thread.modeOverride ?? '',
      choices: [{ value: '', label: t('settings.dx.default', { value: modeLabel(defaultMode(thread)) }) },
                ...CONFIGURABLE_MODES.map((value) => ({ value, label: modeLabel(value) }))],
    });
    const select = mode.control as HTMLSelectElement;
    select.addEventListener('change', () => {
      const value = select.value as ConfigurableThreadMode | '';
      void runCommand(view, select, () => channelsApi().updateDxThread(thread.key, { mode: value || null }));
    });
    row.appendChild(mode.el);
  }
  const reply = createField({
    label: t('settings.dx.column.reply'), value: thread.replyPolicyOverride ?? '',
    choices: [{ value: '', label: t('settings.dx.default', { value: replyLabel(CHANNEL_DEFAULT_REPLY_POLICY) }) },
              ...REPLY_POLICIES.map((value) => ({ value, label: replyLabel(value) }))],
  });
  const select = reply.control as HTMLSelectElement;
  select.addEventListener('change', () => {
    const value = select.value as ReplyPolicy | '';
    void runCommand(view, select, () => channelsApi().updateDxThread(thread.key, { replyPolicy: value || null }));
  });
  row.appendChild(reply.el);
  return row;
}

function renderThreads(state: DxState, view: CommandView, current: number): HTMLElement {
  const section = card(t('settings.dx.threads'));
  if (!state.threads.length) {
    section.appendChild(createEmptyState({ title: t('settings.dx.threadsEmpty') }));
    return section;
  }
  const list = element('div', 'channel-threads');
  for (const thread of state.threads) list.appendChild(renderThread(thread, view));
  section.appendChild(list);
  if (state.nextCursor) {
    const cursor = state.nextCursor;
    const more = createButton({
      label: t('settings.dx.loadMore'), variant: 'ghost',
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

function renderOutbox(state: DxState, view: CommandView): HTMLElement {
  const section = card(t('settings.dx.outbox'));
  if (!state.outbox.length) {
    section.appendChild(createEmptyState({ title: t('settings.dx.outboxEmpty') }));
    return section;
  }
  for (const item of state.outbox) {
    const row = element('div', 'channel-outbox-item');
    row.appendChild(facts([
      [t('settings.dx.status'), t(`settings.dx.outboxState.${item.state}` as const)
        + (item.error ? ` — ${item.error}` : '')],
      [t('settings.dx.column.thread'), `${item.threadTitle || item.thread} (${item.thread})`],
    ]));
    row.appendChild(element('div', 'channel-outbox-text', item.text));
    const actions = element('div', 'channel-actions');
    if (item.state === 'draft') {
      const approve = createButton({
        label: t('settings.dx.approve'), variant: 'primary', size: 'sm',
        onClick: () => { void runCommand(view, approve, () => channelsApi().approveOutbox(item.id)); },
      });
      actions.appendChild(approve);
    }
    if (DISCARDABLE.has(item.state)) {
      const discard = createButton({
        label: t('settings.dx.discard'), variant: 'danger', size: 'sm',
        onClick: () => { void runCommand(view, discard, () => channelsApi().discardOutbox(item.id)); },
      });
      actions.appendChild(discard);
    }
    row.appendChild(actions);
    section.appendChild(row);
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
    renderOutbox(state, view),
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
      api.dxAccount(signal), api.dxThreads('', signal), api.outbox(signal),
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
