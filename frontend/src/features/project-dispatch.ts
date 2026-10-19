/** Human-authorized consultation or bounded file work in the coordinator drawer.
 * Server owns grants/results. No render-triggered writes, polling or delivery inference.
 * One uncertain grant survives drawer teardown: retries reuse its exact body/key.
 */
import { createAlert, createButton, createChoice, createField } from '../ui';
import { t } from '../i18n';

import { createDispatchDependencies, dependencyProjection, sameDependencies } from './project-dispatch-dependencies';
import type { CoordinationBinding } from '../api/project-coordination.generated';
import type { ProjectDispatchRequest, ProjectDispatchRetry, ProjectDispatchRevoke,
  ProjectDispatchReviewRequest, ProjectDispatchProfile, ProjectDispatchReworkOf,
  ProjectDispatchPublishRequest, ProjectDispatchView, ProjectDispatchPage, ProjectDispatchListQuery } from '../api/project-dispatch.generated';
import { createAssignmentHistoryNavigation } from './project-assignment-history';

export interface DispatchHumanTurn { turnId: string; projectionRevision: number; actor: 'human'; promptPreview?: string; }
export interface ProjectDispatchPorts {
  recipients(): Array<{ id: string; title: string }>;
  humanTurns(): DispatchHumanTurn[];
  list(path: string, commandId?: string, options?: Pick<ProjectDispatchListQuery, 'cursor' | 'assignmentId'>): Promise<ProjectDispatchPage>;
  grant(request: ProjectDispatchRequest, commandId: string): Promise<ProjectDispatchView>;
  dispatch(request: ProjectDispatchRetry): Promise<ProjectDispatchView>;
  revoke(request: ProjectDispatchRevoke): Promise<ProjectDispatchView>;
  review(request: ProjectDispatchReviewRequest): Promise<ProjectDispatchView>;

  publish(request: ProjectDispatchPublishRequest): Promise<ProjectDispatchView>;
  commandId(): string;
}
interface Scope {
  path: string; conversationId: string; binding: CoordinationBinding;
  valid(): boolean;
  readBinding(path: string): Promise<CoordinationBinding>;
  navigate(id: string): void;
}
type Pending = { request: ProjectDispatchRequest; commandId: string; busy: boolean; attempted: boolean };
let pending: Pending | null = null;

const text = (content: string, tag = 'p') => {
  const node = document.createElement(tag); node.textContent = content; return node;
};
const samePath = (a: string, b: string) => a.replace(/[/\\]+$/, '') === b.replace(/[/\\]+$/, '');


// Client feedback only; the server revalidates paths and actual I/O in the owned worktree.
function scopePaths(value: string): string[] | null {
  const paths = value.split(',').map(path => path.trim()).filter(Boolean);
  if (paths.length > 32 || new Set(paths).size !== paths.length) return null;
  for (const path of paths) {
    if (path === '.') continue; // Explicit whole-project prefix; metadata fences still apply.
    if (path.length > 1024 || /[\\\\:~%\u0000-\u001f]/.test(path)
      || path.split('/').some(part => !part || part === '.' || part === '..'
        || /^(?:\.git|\.worktrees|data|logs|uploads|write-intents-v1|modifications\.json|project\.json)$/i.test(part)
        || part.toLowerCase().startsWith('.tofu'))) return null;
  }
  return paths;
}
const coveredPath = (path: string, prefixes: string[]) => prefixes.some(prefix =>
  prefix === '.' || path === prefix || path.startsWith(prefix + '/'));

export function createProjectDispatch(ports: ProjectDispatchPorts, scope: Scope): HTMLElement {
  const root = document.createElement('section'); root.className = 'ui-stack';
  root.dataset.testid = 'project-dispatch';
  const disclosure = document.createElement('details');
  disclosure.append(text(t('projectDispatch.title'), 'summary'));
  const form = document.createElement('form'); form.className = 'ui-stack';
  const fields = document.createElement('div'); fields.className = 'ui-stack';
  const inputs: Array<HTMLInputElement | HTMLSelectElement> = [];
  const warning = createAlert({ variant: 'warning', title: t('projectDispatch.boundary'),
    body: t('projectDispatch.warning') });
  form.append(warning.el);
  const add = (name: string, label: string, options: Parameters<typeof createField>[0] = { label }) => {
    const field = createField({ required: true, ...options, label });
    (field.control as HTMLInputElement).name = name;
    const control = field.control as HTMLInputElement | HTMLSelectElement;
    inputs.push(control); fields.append(field.el); return control;
  };
  const humanTurns = ports.humanTurns().filter(turn => turn.actor === 'human' && turn.projectionRevision > 0);
  const source = add('turnId', t('projectDispatch.sourceTurn'), humanTurns.length ? {
    label: '', choices: [{ value: '', label: t('projectDispatch.choose') }, ...humanTurns.map(turn => ({
      value: turn.turnId, label: turn.promptPreview?.trim()
        ? `${turn.promptPreview.trim().slice(0, 160)} · ${turn.turnId} · ${turn.projectionRevision}`
        : `${turn.turnId} · ${turn.projectionRevision}` }))],
  } : { label: '', hint: t('projectDispatch.sourceHint') });
  const revision = add('projectionRevision', t('projectDispatch.sourceRevision'), { label: '', inputType: 'number' });
  if (humanTurns.length) {
    (revision as HTMLInputElement).readOnly = true;
    source.addEventListener('change', () => {
      revision.value = String(humanTurns.find(turn => turn.turnId === source.value)?.projectionRevision || '');
    });
  }
  const recipient = add('recipientId', t('projectDispatch.recipient'), { label: '', choices: [
    { value: '', label: t('projectDispatch.choose') },
    ...ports.recipients().filter(item => item.id !== scope.conversationId).map(item => ({ value: item.id, label: item.title || item.id })),
  ] });
  const brief = add('brief', t('projectDispatch.brief'));
  const model = add('model', t('projectDispatch.model'), { label: '', hint: t('projectDispatch.modelHint') });

  const mode = add('mode', t('projectDispatch.mode'), { label: '', choices: [
    { value: 'consult', label: t('projectDispatch.consultMode') },
    { value: 'files', label: t('projectDispatch.filesMode') },
  ] });
  // Keep the hidden wrapper unstyled: .ui-stack display:flex overrides UA [hidden].
  const fileFields = document.createElement('div');
  const fileStack = document.createElement('div'); fileStack.className = 'ui-stack'; fileFields.append(fileStack);
  const readPaths = add('readPaths', t('projectDispatch.readPaths'), { label: '', hint: t('projectDispatch.pathsHint') });
  const writePaths = add('writePaths', t('projectDispatch.writePaths'), { label: '', required: false, hint: t('projectDispatch.writePathsHint') });
  const toolCalls = add('maxToolCalls', t('projectDispatch.maxToolCalls'), { label: '', inputType: 'number' });
  for (const control of [readPaths, writePaths, toolCalls]) {
    fileStack.append(control.closest('.ui-field')!);
  }
  const reader = createChoice({ label: t('projectDispatch.reader'), checked: true, disabled: true, name: 'read_files' });
  const writer = createChoice({ label: t('projectDispatch.writer'), checked: false, name: 'write_file' });
  const editor = createChoice({ label: t('projectDispatch.editor'), checked: false, name: 'edit_file' });
  inputs.push(writer.input, editor.input);
  fileStack.append(reader.el, writer.el, editor.el,
    createAlert({ variant: 'warning', title: t('projectDispatch.filesBoundary'), body: t('projectDispatch.filesWarning') }).el);
  fields.append(fileFields);
  let reworkOf: ProjectDispatchReworkOf | undefined;
  const reworkHint = text('', 'small'); fields.append(reworkHint);
  const setRework = (value?: ProjectDispatchReworkOf) => {
    reworkOf = value;
    reworkHint.textContent = value ? t('projectDispatch.reworkDraft', {
      assignmentId: value.assignmentId, grantRevision: value.grantRevision, resultRevision: value.resultRevision,
    }) : '';
  };
  const restoreProfile = (profile?: ProjectDispatchProfile) => {
    mode.value = profile?.mode || 'consult';
    readPaths.value = profile?.readPaths.join(', ') || '';
    writePaths.value = profile?.writePaths.join(', ') || '';
    toolCalls.value = profile ? String(profile.maxToolCalls) : '';
    writer.input.checked = !!profile?.tools.includes('write_file');
    editor.input.checked = !!profile?.tools.includes('edit_file');
  };
  const tokens = add('maxTokens', t('projectDispatch.maxTokens'), { label: '', inputType: 'number' });
  const requests = add('maxModelRequests', t('projectDispatch.maxModelRequests'), { label: '', inputType: 'number' });
  const timeout = add('timeout', t('projectDispatch.timeout'), { label: '', inputType: 'number' });
  const deadline = text('', 'small'); fields.append(deadline);
  timeout.addEventListener('input', () => {
    const seconds = Number(timeout.value);
    const date = new Date(Date.now() + seconds * 1000);
    deadline.textContent = seconds > 0 && Number.isFinite(date.getTime())
      ? t('projectDispatch.deadline', { deadline: date.toISOString() }) : '';
  });
  const consent = createChoice({ label: t('projectDispatch.consent'), checked: false, name: 'consent' });
  inputs.push(consent.input); fields.append(consent.el); form.append(fields);
  const feedback = document.createElement('div'); feedback.setAttribute('role', 'status');
  const submit = createButton({ label: t('projectDispatch.submit'), variant: 'primary', type: 'submit', disabled: true });
  form.append(submit, feedback); disclosure.append(form); root.append(disclosure);
  const receipts = document.createElement('div'); receipts.className = 'ui-stack';
  const refresh = createButton({ label: t('projectDispatch.refresh'), onClick: () => { void load(); } });
  let historyCursor: string | undefined;
  root.append(refresh, receipts);
  const owns = (binding: CoordinationBinding) => binding.availability === 'available'
    && binding.conversationId === scope.conversationId && samePath(binding.projectPath, scope.path);
  const ownPending = () => pending && samePath(pending.request.projectPath, scope.path)
    && pending.request.coordinatorId === scope.conversationId;
  let busy = false;
  let readSequence = 0;
  const fail = (message: string) => feedback.replaceChildren(createAlert({ variant: 'error', title: message }).el);

  const dependencies = createDispatchDependencies(scope.path,
    () => scope.valid() && owns(scope.binding) && !pending && !busy,
    () => { consent.input.checked = false; feedback.replaceChildren(); sync(); });
  consent.el.before(dependencies.el);
  function sync() {
    for (const input of inputs) input.disabled = !!pending || busy || !owns(scope.binding);

    dependencies.disable(!!pending || busy || !owns(scope.binding) || !scope.valid());

    fileFields.hidden = mode.value !== 'files';
    reader.input.checked = true; // Disabled checkboxes are still reset by form.reset().
    for (const input of [readPaths, writePaths, toolCalls, writer.input, editor.input]) {
      input.disabled ||= mode.value !== 'files';
    }
    submit.disabled = busy || !!pending?.busy || !owns(scope.binding)
      || (!!pending && !ownPending()) || (!pending && !consent.input.checked);
    submit.textContent = t(ownPending() ? 'projectDispatch.retryExact' : 'projectDispatch.submit');
  }
  if (ownPending() && pending) {
    const req = pending.request;
    source.value = req.authorizationSource.turnId; revision.value = String(req.authorizationSource.projectionRevision);
    recipient.value = req.recipientId; brief.value = req.brief; model.value = req.model;
    tokens.value = String(req.budget.maxTokens); requests.value = String(req.budget.maxModelRequests);

    restoreProfile(req.profile); setRework(req.reworkOf); dependencies.restore(req.dependencies);
    deadline.textContent = t('projectDispatch.deadline', { deadline: new Date(req.budget.deadline * 1000).toISOString() });
    consent.input.checked = true;
    feedback.append(text(t('projectDispatch.uncertain')));
  } else if (pending) feedback.append(text(t('projectDispatch.otherPending')));
  if (!owns(scope.binding)) feedback.append(text(t('projectDispatch.coordinatorRequired')));
  consent.input.addEventListener('change', sync);
  // Changing any grant scope invalidates previously checked consent.
  for (const input of inputs.filter(input => input !== consent.input)) {
    for (const event of ['input', 'change']) input.addEventListener(event, () => {
      if (!pending) {
        consent.input.checked = false;
        if (owns(scope.binding)) feedback.replaceChildren();
      }
      sync();
    });
  }
  sync();

  async function assertScope() {
    if (!scope.valid()) throw new Error(t('projectDispatch.stale'));
    const current = await scope.readBinding(scope.path);
    if (!scope.valid() || !owns(current) || current.revision !== scope.binding.revision) {
      throw new Error(t('projectDispatch.stale'));
    }
  }
  form.addEventListener('submit', event => {
    event.preventDefault(); void grant();
  });
  async function grant() {
    if (busy || pending?.busy || !scope.valid() || !owns(scope.binding) || (pending && !ownPending())) return;
    if (!pending && !consent.input.checked) { fail(t('projectDispatch.consentRequired')); return; }
    let retained: Pending | null = null;
    try {
      if (!pending) {
        const sourceRevision = Number(revision.value);
        const maxTokens = Number(tokens.value); const maxModelRequests = Number(requests.value);
        const seconds = Number(timeout.value);
        const liveTurns = ports.humanTurns();
        if (!source.value.trim() || !Number.isSafeInteger(sourceRevision) || sourceRevision < 1
          || (liveTurns.length > 0 && !liveTurns.some(turn => turn.actor === 'human' && turn.turnId === source.value
            && turn.projectionRevision === sourceRevision))
          || !ports.recipients().some(item => item.id === recipient.value && item.id !== scope.conversationId)
          || !brief.value.trim() || brief.value.length > 4096 || !model.value.trim() || model.value.length > 256
          || !Number.isSafeInteger(maxTokens) || maxTokens < 1 || maxTokens > 2147483647
          || !Number.isSafeInteger(maxModelRequests) || maxModelRequests < 1 || maxModelRequests > 64
          || !Number.isFinite(new Date(Date.now() + seconds * 1000).getTime()) || seconds <= 0) {
          fail(t('projectDispatch.invalid')); return;
        }
        let profile: ProjectDispatchProfile | undefined;
        if (mode.value === 'files') {
          const reads = scopePaths(readPaths.value); const writes = scopePaths(writePaths.value);
          const maxToolCalls = Number(toolCalls.value);
          if (!reads?.length || !writes || !toolCalls.value.trim() || !Number.isSafeInteger(maxToolCalls) || maxToolCalls < 1 || maxToolCalls > 512
            || ((writer.input.checked || editor.input.checked) && !writes.length)
            || writes.some(path => !coveredPath(path, reads))) {
            fail(t('projectDispatch.invalidProfile')); return;
          }
          profile = { mode: 'files', readPaths: reads, writePaths: writes, maxToolCalls,
            tools: ['read_files', ...(writer.input.checked ? ['write_file' as const] : []),
              ...(editor.input.checked ? ['edit_file' as const] : [])] };
        } else if (mode.value !== 'consult') { fail(t('projectDispatch.invalidProfile')); return; }
        const dependencyPins = dependencies.references();
        pending = { commandId: ports.commandId(), busy: false, attempted: false, request: {
          projectPath: scope.path, coordinatorId: scope.conversationId, recipientId: recipient.value,
          authorizationSource: { conversationId: scope.conversationId, turnId: source.value, projectionRevision: sourceRevision },
          brief: brief.value, model: model.value.trim(), budget: { maxTokens, maxModelRequests, deadline: Date.now() / 1000 + seconds },

          ...(profile ? { profile } : {}), ...(reworkOf ? { reworkOf: { ...reworkOf } } : {}),

          ...(dependencyPins.length ? { dependencies: dependencyPins } : {}),
        } };
      }
      retained = pending;
      busy = true; retained.busy = true; sync();
      await assertScope();
      retained.attempted = true; // Retain forever after transport starts unless a server view proves this command.
      const response = await ports.grant(retained.request, retained.commandId);
      // Only an exact, confirmed server view releases the uncertain command.
      if (!response?.assignmentId || !sameRequest(response.request, retained.request)) {
        throw new Error(t('projectDispatch.uncertain'));
      }
      if (pending === retained) pending = null;
      if (scope.valid()) {
        form.reset(); restoreProfile(); setRework(); dependencies.restore(); deadline.textContent = ''; feedback.replaceChildren(text(t('projectDispatch.confirmed')));
        await load();
      }
    } catch (reason) {
      if (retained && !retained.attempted && pending === retained) pending = null;
      if (scope.valid()) fail(`${retained?.attempted ? t('projectDispatch.uncertain') : ''} ${reason instanceof Error ? reason.message : String(reason)}`);
    } finally { if (retained) retained.busy = false; busy = false; sync(); }
  }
  function sameRequest(a: ProjectDispatchRequest | undefined, b: ProjectDispatchRequest) {
    return a?.authorizationSource && a.budget && a.projectPath === b.projectPath && a.coordinatorId === b.coordinatorId && a.recipientId === b.recipientId
      && a.brief === b.brief && a.model === b.model && a.authorizationSource.conversationId === b.authorizationSource.conversationId
      && a.authorizationSource.turnId === b.authorizationSource.turnId
      && a.authorizationSource.projectionRevision === b.authorizationSource.projectionRevision
      && a.budget.maxTokens === b.budget.maxTokens && a.budget.maxModelRequests === b.budget.maxModelRequests
      && a.budget.deadline === b.budget.deadline
      && sameProfile(a.profile, b.profile)

      && sameDependencies(a.dependencies, b.dependencies)
      && (a.reworkOf === undefined && b.reworkOf === undefined || !!a.reworkOf && !!b.reworkOf
        && a.reworkOf.assignmentId === b.reworkOf.assignmentId
        && a.reworkOf.grantRevision === b.reworkOf.grantRevision
        && a.reworkOf.resultRevision === b.reworkOf.resultRevision);
  }
  function sameProfile(a: ProjectDispatchProfile | undefined, b: ProjectDispatchProfile | undefined) {
    if (!a || !b) return a === b;
    const sameList = (left: string[], right: string[]) => Array.isArray(left) && Array.isArray(right)
      && left.length === right.length && left.every((value, index) => value === right[index]);
    return a.mode === b.mode && a.maxToolCalls === b.maxToolCalls
      && sameList(a.tools, b.tools) && sameList(a.readPaths, b.readPaths) && sameList(a.writePaths, b.writePaths);
  }
  async function load() {
    if (!scope.valid() || refresh.disabled) return;
    refresh.disabled = true; const sequence = ++readSequence;
    try {
      const uncertain = ownPending() && pending?.attempted && !pending.busy ? pending : null;
      const [page, reconciliation] = await Promise.all([
        ports.list(scope.path, undefined, historyCursor ? { cursor: historyCursor } : undefined),
        uncertain ? ports.list(scope.path, uncertain.commandId).catch(() => null) : Promise.resolve(null),
      ]);
      if (!scope.valid() || sequence !== readSequence) return;
      if (!Array.isArray(page?.assignments)) throw new Error(t('projectDispatch.unavailable'));

      if (uncertain && pending === uncertain && reconciliation?.assignments?.length === 1
        && reconciliation.assignments[0].assignmentId && sameRequest(reconciliation.assignments[0].request, uncertain.request)) {
        pending = null; form.reset(); restoreProfile(); setRework(); dependencies.restore(); deadline.textContent = '';
        feedback.replaceChildren(text(t('projectDispatch.confirmed'))); sync();
      } // An absent/error/mismatched GET never proves that execution did not occur.
      dependencies.update(page.assignments);
      receipts.replaceChildren();
      receipts.append(createAssignmentHistoryNavigation(page, historyCursor, cursor => {
        if (!scope.valid() || refresh.disabled) return;
        historyCursor = cursor; void load();
      }));
      if (!page.assignments.length) receipts.append(text(t('projectDispatch.empty')));
      for (const view of page.assignments) {
        if (!samePath(view.request.projectPath, scope.path)) continue;
        const card = document.createElement('article'); card.className = 'ui-card ui-stack ui-stack--tight';
        card.append(text(view.request.brief, 'strong'), text(view.assignmentId, 'small'));

        const projectedDependencies = dependencyProjection(view);
        if (projectedDependencies) card.append(projectedDependencies);
        card.append(text(t('projectDispatch.execution', { status: view.execution?.status || view.dispatch || t('projectDispatch.notStarted') })));
        const reviewDecision = view.review
          ? t(view.review.decision === 'accept' ? 'projectDispatch.accept' : view.review.decision === 'rework' ? 'projectDispatch.rework' : 'projectDispatch.reject')
          : t('projectDispatch.notReviewed');
        card.append(text(t('projectDispatch.review', { decision: view.review && view.review.resultRevision !== view.execution?.resultRevision
          ? t('projectDispatch.staleReview', { decision: reviewDecision, revision: view.review.resultRevision }) : reviewDecision })));
        // canonicalDelivery is a server-validated receipt, never completion/acceptance.
        const canonicalDelivered = view.canonicalDelivery === true && view.delivery?.status === 'delivered';
        card.append(text(t(canonicalDelivered ? 'projectDispatch.delivered' : 'projectDispatch.notDelivered')));
        if (view.publication) card.append(text(t('projectDispatch.publicationRequested', {
          revision: view.publication.resultRevision,
        })));
        else if (view.request.profile?.mode === 'files' && !canonicalDelivered) {
          card.append(text(t('projectDispatch.heldAuthorization')));
        }
        if (view.delivery) {
          card.append(text(t('projectDispatch.deliveryState', { status: view.delivery.status })));
          if (view.delivery.reason) card.append(text(t('projectDispatch.deliveryReason', { reason: view.delivery.reason })));
          if (view.delivery.checkpointSha) card.append(text(t('projectDispatch.checkpoint', { sha: view.delivery.checkpointSha }), 'small'));
          if (view.delivery.targetSha) card.append(text(t('projectDispatch.target', { sha: view.delivery.targetSha }), 'small'));
        }
        if (view.revoked) card.append(text(t('projectDispatch.revoked')));
        if (view.execution?.resultRevision) card.append(text(t('projectDispatch.resultRevision', { revision: view.execution.resultRevision })));
        card.append(createButton({ label: t('projectCoordination.openWorker'), size: 'sm', onClick: () => {
          if (scope.valid()) scope.navigate(view.request.recipientId);
        } }));
        const mutable = owns(scope.binding) && view.request.coordinatorId === scope.conversationId && !view.revoked;
        const buttons: HTMLButtonElement[] = [];
        let acting = false;
        const action = (label: string, run: () => Promise<ProjectDispatchView>) => {
          const button = createButton({ label, size: 'sm', onClick: async () => {
            if (acting || !scope.valid()) return;
            acting = true; buttons.forEach(item => { item.disabled = true; });
            try { await assertScope(); if (!scope.valid()) return; await run(); if (scope.valid()) await load(); }
            catch (reason) { if (scope.valid()) fail(reason instanceof Error ? reason.message : String(reason)); }
            finally { acting = false; buttons.forEach(item => { item.disabled = !scope.valid(); }); }
          } }); buttons.push(button); card.append(button);
        };
        if (mutable) {

          const currentReview = view.review && view.review.resultRevision === view.execution?.resultRevision
            && view.execution?.status === 'completed' && Number.isSafeInteger(view.execution.resultRevision)
            && view.execution.resultRevision! > 0;
          if (currentReview && view.review?.decision === 'accept' && view.request.profile?.mode === 'files'
            && !!view.delivery?.checkpointSha && !canonicalDelivered && !view.publication) {
            action(t('projectDispatch.publish'), () => ports.publish({ projectPath: scope.path,
              assignmentId: view.assignmentId, expectedRevision: view.grantRevision,
              resultRevision: view.execution!.resultRevision!, expectedCheckpointSha: view.delivery!.checkpointSha }));
          }
          if (currentReview && view.review?.decision === 'rework') {
            const draft = createButton({ label: t('projectDispatch.prepareRework'), size: 'sm', onClick: () => {
              if (!scope.valid() || pending || busy || acting || !owns(scope.binding)) return;
              // A local draft, not review/grant/dispatch/publication authority.
              form.reset(); restoreProfile(view.request.profile); dependencies.restore();
              recipient.value = view.request.recipientId; brief.value = view.request.brief;
              source.value = ''; revision.value = ''; model.value = '';
              tokens.value = ''; requests.value = ''; timeout.value = ''; toolCalls.value = '';
              consent.input.checked = false; deadline.textContent = '';
              setRework({ assignmentId: view.assignmentId, grantRevision: view.grantRevision,
                resultRevision: view.review!.resultRevision });
              disclosure.open = true; feedback.replaceChildren(text(t('projectDispatch.freshAuthorization')));
              sync(); source.focus();
            } });
            buttons.push(draft); card.append(draft);
          }
          if (!view.execution && view.dispatch !== 'aborted') action(t('projectDispatch.dispatch'), () => ports.dispatch({
            projectPath: scope.path, assignmentId: view.assignmentId,
          }));
          action(t('projectDispatch.revoke'), () => ports.revoke({ projectPath: scope.path,
            assignmentId: view.assignmentId, expectedRevision: view.grantRevision }));
          if (!view.review && view.execution?.status === 'completed' && Number.isSafeInteger(view.execution.resultRevision)
            && view.execution.resultRevision! > 0) {
            const note = createField({ label: t('projectDispatch.note') }); card.append(note.el);
            for (const decision of ['accept', 'rework', 'reject'] as const) {
              action(t(decision === 'accept' ? 'projectDispatch.accept' : decision === 'rework' ? 'projectDispatch.rework' : 'projectDispatch.reject'), () => ports.review({
                projectPath: scope.path, assignmentId: view.assignmentId, expectedRevision: view.grantRevision,
                resultRevision: view.execution!.resultRevision!, decision, note: (note.control as HTMLInputElement).value,
              }));
            }
          }
        }
        receipts.append(card);
      }
    } catch (reason) {
      if (scope.valid()) {
        dependencies.update(null);
        receipts.replaceChildren(createAlert({ variant: 'warning', title: t('projectDispatch.unavailable'),
          body: reason instanceof Error ? reason.message : String(reason) }).el);
      }
    } finally { refresh.disabled = false; }
  }
  void load(); // A read only: grants/actions always require a human click.
  return root;
}
