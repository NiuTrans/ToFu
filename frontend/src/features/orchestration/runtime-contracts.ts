import { orchestrationRegistry } from './registry';
import { record, type ContractRecord } from './contracts';

const FALLBACK_RUNTIME_SECTIONS = Object.freeze([
  'requestLimits',
  'nodeDefaults',
  'nodeRuntimeDefaults',
  'eventContract',
  'runContract',
  'outcomeContract',
  'traceContract',
  'mutationContract',
  'replayContract',
  'runtimeStartContract',
  'durableRunContract',
]);

export interface OrchestrationRuntimeContractPort {
  snapshot(): ContractRecord;
  get(name: string): unknown;
}

export interface RuntimeContractPortOptions {
  source?: (() => unknown) | null;
  contracts?: unknown;
}

type RuntimeContractsWindow = Window & {
  ORCHESTRATION_RUNTIME_CONTRACT_SECTIONS?: readonly string[];
  _orchestrationRuntimeSectionNames?:
    typeof orchestrationRuntimeSectionNames;
  projectOrchestrationRuntimeContracts?:
    typeof projectOrchestrationRuntimeContracts;
  createOrchestrationRuntimeContractPort?:
    typeof createOrchestrationRuntimeContractPort;
  orchestrationRuntimeContractPort?:
    typeof orchestrationRuntimeContractPort;
};

function configuredSections(): readonly string[] {
  const registry = orchestrationRegistry as unknown as RuntimeContractsWindow;
  const globalRegistry = globalThis as unknown as RuntimeContractsWindow;
  const published = registry.ORCHESTRATION_RUNTIME_CONTRACT_SECTIONS
    ?? globalRegistry.ORCHESTRATION_RUNTIME_CONTRACT_SECTIONS;
  return Array.isArray(published) && published.length
    ? published : FALLBACK_RUNTIME_SECTIONS;
}

export function orchestrationRuntimeSectionNames(
  sourceValue: unknown,
): string[] {
  const source = record(sourceValue) ?? {};
  const registry = record(source.contractSections);
  const authoring = registry?.authoring;
  const runtime = registry?.runtime;
  const required = configuredSections();
  if (!Array.isArray(authoring) || !Array.isArray(runtime)
      || !runtime.length) return Array.from(required);
  const seen = new Set<string>();
  const valid = runtime.every((name) => {
    const safe = typeof name === 'string'
      && /^[A-Za-z][A-Za-z0-9]*$/.test(name)
      && authoring.includes(name) && !seen.has(name);
    if (safe) seen.add(name);
    return safe;
  }) && required.every((name) => runtime.includes(name));
  return valid ? runtime.slice() as string[] : Array.from(required);
}

export function projectOrchestrationRuntimeContracts(
  value: unknown,
): ContractRecord {
  const source = record(value) ?? {};
  const projected: ContractRecord = {};
  orchestrationRuntimeSectionNames(source).forEach((name) => {
    const section = record(source[name]);
    projected[name] = section ?? null;
  });
  projected.requestLimits = projected.requestLimits || Object.freeze({});
  return Object.freeze(projected);
}

export function createOrchestrationRuntimeContractPort(
  options: RuntimeContractPortOptions = {},
): OrchestrationRuntimeContractPort {
  const staticContracts = options.contracts;
  const snapshot = (): ContractRecord => {
    let contracts = staticContracts;
    if (typeof options.source === 'function') {
      try {
        contracts = options.source();
      } catch {
        contracts = staticContracts;
      }
    }
    return projectOrchestrationRuntimeContracts(contracts);
  };
  const get = (name: string): unknown => {
    const current = snapshot();
    return Object.prototype.hasOwnProperty.call(current, name)
      ? current[name] : null;
  };
  return Object.freeze({ snapshot, get });
}

export function orchestrationRuntimeContractPort(
  value: unknown,
): OrchestrationRuntimeContractPort {
  const candidate = record(value);
  if (candidate && typeof candidate.snapshot === 'function'
      && typeof candidate.get === 'function') {
    return candidate as unknown as OrchestrationRuntimeContractPort;
  }
  return createOrchestrationRuntimeContractPort({
    source: typeof value === 'function' ? value as () => unknown : null,
    contracts: typeof value === 'function' ? null : value,
  });
}

Object.assign(orchestrationRegistry as unknown as RuntimeContractsWindow, {
  _orchestrationRuntimeSectionNames: orchestrationRuntimeSectionNames,
  projectOrchestrationRuntimeContracts,
  createOrchestrationRuntimeContractPort,
  orchestrationRuntimeContractPort,
});
