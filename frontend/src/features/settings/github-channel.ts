/**
 * GitHub channel Settings tab: connection status and enable/disable, the
 * token (pasted and verified into the credential vault, or an existing vault
 * entry chosen by name), the API base for GitHub Enterprise Server, watched
 * issue and pull request threads with their modes and reply policies, and the
 * GitHub replies waiting in the outbox (docs/CHANNELS.md C8).
 * Entry points: populateGitHubTab / destroyGitHubTab, registered as the
 * independent Settings panel `github` (`_populateGitHubTab` / `_destroyGitHubTab`).
 * Dependencies: the `Api.channels` facade (generated wire DTOs), the
 * `Api.credentials` list for vault entry names, the shared channel view
 * (channel-settings-view.ts), tofu-ui factories, i18n.
 * Server state is the authority: every accepted command re-reads it. A pasted
 * token lives only in its input until submitted and is never rendered back.
 */
import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import { createAlert, createButton, createEmptyState, createField, createSpinner } from '../../ui';
import { apiFailure } from '../../api/errors';
import type { ChannelsEndpoints } from '../../api/endpoint-registry/channels';
import { CHANNEL_DEFAULT_MODE_BY_KIND, CHANNEL_DEFAULT_REPLY_POLICY } from '../../api/channels.generated';
import type {
  ChannelThreadView, ConfigurableThreadMode, GitHubAccountView, OutboxItemView, ReplyPolicy,
} from '../../api/channels.generated';
import {
  card, element, facts, feedback, hint, refreshWhileConnecting, routerLink, rowField, runCommand, statusLine,
  type CommandView,
} from './channel-settings-view';

interface GitHubState {
  account: GitHubAccountView;
  threads: ChannelThreadView[];
  nextCursor: string | null;
  outbox: OutboxItemView[];
  vaultEntries: string[];
}

type GitHubBridge = {
  _populateGitHubTab?: () => void;
  _destroyGitHubTab?: () => void;
};

type CredentialsFacade = { list: () => Promise<unknown> };

const CONFIGURABLE_MODES: ConfigurableThreadMode[] = ['watch', 'mentions', 'ignore'];
const REPLY_POLICIES: ReplyPolicy[] = ['auto', 'draft', 'never'];
const DISCARDABLE = new Set(['draft', 'queued', 'failed']);
const THREAD_PREFIX = 'github:';
// Contract ThreadKey (GitHub part), accepted in any case: owner/repo#number.
const THREAD_INPUT = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}#[1-9][0-9]{0,9}$/;
// Vault entries owned by other features are never offered as GitHub tokens.
const RESERVED_ENTRY = /^(skill|channel)\./;
// GitHub's public REST API base: any other base is a GitHub Enterprise Server.
const GITHUB_COM_API = 'https://api.github.com';
// GitHub's classic token form, pre-filled with the scope tokenHint asks for.
const NEW_TOKEN_PATH = '/settings/tokens/new?scopes=repo&description=Tofu';

let scope: LifecycleScope | null = null;
let generation = 0;
let renders = 0;

function channelsApi(): ChannelsEndpoints {
  const api = (featureRegistry.Api as { channels?: ChannelsEndpoints } | undefined)?.channels;
  if (!api) throw new Error('Channel API is not ready');
  return api;
}

async function vaultEntryNames(): Promise<string[]> {
  const credentials = (featureRegistry.Api as { credentials?: CredentialsFacade } | undefined)?.credentials;
  if (!credentials) return [];
  const listing = await credentials.list().catch(() => null) as { credentials?: Array<{ name?: unknown }> } | null;
  return (listing?.credentials ?? [])
    .map((entry) => (typeof entry.name === 'string' ? entry.name : ''))
    .filter((name) => name && !RESERVED_ENTRY.test(name));
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function modeLabel(mode: ConfigurableThreadMode): string {
  return t(`settings.github.mode.${mode}` as const);
}

function replyLabel(policy: ReplyPolicy): string {
  return t(`settings.github.reply.${policy}` as const);
}

function threadUrl(account: GitHubAccountView, key: string): string {
  const [repository, number] = key.slice(THREAD_PREFIX.length).split('#');
  return `${account.webBaseUrl}/${repository}/issues/${number}`;
}

function commandView(current: number): CommandView {
  return {
    isCurrent: () => current === generation,
    reload: () => load(current),
    pendingLabel: t('settings.channels.working'),
    failure: (error) => t('settings.github.actionFailed', { error: errorText(error) }),
  };
}

function pollingText(account: GitHubAccountView): string {
  const { intervalS, nextPollAtMs } = account.poll;
  if (!intervalS) return t('settings.github.pollingUnknown');
  const time = nextPollAtMs ? new Date(nextPollAtMs).toLocaleTimeString() : '—';
  return t('settings.github.pollingValue', { seconds: String(intervalS), time });
}

function renderAccount(state: GitHubState, view: CommandView): HTMLElement {
  const { account } = state;
  const section = card(t('settings.github.status'));
  section.appendChild(statusLine(account, t(`settings.github.state.${account.status.state}` as const)));
  const identity = account.identity;
  const credential = account.credential;
  const rows: Array<[string, string | Node]> = [
    [t('settings.github.account'), identity ? `${identity.name || identity.mis} (${identity.mis}) · ${identity.uid}`
      : t('settings.github.noIdentity')],
    [t('settings.github.token'), credential
      ? t('settings.github.tokenValue', { hint: credential.hint || '—', name: credential.name })
      : t('settings.github.tokenNone')],
  ];
  if (account.apiBaseUrl !== GITHUB_COM_API) rows.push([t('settings.github.apiBase'), account.apiBaseUrl]);
  if (account.enabled) rows.push([t('settings.github.polling'), pollingText(account)]);
  rows.push([t('settings.github.router'), routerLink(account.routerConversationId, t('settings.channels.routerOpen'),
                                                     t('settings.github.routerMissing'))]);
  section.appendChild(facts(rows));
  const actions = element('div', 'channel-actions');
  const toggle = createButton({
    label: account.enabled ? t('settings.github.disable') : t('settings.github.enable'),
    variant: account.enabled ? 'secondary' : 'primary',
    disabled: !account.enabled && !credential,
    onClick: () => {
      void runCommand(view, toggle, () => channelsApi().updateGitHubAccount({ enabled: !account.enabled }));
    },
  });
  actions.append(toggle, createButton({
    label: t('settings.github.refresh'), variant: 'ghost',
    onClick: () => { void view.reload(); },
  }));
  section.appendChild(actions);
  if (!credential) section.appendChild(hint(t('settings.github.needsToken')));
  return section;
}

function renderToken(state: GitHubState, view: CommandView): HTMLElement {
  const { account } = state;
  const section = card(t('settings.github.token'));
  const help = hint(t('settings.github.tokenHint'));
  const create = element('a', 'channel-link', t('settings.github.tokenCreate'));
  create.href = `${account.webBaseUrl}${NEW_TOKEN_PATH}`;
  create.target = '_blank';
  create.rel = 'noopener noreferrer';
  help.append(' ', create);
  section.appendChild(help);
  // A token is the only missing piece of a disabled account, so adding one also enables it.
  const enable = account.enabled ? {} : { enabled: true };
  // A 400 refuses the token itself (its form, GitHub's verdict or its scope); other failures are not about it.
  const tokenFailure = (error: unknown) => (apiFailure(error).status === 400
    ? t('settings.github.tokenFailed', { error: errorText(error) }) : view.failure(error));
  const secret = element('input', 'ui-input');
  secret.type = 'password';
  secret.autocomplete = 'off';
  secret.spellcheck = false;
  const tokenRow = element('div', 'channel-actions');
  const save = createButton({
    label: account.enabled ? t('settings.github.tokenSave') : t('settings.github.tokenSaveEnable'),
    variant: 'primary',
    onClick: () => {
      const token = secret.value.trim();
      secret.value = '';
      if (!token) {
        secret.focus();
        return;
      }
      void runCommand(view, save, () => channelsApi().updateGitHubAccount({ token, ...enable }), tokenFailure);
    },
  });
  secret.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !save.disabled) save.click();
  });
  tokenRow.append(rowField({ label: t('settings.github.tokenInput'), control: secret }).el, save);
  if (account.credential) {
    const remove = createButton({
      label: t('settings.github.tokenRemove'), variant: 'danger',
      onClick: () => {
        void runCommand(view, remove, () => channelsApi().updateGitHubAccount({ credentialName: null }));
      },
    });
    tokenRow.appendChild(remove);
  }
  section.appendChild(tokenRow);
  if (state.vaultEntries.length) {
    const entryRow = element('div', 'channel-actions');
    const entry = rowField({
      label: t('settings.github.vaultEntry'), value: account.credential?.managed ? '' : account.credential?.name ?? '',
      choices: state.vaultEntries.map((name) => ({ value: name, label: name })),
    });
    const use = createButton({
      label: account.enabled ? t('settings.github.vaultUse') : t('settings.github.vaultUseEnable'),
      onClick: () => {
        const credentialName = (entry.control as HTMLSelectElement).value;
        void runCommand(view, use, () => channelsApi().updateGitHubAccount({ credentialName, ...enable }),
                        tokenFailure);
      },
    });
    entryRow.append(entry.el, use);
    section.appendChild(entryRow);
  }
  const enterprise = element('details', 'channel-advanced');
  enterprise.open = account.apiBaseUrl !== GITHUB_COM_API;
  enterprise.appendChild(element('summary', '', t('settings.github.enterprise')));
  const baseRow = element('div', 'channel-actions');
  const base = rowField({ label: t('settings.github.apiBase'), value: account.apiBaseUrl,
    hint: t('settings.github.apiBaseHint') });
  const saveBase = createButton({
    label: t('settings.github.apiBaseSave'), disabled: account.enabled,
    onClick: () => {
      const apiBaseUrl = (base.control as HTMLInputElement).value.trim();
      void runCommand(view, saveBase, () => channelsApi().updateGitHubAccount({ apiBaseUrl: apiBaseUrl || null }));
    },
  });
  baseRow.append(base.el, saveBase);
  enterprise.appendChild(baseRow);
  section.appendChild(enterprise);
  return section;
}

function renderThread(state: GitHubState, thread: ChannelThreadView, view: CommandView): HTMLElement {
  const row = element('div', 'channel-row');
  const title = element('div', 'channel-row-title');
  const link = element('a', 'channel-key', thread.key);
  link.href = threadUrl(state.account, thread.key);
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  title.append(element('div', '', thread.title || thread.key), link);
  row.appendChild(title);
  const mode = createField({
    label: t('settings.github.column.mode'), value: thread.modeOverride ?? '',
    choices: [{ value: '', label: t('settings.github.default', { value: modeLabel(CHANNEL_DEFAULT_MODE_BY_KIND.issue) }) },
              ...CONFIGURABLE_MODES.map((value) => ({ value, label: modeLabel(value) }))],
  });
  const modeSelect = mode.control as HTMLSelectElement;
  modeSelect.addEventListener('change', () => {
    const value = modeSelect.value as ConfigurableThreadMode | '';
    void runCommand(view, modeSelect, () => channelsApi().updateGitHubThread(thread.key, { mode: value || null }));
  });
  row.appendChild(mode.el);
  const reply = createField({
    label: t('settings.github.column.reply'), value: thread.replyPolicyOverride ?? '',
    choices: [{ value: '', label: t('settings.github.default', { value: replyLabel(CHANNEL_DEFAULT_REPLY_POLICY) }) },
              ...REPLY_POLICIES.map((value) => ({ value, label: replyLabel(value) }))],
  });
  const replySelect = reply.control as HTMLSelectElement;
  replySelect.addEventListener('change', () => {
    const value = replySelect.value as ReplyPolicy | '';
    void runCommand(view, replySelect,
                    () => channelsApi().updateGitHubThread(thread.key, { replyPolicy: value || null }));
  });
  row.appendChild(reply.el);
  return row;
}

function renderThreads(state: GitHubState, view: CommandView, current: number): HTMLElement {
  const section = card(t('settings.github.threads'));
  section.appendChild(hint(t('settings.github.threadsHint')));
  const repositories = [...new Set(state.threads.map((thread) => thread.key.slice(THREAD_PREFIX.length).split('#')[0]))];
  if (repositories.length) {
    section.appendChild(facts([[t('settings.github.repositories'), repositories.join(', ')]]));
  }
  const watchRow = element('div', 'channel-actions');
  const target = rowField({ label: t('settings.github.watchInput') });
  const add = createButton({
    label: t('settings.github.watchAdd'),
    onClick: () => {
      const value = (target.control as HTMLInputElement).value.trim();
      if (!THREAD_INPUT.test(value)) {
        feedback(watchRow, t('settings.github.watchInvalid'));
        return;
      }
      void runCommand(view, add, () => channelsApi().updateGitHubThread(`${THREAD_PREFIX}${value}`, { mode: 'watch' }));
    },
  });
  watchRow.append(target.el, add);
  section.appendChild(watchRow);
  if (!state.threads.length) {
    section.appendChild(createEmptyState({ title: t('settings.github.threadsEmpty') }));
    return section;
  }
  const list = element('div', 'channel-threads');
  for (const thread of state.threads) list.appendChild(renderThread(state, thread, view));
  section.appendChild(list);
  if (state.nextCursor) {
    const cursor = state.nextCursor;
    const more = createButton({
      label: t('settings.github.loadMore'), variant: 'ghost',
      onClick: () => {
        more.disabled = true;
        void channelsApi().githubThreads(cursor, scope?.signal).then((page) => {
          if (!view.isCurrent()) return;
          state.threads.push(...page.items);
          state.nextCursor = page.nextCursor;
          render(state, current);
        }, (error) => {
          if (!view.isCurrent()) return;
          more.disabled = false;
          feedback(more, t('settings.github.loadFailed', { error: errorText(error) }));
        });
      },
    });
    section.appendChild(more);
  }
  return section;
}

function renderOutbox(state: GitHubState, view: CommandView): HTMLElement {
  const section = card(t('settings.github.outbox'));
  if (!state.outbox.length) {
    section.appendChild(createEmptyState({ title: t('settings.github.outboxEmpty') }));
    return section;
  }
  for (const item of state.outbox) {
    const row = element('div', 'channel-outbox-item');
    row.appendChild(facts([
      [t('settings.github.status'), t(`settings.github.outboxState.${item.state}` as const)
        + (item.error ? ` — ${item.error}` : '')],
      [t('settings.github.column.thread'), `${item.threadTitle || item.thread} (${item.thread})`],
    ]));
    row.appendChild(element('div', 'channel-outbox-text', item.text));
    const actions = element('div', 'channel-actions');
    if (item.state === 'draft') {
      const approve = createButton({
        label: t('settings.github.approve'), variant: 'primary', size: 'sm',
        onClick: () => { void runCommand(view, approve, () => channelsApi().approveOutbox(item.id)); },
      });
      actions.appendChild(approve);
    }
    if (DISCARDABLE.has(item.state)) {
      const discard = createButton({
        label: t('settings.github.discard'), variant: 'danger', size: 'sm',
        onClick: () => { void runCommand(view, discard, () => channelsApi().discardOutbox(item.id)); },
      });
      actions.appendChild(discard);
    }
    row.appendChild(actions);
    section.appendChild(row);
  }
  return section;
}

function render(state: GitHubState, current: number): void {
  const root = document.getElementById('githubChannelRoot');
  if (!root || current !== generation) return;
  const view = commandView(current);
  const rendered = ++renders;
  root.replaceChildren(
    renderAccount(state, view),
    renderToken(state, view),
    renderThreads(state, view, current),
    renderOutbox(state, view),
  );
  if (scope) {
    refreshWhileConnecting(scope, state.account, (signal) => channelsApi().githubAccount(signal),
                           () => current === generation && rendered === renders, view.reload);
  }
}

async function load(current: number): Promise<void> {
  const signal = scope?.signal;
  try {
    const api = channelsApi();
    const [account, page, outbox, vaultEntries] = await Promise.all([
      api.githubAccount(signal), api.githubThreads('', signal), api.outbox(signal), vaultEntryNames(),
    ]);
    if (current !== generation) return;
    render({
      account, threads: page.items, nextCursor: page.nextCursor, vaultEntries,
      outbox: outbox.items.filter((item) => item.thread.startsWith(THREAD_PREFIX)),
    }, current);
  } catch (error) {
    const root = document.getElementById('githubChannelRoot');
    if (!root || current !== generation || signal?.aborted) return;
    root.replaceChildren(createAlert({
      variant: 'error', title: t('settings.github.loadFailed', { error: errorText(error) }) }).el);
  }
}

export function populateGitHubTab(): void {
  const root = document.getElementById('githubChannelRoot');
  if (!root) return;
  scope?.destroy();
  scope = createLifecycleScope();
  const current = ++generation;
  root.replaceChildren(createSpinner());
  void load(current);
}

export function destroyGitHubTab(): void {
  generation += 1;
  scope?.destroy();
  scope = null;
}

const bridge = featureRegistry as unknown as GitHubBridge;
bridge._populateGitHubTab = populateGitHubTab;
bridge._destroyGitHubTab = destroyGitHubTab;
