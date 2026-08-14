import { orchestrationRegistry } from './registry';
import {
  compatibilityContract,
  inspectWireFormat,
  record,
  type ContractRecord,
  type ContractSource,
} from './contracts';
import {
  orchestrationContractFieldsMatch,
  orchestrationResultData,
} from './result';

export interface NormalizedDefinitionWrite {
  format: string;
  canonical: boolean;
  malformed: boolean;
  unsupportedFormat: boolean;
  expectedFormat: string;
  conflict: boolean;
  reason: string;
  operation: string;
  expectedUpdatedAt: number | null;
  currentUpdatedAt: number | null;
}

type DefinitionWriteWindow = Window & {
  _orchestrationDefinitionWriteContract?:
    typeof orchestrationDefinitionWriteContract;
  _orchestrationDefinitionVersion?: typeof orchestrationDefinitionVersion;
  normalizeOrchestrationDefinitionWrite?:
    typeof normalizeOrchestrationDefinitionWrite;
  orchestrationDefinitionWriteConflict?:
    typeof orchestrationDefinitionWriteConflict;
};

export function orchestrationDefinitionWriteContract(
  contractSource?: ContractSource,
): ContractRecord | null {
  return compatibilityContract('definitionWriteContract', contractSource);
}

export function orchestrationDefinitionVersion(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const version = Number(value);
  return Number.isSafeInteger(version) && version >= 0 ? version : null;
}

const DEFINITION_CONFLICT_SEMANTICS = [
  'format', 'reason', 'operation', 'expectedUpdatedAt', 'currentUpdatedAt',
] as const;

function definitionConflictField(
  contract: ContractRecord | null,
  semantic: typeof DEFINITION_CONFLICT_SEMANTICS[number],
  fallback: string,
): string {
  const fields = record(contract?.conflictFields);
  const spec = fields ? record(fields[semantic]) : null;
  return typeof spec?.name === 'string' && spec.name ? spec.name : fallback;
}

function definitionConflictMatches(
  source: ContractRecord,
  body: ContractRecord,
  contract: ContractRecord | null,
): boolean {
  if (!contract || !Object.prototype.hasOwnProperty.call(
    contract, 'conflictFields')) return true;
  const fields = record(contract.conflictFields);
  if (!fields || !DEFINITION_CONFLICT_SEMANTICS.every((semantic) =>
    record(fields[semantic]) != null)
      || !orchestrationContractFieldsMatch(source, fields)) return false;
  const reasonField = record(fields.reason)?.name;
  const operationField = record(fields.operation)?.name;
  const currentField = record(fields.currentUpdatedAt)?.name;
  if (typeof reasonField !== 'string' || typeof operationField !== 'string'
      || typeof currentField !== 'string') return false;
  const reason = source[reasonField];
  return reason === contract.conflictReason
    && Array.isArray(contract.operations)
    && contract.operations.includes(source[operationField])
    && body.ok === false && typeof body.error === 'string'
    && body.conflict === reason
    && Object.prototype.hasOwnProperty.call(body, currentField)
    && body[currentField] === source[currentField];
}

export function normalizeOrchestrationDefinitionWrite(
  value: unknown,
  contractSource?: ContractSource,
): NormalizedDefinitionWrite {
  const root = record(value) ?? {};
  let contract = orchestrationDefinitionWriteContract(contractSource);
  const body = orchestrationResultData(root);
  const source = record(body.write) ?? {};
  const wire = inspectWireFormat('definition-write', source, contractSource);
  const reasonField = definitionConflictField(contract, 'reason', 'reason');
  const operationField = definitionConflictField(
    contract, 'operation', 'operation');
  const expectedField = definitionConflictField(
    contract, 'expectedUpdatedAt', 'expectedUpdatedAt');
  const currentField = definitionConflictField(
    contract, 'currentUpdatedAt', 'currentUpdatedAt');
  const malformed = wire.present && wire.supported
    && !definitionConflictMatches(source, body, contract);
  const reason = String(source[reasonField] || body.conflict || '');
  const expected = orchestrationDefinitionVersion(source[expectedField]);
  const current = orchestrationDefinitionVersion(
    source[currentField] != null
      ? source[currentField] : body[currentField]);
  contract = wire.supported ? contract : null;
  const defaults = compatibilityContract('definitionWriteContract') ?? {};
  const conflictStatus = Number(
    contract?.conflictStatus || defaults.conflictStatus);
  const conflictReason = String(
    contract?.conflictReason || defaults.conflictReason);
  return {
    format: String(source.format || ''),
    canonical: wire.present && wire.supported && !malformed,
    malformed,
    unsupportedFormat: !wire.supported,
    expectedFormat: wire.expected,
    conflict: wire.supported && !malformed
      && Number(root.status || 0) === conflictStatus
      && Boolean(conflictReason) && reason === conflictReason,
    reason,
    operation: String(source[operationField] || ''),
    expectedUpdatedAt: expected,
    currentUpdatedAt: current,
  };
}

export function orchestrationDefinitionWriteConflict(
  value: unknown,
  operation: string,
  contractSource?: ContractSource,
): NormalizedDefinitionWrite | null {
  const contract = orchestrationDefinitionWriteContract(contractSource);
  const write = normalizeOrchestrationDefinitionWrite(value, contract);
  if (write.unsupportedFormat) return null;
  if (contract && Array.isArray(contract.operations)
      && !contract.operations.includes(operation)) return null;
  if (!write.conflict) return null;
  if (operation && write.operation && write.operation !== operation) {
    return null;
  }
  return write;
}

const bridge = orchestrationRegistry as unknown as DefinitionWriteWindow;
bridge._orchestrationDefinitionWriteContract =
  orchestrationDefinitionWriteContract;
bridge._orchestrationDefinitionVersion = orchestrationDefinitionVersion;
bridge.normalizeOrchestrationDefinitionWrite =
  normalizeOrchestrationDefinitionWrite;
bridge.orchestrationDefinitionWriteConflict =
  orchestrationDefinitionWriteConflict;
