import { orchestrationRegistry } from './registry';
import { record, type ContractRecord } from './contracts';
import {
  createOrchestrationDirtyGuard,
  type BeforeUnloadTarget,
} from './dirty-guard';
import {
  createOrchestrationDocumentValidationController,
  type DocumentValidationOptions,
  type DocumentValidationState,
  type ValidationClient,
} from './document-validation';
import {
  createOrchestrationDocumentView,
  type OrchestrationDocumentViewOptions,
} from './document-view';
import {
  projectOrchestrationInspection,
  type InspectionProjectionOptions,
} from './inspection-result';

export interface OrchestrationDocumentState
  extends DocumentValidationState {
  [key: string]: unknown;
  dirty: boolean;
  persisted: boolean;
  revision: number;
  saveBusy: boolean;
  writeConflict: ContractRecord | null;
}

export interface OrchestrationDocumentControllerOptions
  extends OrchestrationDocumentViewOptions, InspectionProjectionOptions {
  api?: unknown | (() => unknown);
  canValidate?: DocumentValidationOptions['canValidate'];
  validate?: DocumentValidationOptions['validate'];
  normalizeValidationRead?: DocumentValidationOptions['normalizeValidationRead'];
  validationClient?: ValidationClient;
  validationCoordinator?: DocumentValidationOptions['validationCoordinator'];
  validationState?: DocumentValidationOptions['validationState'];
  snapshot?: DocumentValidationOptions['snapshot'];
  validationDelay?: number | null;
  onInspectionChange?: DocumentValidationOptions['onInspectionChange'];
  onError?: DocumentValidationOptions['onError'];
  confirm?: (
    message: string,
    options: { danger: boolean },
  ) => unknown | PromiseLike<unknown>;
}

type DocumentControllerWindow = Window & {
  createOrchestrationDocumentController?:
    typeof createOrchestrationDocumentController;
};

/** Revisioned dirty/save/validation lifecycle for the Studio document. */
export function createOrchestrationDocumentController(
  options: OrchestrationDocumentControllerOptions,
) {
  const state: OrchestrationDocumentState = {
    dirty: false,
    persisted: false,
    revision: 0,
    validatedRevision: -1,
    validation: 'unknown',
    errors: [],
    warnings: [],
    diagnostics: [],
    contract: null,
    validationFailure: null,
    timer: null,
    saveBusy: false,
    writeConflict: null,
  };
  const view = createOrchestrationDocumentView(options);
  const dirtyGuard = createOrchestrationDirtyGuard({
    isDirty: () => state.dirty,
    translate: options.translate,
    confirm: options.confirm,
  });

  const normalizeInspection = (value: unknown) =>
    projectOrchestrationInspection(options, value);
  const render = (): void => { view.render(state); };
  const validation = createOrchestrationDocumentValidationController({
    state,
    api: options.api,
    canValidate: options.canValidate,
    validate: options.validate,
    normalizeValidationRead: options.normalizeValidationRead,
    inspectionContract: options.inspectionContract,
    validationClient: options.validationClient,
    validationCoordinator: options.validationCoordinator,
    validationState: options.validationState,
    snapshot: options.snapshot,
    validationDelay: options.validationDelay,
    translate: options.translate,
    normalizeInspection,
    onInspectionChange: options.onInspectionChange,
    onError: options.onError,
    toast: options.toast,
    warn: options.warn,
    render,
  });

  const setBaseline = (
    persisted: unknown,
    inspection?: unknown,
  ): void => {
    state.dirty = false;
    state.persisted = Boolean(persisted);
    state.revision += 1;
    validation.prepareRevision('baseline-changed', inspection);
    state.writeConflict = null;
    render();
    if (!inspection) validation.schedule();
  };

  const markDirty = (): void => {
    state.dirty = true;
    state.revision += 1;
    validation.prepareRevision('definition-changed');
    render();
    validation.schedule();
  };

  const restoreHistory = (historyState: unknown): void => {
    const history = record(historyState) ?? {};
    state.dirty = !history.atBaseline;
    state.persisted = Boolean(history.baselinePersisted);
    state.revision += 1;
    validation.prepareRevision('history-restored');
    render();
    validation.schedule();
  };

  const setSaveBusy = (busy: unknown): void => {
    state.saveBusy = Boolean(busy);
    render();
  };

  const acknowledgeSaved = (
    revision: unknown,
    inspection?: unknown,
  ): boolean => {
    state.writeConflict = null;
    if (state.revision === revision) {
      setBaseline(true, inspection);
      return true;
    }
    state.persisted = true;
    render();
    return false;
  };

  const detachPersistedCopy = (): void => {
    state.writeConflict = null;
    state.persisted = false;
    markDirty();
  };

  const markWriteConflict = (conflict: unknown): void => {
    const value = record(conflict);
    state.writeConflict = value ? {
      operation: String(value.operation || 'replace'),
      expectedUpdatedAt: value.expectedUpdatedAt,
      currentUpdatedAt: value.currentUpdatedAt,
    } : { operation: 'replace' };
    render();
  };

  const destroy = (): void => {
    validation.destroy();
    dirtyGuard.destroy();
  };

  return {
    state,
    revision: () => state.revision,
    render,
    setBaseline,
    markDirty,
    restoreHistory,
    scheduleValidation: validation.schedule,
    validateNow: validation.validateNow,
    requireValid: validation.requireValid,
    showIssues: () => view.showIssues(state),
    confirmDiscard: dirtyGuard.confirmDiscard,
    confirmReplace: () => dirtyGuard.confirmDiscard(
      'orch.doc.replaceConfirm'),
    installUnloadGuard: (target: BeforeUnloadTarget) =>
      dirtyGuard.installUnloadGuard(target),
    setSaveBusy,
    acknowledgeSaved,
    markWriteConflict,
    detachPersistedCopy,
    invalidateValidation: validation.invalidate,
    validationPending: validation.pending,
    destroy,
  };
}

export type OrchestrationDocumentController = ReturnType<
  typeof createOrchestrationDocumentController
>;

(orchestrationRegistry as unknown as DocumentControllerWindow).createOrchestrationDocumentController =
  createOrchestrationDocumentController;
