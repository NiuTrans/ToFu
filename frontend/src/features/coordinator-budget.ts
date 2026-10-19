/** Passive request consumption and explicit defaults editor; the server owns admission. */
import type { CoordinatorBudgetState, CoordinatorBudgetUpdate, CoordinatorBudgetSettings } from '../api/project-dispatch.generated';
import { COORDINATOR_BUDGET_LIMITS } from '../api/project-dispatch.generated';
import type { CoordinationBinding } from '../api/project-coordination.generated';
import { createAlert, createButton, createField, createProgress } from '../ui';
import { t } from '../i18n';
import { coordinationText as text } from './project-coordination-board';

export interface CoordinatorBudgetPorts {
  read(path: string): Promise<CoordinatorBudgetState>;
  update(request: CoordinatorBudgetUpdate): Promise<CoordinatorBudgetState>;
}

export function createCoordinatorBudget(ports: CoordinatorBudgetPorts,
  scope: { path: string; binding: CoordinationBinding; valid(): boolean }) {
  const summary = document.createElement('section'); summary.className = 'ui-card ui-stack coordinator-budget';
  const settings = document.createElement('form'); settings.className = 'ui-card ui-stack coordinator-budget-settings';
  settings.append(text(t('coordinatorBudget.settings'), 'h3'), text(t('coordinatorBudget.futureOnly'), 'small'));
  const fields = document.createElement('div'); fields.className = 'coordinator-budget-fields';
  const keys = ['maxTokens', 'maxModelRequests', 'maxToolCalls', 'durationSeconds'] as const;
  const labels = {
    maxTokens: t('coordinatorBudget.tokens'), maxModelRequests: t('coordinatorBudget.requests'),
    maxToolCalls: t('coordinatorBudget.tools'), durationSeconds: t('coordinatorBudget.duration'),
  };
  const inputs = new Map<keyof CoordinatorBudgetSettings, HTMLInputElement>();
  for (const key of keys) {
    const field = createField({ label: labels[key], inputType: 'number', required: true });
    const input = field.control as HTMLInputElement;
    input.min = String(COORDINATOR_BUDGET_LIMITS[key].minimum);
    input.max = String(COORDINATOR_BUDGET_LIMITS[key].maximum); input.step = '1';
    input.name = key; inputs.set(key, input); fields.append(field.el);
  }
  const feedback = document.createElement('div'); feedback.setAttribute('aria-live', 'polite');
  let current: CoordinatorBudgetState | null = null;
  let dirty = false, pending = false;
  const save = createButton({ label: t('coordinatorBudget.save'), variant: 'primary' }); save.type = 'submit'; save.disabled = true;
  settings.append(fields, save, feedback);
  settings.addEventListener('input', () => { dirty = true; feedback.replaceChildren(); });
  const update = (state: CoordinatorBudgetState | null) => {
    summary.replaceChildren(text(t('coordinatorBudget.title'), 'h3'));
    if (!state) {
      summary.append(createAlert({ variant: 'warning', title: t('coordinatorBudget.unavailable') }).el);
      return;
    }
    if (!dirty && !pending) {
      current = state;
      for (const key of keys) inputs.get(key)!.value = String(state.settings[key]);
      save.disabled = scope.binding.availability !== 'available';
    }
    const request = state.request;
    if (!request) { summary.append(text(t('coordinatorBudget.noRequest'), 'small')); return; }
    const { policy, consumption: used } = request;
    const metrics = document.createElement('div'); metrics.className = 'coordinator-budget-metrics';
    const values = [
      [labels.maxTokens, used.observedTokens, used.heldTokens, policy.maxTokens],
      [labels.maxModelRequests, used.modelRequests, used.heldModelRequests, policy.maxModelRequests],
      [labels.maxToolCalls, used.toolCalls, used.heldToolCalls, policy.maxToolCalls],
    ] as const;
    for (const [label, observed, held, limit] of values) {
      const metric = document.createElement('div'); metric.className = 'ui-stack ui-stack--tight';
      metric.append(text(label, 'small'), text(t('coordinatorBudget.used', { used: observed.toLocaleString(), total: limit.toLocaleString() }), 'strong'),
        createProgress({ label, value: limit ? Math.min(100, held / limit * 100) : 100 }).el,
        text(t('coordinatorBudget.reserved', { reserved: Math.max(0, held - observed).toLocaleString(), remaining: Math.max(0, limit - held).toLocaleString() }), 'small'));
      metrics.append(metric);
    }
    summary.append(metrics, text(t('coordinatorBudget.deadline', { date: new Date(policy.deadline * 1000).toLocaleString() }), 'small'),
      text(t('coordinatorBudget.accountingHint'), 'small'));
    if (!used.usageComplete) summary.append(text(t('coordinatorBudget.pending'), 'small'));
  };
  settings.addEventListener('submit', async event => {
    event.preventDefault();
    if (pending || !current || !scope.valid() || scope.binding.availability !== 'available' || !settings.reportValidity()) return;
    const selected = Object.fromEntries(keys.map(key => [key, Number(inputs.get(key)!.value)])) as CoordinatorBudgetSettings;
    if (keys.some(key => !Number.isSafeInteger(selected[key]))) return;
    pending = true; save.disabled = true;
    for (const input of inputs.values()) input.disabled = true;
    try {
      const result = await ports.update({ path: scope.path, conversationId: scope.binding.conversationId,
        bindingRevision: scope.binding.revision, expectedRevision: current.revision, settings: selected });
      if (!scope.valid()) return;
      dirty = false; pending = false; update(result);
      feedback.replaceChildren(text(t('coordinatorBudget.saved')));
    } catch (error) {
      if (scope.valid()) feedback.replaceChildren(createAlert({ variant: 'error', title: t('coordinatorBudget.saveFailed'),
        body: error instanceof Error ? error.message : String(error) }).el);
    } finally {
      pending = false; save.disabled = false;
      for (const input of inputs.values()) input.disabled = false;
    }
  });
  return { summary, settings, update };
}
