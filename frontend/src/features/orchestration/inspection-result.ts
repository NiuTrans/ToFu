import { orchestrationRegistry } from './registry';
import {
  compatibilityContract,
  inspectWireFormat,
  publishedContract,
  record,
  type ContractRecord,
  type ContractSource,
} from './contracts';
import { orchestrationIssueMessages } from './result';

export interface InspectionDiagnostic {
  severity: 'warning' | 'error';
  message: string;
  code: unknown;
  path: unknown;
}

export interface NormalizedInspection {
  format: unknown;
  ok: boolean;
  canonical: boolean;
  unsupportedFormat: boolean;
  expectedFormat: string;
  errors: string[];
  warnings: string[];
  diagnostics: InspectionDiagnostic[];
  contract: Record<string, unknown> | null;
}

export interface InspectionProjectionOptions {
  normalizeInspection?: (
    value: unknown,
    contractSource?: ContractSource,
  ) => NormalizedInspection;
  inspectionContract?: ContractSource;
}

type InspectionWindow = Window & {
  orchestrationInspectionMatchesContract?:
    typeof orchestrationInspectionMatchesContract;
  normalizeOrchestrationInspection?: typeof normalizeOrchestrationInspection;
  projectOrchestrationInspection?: typeof projectOrchestrationInspection;
};

function inspectionSource(value: unknown): ContractRecord {
  const root = record(value) ?? {};
  return record(root.inspection) ?? record(root.validation) ?? root;
}

export function orchestrationInspectionMatchesContract(
  value: unknown,
  contractSource?: ContractSource,
): boolean {
  const source = inspectionSource(value);
  const published = publishedContract('inspectionContract', contractSource);
  const wire = inspectWireFormat('inspection', source, published);
  if (!wire.supported) return false;
  if (!wire.present) return typeof source.ok === 'boolean';
  const contract = compatibilityContract(
    'inspectionContract', wire.contract) ?? {};
  const defaults = compatibilityContract('inspectionContract') ?? {};
  const fields = (name: string): string[] => {
    const configured = contract[name];
    const fallback = defaults[name];
    return Array.isArray(configured) ? configured as string[]
      : Array.isArray(fallback) ? fallback as string[] : [];
  };
  const ownsEvery = (
    candidate: ContractRecord,
    required: readonly string[],
  ): boolean => required.every((field) =>
    Object.prototype.hasOwnProperty.call(candidate, field));
  const responseFields = fields('responseFields');
  const stringArrayFields = fields('responseStringArrayFields');
  const diagnosticFields = fields('diagnosticFields');
  const diagnosticStringFields = fields('diagnosticStringFields');
  const contractFields = fields('contractFields');
  const contractStringFields = fields('contractStringFields');
  const contractIntegerFields = fields('contractNonNegativeIntegerFields');
  const severities = fields('diagnosticSeverities');
  const snapshot = record(source.contract);
  return responseFields.length > 0 && ownsEvery(source, responseFields)
    && typeof source.ok === 'boolean'
    && stringArrayFields.every((field) => Array.isArray(source[field])
      && (source[field] as unknown[]).every((item) => typeof item === 'string'))
    && Array.isArray(source.diagnostics)
    && source.diagnostics.every((item) => {
      const diagnostic = record(item);
      return Boolean(diagnostic) && ownsEvery(diagnostic ?? {}, diagnosticFields)
        && severities.includes(diagnostic?.severity as string)
        && diagnosticStringFields.every(
          (field) => typeof diagnostic?.[field] === 'string');
    })
    && Boolean(snapshot) && ownsEvery(snapshot ?? {}, contractFields)
    && contractStringFields.every(
      (field) => typeof snapshot?.[field] === 'string')
    && contractIntegerFields.every((field) =>
      Number.isSafeInteger(snapshot?.[field]) && Number(snapshot?.[field]) >= 0);
}

export function normalizeOrchestrationInspection(
  value: unknown,
  contractSource?: ContractSource,
): NormalizedInspection {
  const source = inspectionSource(value);
  const published = publishedContract('inspectionContract', contractSource);
  const wire = inspectWireFormat('inspection', source, published);
  const contract = compatibilityContract(
    'inspectionContract', wire.contract) ?? {};
  const defaults = compatibilityContract('inspectionContract') ?? {};
  const severities = Array.isArray(contract.diagnosticSeverities)
    ? contract.diagnosticSeverities
    : Array.isArray(defaults.diagnosticSeverities)
      ? defaults.diagnosticSeverities : [];
  const errors = orchestrationIssueMessages(source.errors);
  const warnings = orchestrationIssueMessages(source.warnings);
  const diagnostics: InspectionDiagnostic[] = [];

  const addMessage = (target: string[], message: string): void => {
    if (message && !target.includes(message)) target.push(message);
  };

  const rawDiagnostics = Array.isArray(source.diagnostics)
    ? source.diagnostics : [];
  for (const rawDiagnostic of rawDiagnostics) {
    const diagnostic = record(rawDiagnostic) ?? {};
    const declaredSeverity = String(diagnostic.severity || '');
    const severity: 'warning' | 'error' = declaredSeverity === 'warning'
      && severities.includes('warning') ? 'warning' : 'error';
    const message = orchestrationIssueMessages(diagnostic, {
      maxMessages: 1,
    })[0] ?? '';
    if (!message) continue;
    diagnostics.push({
      severity,
      message,
      code: diagnostic.code || '',
      path: diagnostic.path || '',
    });
    addMessage(severity === 'warning' ? warnings : errors, message);
  }

  if (diagnostics.length === 0) {
    for (const message of errors) {
      diagnostics.push({ severity: 'error', message, code: '', path: '' });
    }
    for (const message of warnings) {
      diagnostics.push({ severity: 'warning', message, code: '', path: '' });
    }
  }

  return {
    format: source.format || '',
    ok: wire.supported && (typeof source.ok === 'boolean'
      ? source.ok : errors.length === 0),
    canonical: wire.present && wire.supported,
    unsupportedFormat: !wire.supported,
    expectedFormat: wire.expected,
    errors,
    warnings,
    diagnostics,
    contract: record(source.contract),
  };
}

export function projectOrchestrationInspection(
  options: InspectionProjectionOptions = {},
  value: unknown,
): NormalizedInspection {
  const projector = options.normalizeInspection
    ?? normalizeOrchestrationInspection;
  return projector(value, options.inspectionContract);
}

const bridge = orchestrationRegistry as unknown as InspectionWindow;
bridge.orchestrationInspectionMatchesContract =
  orchestrationInspectionMatchesContract;
bridge.normalizeOrchestrationInspection = normalizeOrchestrationInspection;
bridge.projectOrchestrationInspection = projectOrchestrationInspection;
