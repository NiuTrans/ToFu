import { record, type ContractRecord } from './contracts';
import { orchestrationContractFieldsMatch } from './result';

const MUTATION_PAYLOAD_SEMANTICS = [
  'format', 'ok', 'action', 'reason', 'targetId', 'resourceStatus',
  'resourceTerminal', 'targetExists', 'retryable', 'reconcileRequired',
] as const;

export type MutationPayloadSemantic =
  typeof MUTATION_PAYLOAD_SEMANTICS[number];

export function mutationPayloadField(
  contract: ContractRecord,
  semantic: MutationPayloadSemantic,
  fallback: string,
): string {
  const fields = record(contract.payloadFields);
  const spec = fields ? record(fields[semantic]) : null;
  return typeof spec?.name === 'string' && spec.name ? spec.name : fallback;
}

export function mutationPayloadMatches(
  source: ContractRecord,
  contract: ContractRecord,
): boolean {
  if (!Object.prototype.hasOwnProperty.call(contract, 'payloadFields')) {
    return true;
  }
  const fields = record(contract.payloadFields);
  if (!fields || !MUTATION_PAYLOAD_SEMANTICS.every((semantic) =>
    record(fields[semantic]) != null)
      || !orchestrationContractFieldsMatch(source, fields)) return false;
  const action = record(fields.action)?.name;
  const reason = record(fields.reason)?.name;
  return typeof action === 'string' && Array.isArray(contract.actions)
    && contract.actions.includes(source[action])
    && typeof reason === 'string' && Array.isArray(contract.reasons)
    && contract.reasons.includes(source[reason]);
}

export function malformedMutation(
  source: ContractRecord,
  expectedFormat: string,
  httpStatus: number,
) {
  return {
    format: String(source.format || ''), canonical: false,
    unsupportedFormat: false, expectedFormat, ok: false,
    action: '', reason: 'malformed_response', targetId: '',
    resourceStatus: '', resourceTerminal: null, targetExists: null,
    retryable: false, reconcileRequired: true, httpStatus,
  };
}
