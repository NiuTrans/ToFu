import { orchestrationRegistry } from './registry';
import { record, type ContractRecord } from './contracts';

export interface MutableOrchestrationValidationState {
  validatedRevision: unknown;
  validation: string;
  errors: unknown[];
  warnings: unknown[];
  diagnostics: unknown[];
  contract: object | null;
  validationFailure: ContractRecord | null;
}

export interface CachedOrchestrationInspection {
  ok: boolean;
  errors: unknown[];
  warnings: unknown[];
  diagnostics: unknown[];
  contract: object | null;
}

export interface OrchestrationValidationStateController {
  reset(): MutableOrchestrationValidationState;
  begin(): MutableOrchestrationValidationState;
  current(revision: unknown): CachedOrchestrationInspection | null;
  apply(
    revision: unknown,
    inspection: unknown,
  ): CachedOrchestrationInspection | null;
  fail(failure: unknown): ContractRecord;
}

type ValidationStateWindow = Window & {
  createOrchestrationValidationState?:
    typeof createOrchestrationValidationState;
};

export function createOrchestrationValidationState(
  state: MutableOrchestrationValidationState,
): OrchestrationValidationStateController {
  if (!state || typeof state !== 'object') {
    throw new TypeError('validation state requires a mutable state object');
  }

  const reset = (): MutableOrchestrationValidationState => {
    state.validatedRevision = -1;
    state.validation = 'unknown';
    state.errors = [];
    state.warnings = [];
    state.diagnostics = [];
    state.contract = null;
    state.validationFailure = null;
    return state;
  };

  const begin = (): MutableOrchestrationValidationState => {
    state.validation = 'checking';
    state.validationFailure = null;
    return state;
  };

  const current = (
    revision: unknown,
  ): CachedOrchestrationInspection | null => {
    if (state.validatedRevision !== revision
        || (state.validation !== 'valid'
          && state.validation !== 'invalid')) return null;
    return {
      ok: state.validation === 'valid',
      errors: state.errors,
      warnings: state.warnings,
      diagnostics: state.diagnostics,
      contract: state.contract,
    };
  };

  const apply = (
    revision: unknown,
    inspection: unknown,
  ): CachedOrchestrationInspection | null => {
    const value = record(inspection) ?? {};
    state.validatedRevision = revision;
    state.validation = value.ok ? 'valid' : 'invalid';
    state.errors = Array.isArray(value.errors) ? value.errors : [];
    state.warnings = Array.isArray(value.warnings) ? value.warnings : [];
    state.diagnostics = Array.isArray(value.diagnostics)
      ? value.diagnostics : [];
    state.contract = value.contract && typeof value.contract === 'object'
      ? value.contract as object : null;
    state.validationFailure = null;
    return current(revision);
  };

  const fail = (failure: unknown): ContractRecord => {
    const value = record(failure) ?? {};
    reset();
    state.validationFailure = {
      status: value.status == null ? 0 : value.status,
      reason: value.reason || 'transport-failed',
      error: value.error || '',
    };
    return state.validationFailure;
  };

  return Object.freeze({ reset, begin, current, apply, fail });
}

(orchestrationRegistry as unknown as ValidationStateWindow).createOrchestrationValidationState =
  createOrchestrationValidationState;
