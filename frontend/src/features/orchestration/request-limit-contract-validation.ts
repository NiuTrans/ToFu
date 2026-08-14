import { orchestrationRegistry } from './registry';
import {
  orchestrationContractRecord,
  orchestrationRequirePositiveInteger,
  type MissingPaths,
  type ValidationRecord,
} from './contract-validation-primitives';
import { requestLimitFields } from './validation-metadata';

type RequestLimitValidationWindow = Window & {
  _validateRequestLimitsRuntimeSection?: typeof validateRequestLimitsRuntimeSection;
};

export function validateRequestLimitsRuntimeSection(
  section: ValidationRecord,
  missing: MissingPaths,
): void {
  const fields = requestLimitFields();
  Object.keys(fields).forEach((name) => {
    const field = section[name];
    fields[name].forEach((property) => {
      const value = orchestrationContractRecord(field)
        ? field[property] : null;
      orchestrationRequirePositiveInteger(
        value, `requestLimits.${name}.${property}`, missing);
    });
  });
}

(orchestrationRegistry as unknown as RequestLimitValidationWindow)._validateRequestLimitsRuntimeSection =
  validateRequestLimitsRuntimeSection;
