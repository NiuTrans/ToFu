import { orchestrationRegistry } from './registry';
import {
  orchestrationContractRecord,
  orchestrationRequireArraySubset,
  orchestrationRequireString,
  orchestrationRequireStringFields,
  orchestrationRequireStringVocabulary,
  type MissingPaths,
  type ValidationRecord,
} from './contract-validation-primitives';

type RunValidationWindow = Window & {
  _validateRunRuntimeSection?: typeof validateRunRuntimeSection;
};

export function validateRunRuntimeSection(
  section: ValidationRecord,
  missing: MissingPaths,
): void {
  orchestrationRequireString(section.initial, 'runContract.initial', missing);
  const statusesValid = orchestrationRequireStringVocabulary(
    section.statuses, 'runContract.statuses', missing);
  const terminalValid = orchestrationRequireStringVocabulary(
    section.terminal, 'runContract.terminal', missing);
  const statuses = Array.isArray(section.statuses)
    ? section.statuses as string[] : [];
  if (statusesValid && !statuses.includes(section.initial as string)) {
    missing.push('runContract.initial');
  }
  if (statusesValid && terminalValid) {
    orchestrationRequireArraySubset(
      section.terminal, section.statuses, 'runContract.terminal', missing);
  }
  if (section.categories != null) {
    if (!orchestrationContractRecord(section.categories)) {
      missing.push('runContract.categories');
    } else if (statusesValid) {
      orchestrationRequireStringFields(
        section.categories,
        statuses,
        'runContract.categories',
        missing,
      );
    }
  }
}

(orchestrationRegistry as unknown as RunValidationWindow)._validateRunRuntimeSection =
  validateRunRuntimeSection;
