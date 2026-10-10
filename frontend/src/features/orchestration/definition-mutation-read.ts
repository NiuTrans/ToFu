import { orchestrationRegistry } from './registry';
import {
  compatibilityContract,
  inspectWireFormat,
  record,
} from './contracts';
import {
  normalizeOrchestrationDefinitionWrite,
  orchestrationDefinitionVersion,
  orchestrationDefinitionWriteConflict,
} from './definition-write-result';
import { orchestrationDefinitionEntryMatches } from './definition-response-contract';
import { orchestrationInspectionMatchesContract } from './inspection-result';
import {
  orchestrationHttpFailureReason,
  orchestrationHttpRead,
  orchestrationRequiredResponseFieldsMatch,
  registerOrchestrationHttpReadProjectors,
  type OrchestrationReadOptions,
} from './read-core';
import { orchestrationResultError, orchestrationResultOk } from './result';

type DefinitionMutationReadWindow = Window & {
  normalizeOrchestrationDefinitionSave?:
    typeof normalizeOrchestrationDefinitionSave;
  normalizeOrchestrationDefinitionDelete?:
    typeof normalizeOrchestrationDefinitionDelete;
};

export function normalizeOrchestrationDefinitionSave(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const read = orchestrationHttpRead(value);
  const body = record(read.body) ?? {};
  const write = normalizeOrchestrationDefinitionWrite(
    value, options.definitionWriteContract);
  const conflict = orchestrationDefinitionWriteConflict(
    value, 'replace', options.definitionWriteContract);
  const rejected = read.transportOk && body.ok === false;
  const wire = inspectWireFormat(
    'definition-entry', body, options.definitionEntryContract);
  const contract = wire.contract ?? {};
  const defaults = compatibilityContract('definitionEntryContract') ?? {};
  const versionField = String(
    contract.versionField || defaults.versionField);
  const version = orchestrationDefinitionVersion(body[versionField]);
  const versionRequired = contract.versionRequiredOnWrite !== false;
  const entryLike = typeof body.id === 'string'
    || version !== null
    || body.inspection != null && typeof body.inspection === 'object';
  const requiredFields = options.responseRequiredFields;
  const responseMatches = !read.normalized || !wire.present
    || orchestrationRequiredResponseFieldsMatch(body, options)
      && (!Array.isArray(requiredFields)
        || !requiredFields.includes('inspection')
        || orchestrationInspectionMatchesContract(
          body.inspection, options.inspectionContract));
  const accepted = read.transportOk && !rejected && entryLike
    && (!versionRequired || version !== null) && wire.supported
    && responseMatches
    && !write.unsupportedFormat
    && (!wire.present || orchestrationDefinitionEntryMatches(
      body,
      contract,
      Array.isArray(options.requestArgs) ? options.requestArgs[0] : '',
    ));
  const reason = accepted ? 'accepted'
    : conflict ? 'write-conflict'
      : write.unsupportedFormat ? 'unsupported-format'
        : read.transportOk && rejected ? 'save-rejected'
          : read.transportOk && !wire.supported ? 'unsupported-format'
            : orchestrationHttpFailureReason(read);
  return {
    ok: accepted,
    status: read.status,
    reason,
    expectedFormat: wire.expected,
    wireFormat: wire.actual,
    canonical: wire.present && wire.supported,
    unsupportedWriteFormat: write.unsupportedFormat,
    writeExpectedFormat: write.expectedFormat,
    writeWireFormat: write.format,
    versionField,
    version: accepted ? version : null,
    data: accepted ? body : {},
    conflict,
    error: accepted ? '' : orchestrationResultError(value, ''),
  };
}

export function normalizeOrchestrationDefinitionDelete(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const read = orchestrationHttpRead(value, {
    directOk: (direct) => typeof direct === 'boolean'
      || Boolean(direct) && typeof direct === 'object',
  });
  const conflict = orchestrationDefinitionWriteConflict(
    value, 'delete', options.definitionWriteContract);
  const write = normalizeOrchestrationDefinitionWrite(
    value, options.definitionWriteContract);
  const responseMatches = !read.normalized
    || orchestrationRequiredResponseFieldsMatch(
      record(read.body) ?? {}, options);
  const accepted = value === true
    || read.transportOk && orchestrationResultOk(value)
      && responseMatches
      && !write.unsupportedFormat;
  const reason = accepted ? 'accepted'
    : conflict ? 'write-conflict'
      : write.unsupportedFormat ? 'unsupported-format'
        : read.status === 404 ? 'not-found'
          : read.transportOk && !responseMatches ? 'malformed-response'
          : read.transportOk ? 'delete-rejected'
            : orchestrationHttpFailureReason(read);
  return {
    ok: accepted,
    status: read.status,
    reason,
    conflict,
    unsupportedWriteFormat: write.unsupportedFormat,
    writeExpectedFormat: write.expectedFormat,
    writeWireFormat: write.format,
    error: accepted ? '' : orchestrationResultError(value, ''),
  };
}

registerOrchestrationHttpReadProjectors({
  'definition-save': normalizeOrchestrationDefinitionSave,
  'definition-delete': normalizeOrchestrationDefinitionDelete,
});

Object.assign(orchestrationRegistry as unknown as DefinitionMutationReadWindow, {
  normalizeOrchestrationDefinitionSave,
  normalizeOrchestrationDefinitionDelete,
});
