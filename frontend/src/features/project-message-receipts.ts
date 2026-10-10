/** Passive mailbox projection; optional demand reads, no execution commands.
 * Transport and durable state belong to the typed API/server. Each mounted
 * disclosure owns a generation fence; late results cannot replace another view.
 */
import { createButton, createAlert, createEmptyState } from '../ui';
import { t } from '../i18n';
import type { ProjectMessagePage } from '../api/project-coordination.generated';

export function createProjectMessageReceipts(read: (beforeId: string) => Promise<ProjectMessagePage>, valid: () => boolean): HTMLElement {
  const section = document.createElement('section');
  section.className = 'ui-stack';
  const body = document.createElement('div');
  body.className = 'ui-stack';
  body.hidden = true;
  let generation = 0;
  let loaded = false;
  const toggle = createButton({ label: t('projectCoordination.messages'), variant: 'ghost',
    onClick: () => {
      body.hidden = !body.hidden;
      toggle.setAttribute('aria-expanded', String(!body.hidden));
      if (!body.hidden && !loaded) { loaded = true; void load(''); }
    } });
  toggle.setAttribute('aria-expanded', 'false');
  section.append(toggle, body);
  const text = (value: string, tag = 'p') => {
    const node = document.createElement(tag); node.textContent = value; return node;
  };
  async function load(beforeId: string) {

    if (!valid() || !section.isConnected) return;
    const own = ++generation;
    body.replaceChildren(text(t('projectCoordination.loading')));
    const current = () => own === generation && valid() && section.isConnected;
    try {
      const page = await read(beforeId);
      if (!current()) return;
      body.replaceChildren(text(t('projectCoordination.messageCaveat'), 'small'));
      if (!page.messages.length) body.append(createEmptyState({ title: t('projectCoordination.empty') }));
      for (const message of page.messages.slice(0, 20)) {
        const card = document.createElement('article');
        card.className = 'ui-card ui-stack ui-stack--tight';
        card.append(text(message.sender, 'strong'));
        card.append(text(t(`projectCoordination.messageState.${message.state}`), 'small'));
        card.append(text(t(message.decision ? `projectCoordination.decision.${message.decision}`
          : 'projectCoordination.decision.pending'), 'small'));
        card.append(text(message.content));
        if (message.decisionNote) card.append(text(message.decisionNote));
        if (message.inputTurnId) card.append(text(message.inputTurnId, 'small'));
        body.append(card);
      }
      if (page.next_before_id) body.append(createButton({ label: t('projectCoordination.olderMessages'),
        size: 'sm', onClick: () => { void load(page.next_before_id!); } }));
    } catch (reason) {
      if (!current()) return;
      body.replaceChildren(createAlert({ variant: 'warning', title: t('projectCoordination.messagesUnavailable'),
        body: reason instanceof Error ? reason.message : String(reason) }).el);
    }
    if (current()) body.append(createButton({ label: t('projectCoordination.retry'), size: 'sm',
      onClick: () => { void load(''); } }));
  }
  return section;
}
