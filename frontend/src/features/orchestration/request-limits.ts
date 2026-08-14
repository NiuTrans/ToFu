import { orchestrationRegistry } from './registry';
import { record } from './contracts';

interface LimitedInput {
  setAttribute(name: string, value: string): void;
  removeAttribute(name: string): void;
}

interface StudioLimitRoot {
  querySelector(selectors: string): LimitedInput | null;
}

export interface OrchestrationRequestLimitPolicy {
  maxLength(field: string): number | null;
  maxItems(field: string): number | null;
  maxDepth(field: string): number | null;
  retainedItems(field: string): number | null;
  definitionNodeLimit(): number | null;
  subflowDepthLimit(): number | null;
  composeHistoryLimit(): number | null;
  composeHistoryMessageLimit(): number | null;
  applyInput(element: LimitedInput | null, field: string): number | null;
  applyHumanInput(element: LimitedInput | null): number | null;
  applyStudio(root: StudioLimitRoot | null): boolean;
}

export interface RequestLimitsOptions {
  source?: (() => unknown) | null;
  limits?: unknown;
}

type RequestLimitsWindow = Window & {
  _orchestrationRequestLimitValue?: typeof orchestrationRequestLimitValue;
  orchestrationRequestMaxLength?: typeof orchestrationRequestMaxLength;
  orchestrationRequestRetainedItems?: typeof orchestrationRequestRetainedItems;
  orchestrationRequestMaxItems?: typeof orchestrationRequestMaxItems;
  orchestrationRequestMaxDepth?: typeof orchestrationRequestMaxDepth;
  applyOrchestrationInputLimit?: typeof applyOrchestrationInputLimit;
  applyOrchestrationStudioRequestLimits?:
    typeof applyOrchestrationStudioRequestLimits;
  orchestrationRequestLimitPolicy?: typeof orchestrationRequestLimitPolicy;
  createOrchestrationRequestLimits?: typeof createOrchestrationRequestLimits;
};

export function orchestrationRequestLimitValue(
  limits: unknown,
  field: string,
  property: string,
): number | null {
  const entry = record(record(limits)?.[field]);
  const value = entry?.[property];
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
    ? value : null;
}

export const orchestrationRequestMaxLength = (
  limits: unknown, field: string,
): number | null => orchestrationRequestLimitValue(
  limits, field, 'maxLength');
export const orchestrationRequestRetainedItems = (
  limits: unknown, field: string,
): number | null => orchestrationRequestLimitValue(
  limits, field, 'retainedItems');
export const orchestrationRequestMaxItems = (
  limits: unknown, field: string,
): number | null => orchestrationRequestLimitValue(
  limits, field, 'maxItems');
export const orchestrationRequestMaxDepth = (
  limits: unknown, field: string,
): number | null => orchestrationRequestLimitValue(
  limits, field, 'maxDepth');

export function applyOrchestrationInputLimit(
  element: LimitedInput | null,
  limits: unknown,
  field: string,
): number | null {
  return orchestrationRequestLimitPolicy(limits).applyInput(element, field);
}

export function applyOrchestrationStudioRequestLimits(
  root: StudioLimitRoot | null,
  limits: unknown,
): boolean {
  return orchestrationRequestLimitPolicy(limits).applyStudio(root);
}

export function orchestrationRequestLimitPolicy(
  value: unknown,
): OrchestrationRequestLimitPolicy {
  const candidate = record(value);
  const isPolicy = candidate && Object.keys(candidate).some(
    (key) => typeof candidate[key] === 'function');
  if (isPolicy) return candidate as unknown as OrchestrationRequestLimitPolicy;
  return createOrchestrationRequestLimits({
    source: typeof value === 'function' ? value as () => unknown : null,
    limits: typeof value === 'function' ? {} : value,
  });
}

export function createOrchestrationRequestLimits(
  options: RequestLimitsOptions = {},
): OrchestrationRequestLimitPolicy {
  const staticLimits = record(options.limits) ?? {};
  const current = (): Record<string, unknown> => {
    let limits: unknown = staticLimits;
    if (typeof options.source === 'function') {
      try {
        limits = options.source();
      } catch {
        limits = staticLimits;
      }
    }
    return record(limits) ?? {};
  };
  const value = (field: string, property: string): number | null =>
    orchestrationRequestLimitValue(current(), field, property);
  const applyInput = (
    element: LimitedInput | null,
    field: string,
  ): number | null => {
    if (!element) return null;
    const maxLength = value(field, 'maxLength');
    if (maxLength == null) element.removeAttribute('maxlength');
    else element.setAttribute('maxlength', String(maxLength));
    return maxLength;
  };
  const applyStudio = (root: StudioLimitRoot | null): boolean => {
    if (!root || typeof root.querySelector !== 'function') return false;
    applyInput(root.querySelector('#orchNameInput'), 'definitionName');
    applyInput(root.querySelector('#orchAiText'), 'composeRequirement');
    applyInput(root.querySelector('#orchRunInput'), 'runInput');
    return true;
  };
  return Object.freeze({
    maxLength: (field: string) => value(field, 'maxLength'),
    maxItems: (field: string) => value(field, 'maxItems'),
    maxDepth: (field: string) => value(field, 'maxDepth'),
    retainedItems: (field: string) => value(field, 'retainedItems'),
    definitionNodeLimit: () => value('definitionNodes', 'maxItems'),
    subflowDepthLimit: () => value('subflowDepth', 'maxDepth'),
    composeHistoryLimit: () => value('composeHistory', 'retainedItems'),
    composeHistoryMessageLimit: () => value(
      'composeHistory', 'messageMaxLength'),
    applyInput,
    applyHumanInput: (element: LimitedInput | null) =>
      applyInput(element, 'humanInput'),
    applyStudio,
  });
}

Object.assign(orchestrationRegistry as unknown as RequestLimitsWindow, {
  _orchestrationRequestLimitValue: orchestrationRequestLimitValue,
  orchestrationRequestMaxLength,
  orchestrationRequestRetainedItems,
  orchestrationRequestMaxItems,
  orchestrationRequestMaxDepth,
  applyOrchestrationInputLimit,
  applyOrchestrationStudioRequestLimits,
  orchestrationRequestLimitPolicy,
  createOrchestrationRequestLimits,
});
