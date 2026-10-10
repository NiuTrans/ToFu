import { orchestrationRegistry } from './registry';
import {
  compatibilityContract,
  record,
  resolveDirectContractSource,
  type ContractRecord,
  type ContractSource,
} from './contracts';

type DurableRunSnapshotWindow = Window & {
  _orchestrationDurableRunFields?: typeof orchestrationDurableRunFields;
  projectOrchestrationDurableRunSnapshot?:
    typeof projectOrchestrationDurableRunSnapshot;
  _orchestrationDurableRunMatches?: typeof orchestrationDurableRunMatches;
  _orchestrationRunStatuses?: typeof orchestrationRunStatuses;
};

export interface DurableRunFields {
  id: string;
  status: string;
  terminal: string;
  outcome: string;
}

export function orchestrationDurableRunFields(
  source?: unknown,
): DurableRunFields {
  const defaults = compatibilityContract('durableRunContract') ?? {};
  const contract = record(source) ?? {};
  return {
    id: String(contract.idField || defaults.idField || 'id'),
    status: String(contract.statusField || defaults.statusField || 'status'),
    terminal: String(
      contract.terminalField || defaults.terminalField || 'terminal'),
    outcome: String(
      contract.outcomeField || defaults.outcomeField || 'outcome'),
  };
}

export function projectOrchestrationDurableRunSnapshot(
  value: unknown,
  source?: unknown,
): ContractRecord | null {
  const run = record(value);
  if (!run) return null;
  const fields = orchestrationDurableRunFields(source);
  const projected: ContractRecord = {
    ...run,
    id: run[fields.id] == null ? '' : String(run[fields.id]),
    status: run[fields.status] == null ? '' : String(run[fields.status]),
  };
  if (Object.prototype.hasOwnProperty.call(run, fields.terminal)) {
    projected.terminal = run[fields.terminal];
  }
  if (Object.prototype.hasOwnProperty.call(run, fields.outcome)) {
    projected.outcome = run[fields.outcome];
  }
  return projected;
}

export function orchestrationRunStatuses(
  statusContractSource?: ContractSource,
): unknown[] | null {
  const contract = resolveDirectContractSource(statusContractSource);
  return contract && Array.isArray(contract.statuses)
    ? contract.statuses : null;
}

export function orchestrationDurableRunMatches(
  value: unknown,
  fieldName: string,
  source?: unknown,
  statusContractSource?: ContractSource,
  expectedId?: unknown,
): boolean {
  const run = record(value);
  if (!run) return false;
  const contract = record(source);
  const fieldMap = orchestrationDurableRunFields(contract);
  if (expectedId && String(run[fieldMap.id] || '') !== String(expectedId)) {
    return false;
  }
  if (!contract) return true;
  const requiredFields = contract[fieldName];
  const fieldsMatch = Array.isArray(requiredFields) && requiredFields.every(
    (field) => typeof field === 'string'
      && Object.prototype.hasOwnProperty.call(run, field));
  if (!fieldsMatch) return false;
  const statuses = orchestrationRunStatuses(statusContractSource);
  return !Array.isArray(statuses) || statuses.includes(run[fieldMap.status]);
}

Object.assign(orchestrationRegistry as unknown as DurableRunSnapshotWindow, {
  _orchestrationDurableRunFields: orchestrationDurableRunFields,
  projectOrchestrationDurableRunSnapshot,
  _orchestrationDurableRunMatches: orchestrationDurableRunMatches,
  _orchestrationRunStatuses: orchestrationRunStatuses,
});
