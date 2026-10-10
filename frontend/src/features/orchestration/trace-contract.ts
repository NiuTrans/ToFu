import { orchestrationRegistry } from './registry';
import {
  compatibilityContract, record,
  type ContractRecord, type ContractSource,
} from './contracts';

export interface ProjectedTraceText {
  readonly text: string;
  readonly truncated: boolean;
  readonly limit: number;
}

export interface ProjectedTraceSection extends ProjectedTraceText {
  readonly field: string;
}

type TraceContractWindow = Window & {
  _orchestrationTraceContract?: typeof orchestrationTraceContract;
  projectOrchestrationTraceText?: typeof projectOrchestrationTraceText;
  projectOrchestrationTraceSections?: typeof projectOrchestrationTraceSections;
  orchestrationTraceHistoryLimit?: typeof orchestrationTraceHistoryLimit;
};

export function orchestrationTraceContract(
  contractSource?: ContractSource,
): ContractRecord | null {
  return compatibilityContract('traceContract', contractSource);
}

export function projectOrchestrationTraceText(
  traceValue: unknown,
  fieldValue: unknown,
  contractSource?: ContractSource,
  ...explicitValue: readonly unknown[]
): ProjectedTraceText {
  const trace = record(traceValue) ?? {};
  const field = String(fieldValue || '');
  const contract = orchestrationTraceContract(contractSource);
  const defaults = compatibilityContract('traceContract');
  const limits = record(contract?.textLimits);
  const defaultLimits = record(defaults?.textLimits);
  const configured = limits?.[field];
  const fallbackValue = defaultLimits?.[field];
  const fallback = typeof fallbackValue === 'number' ? fallbackValue : 4000;
  const limit = Number.isSafeInteger(configured) && Number(configured) > 0
    ? Number(configured) : fallback;
  const source = explicitValue.length > 0 ? explicitValue[0] : trace[field];
  const raw = String(source == null ? '' : source);
  const flags = record(contract?.truncationFlags);
  const defaultFlags = record(defaults?.truncationFlags);
  const configuredFlag = flags?.[field];
  const fallbackFlag = defaultFlags?.[field] || `${field}_truncated`;
  const flag = typeof configuredFlag === 'string' && configuredFlag
    ? configuredFlag : String(fallbackFlag);
  return Object.freeze({
    text: raw.slice(0, limit),
    truncated: Boolean(trace[flag]) || raw.length > limit,
    limit,
  });
}

export function projectOrchestrationTraceSections(
  traceValue: unknown,
  fieldsValue: readonly unknown[],
  contractSource?: ContractSource,
): readonly ProjectedTraceSection[] {
  const trace = record(traceValue) ?? {};
  const fields = Array.isArray(fieldsValue) ? fieldsValue : [];
  const seen = new Set<string>();
  const sections: ProjectedTraceSection[] = [];
  for (const value of fields) {
    const field = String(value || '');
    if (!field || seen.has(field)) continue;
    seen.add(field);
    const projection = projectOrchestrationTraceText(
      trace, field, contractSource);
    const text = projection.text.trim();
    if (!text) continue;
    sections.push(Object.freeze({ field, text,
      truncated: projection.truncated, limit: projection.limit }));
  }
  return Object.freeze(sections);
}

export function orchestrationTraceHistoryLimit(
  contractSource?: ContractSource,
): number {
  const contract = orchestrationTraceContract(contractSource);
  const defaults = compatibilityContract('traceContract');
  const configured = contract?.historyLimit;
  const fallback = defaults?.historyLimit;
  return Number.isSafeInteger(configured) && Number(configured) > 0
    ? Number(configured)
    : Number.isSafeInteger(fallback) && Number(fallback) > 0
      ? Number(fallback) : 12;
}

Object.assign(orchestrationRegistry as unknown as TraceContractWindow, {
  _orchestrationTraceContract: orchestrationTraceContract,
  projectOrchestrationTraceText,
  projectOrchestrationTraceSections,
  orchestrationTraceHistoryLimit,
});
