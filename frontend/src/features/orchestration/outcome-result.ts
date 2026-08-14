import { orchestrationRegistry } from './registry';
import {
  compatibilityContract,
  inspectWireFormat,
  publishedContract,
  record,
  wireContractSpec,
  type ContractSource,
} from './contracts';
import { orchestrationResultError } from './result';

export interface NormalizedOutcome {
  format: unknown;
  category: string;
  engineStatus: string;
  lifecycleStatus: string;
  chatStatus: string;
  ok: boolean;
  stopReason: string;
  finishReason: string;
  error: string;
  canonical: boolean;
  unsupportedFormat: boolean;
  expectedFormat: string;
}

export interface ProjectedOutcomeText {
  text: string;
  truncated: boolean;
  limit: number;
}

export interface ProjectedFinalResult {
  outcome: NormalizedOutcome;
  finalText: string;
  message: string;
  finalTruncated: boolean;
  messageTruncated: boolean;
  partial: boolean;
  reasonKey: string;
  lineClass: string;
}

type Translate = (key: string) => unknown;
type OutcomeWindow = Window & {
  normalizeOrchestrationOutcome?: typeof normalizeOrchestrationOutcome;
  orchestrationOutcomeMessage?: typeof orchestrationOutcomeMessage;
  _projectOrchestrationOutcomeText?: typeof projectOrchestrationOutcomeText;
  projectOrchestrationFinalResult?: typeof projectOrchestrationFinalResult;
};

export function normalizeOrchestrationOutcome(
  value: unknown,
  contractSource?: ContractSource,
): NormalizedOutcome {
  const rootValue = record(value) ?? {};
  const result = record(rootValue.result) ?? {};
  const completion = record(rootValue.completion) ?? {};
  const errorEnvelope = record(rootValue.error) ?? {};
  const source = record(rootValue.outcome)
    ?? record(rootValue.orchestrationOutcome)
    ?? record(errorEnvelope.outcome)
    ?? record(result.outcome)
    ?? record(completion.outcome)
    ?? {};
  const published = publishedContract('outcomeContract', contractSource);
  const wire = inspectWireFormat('outcome', source, published);
  const contract = compatibilityContract('outcomeContract', wire.contract) ?? {};
  const defaults = compatibilityContract('outcomeContract') ?? {};
  const projectionSource = wire.supported ? source : {};
  const categories = Array.isArray(contract.categories)
    ? contract.categories
    : Array.isArray(defaults.categories) ? defaults.categories : [];
  const incompleteReasons = Array.isArray(contract.incompleteStopReasons)
    ? contract.incompleteStopReasons
    : Array.isArray(defaults.incompleteStopReasons)
      ? defaults.incompleteStopReasons : [];
  const projectionRoot = Object.keys(result).length > 0 ? result
    : Object.keys(completion).length > 0 ? completion : rootValue;
  const status = String(projectionSource.engine_status
    || projectionRoot.status || rootValue.status || '');
  let stopReason = String(projectionSource.stop_reason
    || projectionRoot.stop_reason || rootValue.endpointReason
    || errorEnvelope.message || '');
  let finishReason = String(
    projectionSource.finish_reason || rootValue.finishReason || '');
  let category = String(
    projectionSource.category || rootValue.outcome_category || '');
  const sourceOk = typeof projectionSource.ok === 'boolean'
    ? projectionSource.ok
    : typeof projectionRoot.ok === 'boolean' ? projectionRoot.ok : null;

  if (!wire.supported) {
    category = 'failure';
  } else if (!categories.includes(category)) {
    if (status === 'aborted' || finishReason === 'aborted'
        || stopReason === 'aborted') {
      category = 'aborted';
    } else if (finishReason === 'incomplete'
        || incompleteReasons.includes(stopReason)) {
      category = 'incomplete';
    } else if (sourceOk === true
        || ((status === 'done' || status === 'completed')
          && sourceOk !== false && !rootValue.error)
        || (!status && !rootValue.error && projectionRoot.final != null)) {
      category = 'success';
    } else {
      category = 'failure';
    }
  }

  const lifecycleStatus = String(projectionSource.lifecycle_status || (
    category === 'success' ? 'done'
      : category === 'aborted' ? 'aborted' : 'error'));
  const chatStatus = String(projectionSource.chat_status || (
    category === 'success' || category === 'incomplete' ? 'done'
      : category === 'aborted' ? 'aborted' : 'error'));
  if (!finishReason) {
    finishReason = category === 'success' ? 'stop'
      : category === 'incomplete' ? 'incomplete'
        : category === 'aborted' ? 'aborted' : 'error';
  }
  if (!stopReason) {
    stopReason = category === 'success' ? 'completed'
      : category === 'aborted' ? 'aborted' : 'failed';
  }
  const errorValue = projectionSource.error
    || (category === 'incomplete' && projectionSource.stop_reason
      ? projectionSource.stop_reason : null)
    || (typeof errorEnvelope.message === 'string'
      ? errorEnvelope.message : (rootValue.error || projectionRoot.error));

  return {
    format: source.format || '',
    category,
    engineStatus: status,
    lifecycleStatus,
    chatStatus,
    ok: category === 'success',
    stopReason,
    finishReason,
    error: orchestrationResultError(errorValue, ''),
    canonical: wire.present && wire.supported,
    unsupportedFormat: !wire.supported,
    expectedFormat: wire.expected,
  };
}

export function orchestrationOutcomeMessage(
  value: unknown,
  translate?: Translate,
  fallback?: unknown,
  contractSource?: ContractSource,
): string {
  const outcome = normalizeOrchestrationOutcome(value, contractSource);
  if (outcome.category === 'success') return '';
  const raw = outcome.error || String(fallback || '');
  if (raw && raw !== outcome.stopReason) return raw;
  const key = `orch.ev.stopReason.${outcome.stopReason}`;
  const localized = translate?.(key) ?? key;
  return localized && localized !== key
    ? String(localized) : (raw || outcome.stopReason);
}

export function projectOrchestrationOutcomeText(
  value: unknown,
  field: 'final' | 'error',
  contractSource?: ContractSource,
): ProjectedOutcomeText {
  const spec = wireContractSpec('outcome', contractSource);
  const displayLimits = record(spec.contract?.displayLimits);
  const configured = spec.supported ? displayLimits?.[field] : null;
  const fallback = field === 'error' ? 4000 : 16000;
  const limit = Number.isSafeInteger(configured) && Number(configured) > 0
    ? Number(configured) : fallback;
  const raw = String(value == null ? '' : value);
  return { text: raw.slice(0, limit), truncated: raw.length > limit, limit };
}

export function projectOrchestrationFinalResult(
  value: unknown,
  translate?: Translate,
  contractSource?: ContractSource,
): ProjectedFinalResult {
  const rootValue = record(value) ?? {};
  const root = record(rootValue.result) ?? rootValue;
  const outcome = normalizeOrchestrationOutcome(rootValue, contractSource);
  const finalText = projectOrchestrationOutcomeText(
    root.final, 'final', contractSource);
  const rawError = orchestrationResultError(root.error, '');
  const message = projectOrchestrationOutcomeText(
    orchestrationOutcomeMessage(
      rootValue, translate, rawError, contractSource),
    'error', contractSource);
  return {
    outcome,
    finalText: finalText.text,
    message: message.text,
    finalTruncated: finalText.truncated,
    messageTruncated: message.truncated,
    partial: outcome.category !== 'success',
    reasonKey: outcome.category === 'incomplete'
      ? 'tm.final.incomplete'
      : outcome.category === 'aborted'
        ? 'tm.final.aborted' : 'tm.final.error',
    lineClass: outcome.category === 'incomplete'
      ? 'is-warn' : outcome.category === 'success' ? 'is-done' : 'is-err',
  };
}

const bridge = orchestrationRegistry as unknown as OutcomeWindow;
bridge.normalizeOrchestrationOutcome = normalizeOrchestrationOutcome;
bridge.orchestrationOutcomeMessage = orchestrationOutcomeMessage;
bridge._projectOrchestrationOutcomeText = projectOrchestrationOutcomeText;
bridge.projectOrchestrationFinalResult = projectOrchestrationFinalResult;
