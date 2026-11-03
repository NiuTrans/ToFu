/**
 * 大象 Settings account and command chat cards (docs/CHANNELS.md C6).
 * Responsibility: the account status and what each state needs from the
 * owner (approve the login in 大象, reconnect, or enter a MIS);
 * enable / disable / reconnect; switching the login MIS, where an account
 * with an SSO session reuses it and any other gets one 大象 login approval;
 * and the command chat: create the Tofu Router group, or pick an existing
 * chat from a searchable list. A command answers at once; the account then
 * moves through starting / authorizing by itself while the tab re-reads it.
 * Entry points: renderAccountCard, renderCommandChatCard, createDxCardUi,
 * replaceKeepingFocus.
 * Dependencies: the `Api.channels` facade (through DxCardPorts), the shared
 * channel view, tofu-ui factories and i18n.
 */
import { t } from '../../i18n';
import { createAlert, createButton, createDialog, createField } from '../../ui';
import type { ChannelsEndpoints } from '../../api/endpoint-registry/channels';
import { CHANNEL_MIS_PATTERN } from '../../api/channels.generated';
import type { ChannelAccountView, ChannelThreadView } from '../../api/channels.generated';
import {
  card, cardSection, chip, clearFeedback, element, empty, facts, feedback, hint, routerLink, rowField, runCommand,
  statusLine, type CommandView,
} from './channel-settings-view';
import { compareThreads, threadTitle } from './dx-thread-list';

export interface DxCardPorts {
  api: () => ChannelsEndpoints;
  view: CommandView;
  /** Re-render the cards from the last read after a presentation change. */
  rerender: () => void;
}

/**
 * Presentation state of one opening: which inline editors are open and what
 * the owner typed, so a re-render (a status change while connecting) keeps it.
 */
export interface DxCardUi {
  switching: boolean;
  misDraft: string | null;
  picking: boolean;
  pickQuery: string;
}

const MIS = new RegExp(CHANNEL_MIS_PATTERN);
const NEEDS_ACTION = new Set(['needs_login', 'kicked', 'error']);

export function createDxCardUi(): DxCardUi {
  return { switching: false, misDraft: null, picking: false, pickQuery: '' };
}

/** Name a text input so a re-rendered card can return focus and caret to its successor. */
function focusKey(input: HTMLInputElement, key: string): void {
  input.dataset.focusKey = key;
}

/** Render into `slot`, keeping focus and caret in a keyed input that is re-rendered. */
export function replaceKeepingFocus(slot: HTMLElement, next: HTMLElement): void {
  const active = document.activeElement;
  const focused = active instanceof HTMLInputElement && slot.contains(active) ? active : null;
  const key = focused?.dataset.focusKey;
  const [start, end] = [focused?.selectionStart ?? null, focused?.selectionEnd ?? null];
  slot.replaceChildren(next);
  if (!key) return;
  const successor = slot.querySelector<HTMLInputElement>(`input[data-focus-key="${key}"]`);
  if (!successor) return;
  successor.focus();
  if (start !== null && end !== null) successor.setSelectionRange(start, end);
}

/** An enabled account that has no MIS to log in with: only the owner can supply one. */
function needsMis(account: ChannelAccountView): boolean {
  return account.enabled && account.status.state === 'needs_login' && !account.loginMis;
}

function identityText(account: ChannelAccountView): string {
  const { identity, loginMis } = account;
  if (identity) return `${identity.name || identity.mis} (${identity.mis}) · ${identity.uid}`;
  if (loginMis) return account.enabled ? t('settings.dx.identityPending', { mis: loginMis }) : loginMis;
  return t('settings.dx.noIdentity');
}

function stateHint(account: ChannelAccountView): string {
  if (!account.enabled) return account.skillInstalled ? t('settings.dx.enableHint') : '';
  switch (account.status.state) {
    case 'starting': return t('settings.dx.hint.starting');
    case 'authorizing': return t('settings.dx.hint.authorizing', { mis: account.loginMis ?? '' });
    case 'needs_login': return account.loginMis ? t('settings.dx.hint.needsLogin') : t('settings.dx.hint.needsMis');
    case 'kicked': return t('settings.dx.hint.kicked');
    default: return '';
  }
}

function switchForm(account: ChannelAccountView, ui: DxCardUi, ports: DxCardPorts, forced: boolean): HTMLElement {
  const form = element('form', 'channel-section dx-account-switch');
  form.noValidate = true;
  const field = rowField({ label: t('settings.dx.misLabel'), value: ui.misDraft ?? account.loginMis ?? '' });
  const input = field.control as HTMLInputElement;
  focusKey(input, 'dx-mis');
  input.placeholder = t('settings.dx.misPlaceholder');
  input.autocomplete = 'off';
  input.spellcheck = false;
  input.setAttribute('autocapitalize', 'none');
  const submit = createButton({ label: t('settings.dx.misSubmit'), variant: 'primary', type: 'submit' });
  const row = element('div', 'channel-actions');
  input.addEventListener('input', () => {
    ui.misDraft = input.value;
    if (input.hasAttribute('aria-invalid')) {
      input.removeAttribute('aria-invalid');
      clearFeedback(row);
    }
  });
  row.append(field.el, submit);
  if (!forced) {
    row.appendChild(createButton({
      label: t('common.cancel'), variant: 'ghost',
      onClick: () => {
        ui.switching = false;
        ui.misDraft = null;
        ports.rerender();
      },
    }));
  }
  form.append(row, hint(t('settings.dx.misHint')));
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    const mis = input.value.trim().toLowerCase();
    if (!MIS.test(mis)) {
      // Under the row, like a refused command: inside the field it would push the field below its buttons.
      feedback(row, t('settings.dx.misInvalid'));
      input.setAttribute('aria-invalid', 'true');
      input.focus();
      return;
    }
    void runCommand(ports.view, submit, () => ports.api().updateDxAccount({ mis, enabled: true }).then((view) => {
      ui.switching = false;
      ui.misDraft = null;
      return view;
    }));
  });
  return form;
}

export function renderAccountCard(account: ChannelAccountView, ui: DxCardUi, ports: DxCardPorts): HTMLElement {
  const section = card(t('settings.dx.status'),
                       statusLine(account, t(`settings.dx.state.${account.status.state}` as const)));
  if (!account.skillInstalled) {
    section.appendChild(createAlert({ variant: 'warning', title: t('settings.dx.skillMissing') }).el);
  }
  const forced = needsMis(account);
  const who = element('div', 'dx-account-value');
  who.appendChild(element('span', 'dx-account-name', identityText(account)));
  if (!forced) {
    const toggle = createButton({
      label: t('settings.dx.switchAccount'), variant: 'ghost', size: 'sm',
      onClick: () => {
        ui.switching = !ui.switching;
        if (!ui.switching) ui.misDraft = null;
        ports.rerender();
      },
    });
    toggle.setAttribute('aria-expanded', String(ui.switching));
    who.appendChild(toggle);
  }
  section.appendChild(facts([
    [t('settings.dx.account'), who],
    [t('settings.dx.router'), routerLink(account.routerConversationId, t('settings.channels.routerOpen'),
                                         t('settings.dx.routerMissing'))],
  ]));
  if (forced || ui.switching) section.appendChild(switchForm(account, ui, ports, forced));

  const actions = element('div', 'channel-actions');
  const api = ports.api;
  if (!account.enabled) {
    const enable = createButton({
      label: t('settings.dx.enable'), variant: 'primary', disabled: !account.skillInstalled,
      onClick: () => { void runCommand(ports.view, enable, () => api().updateDxAccount({ enabled: true })); },
    });
    actions.appendChild(enable);
  } else {
    if (NEEDS_ACTION.has(account.status.state) && !forced) {
      const reconnect = createButton({
        label: t('settings.dx.reconnect'), variant: 'primary', disabled: !account.skillInstalled,
        onClick: () => { void runCommand(ports.view, reconnect, () => api().updateDxAccount({ enabled: true })); },
      });
      actions.appendChild(reconnect);
    }
    const disable = createButton({
      label: t('settings.dx.disable'), variant: 'secondary',
      onClick: () => { void runCommand(ports.view, disable, () => api().updateDxAccount({ enabled: false })); },
    });
    actions.appendChild(disable);
  }
  actions.appendChild(createButton({
    label: t('settings.dx.refresh'), variant: 'ghost',
    onClick: () => { void ports.view.reload(); },
  }));
  const footer = cardSection(actions);
  const text = stateHint(account);
  if (text) footer.appendChild(hint(text));
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

function chatPicker(candidates: ChannelThreadView[], ui: DxCardUi, ports: DxCardPorts): HTMLElement {
  const box = element('div', 'channel-section dx-chat-picker');
  const search = createField({ label: t('settings.dx.threadsSearch'), value: ui.pickQuery });
  search.el.classList.add('dx-thread-search');
  const input = search.control as HTMLInputElement;
  focusKey(input, 'dx-pick');
  input.type = 'search';
  input.placeholder = t('settings.dx.threadsSearchPlaceholder');
  input.autocomplete = 'off';
  const list = element('div', 'dx-chat-options');
  const options = [...candidates].sort(compareThreads).map((thread) => {
    const option = element('div', 'dx-chat-option');
    const name = element('div', 'channel-row-title');
    name.append(element('div', 'channel-row-name', threadTitle(thread)), element('div', 'channel-key', thread.key));
    const use = createButton({
      label: t('settings.dx.commandChatUse'), size: 'sm',
      onClick: () => {
        void runCommand(ports.view, use, () => ports.api().updateDxAccount({ commandChat: thread.key }).then((view) => {
          ui.picking = false;
          ui.pickQuery = '';
          return view;
        }));
      },
    });
    option.append(name, use);
    list.appendChild(option);
    return { thread, option };
  });
  const none = empty(t('settings.dx.noMatch'));
  const apply = (): void => {
    const query = ui.pickQuery.trim().toLowerCase();
    let shown = 0;
    for (const { thread, option } of options) {
      option.hidden = Boolean(query) && !thread.key.toLowerCase().includes(query)
        && !thread.title.toLowerCase().includes(query);
      if (!option.hidden) shown += 1;
    }
    none.hidden = shown > 0;
  };
  input.addEventListener('input', () => {
    ui.pickQuery = input.value;
    apply();
  });
  apply();
  box.append(search.el, list, none);
  return box;
}

export function renderCommandChatCard(account: ChannelAccountView, threads: ChannelThreadView[] | null,
                                      ui: DxCardUi, ports: DxCardPorts): HTMLElement {
  const chosen = account.commandChat;
  const selected = threads?.find((thread) => thread.key === chosen);
  const current = chosen
    ? chip(selected ? threadTitle(selected) : chosen, 'ok')
    : chip(t('settings.dx.commandChatNone'), 'idle');
  if (chosen) current.title = selected ? `${threadTitle(selected)} (${selected.key})` : chosen;
  const section = card(t('settings.dx.commandChat'), current);
  section.appendChild(hint(t('settings.dx.commandChatHint')));
  if (!account.enabled || !account.identity) {
    section.appendChild(hint(t('settings.dx.commandChatNeedsAccount')));
    return section;
  }
  const actions = element('div', 'channel-actions');
  if (!chosen) {
    const create = createButton({
      label: t('settings.dx.commandChatCreate'), variant: 'primary',
      onClick: () => {
        void confirmCreate().then((choice) => {
          if (choice === 'create' && ports.view.isCurrent()) {
            void runCommand(ports.view, create, () => ports.api().createDxCommandChat());
          }
        });
      },
    });
    actions.appendChild(create);
  }
  const candidates = (threads ?? []).filter((thread) => thread.kind !== 'pid' && thread.key !== chosen);
  if (candidates.length) {
    const toggle = createButton({
      label: chosen ? t('settings.dx.commandChatChange') : t('settings.dx.commandChatPick'),
      variant: chosen ? 'secondary' : 'ghost',
      onClick: () => {
        ui.picking = !ui.picking;
        ports.rerender();
      },
    });
    toggle.setAttribute('aria-expanded', String(ui.picking));
    actions.appendChild(toggle);
  }
  if (actions.childElementCount) section.appendChild(cardSection(actions));
  if (ui.picking && candidates.length) section.appendChild(chatPicker(candidates, ui, ports));
  return section;
}
