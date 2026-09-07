import { orchestrationRegistry } from './registry';
import {
  compatibilityContract,
  inspectWireFormat,
  record,
  type ContractRecord,
} from './contracts';
import {
  normalizeOrchestrationMutation,
  type NormalizedMutation,
} from './mutation-result';
import { orchestrationInspectionMatchesContract } from './inspection-result';
import {
  orchestrationActionReason,
  orchestrationActionRead,
  registerOrchestrationHttpReadProjectors,
  type OrchestrationReadOptions,
} from './read-core';
import {
  orchestrationResultError,
} from './result';

type RuntimeReadWindow = Window & {
  normalizeOrchestrationPlanRead?: typeof normalizeOrchestrationPlanRead;
  _normalizeOrchestrationRuntimeStart?:
    typeof normalizeOrchestrationRuntimeStart;
  normalizeOrchestrationRunStart?: typeof normalizeOrchestrationRunStart;
  normalizeOrchestrationMutationRead?:
    typeof normalizeOrchestrationMutationRead;
};

function orchestrationPlanStep(value: unknown): boolean {
  const step = record(value);
  return Boolean(step) && typeof step?.node_id === 'string' && Boolean(step.node_id)
    && typeof step.action === 'string' && Boolean(step.action);
}

function orchestrationPlanEvidenceMatches(
  body: ContractRecord,
  options: OrchestrationReadOptions,
): boolean {
  const fields = options.responseRequiredFields;
  return Array.isArray(fields) && fields.length > 0
    && fields.every((field) => typeof field === 'string'
      && Object.prototype.hasOwnProperty.call(body, field))
    && orchestrationInspectionMatchesContract(
      body.inspection, options.inspectionContract)
    && Array.isArray(body.warnings)
    && body.warnings.every((warning) => typeof warning === 'string')
    && Boolean(record(body.contract))
    && typeof body.definitionSource === 'string'
    && (body.error === null || typeof body.error === 'string');
}

export function normalizeOrchestrationPlanRead(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const read = orchestrationActionRead(value);
  const body = record(read.body) ?? {};
  const evidenceMatches = !read.normalized || body.ok !== true
    || orchestrationPlanEvidenceMatches(body, options);
  const accepted = read.recognized && body.ok === true
    && evidenceMatches
    && Array.isArray(body.steps) && body.steps.every(orchestrationPlanStep);
  const reason = orchestrationActionReason(
    read, accepted, 'plan-rejected');
  return {
    ok: accepted,
    status: read.status,
    reason,
    steps: accepted ? body.steps : [],
    plan: accepted ? body : null,
    error: accepted ? '' : orchestrationResultError(value, ''),
  };
}

export function normalizeOrchestrationRuntimeStart(
  value: unknown,
  kind: string,
  rejectedReason: string,
  options: OrchestrationReadOptions = {},
) {
  const read = orchestrationActionRead(value);
  const body = record(read.body) ?? {};
  const hasIdentity = Object.prototype.hasOwnProperty.call(body, 'start');
  const identity = record(body.start);
  const wire = inspectWireFormat(
    'runtime-start', identity, options.runtimeStartContract);
  const contract = wire.contract ?? {};
  const defaults = compatibilityContract('runtimeStartContract') ?? {};
  const kindField = String(contract.kindField || defaults.kindField);
  const idField = String(contract.idField || defaults.idField);
  const kindSupported = !Array.isArray(contract.kinds)
    || contract.kinds.includes(kind);
  const canonicalId = identity && wire.supported && kindSupported
    && identity[kindField] === kind ? String(identity[idField] || '') : '';
  const runtimeId = canonicalId;
  const evidenceFields = options.responseRequiredFields;
  const identityFields = ['ok', 'start'];
  const evidenceMatches = !wire.present || body.ok !== true
    || Array.isArray(evidenceFields) && evidenceFields.length > 0
      && evidenceFields.every((field) => typeof field === 'string'
        && (identityFields.includes(field)
          || Object.prototype.hasOwnProperty.call(body, field)))
      && orchestrationInspectionMatchesContract(
        body.inspection, options.inspectionContract)
      && Array.isArray(body.warnings)
      && body.warnings.every((warning) => typeof warning === 'string')
      && Boolean(record(body.contract))
      && typeof body.definitionSource === 'string';
  const successStatuses = record(contract.successStatuses)
    ?? record(defaults.successStatuses) ?? {};
  const statusMatches = !read.normalized || !wire.present
    || read.status === Number(successStatuses[kind]);
  const accepted = read.recognized && body.ok === true
    && hasIdentity && wire.present && wire.supported && kindSupported
    && Boolean(runtimeId) && evidenceMatches && statusMatches;
  const failedRuntimeId = !accepted && read.envelope
    && body.ok === false && kind === 'durable' ? canonicalId : '';
  const actionReason = orchestrationActionReason(
    read, accepted, rejectedReason);
  return {
    ok: accepted,
    status: read.status,
    reason: !wire.supported ? 'unsupported-format'
      : !kindSupported ? 'unsupported-kind'
        : actionReason,
    runtimeId: accepted ? runtimeId : '',
    failedRuntimeId,
    identity: accepted && hasIdentity ? identity : null,
    canonical: accepted,
    expectedFormat: wire.expected,
    wireFormat: wire.actual,
    data: accepted ? body : null,
    error: accepted ? '' : orchestrationResultError(value, ''),
  };
}

export function normalizeOrchestrationRunStart(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const start = normalizeOrchestrationRuntimeStart(
    value, 'ephemeral', 'run-rejected', options);
  return {
    ok: start.ok,
    status: start.status,
    reason: start.reason,
    taskId: start.runtimeId,
    identity: start.identity,
    canonical: start.canonical,
    expectedFormat: start.expectedFormat,
    wireFormat: start.wireFormat,
    start: start.data,
    error: start.error,
  };
}

export function normalizeOrchestrationMutationRead(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  let mutation: NormalizedMutation = normalizeOrchestrationMutation(
    value, options);
  const expectedTarget = Array.isArray(options.requestArgs)
    ? String(options.requestArgs[0] || '') : '';
  if (mutation.canonical && expectedTarget
      && mutation.targetId !== expectedTarget) {
    mutation = {
      ...mutation,
      ok: false,
      reason: 'target_mismatch',
      resourceStatus: '',
      resourceTerminal: null,
      targetExists: null,
      retryable: false,
      reconcileRequired: true,
    };
  }
  return {
    ok: Boolean(mutation.ok),
    status: Number(mutation.httpStatus || 0),
    reason: mutation.reason,
    mutation,
    response: value,
    retryable: Boolean(mutation.retryable),
    error: mutation.ok ? '' : orchestrationResultError(value, ''),
  };
}

registerOrchestrationHttpReadProjectors({
  plan: normalizeOrchestrationPlanRead,
  'run-start': normalizeOrchestrationRunStart,
  mutation: normalizeOrchestrationMutationRead,
});

Object.assign(orchestrationRegistry as unknown as RuntimeReadWindow, {
  normalizeOrchestrationPlanRead,
  _normalizeOrchestrationRuntimeStart: normalizeOrchestrationRuntimeStart,
  normalizeOrchestrationRunStart,
  normalizeOrchestrationMutationRead,
});
