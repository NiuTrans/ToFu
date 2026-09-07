import { orchestrationRegistry } from './registry';
import { record, type ContractRecord } from './contracts';
import { reportOrchestrationDiagnostic } from './diagnostic-report';
import {
  createOrchestrationValidationCoordinator,
  type OrchestrationValidationCoordinator,
} from './validation-coordinator';
import {
  createOrchestrationValidationClient,
  type ValidationClientOptions,
} from './validation-request';
import {
  createOrchestrationValidationState,
  type CachedOrchestrationInspection,
  type MutableOrchestrationValidationState,
  type OrchestrationValidationStateController,
} from './validation-state';

export interface DocumentValidationState
  extends MutableOrchestrationValidationState {
  revision: unknown;
  timer: number | null;
}

export interface ValidationClient {
  available(): boolean;
  validate(
    definition: unknown,
    options: { signal?: AbortSignal },
  ): Promise<ContractRecord>;
}

export interface DocumentValidationOptions extends ValidationClientOptions {
  state: DocumentValidationState;
  validationClient?: ValidationClient;
  validationCoordinator?: OrchestrationValidationCoordinator;
  validationState?: OrchestrationValidationStateController;
  normalizeValidationRead?: (value: unknown) => unknown;
  snapshot?: () => unknown;
  validationDelay?: number | null;
  translate?: (key: string, params?: Record<string, unknown>) => string;
  normalizeInspection?: (value: unknown) => CachedOrchestrationInspection;
  onInspectionChange?: (diagnostics: unknown[]) => void;
  onError?: (stage: string, error: unknown) => void;
  toast?: (message: string, error?: boolean) => unknown;
  warn?: (message: string, errors: unknown[], blocked?: boolean) => unknown;
  render?: () => unknown;
}

type DocumentValidationWindow = Window & {
  createOrchestrationDocumentValidationController?:
    typeof createOrchestrationDocumentValidationController;
};

export function createOrchestrationDocumentValidationController(
  options: DocumentValidationOptions,
) {
  const state = options.state;
  const validationClient = options.validationClient
    ?? createOrchestrationValidationClient({
      api: options.api,
      canValidate: options.canValidate,
      validate: options.validate,
      normalizeRead: options.normalizeValidationRead,
      inspectionContract: options.inspectionContract,
    });
  const coordinator = options.validationCoordinator
    ?? createOrchestrationValidationCoordinator();
  const validationState = options.validationState
    ?? createOrchestrationValidationState(state);

  const translate = (
    key: string,
    params?: Record<string, unknown>,
  ): string => options.translate ? options.translate(key, params) : key;
  const normalize = (value: unknown): CachedOrchestrationInspection =>
    options.normalizeInspection
      ? options.normalizeInspection(value)
      : value as CachedOrchestrationInspection;
  const notify = (): void => {
    options.onInspectionChange?.(state.diagnostics);
  };
  const render = (): void => { options.render?.(); };
  const clearTimer = (): void => {
    if (state.timer) clearTimeout(state.timer);
    state.timer = null;
  };

  const prepareRevision = (
    reason?: unknown,
    inspection?: unknown,
  ): CachedOrchestrationInspection | null => {
    clearTimer();
    coordinator.invalidate(reason || 'definition-changed');
    const normalized = inspection ? normalize(inspection) : null;
    if (normalized) validationState.apply(state.revision, normalized);
    else validationState.reset();
    notify();
    return normalized;
  };

  const validateNow = async (
    validateOptions: { quiet?: boolean } = {},
  ): Promise<CachedOrchestrationInspection | null> => {
    clearTimer();
    if (!validationClient.available()
        || typeof options.snapshot !== 'function') return null;
    const revision = state.revision;
    const ticket = coordinator.request<ContractRecord>(revision, (signal) =>
      validationClient.validate(
        options.snapshot?.(), signal ? { signal } : {}));
    if (!ticket.shared) {
      validationState.begin();
      render();
    }
    const outcome = await ticket.promise;
    if (revision !== state.revision || outcome.aborted
        || outcome.generation !== coordinator.generation()) return null;
    if (!coordinator.claim(ticket)) {
      return validationState.current(revision);
    }
    const response = outcome.response;
    if (outcome.error) {
      reportOrchestrationDiagnostic(
        options.onError, 'validation', outcome.error);
    }
    if (!response || !response.ok) {
      const failure = validationState.fail(response ? {
        status: response.status,
        reason: response.reason,
        error: response.error,
      } : null);
      notify();
      if (response) {
        reportOrchestrationDiagnostic(
          options.onError, 'validation', response.cause || failure);
      }
      if (!validateOptions.quiet && options.toast) {
        options.toast(translate('orch.doc.validateFailed'), true);
      }
    } else {
      const inspection = normalize(response.inspection);
      validationState.apply(revision, inspection);
      notify();
      render();
      return inspection;
    }
    render();
    return null;
  };

  const schedule = (): void => {
    clearTimer();
    if (!validationClient.available()
        || typeof options.snapshot !== 'function') return;
    state.timer = window.setTimeout(() => {
      state.timer = null;
      void validateNow({ quiet: true });
    }, options.validationDelay == null ? 500 : options.validationDelay);
  };

  const requireValid = async (
    action: unknown,
  ): Promise<CachedOrchestrationInspection | null> => {
    clearTimer();
    const inspection = validationState.current(state.revision)
      ?? await validateNow();
    if (!inspection || !inspection.ok) {
      const errors = inspection?.errors
        || [translate('orch.doc.validateFailed')];
      options.warn?.(translate('orch.doc.blocked', {
        action: action || '',
      }), errors, true);
      return null;
    }
    return inspection;
  };

  const invalidate = (): void => {
    prepareRevision('validation-invalidated');
    render();
  };

  const destroy = (): void => {
    clearTimer();
    coordinator.invalidate('validation-destroyed');
  };

  return Object.freeze({
    prepareRevision,
    schedule,
    validateNow,
    requireValid,
    invalidate,
    pending: coordinator.pending,
    destroy,
  });
}

(orchestrationRegistry as unknown as DocumentValidationWindow)
  .createOrchestrationDocumentValidationController =
    createOrchestrationDocumentValidationController;
