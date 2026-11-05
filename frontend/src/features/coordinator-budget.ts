/** Passive current-request usage view. Requests have no spending limit; the server owns accounting. */
import type { CoordinatorUsageState } from '../api/project-dispatch.generated';
import type { CoordinationBinding } from '../api/project-coordination.generated';
import { createAlert } from '../ui';
import { t } from '../i18n';
import { coordinationText as text } from './project-coordination-board';

export interface CoordinatorBudgetPorts {
  read(path: string, signal?: AbortSignal): Promise<CoordinatorUsageState>;
}

export function createCoordinatorBudget(_ports: CoordinatorBudgetPorts,
  _scope: { path: string; binding: CoordinationBinding; valid(): boolean }) {
  const summary = document.createElement('section'); summary.className = 'ui-card ui-stack coordinator-budget';
  const update = (state: CoordinatorUsageState | null) => {
    summary.replaceChildren(text(t('coordinatorBudget.title'), 'h3'));
    if (!state) {
      summary.append(createAlert({ variant: 'warning', title: t('coordinatorBudget.unavailable') }).el);
      return;
    }
    const request = state.request;
    if (!request) { summary.append(text(t('coordinatorBudget.noRequest'), 'small')); return; }
    const { usage } = request;
    const metrics = document.createElement('div'); metrics.className = 'coordinator-budget-metrics';
    const values = [
      [t('coordinatorBudget.tokens'), usage.tokens],
      [t('coordinatorBudget.requests'), usage.modelRequests],
      [t('coordinatorBudget.tools'), usage.toolCalls],
      [t('coordinatorBudget.workers'), usage.activeWorkers],
    ] as const;
    for (const [label, value] of values) {
      const metric = document.createElement('div'); metric.className = 'ui-stack ui-stack--tight';
      metric.append(text(label, 'small'), text(value.toLocaleString(), 'strong'));
      metrics.append(metric);
    }
    summary.append(metrics, text(t('coordinatorBudget.unlimited'), 'small'));
    if (!usage.usageComplete) summary.append(text(t('coordinatorBudget.pending'), 'small'));
    if (request.pendingDecision) {
      summary.append(text(t('coordinatorBudget.decision', {
        date: new Date(request.pendingDecision.dueAt * 1000).toLocaleString(),
      }), 'small'));
    }
  };
  return { summary, update };
}
