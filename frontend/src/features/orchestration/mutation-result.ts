import { orchestrationRegistry } from './registry';
import {
  compatibilityContract,
  inspectWireFormat,
  publishedContract,
  record,
  type ContractRecord,
  type ContractSource,
} from './contracts';
import {
  orchestrationRequiredResponseFieldsMatch,
  orchestrationResultData,
  orchestrationResultError,
  orchestrationResultOk,
} from './result';
import {
  malformedMutation,
  mutationPayloadField,
  mutationPayloadMatches,
} from './mutation-payload-contract';
import type { OrchestrationReadOptions } from './read-core';

export interface NormalizedMutation {
  format: string;
  canonical: boolean;
  unsupportedFormat: boolean;
  expectedFormat: string;
  ok: boolean;
  action: string;
  reason: string;
  targetId: string;
  resourceStatus: string;
  resourceTerminal: boolean | null;
  targetExists: boolean | null;
  retryable: boolean;
  reconcileRequired: boolean;
  httpStatus: number;
}

type MutationWindow = Window & {
  _orchestrationMutationLegacyValue?: typeof orchestrationMutationLegacyValue;
  normalizeOrchestrationMutation?: typeof normalizeOrchestrationMutation;
  orchestrationMutationMessage?: typeof orchestrationMutationMessage;
};

export function orchestrationMutationLegacyValue(
  body: ContractRecord,
  fields: unknown,
): string {
  const names = Array.isArray(fields) ? fields : [];
  for (const field of names) {
    const value = body[String(field)];
    if (value != null && value !== '') return String(value);
  }
  return '';
}

export function normalizeOrchestrationMutation(
  value: unknown,
  contractSource?: ContractSource | OrchestrationReadOptions,
): NormalizedMutation {
  const root = record(value) ?? {};
  const body = orchestrationResultData(root);
  const source = record(body.mutation) ?? record(root.mutation) ?? {};
  const options = record(contractSource);
  const publishedSource = options?.mutationContract ?? contractSource;
  const published = publishedContract('mutationContract', publishedSource);
  const wire = inspectWireFormat('mutation', source, published);
  const contract = compatibilityContract('mutationContract', wire.contract) ?? {};
  const defaults = compatibilityContract('mutationContract') ?? {};
  const transportFailureReason = String(contract.transportFailureReason
    || defaults.transportFailureReason);
  const httpStatus = Number(root.status || 0);
  if (root.ok === true
      && !orchestrationRequiredResponseFieldsMatch(body, options ?? {})) {
    return malformedMutation(source, wire.expected, httpStatus);
  }
  if (!wire.supported) {
    return {
      format: String(source.format || ''), canonical: false,
      unsupportedFormat: true, expectedFormat: wire.expected, ok: false,
      action: '', reason: 'unsupported_format', targetId: '',
      resourceStatus: '', resourceTerminal: null, targetExists: null,
      retryable: false, reconcileRequired: true, httpStatus,
    };
  }
  if (wire.present && !mutationPayloadMatches(source, contract)) {
    return malformedMutation(source, wire.expected, httpStatus);
  }
  const okField = mutationPayloadField(contract, 'ok', 'ok');
  const actionField = mutationPayloadField(contract, 'action', 'action');
  const reasonField = mutationPayloadField(contract, 'reason', 'reason');
  const retryableField = mutationPayloadField(
    contract, 'retryable', 'retryable');
  const ok = typeof source[okField] === 'boolean'
    ? source[okField] : orchestrationResultOk(root);
  let reason = String(source[reasonField] || body.mutation_reason || '');
  if (!reason) {
    reason = ok ? 'accepted'
      : httpStatus === 0 ? transportFailureReason
        : httpStatus === 404 ? 'not_found'
          : httpStatus >= 500 ? 'persistence_failed' : 'conflict';
  }
  const retryableReasons = Array.isArray(contract.clientRetryableReasons)
    ? contract.clientRetryableReasons
    : Array.isArray(contract.retryableReasons)
      ? contract.retryableReasons
      : Array.isArray(defaults.clientRetryableReasons)
        ? defaults.clientRetryableReasons : [];
  const reconcileField = mutationPayloadField(
    contract, 'reconcileRequired', String(
      contract.reconcileField || defaults.reconcileField));
  const targetExistsField = mutationPayloadField(
    contract, 'targetExists', String(
      contract.targetExistsField || defaults.targetExistsField));
  const terminalField = mutationPayloadField(
    contract, 'resourceTerminal', String(
      contract.resourceTerminalField || defaults.resourceTerminalField));
  const retryable = typeof source[retryableField] === 'boolean'
    ? source[retryableField] as boolean
    : (!ok && retryableReasons.includes(reason));
  const reconcileRequired = typeof source[reconcileField] === 'boolean'
    ? source[reconcileField] as boolean : !ok;
  const targetExists = typeof source[targetExistsField] === 'boolean'
    ? source[targetExistsField] as boolean
    : reason === 'not_found' ? false : null;
  const resourceTerminal = typeof source[terminalField] === 'boolean'
    ? source[terminalField] as boolean : null;
  const targetIdField = mutationPayloadField(
    contract, 'targetId', 'target_id');
  const resourceStatusField = mutationPayloadField(
    contract, 'resourceStatus', 'resource_status');
  const targetId = String(source[targetIdField] || '')
    || orchestrationMutationLegacyValue(
      body, Array.isArray(contract.legacyTargetFields)
        ? contract.legacyTargetFields : defaults.legacyTargetFields);
  const resourceStatus = String(source[resourceStatusField] || '')
    || orchestrationMutationLegacyValue(
      body, Array.isArray(contract.legacyStatusFields)
        ? contract.legacyStatusFields : defaults.legacyStatusFields);
  return {
    format: String(source.format || ''),
    canonical: wire.present && wire.supported,
    unsupportedFormat: !wire.supported,
    expectedFormat: wire.expected,
    ok: Boolean(ok),
    action: String(source[actionField] || body.action || ''),
    reason,
    targetId,
    resourceStatus,
    resourceTerminal,
    targetExists,
    retryable,
    reconcileRequired,
    httpStatus,
  };
}

export function orchestrationMutationMessage(
  value: unknown,
  translate?: (key: string) => unknown,
  fallback?: unknown,
  contractSource?: ContractSource,
): string {
  const mutation = normalizeOrchestrationMutation(value, contractSource);
  if (mutation.ok) return '';
  const keys = mutation.action
    ? [`orch.mutation.${mutation.action}.${mutation.reason}`,
      `orch.mutation.reason.${mutation.reason}`]
    : [`orch.mutation.reason.${mutation.reason}`];
  if (translate) {
    for (const key of keys) {
      const localized = translate(key);
      if (localized && localized !== key) return String(localized);
    }
  }
  return orchestrationResultError(value, fallback);
}

const bridge = orchestrationRegistry as unknown as MutationWindow;
bridge._orchestrationMutationLegacyValue = orchestrationMutationLegacyValue;
bridge.normalizeOrchestrationMutation = normalizeOrchestrationMutation;
bridge.orchestrationMutationMessage = orchestrationMutationMessage;
