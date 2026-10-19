/** Shared-reference commands; no document writes or silent digest/version adoption.
 * Registration pins canonical bytes server-side. Refresh observes old reference
 * metadata only; replacement is a hint requiring a current human source.
 */
import { createAlert, createChoice } from '../ui';
import { t } from '../i18n';
import type { ProjectSharedFilesRegister, ProjectSharedFilesRefresh, ProjectSharedFilesEntry } from '../api/project-continuity.generated';
import type { ProjectDispatchPorts } from './project-dispatch';
import { assertContinuityScope, ownsContinuityScope, type ContinuityScope, type RetainedCommand } from './project-continuity-scope';
import { createContinuityButton as createButton, field, sourceField, showError, stack, text } from './project-continuity-view';
export interface SharedFileCommandPorts {
  register(body: ProjectSharedFilesRegister, key: string): Promise<ProjectSharedFilesEntry>;
  refresh(body: ProjectSharedFilesRefresh, key: string): Promise<ProjectSharedFilesEntry>;
}
type Pending = (RetainedCommand<ProjectSharedFilesRegister> & { kind: 'register'; coordinatorId: string })
  | (RetainedCommand<ProjectSharedFilesRefresh> & { kind: 'refresh'; coordinatorId: string });
let pending: Pending | null = null;
const safeRelative = (value: string) => !!value && !value.startsWith('/') && !value.includes('\\')
  && value.split('/').every(part => !!part && part !== '.' && part !== '..');
export function createSharedFileCommands(ports: SharedFileCommandPorts, dispatch: ProjectDispatchPorts, scope: ContinuityScope,
  onDone: () => void, entry?: ProjectSharedFilesEntry): HTMLElement {
  const root = stack('details'); root.append(text(t(entry ? 'projectShared.refresh' : 'projectShared.register'), 'summary'));
  const form = stack(), status = stack(), retry = stack(); root.append(status, retry, form);
  if (!ownsContinuityScope(scope)) return root;
  const source = sourceField(form, dispatch.humanTurns, scope.conversationId, entry ? 'sharedRefreshSource' : 'sharedSource');
  const path = field(form, entry ? 'sharedReplacement' : 'sharedPath', t(entry ? 'projectShared.replacement' : 'projectShared.path'));
  const purpose = entry ? null : field(form, 'sharedPurpose', t('projectShared.purpose'));
  const revision = entry ? null : field(form, 'sharedExpectedRevision', t('projectShared.expectedRevision'), { value: '0', number: true });
  const consent = createChoice({ label: t(entry ? 'projectShared.refreshConsent' : 'projectShared.consent'), name: entry ? 'sharedRefreshConsent' : 'sharedConsent' });
  const submit = createButton({ label: t(entry ? 'projectShared.refresh' : 'projectShared.register'), disabled: true, onClick: () => {
    if (pending || !consent.input.checked) return;
    const authorizationSource = source.read();
    const expectedRevision = revision ? Number(revision.value) : 0;
    if ((!entry && (!authorizationSource || !purpose?.value.trim() || !safeRelative(path.value.trim())
      || !Number.isSafeInteger(expectedRevision) || expectedRevision < 0))
      || (entry && path.value.trim() && (!authorizationSource || !safeRelative(path.value.trim())))) {
      showError(status, new Error(t('projectContinuity.invalid'))); return;
    }
    pending = entry ? { kind: 'refresh', busy: false, key: dispatch.commandId(), coordinatorId: scope.conversationId,
      body: { projectPath: scope.path, reference: { id: entry.id, revision: entry.revision, digest: entry.digest },
        ...(path.value.trim() ? { replacementPath: path.value.trim(), authorizationSource: authorizationSource! } : {}) } }
      : { kind: 'register', busy: false, key: dispatch.commandId(), coordinatorId: scope.conversationId,
        body: { projectPath: scope.path, path: path.value.trim(), purpose: purpose!.value.trim(), expectedRevision, authorizationSource: authorizationSource! } };
    void send();
  } }); form.append(consent.el, submit);
  function sync() {
    for (const control of form.querySelectorAll<HTMLInputElement | HTMLSelectElement>('input,select')) control.disabled = !!pending;
    submit.disabled = !!pending || !consent.input.checked || !ownsContinuityScope(scope);
    retry.replaceChildren();
    if (pending) {
      retry.append(createAlert({ variant: 'warning', title: t('projectShared.title'), body: t('projectContinuity.uncertain') }).el);
      if (pending.body.projectPath === scope.path && pending.coordinatorId === scope.conversationId) retry.append(createButton({
        label: t('projectContinuity.retryExact'), disabled: pending.busy, onClick: () => { void send(); },
      }));
    }
  }
  async function send() {
    const command = pending;
    if (!command || command.busy || command.body.projectPath !== scope.path || command.coordinatorId !== scope.conversationId) return;
    command.busy = true; sync();
    try {
      await assertContinuityScope(scope);
      if (command.kind === 'register') await ports.register(command.body, command.key); else await ports.refresh(command.body, command.key);
      if (pending === command) pending = null;
      if (scope.valid()) { consent.input.checked = false; status.replaceChildren(); onDone(); }
    } catch (reason) { if (scope.valid()) showError(status, reason); }
    finally { command.busy = false; if (scope.valid()) sync(); }
  }
  const edit = (event: Event) => { if (event.target !== consent.input) consent.input.checked = false; sync(); };
  form.addEventListener('input', edit); form.addEventListener('change', edit); sync(); return root;
}
