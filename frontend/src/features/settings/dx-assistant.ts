/**
 * 大象 Settings assistant card (docs/CHANNELS.md C11): how Tofu helps with
 * other people's messages — the main coordinator reviews them, tells the owner
 * what needs them and drafts replies in their voice; other people's text is
 * never an instruction; drafts wait for the owner's approval in the outbox or
 * by their approval code in the command chat — and the owner's account note,
 * the guidance every draft follows.
 * Entry points: renderAssistantCard; noteField (also the thread list's note editor).
 * Dependencies: the `Api.channels` facade (through DxAssistantPorts), the
 * shared channel view, tofu-ui factories, i18n and the generated wire DTOs.
 * The note is server state: saving re-reads the tab; the card re-renders only
 * when the saved note changes or after its own save, so what the owner types
 * survives other cards' re-renders.
 */
import { t } from '../../i18n';
import { createButton, createField } from '../../ui';
import type { ChannelsEndpoints } from '../../api/endpoint-registry/channels';
import { CHANNEL_DISCLOSURE_LABEL, CHANNEL_OWNER_NOTE_MAX_CHARS } from '../../api/channels.generated';
import type { ChannelAccountView } from '../../api/channels.generated';
import { card, cardSection, element, hint, runCommand, type CommandView } from './channel-settings-view';

export interface DxAssistantPorts {
  api: () => ChannelsEndpoints;
  view: CommandView;
}

export interface NoteFieldOptions {
  label: string;
  hint: string;
  placeholder: string;
  value: string;
}

/** A labelled multi-line field for an owner note, bounded by the contract's note length. */
export function noteField(options: NoteFieldOptions): { el: HTMLElement; input: HTMLTextAreaElement } {
  const input = document.createElement('textarea');
  input.className = 'ui-input';
  input.rows = 3;
  input.maxLength = CHANNEL_OWNER_NOTE_MAX_CHARS;
  input.placeholder = options.placeholder;
  input.value = options.value;
  const field = createField({ label: options.label, hint: options.hint, control: input });
  field.el.classList.add('channel-note');
  return { el: field.el, input };
}

export function renderAssistantCard(account: ChannelAccountView, ports: DxAssistantPorts): HTMLElement {
  const section = card(t('settings.dx.assistant.title'));
  section.classList.add('dx-assistant');
  section.append(hint(t('settings.dx.assistant.how', { label: CHANNEL_DISCLOSURE_LABEL })),
                 hint(t('settings.dx.assistant.approval')));
  const field = noteField({
    label: t('settings.dx.assistant.note'), hint: t('settings.dx.assistant.noteHint'),
    placeholder: t('settings.dx.assistant.notePlaceholder'), value: account.assistantNote,
  });
  const actions = element('div', 'channel-actions');
  const save = createButton({
    label: t('settings.dx.noteSave'), variant: 'primary',
    onClick: () => {
      const note = field.input.value.trim();
      void runCommand(ports.view, save, () => ports.api().updateDxAccount({ assistantNote: note || null }));
    },
  });
  actions.appendChild(save);
  section.appendChild(cardSection(field.el, actions));
  return section;
}
