/** Program creation policy and frozen read projection. No transport or timers.
 * The program/run contracts own limits and role admission; browser drafts carry
 * only explicit input. Empty settings preserve existing backend defaults.
 */
import { createField } from '../../../ui';
import type { CreateProgramBody, ExplorationProgram } from '../../../api/evolution-programs';
import type { EvolutionModel, RoleModels } from '../../../api/evolution-runs';

type ModelOption = {model: EvolutionModel; label: string};
type Policy = Pick<CreateProgramBody, 'budget' | 'review_reserve' | 'max_attempts' | 'role_models'>;
type Copy = (key: string) => string;
const names = ['program-token-limit', 'program-tool-limit', 'program-hours', 'program-attempts'];
const modelIdentity = (model: EvolutionModel) => JSON.stringify(Object.entries(model).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0));
function storedModelIdentity(value: string): string | undefined {
  try {
    const parsed: unknown = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? modelIdentity(parsed as EvolutionModel) : undefined;
  } catch {return undefined;}
}

export function mountExecutionOptions(form: HTMLFormElement, copy: Copy) {
  const details = document.createElement('details'); details.className = 'exp-execution-options';
  const summary = document.createElement('summary'); summary.textContent = copy('executionOptions');
  const hint = document.createElement('p'); hint.className = 'exp-muted'; hint.textContent = copy('executionOptionsHint');
  const fields = document.createElement('div'); fields.className = 'exp-policy-fields';
  const labels = ['policyTokens', 'policyTools', 'policyHours', 'policyAttempts'];
  const numbers = names.map((name, index) => {
    const input = document.createElement('input'); input.type = 'number'; input.name = name;
    input.className = 'ui-input'; input.min = '0'; input.step = index === 2 ? 'any' : '1';
    input.max = String(index === 2 ? Number.MAX_SAFE_INTEGER / 3600 : Number.MAX_SAFE_INTEGER);
    fields.append(createField({label: copy(labels[index]!), hint: copy(index === 3 ? 'policyAttemptsHint' : 'policyUnlimitedHint'),
      control: input, controlId: 'exp-' + name}).el); return input;
  });
  const reserveNames = ['program-review-tokens', 'program-review-tools', 'program-review-seconds'];
  const reserveLabels = ['reviewReserveTokens', 'reviewReserveTools', 'reviewReserveSeconds'];
  const reserves = reserveNames.map((name, index) => {
    const input = document.createElement('input'); input.type = 'number'; input.name = name;
    input.className = 'ui-input'; input.min = '0'; input.step = '1'; input.max = String(Number.MAX_SAFE_INTEGER);
    fields.append(createField({label: copy(reserveLabels[index]!), hint: copy('reviewReserveHint'),
      control: input, controlId: 'exp-' + name}).el); return input;
  });
  let models: ModelOption[] = [], signature = '';
  const roles = ['executor', 'reviewer'] as const;
  const selectors = roles.map(role => {
    const select = document.createElement('select'); select.name = 'program-' + role + '-model';select.className = 'ui-input';
    select.append(new Option(copy('inheritProgramModel'), ''));
    fields.append(createField({label: copy('policy.' + role), control: select, controlId: 'exp-' + role + '-model'}).el);
    return select;
  });
  details.append(summary, hint, fields); form.append(details);
  return {
    controls: [...numbers, ...selectors, ...reserves],
    refreshModels(next: ModelOption[]) {
      const nextSignature = JSON.stringify(next.map(option => [modelIdentity(option.model), option.label]));
      if (nextSignature === signature) return;
      signature = nextSignature; models = next;
      selectors.forEach(select => {
        const saved = select.value, savedLabel = select.selectedOptions[0]?.textContent || '';
        select.replaceChildren(new Option(copy('inheritProgramModel'), ''));
        models.forEach(option => select.append(new Option(option.label,
          saved && storedModelIdentity(saved) === modelIdentity(option.model) ? saved : JSON.stringify(option.model))));
        if (saved && ![...select.options].some(option => option.value === saved)) {
          const missing = new Option(copy('unavailableModel') + ': ' + savedLabel, saved);missing.disabled = true;select.append(missing);
        }
        select.value = saved;
      });
    },
    modelsAvailable() {return selectors.every(select => !select.value || models.some(option => modelIdentity(option.model) === storedModelIdentity(select.value)));},
    read(): Policy {
      const values = numbers.map((input, index) => {
        const raw = input.value.trim(); if (!raw) return undefined;
        const value = Number(raw), scaled = index === 2 ? value * 3600 : value;
        if (!Number.isSafeInteger(scaled) || scaled < 0) throw Error(copy('executionOptionsInvalid'));
        return scaled;
      });
      const reserved = reserves.map((input, index) => {
        if (!input.value.trim()) return undefined;
        const amount = Number(input.value);
        if (!Number.isSafeInteger(amount) || amount < 0) throw Error(copy('executionOptionsInvalid'));
        const cap = values[index];
        if (amount && (!cap || amount >= cap)) throw Error(copy('reviewReserveInvalid'));
        return amount;
      });
      const role_models: RoleModels = {};
      selectors.forEach((select, index) => {
        if (!select.value) return;
        // Keep original serialization for ambiguous request identity; catalog
        // equality ignores JSON key order, while backend admission owns routing.
        const parsed: unknown = JSON.parse(select.value);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw Error(copy('executionModelUnavailable'));
        role_models[roles[index]!] = {...parsed} as EvolutionModel;
      });
      return {
        ...(values.slice(0, 3).some(value => value !== undefined) ? {budget: {
          ...(values[0] !== undefined ? {max_tokens: values[0]} : {}),
          ...(values[1] !== undefined ? {max_tool_calls: values[1]} : {}),
          ...(values[2] !== undefined ? {max_wall_clock_seconds: values[2]} : {}),
        }} : {}),
        ...(reserved.some(value => value !== undefined) ? {review_reserve: {
          ...(reserved[0] !== undefined ? {tokens: reserved[0]} : {}),
          ...(reserved[1] !== undefined ? {tool_calls: reserved[1]} : {}),
          ...(reserved[2] !== undefined ? {wall_clock_seconds: reserved[2]} : {}),
        }} : {}),
        ...(values[3] !== undefined ? {max_attempts: values[3]} : {}),
        ...(Object.keys(role_models).length ? {role_models} : {}),
      };
    },
  };
}

export function executionPolicyText(program: ExplorationProgram, models: ModelOption[], copy: Copy): string {
  const options = program.execution_options || {}, budget = options.budget || {};
  const cap = (value: number | null | undefined) => value ? value.toLocaleString(undefined, {maximumFractionDigits: 12}) : copy('policyUnlimited');
  const remaining = (limit: number | null | undefined, consumed: number) => {
    if (!limit) return copy('policyUnlimited');
    return program.actuals_complete === false ? copy('policyRemainingUnknown') : program.current ? copy('policyRemainingPending') : Math.max(0, limit - consumed).toLocaleString();
  };
  const label = (role: 'executor' | 'reviewer') => {
    const selected = options.role_models?.[role] || options.model;
    return !selected ? copy('automatic') : models.find(option => modelIdentity(option.model) === modelIdentity(selected))?.label || copy('unavailableModel');
  };
  const attempts = options.max_attempts === undefined ? (program.auto_replan ? 3 : 0) : options.max_attempts;
  return [
    `${copy('policyTokens')}: ${cap(budget.max_tokens)} · ${copy('policyRemaining')}: ${remaining(budget.max_tokens, program.actuals.tokens)}`,
    `${copy('policyTools')}: ${cap(budget.max_tool_calls)} · ${copy('policyRemaining')}: ${remaining(budget.max_tool_calls, program.actuals.tool_calls)}`,
    `${copy('policyHours')}: ${cap(budget.max_wall_clock_seconds ? budget.max_wall_clock_seconds / 3600 : 0)}`,
    ...(Object.values(options.review_reserve || {}).some(Boolean) ? [
      `${copy('reviewReserveTokens')}: ${(options.review_reserve?.tokens || 0).toLocaleString()}`,
      `${copy('reviewReserveTools')}: ${(options.review_reserve?.tool_calls || 0).toLocaleString()}`,
      `${copy('reviewReserveSeconds')}: ${(options.review_reserve?.wall_clock_seconds || 0).toLocaleString()}`,
      copy('reviewReserveHint'),
    ] : []),
    `${copy('policyAttempts')}: ${cap(attempts)}`,
    `${copy('policy.executor')}: ${label('executor')}`,
    `${copy('policy.reviewer')}: ${label('reviewer')}`,
  ].join('\n');
}
