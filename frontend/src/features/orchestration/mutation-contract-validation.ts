import { orchestrationRegistry } from './registry';
import {
  orchestrationContractRecord,
  orchestrationRequireArray,
  orchestrationRequireArraySubset,
  orchestrationRequireFieldSpecs,
  orchestrationRequirePositiveInteger,
  orchestrationRequireString,
  orchestrationRequireStringVocabulary,
  type MissingPaths,
  type ValidationRecord,
} from './contract-validation-primitives';
import { childRecord, runtimeSectionMetadata } from './validation-metadata';

type MutationValidationWindow = Window & {
  _validateMutationRuntimeSection?: typeof validateMutationRuntimeSection;
};

export function validateMutationRuntimeSection(
  section: ValidationRecord,
  missing: MissingPaths,
): void {
  const metadata = runtimeSectionMetadata('mutationContract');
  const vocabularies: Record<string, boolean> = {};
  metadata.requiredStringArrayFields.forEach((field) => {
    vocabularies[field] = orchestrationRequireStringVocabulary(
      section[field], `mutationContract.${field}`, missing);
  });
  if (vocabularies.reasons && vocabularies.retryableReasons) {
    orchestrationRequireArraySubset(
      section.retryableReasons,
      section.reasons,
      'mutationContract.retryableReasons',
      missing,
    );
  }
  if (section.clientRetryableReasons != null) {
    const clientPath = 'mutationContract.clientRetryableReasons';
    if (orchestrationRequireStringVocabulary(
      section.clientRetryableReasons, clientPath, missing)) {
      orchestrationRequireArray(
        section.clientRetryableReasons,
        clientPath,
        missing,
        section.retryableReasons as unknown[] | undefined,
      );
      orchestrationRequireArray(
        section.clientRetryableReasons,
        clientPath,
        missing,
        [section.transportFailureReason],
      );
    }
    orchestrationRequireString(
      section.transportFailureReason,
      'mutationContract.transportFailureReason',
      missing,
    );
  }
  metadata.requiredStringFields.forEach((field) => {
    orchestrationRequireString(
      section[field], `mutationContract.${field}`, missing);
  });
  if (!orchestrationContractRecord(section.httpStatusByReason)) {
    missing.push('mutationContract.httpStatusByReason');
  } else {
    const statuses = childRecord(section.httpStatusByReason);
    const reasons = Array.isArray(section.reasons) ? section.reasons : [];
    reasons.forEach((reason) => {
      orchestrationRequirePositiveInteger(
        statuses[String(reason)],
        `mutationContract.httpStatusByReason.${String(reason)}`,
        missing,
      );
    });
  }
  if (section.payloadFields != null) {
    const types = {
      format: 'string', ok: 'boolean', action: 'string', reason: 'string',
      targetId: 'string', resourceStatus: 'string',
      resourceTerminal: 'nullable_boolean', targetExists: 'nullable_boolean',
      retryable: 'boolean', reconcileRequired: 'boolean',
    };
    orchestrationRequireFieldSpecs(
      section.payloadFields, types,
      'mutationContract.payloadFields', missing);
    const fields = childRecord(section.payloadFields);
    ([
      ['reconcileField', 'reconcileRequired'],
      ['targetExistsField', 'targetExists'],
      ['resourceTerminalField', 'resourceTerminal'],
    ] as const).forEach(([alias, semantic]) => {
      if (section[alias] !== childRecord(fields[semantic]).name) {
        missing.push(`mutationContract.${alias}`);
      }
    });
    if (childRecord(fields.format).name !== 'format') {
      missing.push('mutationContract.payloadFields');
    }
  }
}

(orchestrationRegistry as unknown as MutationValidationWindow)._validateMutationRuntimeSection =
  validateMutationRuntimeSection;
