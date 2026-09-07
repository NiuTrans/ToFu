import { orchestrationRegistry } from './registry';
import { orchestrationRunPresentation, type OrchestrationRunPresentation } from './run-status';

export interface TaskModeRunStatusOptions {
  escape?: (value: unknown) => unknown;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  isTerminal?: (run: unknown) => unknown;
  normalizeOutcome?: (value: unknown) => Record<string, unknown> | null;
  outcomeMessage?: (value: unknown, fallback: string) => unknown;
  runContract?: unknown;
}

type TaskModeRunStatusWindow = Window & {
  createTaskModeRunStatusPresentation?:
    typeof createTaskModeRunStatusPresentation;
};

export function createTaskModeRunStatusPresentation(
  options: TaskModeRunStatusOptions = {},
) {
  const escape = (value: unknown): string => String(options.escape
    ? options.escape(value) : value == null ? '' : value);
  const translate = (key: string, params?: Record<string, unknown>): unknown =>
    options.translate ? options.translate(key, params) : key;
  const isTerminal = (value: unknown): boolean => Boolean(
    options.isTerminal ? options.isTerminal(value) : false);
  const normalizeOutcome = (value: unknown): Record<string, unknown> | null =>
    options.normalizeOutcome ? options.normalizeOutcome(value) : null;
  const outcomeMessage = (value: unknown): string => String(
    options.outcomeMessage ? options.outcomeMessage(value, '') : '');
  const statusPresentation = (runOrStatus: unknown) => {
    const run = runOrStatus && typeof runOrStatus === 'object'
      ? runOrStatus : null;
    const projection: OrchestrationRunPresentation = orchestrationRunPresentation(
      runOrStatus, options.runContract);
    let value = projection.status;
    let token = projection.token;
    const outcome = run && isTerminal(run) ? normalizeOutcome(run) : null;
    if (outcome?.category === 'incomplete') {
      value = 'incomplete';
      token = 'incomplete';
    }
    const key = `tm.status.${value}`;
    let label = translate(key);
    if (label === key && projection.category !== value) {
      const categoryKey = `tm.statusCategory.${projection.category}`;
      const categoryLabel = translate(categoryKey);
      if (categoryLabel !== categoryKey) label = categoryLabel;
    }
    return {
      value,
      token,
      label: label === key ? value : label,
      title: outcome?.category === 'incomplete'
        ? outcomeMessage(run) || value : value,
    };
  };
  const statusLabel = (value: unknown): unknown =>
    statusPresentation(value).label;
  const statusChip = (value: unknown): string => {
    const state = statusPresentation(value);
    return `<span class="tm-chip tm-chip-${state.token}" title="${escape(
      state.title)}">${escape(state.label)}</span>`;
  };
  return { isTerminal, statusChip, statusLabel, statusPresentation };
}

(orchestrationRegistry as unknown as TaskModeRunStatusWindow).createTaskModeRunStatusPresentation =
  createTaskModeRunStatusPresentation;
