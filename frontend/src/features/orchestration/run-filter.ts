import { orchestrationRegistry } from './registry';
import {
  orchestrationRunIsTerminal,
  type OrchestrationRunPresentation,
} from './run-status';
import type { ContractSource } from './contracts';

export const ORCHESTRATION_RUN_FILTERS = Object.freeze([
  'all', 'active', 'finished',
] as const);

export type OrchestrationRunFilterName =
  typeof ORCHESTRATION_RUN_FILTERS[number];

export interface RunFilterOptions<TRun> {
  isTerminal?: (run: TRun) => boolean;
  runContract?: ContractSource;
}

export interface OrchestrationRunFilter<TRun> {
  apply(value: unknown): TRun[];
  counts(value: unknown): Readonly<{
    all: number;
    active: number;
    finished: number;
  }>;
  names(): OrchestrationRunFilterName[];
  reveal(run: TRun | null | undefined): boolean;
  select(value: unknown): OrchestrationRunFilterName;
  value(): OrchestrationRunFilterName;
}

type RunFilterWindow = Window & {
  ORCHESTRATION_RUN_FILTERS?: typeof ORCHESTRATION_RUN_FILTERS;
  createOrchestrationRunFilter?: typeof createOrchestrationRunFilter;
};

/** Pure durable-run list filtering over the shared terminal policy. */
export function createOrchestrationRunFilter<
  TRun extends Record<string, unknown> = Record<string, unknown>,
>(options: RunFilterOptions<TRun> = {}): OrchestrationRunFilter<TRun> {
  let selected: OrchestrationRunFilterName = 'all';

  function terminal(run: TRun): boolean {
    return options.isTerminal
      ? options.isTerminal(run)
      : orchestrationRunIsTerminal(run, options.runContract);
  }

  function runs(value: unknown): TRun[] {
    return Array.isArray(value) ? value as TRun[] : [];
  }

  function select(value: unknown): OrchestrationRunFilterName {
    selected = ORCHESTRATION_RUN_FILTERS.includes(
      value as OrchestrationRunFilterName)
      ? value as OrchestrationRunFilterName : 'all';
    return selected;
  }

  function apply(value: unknown): TRun[] {
    const items = runs(value);
    if (selected === 'active') return items.filter((run) => !terminal(run));
    if (selected === 'finished') return items.filter(terminal);
    return items.slice();
  }

  function reveal(run: TRun | null | undefined): boolean {
    if (!run) return false;
    const finished = terminal(run);
    if (selected === 'all' || (selected === 'finished') === finished) {
      return false;
    }
    selected = finished ? 'finished' : 'active';
    return true;
  }

  function counts(value: unknown) {
    const items = runs(value);
    const finished = items.filter(terminal).length;
    return Object.freeze({
      all: items.length,
      active: items.length - finished,
      finished,
    });
  }

  return Object.freeze({
    apply,
    counts,
    names: () => ORCHESTRATION_RUN_FILTERS.slice(),
    reveal,
    select,
    value: () => selected,
  });
}

const bridge = orchestrationRegistry as unknown as RunFilterWindow;
bridge.ORCHESTRATION_RUN_FILTERS = ORCHESTRATION_RUN_FILTERS;
bridge.createOrchestrationRunFilter = createOrchestrationRunFilter;

// Keep this type reachable for downstream TS consumers without widening the
// runtime facade; it documents that status presentation belongs next door.
export type { OrchestrationRunPresentation };
