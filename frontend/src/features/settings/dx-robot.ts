/**
 * 大象 Settings notification robot card (docs/CHANNELS.md C6, "Notification robot").
 * Responsibility: the robot's state from the account view (not configured,
 * configured, or its last refusal with the setup step that fixes it); saving
 * the client ID with a pasted client secret or an existing credential vault
 * entry; removing the robot; and the owner's setup steps. The secret is
 * write-only: the view carries only the vault's redacted hint, the password
 * input is never filled from it, and a submitted secret leaves the input
 * before the request goes out.
 * Entry points: renderRobotCard.
 * Dependencies: the `Api.channels` facade (through DxRobotPorts), the shared
 * channel view (channel-settings-view.ts), tofu-ui factories and i18n.
 */
import { t } from '../../i18n';
import { createAlert, createButton, createDialog } from '../../ui';
import { apiFailure } from '../../api/errors';
import type { ChannelsEndpoints } from '../../api/endpoint-registry/channels';
import type { ChannelAccountView, DxRobotError, DxRobotUpdate, DxRobotView } from '../../api/channels.generated';
import {
  card, chip, clearFeedback, element, facts, feedback, hint, rowField, runCommand, type CommandView,
} from './channel-settings-view';

export interface DxRobotPorts {
  api: () => ChannelsEndpoints;
  view: CommandView;
}

type RobotFix = 'group' | 'permission' | 'app' | 'credentials' | 'secretMissing' | 'network';

// The setup step a refusal code asks the owner to redo: lib/channels/dx/robot.py's
// own codes, and robot_<code> for the open platform's 错误码
// (https://km.internal.example.com/collabpage/719464490). Other codes show their detail only.
const ROBOT_FIXES: Readonly<Record<string, RobotFix>> = {
  robot_70003: 'group',
  robot_30002: 'permission',
  robot_70004: 'permission',
  robot_70001: 'app',
  robot_20001: 'app',
  robot_20002: 'app',
  robot_20003: 'credentials',
  token_rejected: 'credentials',
  robot_credential_missing: 'secretMissing',
  credential_missing: 'secretMissing',
  token_unavailable: 'network',
};

// The consoles of the owner setup steps (docs/CHANNELS.md C6, "Owner setup").
const APP_CONSOLE = 'https://open.internal.example.com/developer/app/apply';
const OFFICE_CONSOLE = 'https://xopen.internal.example.com/home';
const GROUP_THREAD_PREFIX = 'dx:gid:';

function fixText(code: string): string {
  const fix = ROBOT_FIXES[code];
  return fix ? t(`settings.dx.robot.fix.${fix}` as const) : '';
}

function externalLink(text: string, href: string): HTMLAnchorElement {
  const link = element('a', 'channel-link', text);
  link.href = href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

function stateChip(robot: DxRobotView | null): HTMLElement {
  if (!robot) return chip(t('settings.dx.robot.state.none'), 'idle');
  if (robot.error) return chip(t('settings.dx.robot.state.refused'), 'bad');
  return chip(t('settings.dx.robot.state.configured'), 'ok');
}

/** The last refusal: when, the open platform's own words and code, and what fixes it. */
function refusal(error: DxRobotError): HTMLElement {
  const time = new Intl.DateTimeFormat(document.documentElement.lang || undefined,
                                       { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(error.atMs));
  const alert = createAlert({
    variant: 'warning',
    title: t('settings.dx.robot.refused', { time }),
    body: `${error.detail} (${error.code})`,
  }).el;
  alert.classList.add('dx-robot-refusal');
  const fix = fixText(error.code);
  if (fix) alert.appendChild(element('div', 'dx-robot-fix', fix));
  return alert;
}

function setupSteps(open: boolean): HTMLElement {
  const steps = element('details', 'channel-advanced dx-robot-setup');
  steps.open = open;
  steps.appendChild(element('summary', '', t('settings.dx.robot.steps')));
  const list = element('ol', 'dx-robot-steps');
  const consoles: Array<[string, string] | null> = [
    ['open.internal.example.com', APP_CONSOLE], ['xopen.internal.example.com', OFFICE_CONSOLE], null, null,
  ];
  (['step1', 'step2', 'step3', 'step4'] as const).forEach((step, index) => {
    const item = element('li', '', t(`settings.dx.robot.${step}` as const));
    const target = consoles[index];
    if (target) item.append(' ', externalLink(...target));
    list.appendChild(item);
  });
  steps.append(list, hint(t('settings.dx.robot.stepsSource')));
  return steps;
}

function confirmRemove(): Promise<string | null> {
  return createDialog({
    title: t('settings.dx.robot.remove'), body: t('settings.dx.robot.removeConfirm'),
    actions: [{ id: 'cancel', label: t('common.cancel') },
              { id: 'remove', label: t('settings.dx.robot.remove'), variant: 'danger' }],
  }).open();
}

export function renderRobotCard(account: ChannelAccountView, vaultEntries: string[],
                                ports: DxRobotPorts): HTMLElement {
  // An account view without the field (an older server) has no robot.
  const robot = account.robot ?? null;
  const section = card(t('settings.dx.robot.title'), stateChip(robot));
  section.appendChild(hint(t('settings.dx.robot.hint')));
  if (robot) {
    section.appendChild(facts([
      [t('settings.dx.robot.clientId'), robot.clientId],
      [t('settings.dx.robot.secret'), t('settings.dx.robot.secretValue',
                                        { hint: robot.credentialHint || '—', name: robot.credentialName })],
    ]));
    if (robot.error) section.appendChild(refusal(robot.error));
    if (!account.commandChat?.startsWith(GROUP_THREAD_PREFIX)) {
      section.appendChild(createAlert({ variant: 'warning', title: t('settings.dx.robot.groupOnly') }).el);
    }
  }
  const failure = (error: unknown): string => {
    const fix = fixText(String(apiFailure(error).code ?? ''));
    return fix ? `${ports.view.failure(error)} ${fix}` : ports.view.failure(error);
  };
  const save = (control: HTMLButtonElement, update: DxRobotUpdate): void => {
    void runCommand(ports.view, control, () => ports.api().updateDxAccount({ robot: update }), failure);
  };

  const form = element('form', 'channel-section dx-robot-form');
  form.noValidate = true;
  const row = element('div', 'channel-actions');
  const clientField = rowField({ label: t('settings.dx.robot.clientId'), value: robot?.clientId ?? '' });
  const clientInput = clientField.control as HTMLInputElement;
  clientInput.dataset.focusKey = 'dx-robot-client';
  clientInput.autocomplete = 'off';
  clientInput.spellcheck = false;
  clientInput.setAttribute('autocapitalize', 'none');
  const secretField = rowField({ label: t('settings.dx.robot.secret') });
  const secretInput = secretField.control as HTMLInputElement;
  secretInput.type = 'password';
  // Never offer a saved login password for the robot's secret.
  secretInput.autocomplete = 'new-password';
  secretInput.spellcheck = false;
  if (robot) secretInput.placeholder = t('settings.dx.robot.secretKeep');
  const submit = createButton({ label: t('settings.dx.robot.save'), variant: 'primary', type: 'submit' });
  row.append(clientField.el, secretField.el, submit);
  if (robot) {
    const remove = createButton({
      label: t('settings.dx.robot.remove'), variant: 'danger',
      onClick: () => {
        void confirmRemove().then((choice) => {
          if (choice === 'remove' && ports.view.isCurrent()) {
            void runCommand(ports.view, remove, () => ports.api().updateDxAccount({ robot: null }));
          }
        });
      },
    });
    row.appendChild(remove);
  }
  for (const input of [clientInput, secretInput]) input.addEventListener('input', () => clearFeedback(row));
  form.appendChild(row);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    const clientId = clientInput.value.trim();
    const secret = secretInput.value.trim();
    if (!clientId) {
      feedback(row, t('settings.dx.robot.needsClientId'));
      clientInput.focus();
      return;
    }
    if (!secret && !robot) {
      feedback(row, t('settings.dx.robot.needsSecret'));
      secretInput.focus();
      return;
    }
    secretInput.value = '';
    // Without a new secret the robot keeps the one it has.
    save(submit, secret ? { clientId, secret } : { clientId });
  });
  section.appendChild(form);

  if (vaultEntries.length) {
    const vault = element('details', 'channel-advanced dx-robot-vault');
    vault.open = Boolean(robot && !robot.managed);
    vault.appendChild(element('summary', '', t('settings.dx.robot.vault')));
    const entryRow = element('div', 'channel-actions');
    const entry = rowField({
      label: t('settings.dx.robot.vaultEntry'), value: robot && !robot.managed ? robot.credentialName : '',
      choices: vaultEntries.map((name) => ({ value: name, label: name })),
    });
    const use = createButton({
      label: t('settings.dx.robot.vaultUse'),
      onClick: () => {
        const clientId = clientInput.value.trim();
        if (!clientId) {
          feedback(entryRow, t('settings.dx.robot.needsClientId'));
          clientInput.focus();
          return;
        }
        save(use, { clientId, credentialName: (entry.control as HTMLSelectElement).value });
      },
    });
    entryRow.append(entry.el, use);
    vault.appendChild(entryRow);
    section.appendChild(vault);
  }
  section.appendChild(setupSteps(!robot || Boolean(robot.error)));
  return section;
}
