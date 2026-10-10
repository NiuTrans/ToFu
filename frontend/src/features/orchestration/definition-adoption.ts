import { orchestrationRegistry } from './registry';
export interface DefinitionAdoptionResult {
  ok: boolean;
  workspace?: unknown;
  reason?: string;
  code?: string;
  path?: string;
  cause?: unknown;
}

export interface DefinitionAdoptionProjection {
  workspaceFromDefinitionResult?: (
    definition: unknown,
  ) => DefinitionAdoptionResult | null;
  workspaceFromDefinition?: (definition: unknown) => unknown;
}

type AdoptionWindow = Window & {
  projectOrchestrationDefinitionAdoption?:
    typeof projectOrchestrationDefinitionAdoption;
};

/** Calls either generation of workspace projector without leaking failures. */
export function projectOrchestrationDefinitionAdoption(
  options: DefinitionAdoptionProjection = {},
  definition: unknown,
): DefinitionAdoptionResult {
  try {
    if (typeof options.workspaceFromDefinitionResult === 'function') {
      const result = options.workspaceFromDefinitionResult(definition);
      if (result?.ok && result.workspace) return result;
      return result?.ok === false
        ? result : { ok: false, reason: 'invalid-definition' };
    }
    const workspace = options.workspaceFromDefinition?.(definition);
    return workspace ? { ok: true, workspace }
      : { ok: false, reason: 'invalid-definition' };
  } catch (cause: unknown) {
    return { ok: false, reason: 'invalid-definition', cause };
  }
}

export function normalizeOrchestrationDefinitionAdoption(
  value: unknown,
): DefinitionAdoptionResult {
  if (value && typeof value === 'object'
      && typeof (value as DefinitionAdoptionResult).ok === 'boolean') {
    return value as DefinitionAdoptionResult;
  }
  return value === null || value === false
    ? { ok: false, reason: 'invalid-definition' }
    : { ok: true, workspace: value || null };
}

(orchestrationRegistry as unknown as AdoptionWindow).projectOrchestrationDefinitionAdoption =
  projectOrchestrationDefinitionAdoption;
(orchestrationRegistry as unknown as AdoptionWindow & {
  normalizeOrchestrationDefinitionAdoption?:
    typeof normalizeOrchestrationDefinitionAdoption;
}).normalizeOrchestrationDefinitionAdoption =
  normalizeOrchestrationDefinitionAdoption;
