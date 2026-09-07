import { orchestrationRegistry } from './registry';
import {
  compatibilityContract,
  inspectWireFormat,
  record,
  wireContractSpec,
} from './contracts';
import { orchestrationDefinitionVersion } from './definition-write-result';
import {
  orchestrationDefinitionEntryMatches,
  orchestrationDefinitionFields,
  orchestrationDefinitionListMatches,
} from './definition-response-contract';
import {
  orchestrationHttpFailureReason,
  orchestrationHttpRead,
  orchestrationRequiredResponseFieldsMatch,
  registerOrchestrationHttpReadProjectors,
  type OrchestrationReadOptions,
} from './read-core';
import { orchestrationResultError } from './result';

type DefinitionReadWindow = Window & {
  normalizeOrchestrationDefinitionRead?:
    typeof normalizeOrchestrationDefinitionRead;
  normalizeOrchestrationDefinitionListRead?:
    typeof normalizeOrchestrationDefinitionListRead;
};

export function normalizeOrchestrationDefinitionRead(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const read = orchestrationHttpRead(value);
  const body = record(read.body) ?? {};
  const rejected = read.transportOk && body.ok === false;
  const wire = inspectWireFormat(
    'definition-entry', body, options.definitionEntryContract);
  const contract = wire.contract ?? {};
  const defaults = compatibilityContract('definitionEntryContract') ?? {};
  const expectedId = Array.isArray(options.requestArgs)
    ? options.requestArgs[0] : '';
  const hasDefinition = body.definition != null
    && typeof body.definition === 'object';
  const responseMatches = !read.normalized || !wire.present
    || orchestrationRequiredResponseFieldsMatch(body, options);
  const entry = read.transportOk && !rejected && hasDefinition
    && wire.supported && responseMatches
    && (!wire.present || orchestrationDefinitionEntryMatches(
      body, contract, expectedId))
    ? orchestrationDefinitionFields(body, contract.fields) : null;
  const versionField = String(
    contract.versionField || defaults.versionField);
  const reason = entry ? 'accepted'
    : !read.transportOk && read.status === 404 ? 'not-found'
      : read.transportOk && !rejected && !wire.supported
        ? 'unsupported-format'
        : read.transportOk && rejected ? 'read-rejected'
          : orchestrationHttpFailureReason(read);
  return {
    ok: Boolean(entry),
    status: read.status,
    reason,
    notFound: reason === 'not-found',
    malformed: reason === 'malformed-response',
    expectedFormat: wire.expected,
    wireFormat: wire.actual,
    canonical: wire.present && wire.supported,
    versionField,
    version: entry
      ? orchestrationDefinitionVersion(entry[versionField]) : null,
    entry,
    error: entry ? '' : orchestrationResultError(value, ''),
  };
}

export function normalizeOrchestrationDefinitionListRead(
  value: unknown,
  options: OrchestrationReadOptions = {},
) {
  const read = orchestrationHttpRead(value);
  const body = read.body;
  const bodyRecord = record(body) ?? {};
  const rejected = read.transportOk && !Array.isArray(body)
    && bodyRecord.ok === false;
  const wire = inspectWireFormat(
    'definition-list', body, options.definitionListContract);
  const contract = wire.contract ?? {};
  const entrySpec = wireContractSpec(
    'definition-entry', options.definitionEntryContract);
  const entryDefaults = compatibilityContract('definitionEntryContract') ?? {};
  const versionField = String(
    entrySpec.contract?.versionField || entryDefaults.versionField);
  const items = Array.isArray(body) ? body
    : Array.isArray(bodyRecord.items) ? bodyRecord.items : null;
  const responseMatches = !read.normalized || !wire.present
    || orchestrationRequiredResponseFieldsMatch(bodyRecord, options);
  const accepted = read.transportOk && !rejected && Boolean(items)
    && wire.supported && responseMatches
    && (!wire.present || orchestrationDefinitionListMatches(
      items ?? [], contract, versionField));
  const reason = accepted ? 'accepted'
    : read.transportOk && !rejected && !wire.supported
      ? 'unsupported-format'
      : read.transportOk && rejected ? 'read-rejected'
        : orchestrationHttpFailureReason(read);
  return {
    ok: accepted,
    status: read.status,
    reason,
    expectedFormat: wire.expected,
    wireFormat: wire.actual,
    canonical: wire.present && wire.supported,
    orderBy: Array.isArray(contract.orderBy)
      ? contract.orderBy.slice() : [],
    items: accepted ? (items ?? []).map((item) => {
      const source = record(item);
      if (!source) return item;
      const projected = orchestrationDefinitionFields(
        source, contract.itemFields);
      if (contract.definitionIncluded === false) delete projected.definition;
      if (entrySpec.contract) {
        projected.definitionVersion = orchestrationDefinitionVersion(
          source[versionField]);
      }
      return projected;
    }) : [],
    versionField,
    error: accepted ? '' : orchestrationResultError(value, ''),
  };
}

registerOrchestrationHttpReadProjectors({
  'definition-list': normalizeOrchestrationDefinitionListRead,
  'definition-read': normalizeOrchestrationDefinitionRead,
});

Object.assign(orchestrationRegistry as unknown as DefinitionReadWindow, {
  normalizeOrchestrationDefinitionRead,
  normalizeOrchestrationDefinitionListRead,
});
