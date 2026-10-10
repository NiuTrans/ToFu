import { orchestrationRegistry } from './registry';
import { compatibilityContract } from './contracts';
import {
  orchestrationContractRecord,
  orchestrationRequireArray,
  orchestrationRequireBoolean,
  orchestrationRequirePositiveInteger,
  orchestrationRequireString,
  type MissingPaths,
  type ValidationRecord,
} from './contract-validation-primitives';
import { childRecord, runtimeSectionMetadata } from './validation-metadata';

type ReplayValidationWindow = Window & {
  _validateReplayRuntimeSection?: typeof validateReplayRuntimeSection;
};

export function validateReplayRuntimeSection(
  section: ValidationRecord,
  missing: MissingPaths,
): void {
  const defaults = compatibilityContract('replayContract') ?? {};
  const metadata = runtimeSectionMetadata('replayContract');
  metadata.requiredStringFields.forEach((field) => {
    orchestrationRequireString(
      section[field], `replayContract.${field}`, missing);
  });
  if (section.caughtUpField != null) {
    orchestrationRequireString(
      section.caughtUpField, 'replayContract.caughtUpField', missing);
  }
  orchestrationRequireArray(
    section.eventRequiredFields,
    'replayContract.eventRequiredFields',
    missing,
    [section.eventTypeField, section.eventSequenceField],
  );
  if (section.unknownEventTypes !== defaults.unknownEventTypes) {
    missing.push('replayContract.unknownEventTypes');
  }
  const cursor = childRecord(section.cursor);
  metadata.cursor.requiredStringFields.forEach((field) => {
    orchestrationRequireString(
      cursor[field], `replayContract.cursor.${field}`, missing);
  });
  metadata.cursor.requiredNonNegativeIntegerFields.forEach((field) => {
    const value = cursor[field];
    if (!Number.isSafeInteger(value) || Number(value) < 0) {
      missing.push(`replayContract.cursor.${field}`);
    }
  });
  if (Number.isSafeInteger(cursor.minimum)
      && Number.isSafeInteger(cursor.default)
      && Number(cursor.default) < Number(cursor.minimum)) {
    missing.push('replayContract.cursor.default');
  }
  metadata.cursor.requiredBooleanFields.forEach((field) => {
    orchestrationRequireBoolean(
      cursor[field], `replayContract.cursor.${field}`, missing);
  });
  orchestrationRequireArray(
    section.pageFields,
    'replayContract.pageFields',
    missing,
    [...metadata.staticPageFields,
      section.eventsField,
      section.nextCursorField,
      section.statusField,
      section.terminalField,
      cursor.field,
    ],
  );
  orchestrationRequireArray(
    section.terminalEventTypes,
    'replayContract.terminalEventTypes',
    missing,
    defaults.terminalEventTypes as unknown[] | undefined,
  );
  const snapshot = childRecord(section.terminalSnapshot);
  metadata.terminalSnapshot.requiredStringFields.forEach((field) => {
    orchestrationRequireString(
      snapshot[field], `replayContract.terminalSnapshot.${field}`, missing);
  });
  const when = childRecord(snapshot.when);
  if (!orchestrationContractRecord(snapshot.when)
      || metadata.terminalSnapshotWhen.requiredStringFields.some(
        (field) => typeof when[field] !== 'string')
      || metadata.terminalSnapshotWhen.requiredBooleanFields.some(
        (field) => typeof when[field] !== 'boolean')) {
    missing.push('replayContract.terminalSnapshot.when');
  }
  metadata.terminalSnapshot.requiredBooleanFields.forEach((field) => {
    orchestrationRequireBoolean(
      snapshot[field], `replayContract.terminalSnapshot.${field}`, missing);
  });
  const statuses = childRecord(section.httpStatuses);
  Object.keys(childRecord(defaults.httpStatuses)).forEach((field) => {
    orchestrationRequirePositiveInteger(
      statuses[field], `replayContract.httpStatuses.${field}`, missing);
  });
}

(orchestrationRegistry as unknown as ReplayValidationWindow)._validateReplayRuntimeSection =
  validateReplayRuntimeSection;
