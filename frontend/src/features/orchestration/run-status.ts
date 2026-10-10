import { orchestrationRegistry } from './registry';
import {
  compatibilityContract,
  publishedContract,
  record,
  type ContractRecord,
  type ContractSource,
} from './contracts';

export interface OrchestrationRunPresentation {
  status: string;
  category: string;
  token: string;
  terminal: boolean;
}

type RunStatusWindow = Window & {
  orchestrationRunContract?: typeof orchestrationRunContract;
  orchestrationRunStatus?: typeof orchestrationRunStatus;
  orchestrationRunHasStatus?: typeof orchestrationRunHasStatus;
  orchestrationRunIsTerminal?: typeof orchestrationRunIsTerminal;
  orchestrationRunPresentation?: typeof orchestrationRunPresentation;
};

export function orchestrationRunContract(
  contractSource?: ContractSource,
): ContractRecord | null {
  return publishedContract('runContract', contractSource);
}

export function orchestrationRunStatus(runOrStatus: unknown): string {
  const run = record(runOrStatus);
  const status = run ? run.status : runOrStatus;
  return typeof status === 'string' ? status : '';
}

export function orchestrationRunHasStatus(
  runOrStatus: unknown,
  contractSource?: ContractSource,
): boolean {
  const status = orchestrationRunStatus(runOrStatus);
  const contract = orchestrationRunContract(contractSource);
  return Boolean(status && contract && Array.isArray(contract.statuses)
    && contract.statuses.includes(status));
}

export function orchestrationRunIsTerminal(
  runOrStatus: unknown,
  contractSource?: ContractSource,
): boolean {
  const run = record(runOrStatus);
  if (run && typeof run.terminal === 'boolean') return run.terminal;
  const status = orchestrationRunStatus(runOrStatus);
  const contract = orchestrationRunContract(contractSource);
  return Boolean(status && contract && Array.isArray(contract.terminal)
    && contract.terminal.includes(status));
}

export function orchestrationRunPresentation(
  runOrStatus: unknown,
  contractSource?: ContractSource,
): OrchestrationRunPresentation {
  const contract = orchestrationRunContract(contractSource);
  const defaults = compatibilityContract('runContract');
  let status = orchestrationRunStatus(runOrStatus);
  if (!status && contract && typeof contract.initial === 'string') {
    status = contract.initial;
  }
  if (!status && defaults && typeof defaults.initial === 'string') {
    status = defaults.initial;
  }
  const categories = record(contract?.categories);
  const category = categories && typeof categories[status] === 'string'
    ? categories[status] as string : status;
  const token = String(category || status || 'unknown').toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'unknown';
  return {
    status,
    category,
    token,
    terminal: orchestrationRunIsTerminal(runOrStatus, contract),
  };
}

const bridge = orchestrationRegistry as unknown as RunStatusWindow;
bridge.orchestrationRunContract = orchestrationRunContract;
bridge.orchestrationRunStatus = orchestrationRunStatus;
bridge.orchestrationRunHasStatus = orchestrationRunHasStatus;
bridge.orchestrationRunIsTerminal = orchestrationRunIsTerminal;
bridge.orchestrationRunPresentation = orchestrationRunPresentation;
