import { orchestrationRegistry } from './registry';
import { compatibilityContract } from './contracts';
import {
  orchestrationContractRecord,
  orchestrationRequirePositiveInteger,
  type MissingPaths,
  type ValidationRecord,
} from './contract-validation-primitives';
import { childRecord, runtimeSectionMetadata } from './validation-metadata';

type EventValidationWindow = Window & {
  _validateEventRuntimeSection?: typeof validateEventRuntimeSection;
};

export function validateEventRuntimeSection(
  section: ValidationRecord,
  missing: MissingPaths,
): void {
  const defaults = compatibilityContract('eventContract') ?? {};
  const metadata = runtimeSectionMetadata('eventContract');
  const limits = childRecord(section.previewLimits);
  Object.keys(childRecord(defaults.previewLimits)).forEach((field) => {
    orchestrationRequirePositiveInteger(
      limits[field], `eventContract.previewLimits.${field}`, missing);
  });
  if (!orchestrationContractRecord(section.types)
      || !Object.keys(section.types).length) {
    missing.push('eventContract.types');
    return;
  }
  Object.keys(section.types).forEach((type) => {
    const spec = section.types as ValidationRecord;
    if (!orchestrationContractRecord(spec[type])) {
      missing.push(`eventContract.types.${type}`);
      return;
    }
    const capabilities = spec[type] as ValidationRecord;
    metadata.eventTypeBooleanFields.forEach((capability) => {
      if (typeof capabilities[capability] !== 'boolean') {
        missing.push(`eventContract.types.${type}.${capability}`);
      }
    });
    metadata.eventTypeOptionalStringFields.forEach((field) => {
      const value = capabilities[field];
      if (value != null && (typeof value !== 'string' || !value)) {
        missing.push(`eventContract.types.${type}.${field}`);
      }
    });
  });
}

(orchestrationRegistry as unknown as EventValidationWindow)._validateEventRuntimeSection =
  validateEventRuntimeSection;
