import { orchestrationRegistry } from './registry';
import { compatibilityContract } from './contracts';
import {
  orchestrationContractRecord,
  orchestrationRequireArray,
  orchestrationRequirePositiveInteger,
  orchestrationRequireStringFields,
  type MissingPaths,
  type ValidationRecord,
} from './contract-validation-primitives';
import { childRecord, runtimeSectionMetadata } from './validation-metadata';

type DurableValidationWindow = Window & {
  validateOrchestrationDurableListEnvelope?: typeof validateOrchestrationDurableListEnvelope;
  _validateDurableRunRuntimeSection?: typeof validateDurableRunRuntimeSection;
};

export function validateOrchestrationDurableListEnvelope(
  value: unknown,
  missing: MissingPaths,
): void {
  if (!orchestrationContractRecord(value)) {
    missing.push('durableRunContract.listEnvelope');
    return;
  }
  const metadata = runtimeSectionMetadata('durableRunContract').listEnvelope;
  orchestrationRequireStringFields(
    value,
    metadata.requiredStringFields,
    'durableRunContract.listEnvelope',
    missing,
  );
  orchestrationRequireArray(
    value.pageFields,
    'durableRunContract.listEnvelope.pageFields',
    missing,
    [value.limitField, value.hasMoreField, value.nextLimitField],
  );
  metadata.requiredPositiveIntegerFields.forEach((field) => {
    orchestrationRequirePositiveInteger(
      value[field], `durableRunContract.listEnvelope.${field}`, missing);
  });
  if (Number.isSafeInteger(value.defaultLimit)
      && Number.isSafeInteger(value.maxLimit)
      && Number(value.defaultLimit) > Number(value.maxLimit)) {
    missing.push('durableRunContract.listEnvelope.defaultLimit');
  }
}

export function validateDurableRunRuntimeSection(
  section: ValidationRecord,
  missing: MissingPaths,
): void {
  const defaults = compatibilityContract('durableRunContract') ?? {};
  const metadata = runtimeSectionMetadata('durableRunContract');
  orchestrationRequireStringFields(
    section,
    metadata.requiredStringFields,
    'durableRunContract',
    missing,
  );
  const identityFields = [
    section.idField, section.statusField, section.terminalField,
  ];
  orchestrationRequireArray(
    section.listFields,
    'durableRunContract.listFields',
    missing,
    identityFields,
  );
  const defaultReadFields = Array.isArray(defaults.readFields)
    ? defaults.readFields : [];
  const defaultListFields = Array.isArray(defaults.listFields)
    ? defaults.listFields : [];
  const readOnlyFields = defaultReadFields.filter(
    (field) => !defaultListFields.includes(field));
  orchestrationRequireArray(
    section.readFields,
    'durableRunContract.readFields',
    missing,
    identityFields.concat(readOnlyFields),
  );
  if (!Array.isArray(section.optionalFields)
      || !section.optionalFields.includes(section.outcomeField)) {
    missing.push('durableRunContract.optionalFields');
  }
  if (section.listEnvelope !== undefined) {
    validateOrchestrationDurableListEnvelope(section.listEnvelope, missing);
  }
}

Object.assign(orchestrationRegistry as unknown as DurableValidationWindow, {
  validateOrchestrationDurableListEnvelope,
  _validateDurableRunRuntimeSection: validateDurableRunRuntimeSection,
});
