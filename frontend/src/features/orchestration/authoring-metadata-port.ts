import { orchestrationRegistry } from './registry';
export interface SectionValidationMetadata {
  requiredStringFields: readonly string[];
  requiredStringArrayFields: readonly string[];
  requiredPositiveIntegerFields: readonly string[];
  requiredBooleanFields: readonly string[];
  requiredNonNegativeIntegerFields: readonly string[];
  eventTypeBooleanFields: readonly string[];
  eventTypeOptionalStringFields: readonly string[];
  staticPageFields: readonly string[];
  cursor: SectionValidationMetadata;
  terminalSnapshot: SectionValidationMetadata;
  terminalSnapshotWhen: SectionValidationMetadata;
  listEnvelope: SectionValidationMetadata;
}

interface AuthoringValidationMetadata {
  runtimeSections: Record<string, SectionValidationMetadata>;
}

type AuthoringMetadataWindow = Window & {
  ORCHESTRATION_AUTHORING_VALIDATION_METADATA?:
    AuthoringValidationMetadata;
  ORCHESTRATION_REQUEST_LIMIT_FIELDS?:
    Record<string, readonly string[]>;
};

function metadataError(owner: string): Error {
  const error = new Error(`Missing orchestration authoring metadata: ${owner}`);
  error.name = 'OrchestrationAuthoringMetadataError';
  return error;
}

function generatedMetadata(): AuthoringValidationMetadata {
  const registry = orchestrationRegistry as unknown as AuthoringMetadataWindow;
  const published = globalThis as unknown as AuthoringMetadataWindow;
  const metadata = registry.ORCHESTRATION_AUTHORING_VALIDATION_METADATA
    ?? published.ORCHESTRATION_AUTHORING_VALIDATION_METADATA;
  if (!metadata || !metadata.runtimeSections
      || typeof metadata.runtimeSections !== 'object') {
    throw metadataError('runtimeSections');
  }
  return metadata;
}

export function generatedRuntimeSectionMetadata(
  name: string,
): SectionValidationMetadata {
  const metadata = generatedMetadata().runtimeSections[name];
  if (!metadata || typeof metadata !== 'object') throw metadataError(name);
  return metadata;
}

export function generatedRequestLimitFields(
): Record<string, readonly string[]> {
  const registry = orchestrationRegistry as unknown as AuthoringMetadataWindow;
  const published = globalThis as unknown as AuthoringMetadataWindow;
  const fields = registry.ORCHESTRATION_REQUEST_LIMIT_FIELDS
    ?? published.ORCHESTRATION_REQUEST_LIMIT_FIELDS;
  if (!fields || typeof fields !== 'object') {
    throw metadataError('requestLimitFields');
  }
  return fields;
}
