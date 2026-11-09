/**
 * Bot platform Settings panels — 企业微信, QQ and 微信 (docs/CHANNELS.md C12):
 * status and enable/disable, the app credentials (企业微信 BotID and Secret,
 * QQ AppID and AppSecret, pasted into the credential vault or chosen from it),
 * binding the owner (a one-time pairing code sent to the bot in a private
 * chat; for 微信 the ClawBot QR login), the setup steps, the chats with their
 * modes and reply policies, and the replies waiting in that channel's outbox.
 * Entry points: populateBotTab / destroyBotTab, registered as the independent
 * Settings panels `wecom`, `qq` and `wechat` (`_populate<Name>Tab` /
 * `_destroy<Name>Tab`). Dependencies: the `Api.channels` facade (generated wire
 * DTOs), the shared channel view (channel-settings-view.ts), tofu-ui factories,
 * i18n. Generic channel copy (enable, modes, outbox states) is shared with the
 * GitHub panel's `settings.github.*` keys.
 * Server state is the authority: every accepted command re-reads it. A pasted
 * secret lives only in its input until submitted and is never rendered back.
 */
import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import { createAlert, createButton, createSpinner } from '../../ui';
import type { BotChannel, ChannelsEndpoints } from '../../api/endpoint-registry/channels';
import { CHANNEL_DEFAULT_MODE_BY_KIND, CHANNEL_DEFAULT_REPLY_POLICY } from '../../api/channels.generated';
import type {
  BotAccountUpdate, BotAccountView, ChannelThreadView, ConfigurableThreadMode, OutboxItemView, ReplyPolicy,
} from '../../api/channels.generated';
import {
  card, cardSection, element, empty, facts, feedback, hint, outboxCard, refreshWhileConnecting, routerLink,
  rowField, runCommand, selectCell, statusLine, threadRow, threadTable, vaultEntryNames, type CommandView,
} from './channel-settings-view';

interface BotState {
  account: BotAccountView;
  threads: ChannelThreadView[];
  nextCursor: string | null;
  outbox: OutboxItemView[];
  vaultEntries: string[];
}

interface Lifetime {
  scope: LifecycleScope | null;
  generation: number;
  renders: number;
}

export const BOT_CHANNELS: readonly BotChannel[] = ['wecom', 'qq', 'wechat'];
const CONFIGURABLE_MODES: ConfigurableThreadMode[] = ['watch', 'mentions', 'ignore'];
const REPLY_POLICIES: ReplyPolicy[] = ['notify', 'draft', 'auto', 'never'];
const PANEL_NAMES: Record<BotChannel, string> = { wecom: 'Wecom', qq: 'Qq', wechat: 'Wechat' };
const NAME_KEYS = { wecom: 'settings.tabWecom', qq: 'settings.tabQq', wechat: 'settings.tabWechat' } as const;
const lifetimes: Record<BotChannel, Lifetime> = {
  wecom: { scope: null, generation: 0, renders: 0 },
  qq: { scope: null, generation: 0, renders: 0 },
  wechat: { scope: null, generation: 0, renders: 0 },
};

function channelsApi(): ChannelsEndpoints {
  const api = (featureRegistry.Api as { channels?: ChannelsEndpoints } | undefined)?.channels;
  if (!api) throw new Error('Channel API is not ready');
  return api;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function platformName(channel: BotChannel): string {
  return t(NAME_KEYS[channel]);
}

function commandView(channel: BotChannel, current: number): CommandView {
  return {
    isCurrent: () => current === lifetimes[channel].generation,
    reload: () => load(channel, current),
    pendingLabel: t('settings.channels.working'),
    failure: (error) => t('settings.github.actionFailed', { error: errorText(error) }),
  };
}

function update(channel: BotChannel, body: BotAccountUpdate): () => Promise<unknown> {
  return () => channelsApi().updateBotAccount(channel, body);
}

function renderAccount(channel: BotChannel, state: BotState, view: CommandView): HTMLElement {
  const { account } = state;
  const section = card(t('settings.github.status'),
                       statusLine(account, t(`settings.bots.state.${account.status.state}` as const)));
  const credential = account.credential;
  const rows: Array<[string, string | Node]> = [];
  if (account.appId) rows.push([t('settings.bots.bot'), account.appId]);
  rows.push([t('settings.bots.owner'), account.identity
    ? (account.identity.name ? `${account.identity.name} · ${account.identity.uid}` : account.identity.uid)
    : t('settings.bots.ownerNone')]);
  if (channel !== 'wechat') {
    rows.push([t('settings.bots.credentials'), credential
      ? t('settings.github.tokenValue', { hint: credential.hint || '—', name: credential.name })
      : t('settings.bots.credentialNone')]);
  }
  rows.push([t('settings.github.router'), routerLink(account.routerConversationId, t('settings.channels.routerOpen'),
                                                     t('settings.github.routerMissing'))]);
  section.appendChild(facts(rows));
  const actions = element('div', 'channel-actions');
  if (channel !== 'wechat' || credential) {
    const toggle = createButton({
      label: account.enabled ? t('settings.github.disable') : t('settings.github.enable'),
      variant: account.enabled ? 'secondary' : 'primary',
      disabled: !account.enabled && (!credential || (channel !== 'wechat' && !account.appId)),
      onClick: () => { void runCommand(view, toggle, update(channel, { enabled: !account.enabled })); },
    });
    actions.appendChild(toggle);
  }
  actions.appendChild(createButton({
    label: t('settings.github.refresh'), variant: 'ghost', onClick: () => { void view.reload(); },
  }));
  const footer = cardSection(actions);
  if (channel !== 'wechat' && !credential) footer.appendChild(hint(t('settings.bots.needsCredential')));
  section.appendChild(footer);
  return section;
}

function renderCredentials(channel: 'wecom' | 'qq', state: BotState, view: CommandView): HTMLElement {
  const { account } = state;
  const section = card(t('settings.bots.credentials'));
  const enable = account.enabled ? {} : { enabled: true };
  const appField = rowField({ label: t(`settings.bots.${channel}.appId` as const), value: account.appId ?? '' });
  const appInput = appField.control as HTMLInputElement;
  appInput.disabled = account.enabled;
  appInput.spellcheck = false;
  const secretField = rowField({ label: t(`settings.bots.${channel}.secret` as const) });
  const secret = secretField.control as HTMLInputElement;
  secret.type = 'password';
  secret.autocomplete = 'off';
  secret.spellcheck = false;
  const row = element('div', 'channel-actions');
  const save = createButton({
    label: account.enabled ? t('settings.bots.save') : t('settings.bots.saveEnable'),
    variant: 'primary',
    onClick: () => {
      const value = secret.value.trim();
      secret.value = '';
      const appId = appInput.value.trim();
      if (!appId) {
        appInput.focus();
        return;
      }
      if (!value && !account.credential) {
        secret.focus();
        return;
      }
      const body: BotAccountUpdate = { ...enable };
      if (!account.enabled && appId !== account.appId) body.appId = appId;
      if (value) body.secret = value;
      void runCommand(view, save, update(channel, body));
    },
  });
  secret.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !save.disabled) save.click();
  });
  row.append(appField.el, secretField.el, save);
  if (account.credential) {
    const remove = createButton({
      label: t('settings.bots.credentialRemove'), variant: 'danger',
      onClick: () => { void runCommand(view, remove, update(channel, { credentialName: null })); },
    });
    row.appendChild(remove);
  }
  const group = cardSection(row);
  if (state.vaultEntries.length) {
    const entryRow = element('div', 'channel-actions');
    const entry = rowField({
      label: t('settings.github.vaultEntry'), value: account.credential?.managed ? '' : account.credential?.name ?? '',
      choices: state.vaultEntries.map((name) => ({ value: name, label: name })),
    });
    const use = createButton({
      label: account.enabled ? t('settings.github.vaultUse') : t('settings.github.vaultUseEnable'),
      disabled: !account.appId,
      onClick: () => {
        const credentialName = (entry.control as HTMLSelectElement).value;
        void runCommand(view, use, update(channel, { credentialName, ...enable }));
      },
    });
    entryRow.append(entry.el, use);
    group.appendChild(entryRow);
  }
  section.appendChild(group);
  return section;
}

function renderPairing(channel: 'wecom' | 'qq', state: BotState, view: CommandView): HTMLElement {
  const { account } = state;
  const section = card(t('settings.bots.pairing'));
  const actions = element('div', 'channel-actions');
  if (account.pairingCode) {
    section.appendChild(hint(t('settings.bots.pairingCode', { platform: platformName(channel) })));
    section.appendChild(element('div', 'channel-pairing-code', account.pairingCode));
    section.appendChild(hint(t('settings.bots.pairingHint')));
    const cancel = createButton({
      label: t('settings.bots.pairCancel'), variant: 'ghost',
      onClick: () => { void runCommand(view, cancel, update(channel, { pair: false })); },
    });
    actions.appendChild(cancel);
  } else {
    if (account.identity) {
      section.appendChild(hint(t('settings.bots.paired', { owner: account.identity.name || account.identity.uid })));
    }
    const pair = createButton({
      label: t('settings.bots.pairNew'), variant: account.identity ? 'secondary' : 'primary',
      disabled: !account.credential,
      onClick: () => { void runCommand(view, pair, update(channel, { pair: true })); },
    });
    actions.appendChild(pair);
  }
  section.appendChild(cardSection(actions));
  return section;
}

function renderLogin(state: BotState, view: CommandView): HTMLElement {
  const { account } = state;
  const section = card(t('settings.bots.wechat.login'));
  const login = account.login;
  const actions = element('div', 'channel-actions');
  const pending = login?.state === 'waiting' || login?.state === 'scanned';
  const loginFailure = (error: unknown): string => {
    if (error && typeof error === 'object' && 'code' in error && error.code === 'wechat_unavailable') {
      return t('settings.bots.wechat.loginUnavailable');
    }
    return view.failure(error);
  };
  if (pending) {
    const status = hint(login.state === 'scanned' ? t('settings.bots.wechat.loginScanned')
      : t('settings.bots.wechat.loginWaiting'));
    status.setAttribute('role', 'status');
    section.append(status, hint(t('settings.bots.wechat.loginExpiryHint')));
    if (login.qrDataUri) {
      const code = element('img', 'channel-login-qr');
      code.src = login.qrDataUri;
      code.alt = t('settings.bots.wechat.loginWaiting');
      section.appendChild(code);
    }
  } else {
    if (login?.state === 'expired') {
      const status = createAlert({ variant: 'warning', title: t('settings.bots.wechat.loginExpired') }).el;
      status.setAttribute('role', 'status');
      section.appendChild(status);
    } else if (login?.state === 'failed') section.appendChild(hint(t('settings.bots.wechat.loginFailed', { detail: login.detail })));
    else if (account.credential) section.appendChild(hint(t('settings.bots.wechat.loggedIn')));
  }
  const start = createButton({
    label: login ? t('settings.bots.wechat.loginRefresh')
      : account.credential ? t('settings.bots.wechat.loginAgain') : t('settings.bots.wechat.loginStart'),
    variant: pending || account.credential ? 'secondary' : 'primary',
    onClick: () => { void runCommand(view, start, update('wechat', { login: true }), loginFailure); },
  });
  actions.appendChild(start);
  if (pending) {
    const cancel = createButton({
      label: t('settings.bots.wechat.loginCancel'), variant: 'ghost',
      onClick: () => { void runCommand(view, cancel, update('wechat', { login: false })); },
    });
    actions.appendChild(cancel);
  }
  section.appendChild(cardSection(actions));
  return section;
}

function renderSetup(channel: BotChannel): HTMLElement {
  const section = card(t('settings.bots.setup'));
  const steps = element('ol', 'channel-steps');
  for (const step of ['step1', 'step2', 'step3'] as const) {
    steps.appendChild(element('li', '', t(`settings.bots.${channel}.${step}` as const)));
  }
  section.appendChild(steps);
  return section;
}

function renderThreads(channel: BotChannel, state: BotState, view: CommandView, current: number): HTMLElement {
  const section = card(t('settings.bots.threads'));
  section.appendChild(hint(t('settings.bots.threadsHint')));
  if (!state.threads.length) {
    section.appendChild(empty(t('settings.bots.threadsEmpty')));
    return section;
  }
  const modeLabel = (mode: ConfigurableThreadMode) => t(`settings.github.mode.${mode}` as const);
  const replyLabel = (policy: ReplyPolicy) => t(`settings.github.reply.${policy}` as const);
  const rows = state.threads.map((thread) => {
    const fallback = thread.kind === 'gid' ? CHANNEL_DEFAULT_MODE_BY_KIND.gid : CHANNEL_DEFAULT_MODE_BY_KIND.uid;
    const mode = thread.isCommandChat
      ? element('div', 'channel-cell channel-cell-static', t('settings.bots.commandChat'))
      : selectCell(t('settings.github.column.mode'), thread.modeOverride ?? '',
        [{ value: '', label: t('settings.github.default', { value: modeLabel(fallback as ConfigurableThreadMode) }) },
         ...CONFIGURABLE_MODES.map((value) => ({ value, label: modeLabel(value) }))],
        (select) => {
          const value = select.value as ConfigurableThreadMode | '';
          void runCommand(view, select, () => channelsApi().updateBotThread(channel, thread.key, { mode: value || null }));
        });
    const reply = selectCell(t('settings.github.column.reply'), thread.replyPolicyOverride ?? '',
      [{ value: '', label: t('settings.github.default', { value: replyLabel(CHANNEL_DEFAULT_REPLY_POLICY) }) },
       ...REPLY_POLICIES.map((value) => ({ value, label: replyLabel(value) }))],
      (select) => {
        const value = select.value as ReplyPolicy | '';
        void runCommand(view, select,
                        () => channelsApi().updateBotThread(channel, thread.key, { replyPolicy: value || null }));
      });
    return threadRow(thread.title || thread.key, thread.key, mode, reply);
  });
  section.appendChild(threadTable(
    { thread: t('settings.bots.column.thread'), mode: t('settings.github.column.mode'),
      reply: t('settings.github.column.reply') }, rows));
  if (state.nextCursor) {
    const cursor = state.nextCursor;
    const more = createButton({
      label: t('settings.github.loadMore'), variant: 'ghost', size: 'sm',
      onClick: () => {
        more.disabled = true;
        void channelsApi().botThreads(channel, cursor, lifetimes[channel].scope?.signal).then((page) => {
          if (!view.isCurrent()) return;
          state.threads.push(...page.items);
          state.nextCursor = page.nextCursor;
          render(channel, state, current);
        }, (error) => {
          if (!view.isCurrent()) return;
          more.disabled = false;
          feedback(more, t('settings.bots.loadFailed', { error: errorText(error) }));
        });
      },
    });
    section.appendChild(more);
  }
  return section;
}

function render(channel: BotChannel, state: BotState, current: number): void {
  const life = lifetimes[channel];
  const root = document.getElementById(`${channel}ChannelRoot`);
  if (!root || current !== life.generation) return;
  const view = commandView(channel, current);
  const rendered = ++life.renders;
  const cards: HTMLElement[] = [renderAccount(channel, state, view)];
  if (channel === 'wechat') cards.push(renderLogin(state, view));
  else cards.push(renderCredentials(channel, state, view), renderPairing(channel, state, view));
  cards.push(renderSetup(channel), renderThreads(channel, state, view, current), outboxCard(state.outbox, {
    title: t('settings.github.outbox'), empty: t('settings.bots.outboxEmpty'),
    approve: t('settings.github.approve'), discard: t('settings.github.discard'),
    state: (value) => t(`settings.github.outboxState.${value}` as const),
  }, view, { approve: (id) => channelsApi().approveOutbox(id), discard: (id) => channelsApi().discardOutbox(id) }));
  root.replaceChildren(...cards);
  if (life.scope) {
    refreshWhileConnecting(life.scope, state.account, (signal) => channelsApi().botAccount(channel, signal),
                           () => current === life.generation && rendered === life.renders, view.reload,
                           (account) => JSON.stringify([account.enabled, account.status, account.login]));
  }
}

async function load(channel: BotChannel, current: number): Promise<void> {
  const life = lifetimes[channel];
  const signal = life.scope?.signal;
  try {
    const api = channelsApi();
    const [account, page, outbox, vaultEntries] = await Promise.all([
      api.botAccount(channel, signal), api.botThreads(channel, '', signal), api.outbox(channel, signal),
      channel === 'wechat' ? Promise.resolve([]) : vaultEntryNames(),
    ]);
    if (current !== life.generation) return;
    render(channel, { account, threads: page.items, nextCursor: page.nextCursor, outbox: outbox.items, vaultEntries },
           current);
  } catch (error) {
    const root = document.getElementById(`${channel}ChannelRoot`);
    if (!root || current !== life.generation || signal?.aborted) return;
    root.replaceChildren(createAlert({
      variant: 'error', title: t('settings.bots.loadFailed', { error: errorText(error) }) }).el);
  }
}

export function populateBotTab(channel: BotChannel): void {
  const root = document.getElementById(`${channel}ChannelRoot`);
  if (!root) return;
  const life = lifetimes[channel];
  life.scope?.destroy();
  life.scope = createLifecycleScope();
  const current = ++life.generation;
  root.replaceChildren(createSpinner());
  void load(channel, current);
}

export function destroyBotTab(channel: BotChannel): void {
  const life = lifetimes[channel];
  life.generation += 1;
  life.scope?.destroy();
  life.scope = null;
}

const bridge = featureRegistry as unknown as Record<string, unknown>;
for (const channel of BOT_CHANNELS) {
  bridge[`_populate${PANEL_NAMES[channel]}Tab`] = () => populateBotTab(channel);
  bridge[`_destroy${PANEL_NAMES[channel]}Tab`] = () => destroyBotTab(channel);
}
