/** Advanced coordinator management. Replacement is a revision-bound explicit
 * confirmation, never context maintenance. Uncertain writes only offer a read.
 */
import { createAlert, createButton, createDialog } from '../ui';
import { t } from '../i18n';
import type { CoordinationBinding } from '../api/project-coordination.generated';
import type { ProjectCoordinationPorts } from './project-coordination';
import { createCoordinationReference } from './project-coordination-reference';

const text = (value: string, tag = 'p') => { const node = document.createElement(tag); node.textContent = value; return node; };
export function createCoordinationManagement(ports: ProjectCoordinationPorts, options: {
  binding(): CoordinationBinding; valid(): boolean; refresh(): Promise<boolean>;
  inspect?(id: string): void; diagnose?(id: string): void;
}) {
  const root = document.createElement('section'); root.className = 'coordination-management';
  let pending = false, disposed = false;
  let confirmation: ReturnType<typeof createDialog> | undefined;
  const feedback = document.createElement('div'); feedback.setAttribute('role', 'status');
  const context = document.createElement('section'); context.className = 'coordination-management-section';
  context.append(text(t('projectCoordination.context.title'), 'h3'), text(t('projectCoordination.context.help')),
    text(t('projectCoordination.context.unavailable'), 'small'));
  const diagnose = createButton({ label: t('projectCoordination.context.diagnose'), size: 'sm', onClick: () => {
    const binding = options.binding();
    if (options.valid() && binding.availability === 'available') options.diagnose?.(binding.conversationId);
  } });
  diagnose.disabled = !options.diagnose || options.binding().availability !== 'available';
  context.append(diagnose);

  for (const action of ['organize', 'fresh'] as const) {
    const unavailable = createButton({ label: t(`projectCoordination.context.${action}`), size: 'sm', variant: 'ghost' });
    unavailable.disabled = true; unavailable.title = t('projectCoordination.context.unavailable'); context.append(unavailable);
  }
  const replacement = document.createElement('section'); replacement.className = 'coordination-management-section';
  replacement.append(text(t('projectCoordination.replacement.title'), 'h3'), text(t('projectCoordination.replacement.hint')));
  const replace = createButton({ label: t('projectCoordination.replace'), variant: 'danger', size: 'sm', onClick: async () => {
    if (!ports.create || !options.valid() || disposed || pending) return;
    const expected = options.binding(); pending = true; replace.disabled = true;
    const body = document.createElement('div'); body.className = 'coordination-management-section';
    body.append(text(t('projectCoordination.replacement.impact')),
      createCoordinationReference({ conversationId: expected.conversationId }, 'replacement', options.inspect));
    confirmation = createDialog({ title: t('projectCoordination.replacement.confirmTitle'), body,
      actions: [{ id: 'cancel', label: t('projectCoordination.replacement.cancel') },
        { id: 'replace', label: t('projectCoordination.replacement.confirm'), variant: 'danger' }] });
    document.body.append(confirmation.el);
    const action = await confirmation.open(); confirmation.el.remove(); confirmation = undefined;
    if (action !== 'replace' || !options.valid() || disposed) { pending = false; replace.disabled = !ports.create; return; }
    feedback.replaceChildren(text(t('projectCoordination.loading')));
    try {
      const fresh = await ports.binding(ports.path);
      if (!options.valid() || disposed) return;
      if (fresh.revision !== expected.revision || fresh.conversationId !== expected.conversationId) {
        throw new Error(t('projectCoordination.replacement.changed'));
      }
      await ports.create(ports.path, expected.revision);
      if (!options.valid() || disposed) return;
      feedback.replaceChildren(text(t('projectCoordination.replacement.done')));
      await options.refresh();
    } catch (error) {
      if (!options.valid() || disposed) return;
      feedback.replaceChildren(createAlert({ variant: 'warning', title: t('projectCoordination.replacement.uncertain'),
        body: error instanceof Error ? error.message : String(error) }).el);
      const retry = createButton({ label: t('projectCoordination.retry'), size: 'sm', onClick: async () => {
        if (!options.valid() || disposed) return;
        retry.disabled = true;
        const read = await options.refresh();
        if (disposed) return;
        retry.disabled = false;
        if (read) { feedback.replaceChildren(); replace.disabled = !ports.create; }
      } });
      feedback.append(retry);
      return; // Read status before offering another separately confirmed write.
    } finally { pending = false; }
    replace.disabled = !ports.create;
  } });
  replace.disabled = !ports.create;
  replacement.append(replace, feedback); root.append(context, replacement);
  return { el: root, destroy() { disposed = true; confirmation?.close(); confirmation?.el.remove(); } };
}
