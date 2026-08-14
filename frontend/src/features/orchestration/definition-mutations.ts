import { orchestrationRegistry } from './registry';
import {
  createOrchestrationSingleFlight,
  type OrchestrationSingleFlight,
} from './single-flight';
import { type WorkspaceMutationCoordinator } from './workspace-command-types';

export interface DefinitionMutationCoordinatorOptions {
  flights?: OrchestrationSingleFlight;
}

type DefinitionMutationsWindow = Window & {
  createOrchestrationDefinitionMutationCoordinator?:
    typeof createOrchestrationDefinitionMutationCoordinator;
};

/** Definition-scoped single-flight ownership plus stale-read generations. */
export function createOrchestrationDefinitionMutationCoordinator(
  options: DefinitionMutationCoordinatorOptions = {},
): WorkspaceMutationCoordinator {
  const generations: Record<string, number> = Object.create(null);
  const flights = options.flights ?? createOrchestrationSingleFlight();
  const key = (value: unknown): string => String(value || '');

  const generation = (id: unknown): number => generations[key(id)] || 0;

  const advance = (id: unknown): number => {
    const idKey = key(id);
    if (!idKey) return 0;
    const next = generation(idKey) + 1;
    generations[idKey] = next;
    return next;
  };

  const share = <T>(
    kind: string,
    id: unknown,
    operation: () => T | PromiseLike<T>,
  ): Promise<T> => flights.share(
    `${String(kind || 'mutation')}:${key(id)}`, operation);

  return Object.freeze({ advance, generation, share });
}

(orchestrationRegistry as unknown as DefinitionMutationsWindow)
  .createOrchestrationDefinitionMutationCoordinator =
    createOrchestrationDefinitionMutationCoordinator;
