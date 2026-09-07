import { orchestrationRegistry } from './registry';
import { compatibilityContract, record, type ContractSource } from './contracts';
import { orchestrationTraceContract } from './trace-contract';

export interface ProjectedTraceActivity {
  readonly stateChanging: number;
  readonly exploratory: number;
  readonly stateChangingTools: readonly string[];
}

export type OrchestrationTraceStatus = 'running' | 'done' | 'error' | 'unknown';

export interface TraceStatusPresentation {
  readonly status: OrchestrationTraceStatus;
  readonly label: string;
}

interface TraceActivityFields {
  readonly stateChanging: string;
  readonly exploratory: string;
  readonly stateChangingTools: string;
}

type TraceActivityWindow = Window & {
  projectOrchestrationTraceActivity?: typeof projectOrchestrationTraceActivity;
  applyOrchestrationTraceActivity?: typeof applyOrchestrationTraceActivity;
  projectOrchestrationTraceStatus?: typeof projectOrchestrationTraceStatus;
  projectOrchestrationTraceStatusPresentation?:
    typeof projectOrchestrationTraceStatusPresentation;
};

function traceActivityFields(
  contractSource?: ContractSource,
): TraceActivityFields {
  const contract = orchestrationTraceContract(contractSource);
  const defaults = compatibilityContract('traceContract');
  const fields = record(contract?.activityFields) ?? {};
  const fallbackFields = record(defaults?.activityFields) ?? {};
  const field = (name: string, legacy: string): string => {
    const configured = fields[name];
    const fallback = fallbackFields[name];
    return typeof configured === 'string' && configured
      ? configured
      : typeof fallback === 'string' && fallback ? fallback : legacy;
  };
  return {
    stateChanging: field('stateChanging', 'state_changing'),
    exploratory: field('exploratory', 'exploratory'),
    stateChangingTools: field('stateChangingTools', 'state_changing_tools'),
  };
}

export function projectOrchestrationTraceActivity(
  traceValue: unknown,
  contractSource?: ContractSource,
): ProjectedTraceActivity {
  const trace = record(traceValue) ?? {};
  const fields = traceActivityFields(contractSource);
  const count = (value: unknown): number => Number.isSafeInteger(value)
    && Number(value) > 0 ? Number(value) : 0;
  const rawTools = trace[fields.stateChangingTools];
  const tools = Array.isArray(rawTools)
    ? rawTools.filter((name): name is string =>
      typeof name === 'string' && Boolean(name.trim())).map((name) => name.trim())
    : [];
  return Object.freeze({
    stateChanging: count(trace[fields.stateChanging]),
    exploratory: count(trace[fields.exploratory]),
    stateChangingTools: Object.freeze(tools),
  });
}

export function applyOrchestrationTraceActivity(
  targetValue: unknown,
  sourceValue: unknown,
  contractSource?: ContractSource,
): boolean {
  const target = record(targetValue);
  const source = record(sourceValue);
  if (!target || !source) return false;
  const fields = traceActivityFields(contractSource);
  const present = Object.values(fields).some((field) =>
    Object.prototype.hasOwnProperty.call(source, field));
  if (!present) return false;
  const activity = projectOrchestrationTraceActivity(source, contractSource);
  target[fields.stateChanging] = activity.stateChanging;
  target[fields.exploratory] = activity.exploratory;
  target[fields.stateChangingTools] = activity.stateChangingTools;
  return true;
}

export function projectOrchestrationTraceStatus(
  value: unknown,
  contractSource?: ContractSource,
): OrchestrationTraceStatus {
  const raw = String(value == null ? '' : value).trim();
  const contract = orchestrationTraceContract(contractSource);
  const defaults = compatibilityContract('traceContract');
  const configured = record(contract?.statusMap)?.[raw];
  const fallback = record(defaults?.statusMap)?.[raw];
  const status = typeof configured === 'string' && configured
    ? configured : fallback;
  return status === 'running' || status === 'done' || status === 'error'
    ? status : 'unknown';
}

export function projectOrchestrationTraceStatusPresentation(
  value: unknown,
  contractSource?: ContractSource,
  translate?: (key: string) => unknown,
): TraceStatusPresentation {
  const raw = String(value == null ? '' : value).trim();
  const status = projectOrchestrationTraceStatus(raw, contractSource);
  const keys: Record<OrchestrationTraceStatus, string> = {
    running: 'orch.run.statusRunning', done: 'orch.run.statusDone',
    error: 'orch.run.statusError', unknown: 'orch.run.statusUnknown',
  };
  const key = keys[status];
  let label: unknown = status === 'unknown' && raw ? raw : key;
  if (!(status === 'unknown' && raw) && typeof translate === 'function') {
    label = translate(key);
  }
  return Object.freeze({ status, label: String(label || status) });
}

Object.assign(orchestrationRegistry as unknown as TraceActivityWindow, {
  projectOrchestrationTraceActivity,
  applyOrchestrationTraceActivity,
  projectOrchestrationTraceStatus,
  projectOrchestrationTraceStatusPresentation,
});
