import { orchestrationRegistry } from './registry';
import { record, type ContractRecord } from './contracts';
import { projectOrchestrationDocumentStatus } from './document-status';

export interface OrchestrationDocumentViewState extends ContractRecord {
  errors?: unknown[];
  warnings?: unknown[];
  diagnostics?: unknown[];
  validation?: string;
  saveBusy?: boolean;
  writeConflict?: unknown;
  contract?: unknown;
}

export interface OrchestrationDocumentViewOptions {
  document?: Document;
  translate?: (key: string, params?: Record<string, unknown>) => string;
  syncIssues?: (state: OrchestrationDocumentViewState) => unknown;
  onWriteConflict?: (conflict: unknown) => unknown;
  toast?: (message: string, error?: boolean) => unknown;
  showIssues?: (state: OrchestrationDocumentViewState) => unknown;
  nodeCount?: () => unknown;
  warn?: (message: string, issues: unknown[], blocked?: boolean) => unknown;
}

type DocumentViewWindow = Window & {
  createOrchestrationDocumentView?: typeof createOrchestrationDocumentView;
};

/** DOM projection for document status and inspection feedback. */
export function createOrchestrationDocumentView(
  options: OrchestrationDocumentViewOptions = {},
) {
  const translate = (
    key: string,
    params?: Record<string, unknown>,
  ): string => options.translate ? options.translate(key, params) : key;

  const render = (state: OrchestrationDocumentViewState): void => {
    const doc = options.document ?? document;
    const element = doc.getElementById('orchDocState');
    if (!element) return;
    const status = projectOrchestrationDocumentStatus(state);
    const errors = Array.isArray(state.errors) ? state.errors : [];
    const warnings = Array.isArray(state.warnings) ? state.warnings : [];
    const detail = errors.concat(warnings).join('\n');
    element.className = `orch-doc-state ${status.className}`;
    element.textContent = translate(status.key, {
      errors: errors.length,
      warnings: warnings.length,
    });
    element.title = status.conflict
      ? translate('orch.save.conflict')
      : detail || translate('orch.doc.statusTip');
    const save = doc.getElementById('orchSaveBtn') as HTMLButtonElement | null;
    if (save) {
      save.disabled = Boolean(state.saveBusy)
        || state.validation === 'checking';
      save.setAttribute('aria-busy', state.saveBusy ? 'true' : 'false');
    }
    options.syncIssues?.(state);
  };

  const showIssues = (state: OrchestrationDocumentViewState): unknown => {
    const errors = Array.isArray(state.errors) ? state.errors : [];
    const warnings = Array.isArray(state.warnings) ? state.warnings : [];
    const issues = errors.concat(warnings);
    if (state.writeConflict) {
      if (typeof options.onWriteConflict === 'function') {
        return options.onWriteConflict(state.writeConflict);
      }
      return options.toast?.(translate('orch.save.conflict'), true) ?? null;
    }
    if (typeof options.showIssues === 'function') {
      return options.showIssues(state);
    }
    if (!issues.length) {
      const contract = record(state.contract) ?? {};
      return options.toast?.(translate('orch.doc.contract', {
        projection: contract.projection || 'flow',
        nodes: contract.nodes || options.nodeCount?.() || 0,
      })) ?? null;
    }
    return options.warn?.(
      translate('orch.doc.issues', { count: issues.length }),
      issues,
      errors.length > 0,
    ) ?? null;
  };

  return Object.freeze({ render, showIssues });
}

(orchestrationRegistry as unknown as DocumentViewWindow).createOrchestrationDocumentView =
  createOrchestrationDocumentView;
