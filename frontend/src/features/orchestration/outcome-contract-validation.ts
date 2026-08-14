import { orchestrationRegistry } from './registry';
import { compatibilityContract } from './contracts';
import {
  orchestrationRequireArray,
  orchestrationRequirePositiveInteger,
  orchestrationRequireStringVocabulary,
  type MissingPaths,
  type ValidationRecord,
} from './contract-validation-primitives';
import { childRecord, runtimeSectionMetadata } from './validation-metadata';

type OutcomeValidationWindow = Window & {
  _validateOutcomeRuntimeSection?: typeof validateOutcomeRuntimeSection;
};

export function validateOutcomeRuntimeSection(
  section: ValidationRecord,
  missing: MissingPaths,
): void {
  const defaults = compatibilityContract('outcomeContract') ?? {};
  const metadata = runtimeSectionMetadata('outcomeContract');
  metadata.requiredStringArrayFields.forEach((field) => {
    const path = `outcomeContract.${field}`;
    if (orchestrationRequireStringVocabulary(section[field], path, missing)) {
      orchestrationRequireArray(
        section[field], path, missing, defaults[field] as unknown[] | undefined);
    }
  });
  const limits = childRecord(section.displayLimits);
  Object.keys(childRecord(defaults.displayLimits)).forEach((field) => {
    orchestrationRequirePositiveInteger(
      limits[field], `outcomeContract.displayLimits.${field}`, missing);
  });
  if (section.incompleteStopReasons != null
      && orchestrationRequireStringVocabulary(
        section.incompleteStopReasons,
        'outcomeContract.incompleteStopReasons',
        missing,
      )) {
    orchestrationRequireArray(
      section.incompleteStopReasons,
      'outcomeContract.incompleteStopReasons',
      missing,
      defaults.incompleteStopReasons as unknown[] | undefined,
    );
  }
}

(orchestrationRegistry as unknown as OutcomeValidationWindow)._validateOutcomeRuntimeSection =
  validateOutcomeRuntimeSection;
