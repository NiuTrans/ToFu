import { orchestrationRegistry } from './registry';
import { ORCHESTRATION_COMPATIBILITY_DEFAULTS } from
  './compatibility-defaults.generated';
import {
  record,
  resolveDirectContractSource,
  resolveContractSource,
  type ContractRecord,
  type ContractSource,
} from './contract-source';

type CompatibilityContractWindow = Window & {
  orchestrationDirectContract?: typeof resolveDirectContractSource;
  orchestrationPublishedContract?: typeof publishedContract;
  orchestrationCompatibilityContract?: typeof compatibilityContract;
};

export function publishedContract(
  name: string,
  source?: ContractSource,
): ContractRecord | null {
  return resolveContractSource(name, source);
}

export function compatibilityContract(
  name: string,
  source?: ContractSource,
): ContractRecord | null {
  return publishedContract(name, source)
    ?? record(ORCHESTRATION_COMPATIBILITY_DEFAULTS[name]);
}

const bridge = orchestrationRegistry as unknown as CompatibilityContractWindow;
bridge.orchestrationDirectContract = resolveDirectContractSource;
bridge.orchestrationPublishedContract = publishedContract;
bridge.orchestrationCompatibilityContract = compatibilityContract;
