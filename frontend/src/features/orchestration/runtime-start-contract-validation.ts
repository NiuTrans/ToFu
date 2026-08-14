import { orchestrationRegistry } from './registry';
import { compatibilityContract } from './contracts';
import {
  orchestrationContractRecord,
  orchestrationRequireArray,
  orchestrationRequirePositiveInteger,
  orchestrationRequireStringFields,
  orchestrationRequireStringVocabulary,
  type MissingPaths,
  type ValidationRecord,
} from './contract-validation-primitives';
import { childRecord, runtimeSectionMetadata } from './validation-metadata';

type RuntimeStartValidationWindow = Window & {
  _validateRuntimeStartRuntimeSection?: typeof validateRuntimeStartRuntimeSection;
};

export function validateRuntimeStartRuntimeSection(
  section: ValidationRecord,
  missing: MissingPaths,
): void {
  const defaults = compatibilityContract('runtimeStartContract') ?? {};
  const kinds = Array.isArray(defaults.kinds) ? defaults.kinds : [];
  const metadata = runtimeSectionMetadata('runtimeStartContract');
  if (orchestrationRequireStringVocabulary(
    section.kinds, 'runtimeStartContract.kinds', missing)) {
    orchestrationRequireArray(
      section.kinds, 'runtimeStartContract.kinds', missing, kinds);
  }
  orchestrationRequireStringFields(
    section, metadata.requiredStringFields, 'runtimeStartContract', missing);
  if (!orchestrationContractRecord(section.legacyIdFields)) {
    missing.push('runtimeStartContract.legacyIdFields');
  } else {
    orchestrationRequireStringFields(
      section.legacyIdFields,
      kinds.map(String),
      'runtimeStartContract.legacyIdFields',
      missing,
    );
  }
  if (!orchestrationContractRecord(section.successStatuses)) {
    missing.push('runtimeStartContract.successStatuses');
  } else {
    const statuses = childRecord(section.successStatuses);
    kinds.forEach((kind) => {
      orchestrationRequirePositiveInteger(
        statuses[String(kind)],
        `runtimeStartContract.successStatuses.${String(kind)}`,
        missing,
      );
    });
  }
}

(orchestrationRegistry as unknown as RuntimeStartValidationWindow)._validateRuntimeStartRuntimeSection =
  validateRuntimeStartRuntimeSection;
