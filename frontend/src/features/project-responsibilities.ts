/** Finite standing consultation subscriptions. Existing automation/dispatch
 * remain execution authorities. This surface grants no file work or publishing.
 * Explicit cron, model, worker, human source and finite allowance are required.
 */
import { createAlert, createChoice, createEmptyState } from '../ui';
import { t } from '../i18n';
import type { ProjectResponsibilityCreate, ProjectResponsibilityCommand, ProjectResponsibilityView } from '../api/project-continuity.generated';
import type { ProjectDispatchPorts } from './project-dispatch';
import { assertContinuityScope, createContinuityReadFence, ownsContinuityScope, sameCommandScope,
  type ContinuityScope, type RetainedCommand } from './project-continuity-scope';
import { createContinuityButton as createButton, field, positiveInteger, showError, sourceField, stack, text } from './project-continuity-view';
export interface ProjectResponsibilityPorts {
  list(path: string): Promise<ProjectResponsibilityView[]>;
  create(body: ProjectResponsibilityCreate, key: string): Promise<ProjectResponsibilityView>;
  pause(body: ProjectResponsibilityCommand, key: string): Promise<ProjectResponsibilityView>;
  cancel(body: ProjectResponsibilityCommand, key: string): Promise<ProjectResponsibilityView>;
  reconcile(body: ProjectResponsibilityCommand, key: string): Promise<ProjectResponsibilityView>;
}
type Pending = (RetainedCommand<ProjectResponsibilityCreate> & { kind: 'create' })
  | (RetainedCommand<ProjectResponsibilityCommand> & { kind: 'pause' | 'cancel' | 'reconcile'; coordinatorId: string });
let pending: Pending | null = null;
export function createProjectResponsibilities(ports: ProjectResponsibilityPorts, dispatch: ProjectDispatchPorts, scope: ContinuityScope): HTMLElement {
  const root = stack('section'); root.dataset.testid = 'project-responsibilities';
  root.append(text(t('projectResponsibility.title'), 'h3'), text(t('projectResponsibility.help')));
  if (!ownsContinuityScope(scope)) return root;
  const form = stack(), status = stack(), retries = stack(), history = stack();
  const name = field(form, 'responsibilityName', t('projectResponsibility.name'));
  const source = sourceField(form, dispatch.humanTurns, scope.conversationId, 'responsibilitySource');
  const recipient = field(form, 'responsibilityRecipient', t('projectDispatch.recipient'), { choices: [
    { value: '', label: t('projectDispatch.choose') },
    ...dispatch.recipients().filter(item => item.id !== scope.conversationId).map(item => ({ value: item.id, label: item.title || item.id })),
  ] });
  const brief = field(form, 'responsibilityBrief', t('projectDispatch.brief'));
  const model = field(form, 'responsibilityModel', t('projectDispatch.model'));
  const maxRuns = field(form, 'responsibilityMaxRuns', t('projectResponsibility.maxRuns'), { number: true });
  const totalTokens = field(form, 'responsibilityTotalTokens', t('projectResponsibility.totalTokens'), { number: true });
  const perRunTokens = field(form, 'responsibilityPerRunTokens', t('projectDispatch.maxTokens'), { number: true });
  const perRunRequests = field(form, 'responsibilityPerRunRequests', t('projectDispatch.maxModelRequests'), { number: true });
  const perRunSeconds = field(form, 'responsibilityPerRunSeconds', t('projectResponsibility.perRunSeconds'), { number: true });
  const deadline = field(form, 'responsibilityDeadline', t('projectResponsibility.deadline'));
  const schedule = field(form, 'responsibilitySchedule', t('projectResponsibility.schedule'));
  const timezone = field(form, 'responsibilityTimezone', t('projectResponsibility.timezone'), { value: 'UTC' });
  const consent = createChoice({ label: t('projectResponsibility.consent'), name: 'responsibilityConsent' });
  const submit = createButton({ label: t('projectResponsibility.submit'), variant: 'primary', disabled: true, onClick: () => {
    if (!consent.input.checked || pending) return;
    const authorizationSource = source.read();
    const runs = positiveInteger(maxRuns, 128), total = positiveInteger(totalTokens, 2147483647);
    const tokens = positiveInteger(perRunTokens, 10000000), requests = positiveInteger(perRunRequests, 64);
    const seconds = positiveInteger(perRunSeconds, 86400), end = Date.parse(deadline.value) / 1000;
    if (!authorizationSource || !runs || !total || !tokens || !requests || !seconds || total < tokens
      || !Number.isFinite(end) || end <= Date.now() / 1000 || !/Z$/.test(deadline.value)
      || !name.value.trim() || !brief.value.trim() || !model.value.trim() || !schedule.value.trim() || !timezone.value.trim()
      || !dispatch.recipients().some(item => item.id === recipient.value && item.id !== scope.conversationId)) {
      showError(status, new Error(t('projectContinuity.invalid'))); return;
    }
    pending = { kind: 'create', busy: false, key: dispatch.commandId(), body: {
      projectPath: scope.path, name: name.value.trim(), assignment: {
        projectPath: scope.path, coordinatorId: scope.conversationId, recipientId: recipient.value,
        authorizationSource, brief: brief.value.trim(), model: model.value.trim(),
        budget: { maxTokens: tokens, maxModelRequests: requests, deadline: end },
        profile: { mode: 'consult', tools: [], readPaths: [], writePaths: [], maxToolCalls: 0 },
      },
      trigger: { kind: 'schedule', schedule: schedule.value.trim(), timezone: timezone.value.trim(), catchUp: 'skip' },
      allowance: { maxRuns: runs, maxTotalTokens: total, deadline: end }, perRunSeconds: seconds,
    } }; void send();
  } });
  form.append(consent.el, submit); root.append(status, retries, form, history);
  const fence = createContinuityReadFence(scope.valid);
  function belongs(command: Pending) {
    return command.kind === 'create' ? sameCommandScope(command.body.assignment, scope)
      : command.body.projectPath === scope.path && command.coordinatorId === scope.conversationId;
  }
  function synchronize() {
    for (const input of form.querySelectorAll<HTMLInputElement | HTMLSelectElement>('input,select')) input.disabled = !!pending;
    submit.disabled = !!pending || !consent.input.checked || !ownsContinuityScope(scope);
    retries.replaceChildren();
    if (pending) {
      retries.append(createAlert({ variant: 'warning', title: t('projectResponsibility.title'), body: t('projectContinuity.uncertain') }).el);
      if (belongs(pending)) retries.append(createButton({ label: t('projectContinuity.retryExact'), disabled: pending.busy,
        onClick: () => { void send(); } }));
    }
  }
  const edit = (event: Event) => {
    if (event.target !== consent.input) consent.input.checked = false;
    status.replaceChildren(); synchronize();
  };
  form.addEventListener('input', edit); form.addEventListener('change', edit);
  function render(rows: ProjectResponsibilityView[]) {
    history.replaceChildren();
    if (!rows.length) history.append(createEmptyState({ title: t('projectResponsibility.empty') }));
    for (const row of rows.slice(0, 32)) {
      const card = stack('article', true); card.append(text(row.request.name, 'strong'), text(row.responsibilityId, 'small'));
      card.append(text(t(`projectResponsibility.state.${row.state}`)), text(t('projectResponsibility.usage', {
        runs: row.runsReserved, maxRuns: row.request.allowance.maxRuns, tokens: row.tokensReserved, maxTokens: row.request.allowance.maxTotalTokens,
      })));
      card.append(text(`${row.request.assignment.recipientId} · ${row.request.assignment.model} · ${row.request.assignment.budget.maxTokens} tokens/run · ${row.request.perRunSeconds}s/run`, 'small'));
      card.append(text(new Date(row.request.allowance.deadline * 1000).toISOString(), 'small'));
      for (const occurrence of row.occurrences) card.append(text(`${occurrence.occurrenceId} · ${occurrence.status} · ${occurrence.reservedTokens} tokens · ${occurrence.assignmentId ?? '—'}`, 'small'));
      for (const kind of ['pause', 'cancel', 'reconcile'] as const) {
        if (kind === 'pause' && row.state !== 'active') continue;
        if (kind === 'cancel' && ['cancelled', 'expired', 'exhausted'].includes(row.state)) continue;
        card.append(createButton({ label: t(`projectResponsibility.${kind}`), variant: kind === 'cancel' ? 'danger' : 'secondary', disabled: !!pending,
          onClick: () => {
            if (pending || !ownsContinuityScope(scope)) return;
            pending = { kind, busy: false, coordinatorId: scope.conversationId, key: dispatch.commandId(),
              body: { projectPath: scope.path, responsibilityId: row.responsibilityId, expectedRevision: row.revision } }; void send();
          } }));
      }
      history.append(card);
    }
  }
  async function send() {
    const command = pending; if (!command || command.busy || !belongs(command) || !scope.valid()) return;
    command.busy = true; synchronize();
    try {
      await assertContinuityScope(scope);
      const result = command.kind === 'create' ? await ports.create(command.body, command.key)
        : await ports[command.kind](command.body, command.key);
      if (pending === command) pending = null;
      if (scope.valid()) { consent.input.checked = false; render([result]); await load(); }
    } catch (reason) { if (scope.valid()) showError(status, reason); }
    finally { command.busy = false; if (scope.valid()) synchronize(); }
  }
  async function load() {
    if (!scope.valid()) return;
    const epoch = fence.begin();
    try {
      const rows = await ports.list(scope.path); if (!fence.accepts(epoch)) return;
      render(rows); synchronize();
    } catch (reason) { if (fence.accepts(epoch)) showError(status, reason); }
  }
  root.append(createButton({ label: t('projectContinuity.refresh'), onClick: () => { void load(); } }));
  synchronize(); void load(); return root;
}
