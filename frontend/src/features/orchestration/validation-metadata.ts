import type { ValidationRecord } from './contract-validation-primitives';
import {
  generatedRequestLimitFields,
  generatedRuntimeSectionMetadata,
  type SectionValidationMetadata,
} from './authoring-metadata-port';

export type { SectionValidationMetadata } from './authoring-metadata-port';

export function runtimeSectionMetadata(name: string): SectionValidationMetadata {
  return generatedRuntimeSectionMetadata(name);
}

export function requestLimitFields(): Record<string, readonly string[]> {
  return generatedRequestLimitFields();
}

export function childRecord(value: unknown): ValidationRecord {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as ValidationRecord : {};
}
