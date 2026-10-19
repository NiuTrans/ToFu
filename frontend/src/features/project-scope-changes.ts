/** P3 scope reduction in the existing coordination drawer. Dispatch supplies
 * current grants; scope-change schema owns writes and canonical stop evidence.
 * One uncertain write is retained across drawer close; no automatic retry.
 */
import { createAlert, createChoice, createEmptyState } from '../ui';
import { t } from '../i18n';
import type { ProjectScopeChangeRequest, ProjectScopeChangeRetry, ProjectScopeChangeView } from '../api/project-continuity.generated';
import { PROJECT_SCOPE_TARGET_LIMIT } from '../api/project-continuity.generated';
import type { ProjectDispatchPorts } from './project-dispatch';
import { assertContinuityScope, createContinuityReadFence, ownsContinuityScope, sameCommandScope,
  type ContinuityScope, type RetainedCommand } from './project-continuity-scope';
import { createContinuityButton as createButton, field, showError, sourceField, stack, text } from './project-continuity-view';
export interface ProjectScopeChangePorts {
  list(path: string): Promise<ProjectScopeChangeView[]>;
  apply(body: ProjectScopeChangeRequest, key: string): Promise<ProjectScopeChangeView>;
  retry(body: ProjectScopeChangeRetry, key: string): Promise<ProjectScopeChangeView>;
}
let pending: (RetainedCommand<ProjectScopeChangeRequest> & { kind: 'apply' })
  | (RetainedCommand<ProjectScopeChangeRetry> & { kind: 'retry'; coordinatorId: string }) | null = null;
export function createProjectScopeChanges(ports: ProjectScopeChangePorts, dispatch: ProjectDispatchPorts, scope: ContinuityScope): HTMLElement {
  const root = stack('section'); root.dataset.testid = 'project-scope-changes';
  root.append(text(t('projectScope.title'), 'h3'), text(t('projectScope.help')));
  if (!ownsContinuityScope(scope)) return root;
  const status = stack(), form = stack(), targets = stack(), history = stack(), retryHost = stack();
  const source = sourceField(form, dispatch.humanTurns, scope.conversationId, 'scopeSource');
  const note = field(form, 'scopeNote', t('projectScope.note'));
  const selected = new Map<string, number>();

  let allTargets: ProjectScopeChangeRequest['targets'] = [];
  let targetSetComplete = false;
  const stopConsent = createChoice({ label: t('projectScope.stopConsent'), name: 'scopeStopConsent' });
  const stopConfirm = createChoice({ label: t('projectScope.stopConfirm'), name: 'scopeStopConfirm' });
  const consent = createChoice({ label: t('projectScope.consent'), name: 'scopeConsent' });
  const submit = createButton({ label: t('projectScope.submit'), variant: 'danger', disabled: true, onClick: () => {
    const authorizationSource = source.read();
    if (!authorizationSource || !consent.input.checked || !selected.size || pending) return;
    pending = { kind: 'apply', busy: false, key: dispatch.commandId(), body: {
      projectPath: scope.path, coordinatorId: scope.conversationId, authorizationSource,
      kind: 'shrink', note: note.value.trim(), targets: [...selected].map(([assignmentId, grantRevision]) => ({ assignmentId, grantRevision })),
    } };
    void send();
  } });
  const stop = createButton({ label: t('projectScope.stop'), variant: 'danger', disabled: true, onClick: () => {
    const authorizationSource = source.read();
    if (!authorizationSource || !stopConsent.input.checked || !stopConfirm.input.checked || pending || !targetSetComplete) return;
    pending = { kind: 'apply', busy: false, key: dispatch.commandId(), body: {
      projectPath: scope.path, coordinatorId: scope.conversationId, authorizationSource, kind: 'stop_project',
      note: note.value.trim(), targets: structuredClone(allTargets),
    } }; void send();
  } });
  form.append(targets, consent.el, submit, stopConsent.el, stopConfirm.el, stop); root.append(status, retryHost, form, history);
  const fence = createContinuityReadFence(scope.valid);
  const synchronize = () => {
    for (const input of form.querySelectorAll<HTMLInputElement | HTMLSelectElement>('input,select')) input.disabled = !!pending;
    submit.disabled = !!pending || !consent.input.checked || !source.read() || !selected.size || !ownsContinuityScope(scope);

    stop.disabled = !!pending || !targetSetComplete || !stopConsent.input.checked || !stopConfirm.input.checked || !source.read() || !ownsContinuityScope(scope);
    retryHost.replaceChildren();
    if (pending) {
      retryHost.append(createAlert({ variant: 'warning', title: t('projectScope.title'), body: t('projectContinuity.uncertain') }).el);
      const belongs = pending.kind === 'apply' ? sameCommandScope(pending.body, scope)
        : pending.body.projectPath === scope.path && pending.coordinatorId === scope.conversationId;
      if (belongs) retryHost.append(createButton({ label: t('projectContinuity.retryExact'), disabled: pending.busy,
        onClick: () => { void send(); } }));
    }
  };
  form.addEventListener('input', event => {
    if (![consent.input, stopConsent.input, stopConfirm.input].includes(event.target as HTMLInputElement)) {
      consent.input.checked = false; stopConsent.input.checked = false; stopConfirm.input.checked = false;
    }
    synchronize();
  });
  form.addEventListener('change', event => {
    if (![consent.input, stopConsent.input, stopConfirm.input].includes(event.target as HTMLInputElement)) {
      consent.input.checked = false; stopConsent.input.checked = false; stopConfirm.input.checked = false;
    }
    synchronize();
  });
  function render(rows: ProjectScopeChangeView[]) {
    history.replaceChildren();
    for (const row of rows.slice(0, 128)) {
      const card = stack('article', true); card.append(text(row.changeId, 'strong'), text(row.request.note));
      card.append(text(t('projectScope.revoked')), text(t(row.stopConfirmed ? 'projectScope.stopped' : 'projectScope.pending')));
      for (const revoked of row.revocations) {
        card.append(text(`${revoked.assignmentId} · r${revoked.grantRevision} → r${revoked.revokedRevision}`, 'small'));
        card.append(text(t('projectScope.cancellation', revoked.cancellation), 'small'));
      }
      for (const notification of row.notifications) card.append(text(`${notification.recipientId} · ` + t('projectScope.notification', {
        state: notification.state, evidence: notification.messageEvidence?.state ?? 'unknown',
      }), 'small'));
      if (!row.stopConfirmed || row.notifications.some(item => item.state !== 'submitted')) card.append(createButton({
        label: t('projectScope.retry'), disabled: !!pending, onClick: () => {
          if (pending || !ownsContinuityScope(scope)) return;
          pending = { kind: 'retry', coordinatorId: scope.conversationId, busy: false, key: dispatch.commandId(),
            body: { projectPath: scope.path, changeId: row.changeId } }; void send();
        },
      }));
      history.append(card);
    }
  }
  async function send() {
    const command = pending; if (!command || command.busy || !scope.valid()) return;
    const belongs = command.kind === 'apply' ? sameCommandScope(command.body, scope)
      : command.body.projectPath === scope.path && command.coordinatorId === scope.conversationId;
    if (!belongs) return;
    command.busy = true; synchronize();
    try {
      await assertContinuityScope(scope);
      const result = command.kind === 'apply' ? await ports.apply(command.body, command.key) : await ports.retry(command.body, command.key);
      if (pending === command) pending = null;
      if (scope.valid()) { consent.input.checked = false; selected.clear(); render([result]); await load(); }
    } catch (reason) { if (scope.valid()) showError(status, reason); }
    finally { command.busy = false; if (scope.valid()) synchronize(); }
  }
  async function load() {
    if (!scope.valid()) return;
    const epoch = fence.begin(); consent.input.checked = false; stopConsent.input.checked = false;
    stopConfirm.input.checked = false; allTargets = []; targetSetComplete = false;
    selected.clear(); submit.disabled = true; stop.disabled = true;
    try {
      const [grants, rows] = await Promise.all([dispatch.list(scope.path), ports.list(scope.path)]);
      if (!fence.accepts(epoch)) return;
      const belongs = (item: typeof grants.assignments[number]) => !item.revoked && item.request.coordinatorId === scope.conversationId;
      const candidates = grants.assignments.filter(belongs);
      let next = grants.olderCursor;
      const visited = new Set<string>();
      while (next && candidates.length <= PROJECT_SCOPE_TARGET_LIMIT && visited.size < PROJECT_SCOPE_TARGET_LIMIT) {
        if (visited.has(next)) throw new Error(t('projectCoordination.error'));
        visited.add(next);
        const page = await dispatch.list(scope.path, undefined, { cursor: next });
        if (!fence.accepts(epoch)) return;
        candidates.push(...page.assignments.filter(belongs)); next = page.olderCursor;
      }
      targetSetComplete = !next && candidates.length <= PROJECT_SCOPE_TARGET_LIMIT;
      targets.replaceChildren();

      allTargets = candidates.slice(0, PROJECT_SCOPE_TARGET_LIMIT)
        .map(item => ({ assignmentId: item.assignmentId, grantRevision: item.grantRevision }));
      if (!targetSetComplete) targets.append(text(t('projectScope.partial', { limit: PROJECT_SCOPE_TARGET_LIMIT }), 'small'));
      for (const grant of candidates.slice(0, PROJECT_SCOPE_TARGET_LIMIT)) {
        const choice = createChoice({ name: 'scopeTarget',
          label: `${grant.assignmentId} · r${grant.grantRevision} · ${grant.request.brief}`,
          onChange: checked => {
            consent.input.checked = false;

            stopConsent.input.checked = false; stopConfirm.input.checked = false;
            if (checked) selected.set(grant.assignmentId, grant.grantRevision); else selected.delete(grant.assignmentId);
            synchronize();
          } });
        choice.input.dataset.assignmentId = grant.assignmentId; targets.append(choice.el);
      }
      if (!targets.childElementCount) targets.append(createEmptyState({ title: t('projectScope.empty') }));
      render(rows); status.replaceChildren(); synchronize();
    } catch (reason) { if (fence.accepts(epoch)) showError(status, reason); }
  }
  root.append(createButton({ label: t('projectContinuity.refresh'), onClick: () => { void load(); } }));
  synchronize(); void load(); return root;
}
