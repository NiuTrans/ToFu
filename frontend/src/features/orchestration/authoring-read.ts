import { orchestrationRegistry } from './registry';
import { inspectWireFormat, record, type ContractRecord } from './contracts';
import {
  orchestrationActionRead,
  orchestrationActionReason,
  orchestrationHttpFailureReason,
  orchestrationHttpRead,
  orchestrationRequiredResponseFieldsMatch,
  registerOrchestrationHttpReadProjectors,
  type OrchestrationReadOptions,
} from './read-core';
import { orchestrationResultError } from './result';
import { orchestrationInspectionMatchesContract } from './inspection-result';

type AuthoringReadWindow = Window & {
  _orchestrationAuthoringContractProblems?:
    (body: ContractRecord) => string[];
  normalizeOrchestrationComposeResult?:
    typeof normalizeOrchestrationComposeResult;
  normalizeOrchestrationAuthoringContractRead?:
    typeof normalizeOrchestrationAuthoringContractRead;
  normalizeOrchestrationValidationRead?:
    typeof normalizeOrchestrationValidationRead;
  _orchestrationDefinitionActionRead?:
    typeof orchestrationDefinitionActionRead;
  normalizeOrchestrationBuiltinRead?: typeof normalizeOrchestrationBuiltinRead;
  normalizeOrchestrationLayoutRead?: typeof normalizeOrchestrationLayoutRead;
  projectOrchestrationLayoutPositions?: (
    definition: unknown, expectedDefinition?: unknown,
  ) => {
    ok: boolean;
    positions?: unknown;
    code?: string;
    path?: string;
    cause?: unknown;
  };
};

function authoringContractProblems(body: ContractRecord): string[] {
  const validate = (orchestrationRegistry as unknown as AuthoringReadWindow)
    ._orchestrationAuthoringContractProblems
    ?? (globalThis as unknown as AuthoringReadWindow)
      ._orchestrationAuthoringContractProblems;
  if (!validate) throw new Error('Orchestration authoring validator is not ready');
  return validate(body);
}

export function normalizeOrchestrationComposeResult(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const read = orchestrationHttpRead(value);
  const body = record(read.body) ?? {};
  const successDefinition = body.ok !== true
    || Boolean(record(body.definition));
  const inspectionMatches = body.ok !== true || body.inspection == null
    || orchestrationInspectionMatchesContract(
      body.inspection, options.inspectionContract);
  const responseMatches = !read.normalized
    || orchestrationRequiredResponseFieldsMatch(body, options)
      && typeof body.reply === 'string'
      && (body.definition === null || Boolean(record(body.definition)))
      && (body.validation === null || Boolean(record(body.validation)))
      && (body.error === null || typeof body.error === 'string');
  const accepted = read.transportOk && typeof body.ok === 'boolean'
    && successDefinition && inspectionMatches && responseMatches;
  const reason = accepted ? 'accepted' : orchestrationHttpFailureReason(read);
  return {
    ok: accepted,
    transportOk: read.transportOk,
    status: read.status,
    malformed: read.transportOk && !accepted,
    reason,
    result: accepted ? body : null,
    error: accepted ? '' : orchestrationResultError(value, ''),
  };
}

export function normalizeOrchestrationAuthoringContractRead(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const read = orchestrationHttpRead(value);
  const body = record(read.body) ?? {};
  const wire = inspectWireFormat('authoring-contract', body);
  const missingFields = authoringContractProblems(body);
  const contract = read.transportOk && body.ok === true && wire.supported
    && (!read.normalized
      || orchestrationRequiredResponseFieldsMatch(body, options))
    && !missingFields.length ? body : null;
  return {
    ok: Boolean(contract),
    status: read.status,
    notFound: !read.transportOk && read.status === 404,
    malformed: read.transportOk && !contract && wire.supported,
    unsupportedFormat: read.transportOk && !wire.supported,
    retryable: !read.transportOk && (read.status === 0 || read.status >= 500),
    expectedFormat: wire.expected,
    wireFormat: wire.actual,
    missingFields,
    contract,
    error: contract ? '' : orchestrationResultError(value, ''),
  };
}

export function normalizeOrchestrationValidationRead(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const read = orchestrationActionRead(value);
  const body = record(read.body) ?? {};
  const nested = record(body.inspection);
  const inspection = read.transportOk && nested
    && typeof nested.ok === 'boolean' ? nested
    : read.transportOk && typeof body.ok === 'boolean' ? body : null;
  const wire = inspectWireFormat(
    'inspection', inspection, options.inspectionContract);
  const accepted = Boolean(inspection) && wire.supported
    && orchestrationInspectionMatchesContract(
      inspection, options.inspectionContract);
  return {
    ok: accepted,
    status: read.status,
    reason: !wire.supported ? 'unsupported-format'
      : orchestrationActionReason(read, accepted, 'validation-rejected'),
    expectedFormat: wire.expected,
    wireFormat: wire.actual,
    inspection: accepted ? inspection : null,
    error: accepted ? '' : orchestrationResultError(value, ''),
  };
}

export function orchestrationDefinitionActionRead(
  value: unknown,
  rejectedReason: string,
  options: OrchestrationReadOptions = {},
) {
  const read = orchestrationActionRead(value);
  const body = record(read.body) ?? {};
  const fields = options.responseRequiredFields;
  const responseMatches = !read.normalized
    || orchestrationRequiredResponseFieldsMatch(body, options)
      && (!Array.isArray(fields) || !fields.includes('definitionSource')
        || typeof body.definitionSource === 'string')
      && (!Array.isArray(fields) || !fields.includes('inspection')
        || orchestrationInspectionMatchesContract(
          body.inspection, options.inspectionContract));
  const definition = read.transportOk && body.ok === true
    && responseMatches ? record(body.definition) : null;
  const accepted = Boolean(definition);
  const reason = accepted ? 'accepted'
    : read.transportOk && body.ok === false ? rejectedReason
      : orchestrationHttpFailureReason(read);
  return {
    ok: accepted,
    status: read.status,
    reason,
    definition,
    inspection: accepted ? record(body.inspection) : null,
    definitionSource: accepted ? String(body.definitionSource || '') : '',
    error: accepted ? '' : orchestrationResultError(value, ''),
  };
}

export function normalizeOrchestrationBuiltinRead(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const result = orchestrationDefinitionActionRead(
    value, 'builtin-rejected', options);
  if (!result.ok && result.status === 404) result.reason = 'not-found';
  return result;
}

export function normalizeOrchestrationLayoutRead(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const result = orchestrationDefinitionActionRead(
    value, 'layout-rejected', options);
  if (!result.ok) return result;
  const registry = orchestrationRegistry as unknown as AuthoringReadWindow;
  const published = globalThis as unknown as AuthoringReadWindow;
  const projector = registry.projectOrchestrationLayoutPositions
    ?? published.projectOrchestrationLayoutPositions;
  const expectedDefinition = options.requestArgs?.[0];
  const projection = typeof projector === 'function'
    ? projector(result.definition, expectedDefinition)
    : { ok: false, code: 'layout.projection.unavailable', path: '' };
  return projection.ok ? { ...result, positions: projection.positions } : {
    ...result,
    ok: false,
    reason: 'malformed-response',
    definition: null,
    positions: null,
    projection,
    cause: projection.cause,
    error: projection.code || 'invalid-layout',
  };
}

registerOrchestrationHttpReadProjectors({
  compose: normalizeOrchestrationComposeResult,
  'authoring-contract': normalizeOrchestrationAuthoringContractRead,
  validation: normalizeOrchestrationValidationRead,
  builtin: normalizeOrchestrationBuiltinRead,
  layout: normalizeOrchestrationLayoutRead,
});

Object.assign(orchestrationRegistry as unknown as AuthoringReadWindow, {
  normalizeOrchestrationComposeResult,
  normalizeOrchestrationAuthoringContractRead,
  normalizeOrchestrationValidationRead,
  _orchestrationDefinitionActionRead: orchestrationDefinitionActionRead,
  normalizeOrchestrationBuiltinRead,
  normalizeOrchestrationLayoutRead,
});
