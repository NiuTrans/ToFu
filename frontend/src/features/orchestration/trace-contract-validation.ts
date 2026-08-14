import { orchestrationRegistry } from './registry';
import { compatibilityContract } from './contracts';
import {
  orchestrationRequireMapValues,
  orchestrationRequirePositiveInteger,
  orchestrationRequireString,
  type MissingPaths,
  type ValidationRecord,
} from './contract-validation-primitives';
import { childRecord } from './validation-metadata';

type TraceValidationWindow = Window & {
  _validateTraceRuntimeSection?: typeof validateTraceRuntimeSection;
};

export function validateTraceRuntimeSection(
  section: ValidationRecord,
  missing: MissingPaths,
): void {
  const defaults = compatibilityContract('traceContract') ?? {};
  if (section.historyLimit !== undefined) {
    orchestrationRequirePositiveInteger(
      section.historyLimit, 'traceContract.historyLimit', missing);
  }
  const statusMap = childRecord(defaults.statusMap);
  const fields = Object.keys(statusMap);
  const statuses = fields.map((field) => statusMap[field]);
  orchestrationRequireMapValues(
    section.statusMap, fields, statuses, 'traceContract.statusMap', missing);
  const activityDefaults = childRecord(defaults.activityFields);
  const activityFields = Object.keys(activityDefaults);
  if (section.activityFields !== undefined) {
    orchestrationRequireMapValues(
      section.activityFields,
      activityFields,
      activityFields.map((field) => activityDefaults[field]),
      'traceContract.activityFields',
      missing,
    );
  }
  const limits = childRecord(section.textLimits);
  const flags = childRecord(section.truncationFlags);
  Object.keys(childRecord(defaults.textLimits)).forEach((field) => {
    orchestrationRequirePositiveInteger(
      limits[field], `traceContract.textLimits.${field}`, missing);
    orchestrationRequireString(
      flags[field], `traceContract.truncationFlags.${field}`, missing);
  });
}

(orchestrationRegistry as unknown as TraceValidationWindow)._validateTraceRuntimeSection =
  validateTraceRuntimeSection;
