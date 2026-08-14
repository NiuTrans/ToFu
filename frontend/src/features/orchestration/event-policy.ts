import { orchestrationRegistry } from './registry';
import {
  compatibilityContract,
  publishedContract,
  record,
  type ContractRecord,
  type ContractSource,
} from './contracts';

type EventPolicyWindow = Window & {
  orchestrationEventContractSpec?: typeof orchestrationEventContractSpec;
  orchestrationEventCapability?: typeof orchestrationEventCapability;
  orchestrationEventStringCapability?: typeof orchestrationEventStringCapability;
  orchestrationEventGateEffect?: typeof orchestrationEventGateEffect;
  orchestrationEventShouldReduce?: typeof orchestrationEventShouldReduce;
  orchestrationEventShouldTimeline?: typeof orchestrationEventShouldTimeline;
  orchestrationEventPreviewLimit?: typeof orchestrationEventPreviewLimit;
  projectOrchestrationEventPreview?: typeof projectOrchestrationEventPreview;
};

export function orchestrationEventContractSpec(
  contractSource: ContractSource | undefined,
  eventType: unknown,
): ContractRecord | null {
  const contract = publishedContract('eventContract', contractSource);
  const defaults = compatibilityContract('eventContract');
  const types = record(contract?.types) ?? record(defaults?.types);
  return record(types?.[String(eventType || '')]);
}

export function orchestrationEventCapability(
  contractSource: ContractSource | undefined,
  eventType: unknown,
  capability: string,
  fallback: unknown,
): boolean {
  const spec = orchestrationEventContractSpec(contractSource, eventType);
  return spec && typeof spec[capability] === 'boolean'
    ? spec[capability] as boolean : Boolean(fallback);
}

export function orchestrationEventStringCapability(
  contractSource: ContractSource | undefined,
  eventType: unknown,
  capability: string,
  fallback: unknown,
): string {
  const spec = orchestrationEventContractSpec(contractSource, eventType);
  let value = spec?.[capability];
  if (typeof value === 'string' && value) return value;
  const defaults = compatibilityContract('eventContract');
  value = record(defaults?.types)?.[String(eventType || '')];
  const defaultValue = record(value)?.[capability];
  return typeof defaultValue === 'string' && defaultValue
    ? defaultValue : String(fallback || '');
}

export function orchestrationEventGateEffect(
  contractSource: ContractSource | undefined,
  eventType: unknown,
): '' | 'open' | 'close' {
  const effect = orchestrationEventStringCapability(
    contractSource, eventType, 'gateEffect', '');
  return effect === 'open' || effect === 'close' ? effect : '';
}

export function orchestrationEventShouldReduce(
  contractSource: ContractSource | undefined,
  eventType: unknown,
): boolean {
  if (eventType === 'done') return true;
  return orchestrationEventCapability(
    contractSource, eventType, 'reduce', true);
}

export function orchestrationEventShouldTimeline(
  contractSource: ContractSource | undefined,
  eventType: unknown,
): boolean {
  if (eventType === 'done') return false;
  return orchestrationEventCapability(
    contractSource, eventType, 'timeline', true);
}

export function orchestrationEventPreviewLimit(
  contractSource: ContractSource | undefined,
  surface: unknown,
): number {
  const contract = publishedContract('eventContract', contractSource);
  const defaults = compatibilityContract('eventContract');
  const key = surface === 'timeline' ? 'timeline' : 'wire';
  const configured = record(contract?.previewLimits)?.[key];
  if (Number.isSafeInteger(configured) && Number(configured) > 0) {
    return Number(configured);
  }
  const fallback = record(defaults?.previewLimits)?.[key];
  return Number.isSafeInteger(fallback) && Number(fallback) > 0
    ? Number(fallback) : 4000;
}

export function projectOrchestrationEventPreview(
  value: unknown,
  contractSource: ContractSource | undefined,
  surface: unknown,
): string {
  const raw = String(value == null ? '' : value);
  return raw.slice(0, orchestrationEventPreviewLimit(
    contractSource, surface));
}

const bridge = orchestrationRegistry as unknown as EventPolicyWindow;
bridge.orchestrationEventContractSpec = orchestrationEventContractSpec;
bridge.orchestrationEventCapability = orchestrationEventCapability;
bridge.orchestrationEventStringCapability = orchestrationEventStringCapability;
bridge.orchestrationEventGateEffect = orchestrationEventGateEffect;
bridge.orchestrationEventShouldReduce = orchestrationEventShouldReduce;
bridge.orchestrationEventShouldTimeline = orchestrationEventShouldTimeline;
bridge.orchestrationEventPreviewLimit = orchestrationEventPreviewLimit;
bridge.projectOrchestrationEventPreview = projectOrchestrationEventPreview;
