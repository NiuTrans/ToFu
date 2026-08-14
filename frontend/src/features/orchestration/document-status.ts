import { orchestrationRegistry } from './registry';
import { record } from './contracts';

export interface OrchestrationDocumentStatus {
  readonly key: string;
  readonly className: string;
  readonly conflict: boolean;
}

type DocumentStatusWindow = Window & {
  projectOrchestrationDocumentStatus?: typeof projectOrchestrationDocumentStatus;
};

/** Pure status-badge projection with explicit live-command precedence. */
export function projectOrchestrationDocumentStatus(
  state: unknown,
): OrchestrationDocumentStatus {
  const value = record(state) ?? {};
  const validation = String(value.validation || 'unknown');
  let key = value.dirty ? 'orch.doc.unsaved' : 'orch.doc.draft';
  let className = value.dirty ? 'is-dirty' : 'is-draft';

  if (value.saveBusy) {
    key = 'orch.doc.saving';
    className = 'is-saving';
  } else if (validation === 'checking') {
    key = 'orch.doc.checking';
    className = 'is-checking';
  } else if (validation === 'invalid') {
    key = 'orch.doc.invalid';
    className = 'is-invalid';
  } else if (value.writeConflict) {
    key = 'orch.doc.conflict';
    className = 'is-conflict';
  } else if (!value.dirty && value.persisted) {
    key = Array.isArray(value.warnings) && value.warnings.length
      ? 'orch.doc.savedWarn' : 'orch.doc.saved';
    className = Array.isArray(value.warnings) && value.warnings.length
      ? 'is-warning' : 'is-saved';
  } else if (validation === 'valid' && value.dirty
      && Array.isArray(value.warnings) && value.warnings.length) {
    key = 'orch.doc.unsavedWarn';
    className = 'is-warning';
  }

  return Object.freeze({
    key,
    className,
    conflict: className === 'is-conflict',
  });
}

(orchestrationRegistry as unknown as DocumentStatusWindow).projectOrchestrationDocumentStatus =
  projectOrchestrationDocumentStatus;
