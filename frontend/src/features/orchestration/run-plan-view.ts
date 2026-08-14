import { orchestrationRegistry } from './registry';
import { record, type ContractRecord } from './contracts';

export interface RunPlanViewOptions {
  escape?: (value: unknown) => unknown;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  icon?: (name: string) => unknown;
  log?: (html: string, className?: string) => unknown;
}

type RunPlanViewWindow = Window & {
  createOrchestrationRunPlanView?: typeof createOrchestrationRunPlanView;
};

/** Studio plan-result rows and failure projection. */
export function createOrchestrationRunPlanView(
  options: RunPlanViewOptions = {},
) {
  const escape = (value: unknown): string => String(options.escape
    ? options.escape(value) : value == null ? '' : value);
  const translate = (
    key: string,
    params?: Record<string, unknown>,
  ): unknown => options.translate ? options.translate(key, params) : key;
  const icon = (name: string): string => String(
    options.icon ? options.icon(name) || '' : '');
  const log = (html: string, className?: string): unknown =>
    typeof options.log === 'function' ? options.log(html, className) : null;
  const failure = (message: string): false => {
    log(`${icon('warn')} ${escape(message)}`, 'is-err');
    return false;
  };
  const render = (response: ContractRecord): true => {
    const steps = Array.isArray(response?.steps) ? response.steps : [];
    log(`<b>${escape(translate('orch.run.planTitle', {
      n: steps.length,
    }))}</b>`);
    steps.forEach((stepValue, index) => {
      const step = record(stepValue) ?? {};
      const label = step.role
        ? `${icon('bot')} ${escape(step.role)}`
        : `${icon('layout')} ${escape(step.kind || step.action)}`;
      log(`${index + 1}. ${label} <span class="orch-run-dim">(${
        escape(step.action)})</span>`);
    });
    return true;
  };
  return Object.freeze({ failure, render });
}

(orchestrationRegistry as unknown as RunPlanViewWindow).createOrchestrationRunPlanView =
  createOrchestrationRunPlanView;
