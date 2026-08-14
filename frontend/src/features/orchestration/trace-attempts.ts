import { orchestrationRegistry } from './registry';
import { record, type ContractRecord, type ContractSource } from './contracts';
import {
  projectOrchestrationTraceText, type ProjectedTraceText,
} from './trace-contract';
import {
  projectOrchestrationTraceActivity,
  projectOrchestrationTraceStatus,
} from './trace-activity';

export interface ProjectedTraceAttempt {
  readonly key: string;
  readonly ordinal: number;
  readonly total: number;
  readonly current: boolean;
  readonly trace: ContractRecord;
}

export interface ProjectedTraceAttemptDelta {
  readonly changedFields: readonly string[];
  readonly statusChanged: boolean;
  readonly stateChangingDelta: number;
  readonly exploratoryDelta: number;
  readonly toolsAdded: readonly string[];
  readonly toolsRemoved: readonly string[];
  readonly elapsedDelta: number | null;
  readonly hasChanges: boolean;
}

export interface TraceAttemptDeltaPresentation {
  readonly delta: ProjectedTraceAttemptDelta;
  readonly bits: readonly string[];
  readonly label: string;
}

type TraceAttemptsWindow = Window & {
  projectOrchestrationTraceAttempts?: typeof projectOrchestrationTraceAttempts;
  projectOrchestrationTraceAttemptDelta?:
    typeof projectOrchestrationTraceAttemptDelta;
  projectOrchestrationTraceAttemptDeltaPresentation?:
    typeof projectOrchestrationTraceAttemptDeltaPresentation;
};

export function projectOrchestrationTraceAttempts(
  traceValue: unknown,
  historyValue: readonly unknown[],
  totalCountValue: unknown,
): readonly ProjectedTraceAttempt[] {
  const trace = record(traceValue);
  const history = Array.isArray(historyValue)
    ? historyValue.map((entry) => record(entry)).filter(
      (entry): entry is ContractRecord => Boolean(entry)) : [];
  let total = Number.isSafeInteger(totalCountValue)
    && Number(totalCountValue) >= 0 ? Number(totalCountValue) : history.length;
  total = Math.max(total, history.length);
  const start = Math.max(1, total - history.length + 1);
  const sources = history.map((entry, index) => ({
    trace: entry, ordinal: start + index,
  }));
  if (trace) {
    const latest = sources.at(-1);
    const traceSeq = Number(trace.seq);
    const latestSeq = Number(latest?.trace.seq);
    const marker = Number(trace.__orchestrationTraceAttempt);
    const replacesLatest = Boolean(latest) && (
      Number.isSafeInteger(traceSeq) && traceSeq > 0 && traceSeq === latestSeq
      || Number.isSafeInteger(marker) && marker > 0
        && marker === latest?.ordinal);
    if (replacesLatest && latest) latest.trace = trace;
    else {
      const ordinal = Number.isSafeInteger(marker) && marker > 0
        ? marker : !sources.length && total > 0 ? total : total + 1;
      total = Math.max(total, ordinal);
      sources.push({ trace, ordinal });
    }
  }
  return Object.freeze(sources.map((entry, index) => Object.freeze({
    key: `attempt:${entry.ordinal}`, ordinal: entry.ordinal, total,
    current: index === sources.length - 1, trace: entry.trace,
  })));
}

export function projectOrchestrationTraceAttemptDelta(
  previousValue: unknown,
  currentValue: unknown,
  fieldsValue: readonly unknown[] = ['brief', 'input', 'output', 'error'],
  contractSource?: ContractSource,
): ProjectedTraceAttemptDelta {
  const previous = record(previousValue) ?? {};
  const current = record(currentValue) ?? {};
  const fields = Array.isArray(fieldsValue) && fieldsValue.length
    ? fieldsValue : ['brief', 'input', 'output', 'error'];
  const changedFields: string[] = [];
  const seen = new Set<string>();
  for (const value of fields) {
    const field = String(value || '');
    if (!field || seen.has(field)) continue;
    seen.add(field);
    const comparable = (trace: ContractRecord): ProjectedTraceText => {
      let raw = trace[field];
      if (raw && typeof raw === 'object') {
        try { raw = JSON.stringify(raw); } catch { raw = String(raw); }
      }
      return projectOrchestrationTraceText(trace, field, contractSource, raw);
    };
    const before = comparable(previous);
    const after = comparable(current);
    if (before.text !== after.text || before.truncated !== after.truncated) {
      changedFields.push(field);
    }
  }
  const beforeActivity = projectOrchestrationTraceActivity(
    previous, contractSource);
  const afterActivity = projectOrchestrationTraceActivity(
    current, contractSource);
  const toolsAdded = afterActivity.stateChangingTools.filter(
    (name) => !beforeActivity.stateChangingTools.includes(name));
  const toolsRemoved = beforeActivity.stateChangingTools.filter(
    (name) => !afterActivity.stateChangingTools.includes(name));
  const beforeElapsed = Number(previous.elapsed);
  const afterElapsed = Number(current.elapsed);
  const elapsedDelta = Number.isFinite(beforeElapsed)
    && Number.isFinite(afterElapsed)
    ? Math.round((afterElapsed - beforeElapsed) * 100) / 100 : null;
  const statusChanged = projectOrchestrationTraceStatus(
    previous.status, contractSource) !== projectOrchestrationTraceStatus(
    current.status, contractSource);
  const stateChangingDelta = afterActivity.stateChanging
    - beforeActivity.stateChanging;
  const exploratoryDelta = afterActivity.exploratory
    - beforeActivity.exploratory;
  return Object.freeze({
    changedFields: Object.freeze(changedFields), statusChanged,
    stateChangingDelta, exploratoryDelta,
    toolsAdded: Object.freeze(toolsAdded),
    toolsRemoved: Object.freeze(toolsRemoved), elapsedDelta,
    hasChanges: statusChanged || changedFields.length > 0
      || stateChangingDelta !== 0 || exploratoryDelta !== 0
      || toolsAdded.length > 0 || toolsRemoved.length > 0,
  });
}

export function projectOrchestrationTraceAttemptDeltaPresentation(
  previous: unknown,
  current: unknown,
  contractSource?: ContractSource,
  translate?: (key: string) => unknown,
): TraceAttemptDeltaPresentation {
  const delta = projectOrchestrationTraceAttemptDelta(
    previous, current, undefined, contractSource);
  const tr = (key: string): string => String(
    typeof translate === 'function' ? translate(key) : key);
  const signed = (value: number): string => value > 0 ? `+${value}` : `${value}`;
  const bits: string[] = [];
  if (delta.changedFields.length) {
    bits.push(`Δ ${delta.changedFields.map(
      (field) => tr(`tm.trace.${field}`)).join(', ')}`);
  }
  if (delta.stateChangingDelta) {
    bits.push(`${signed(delta.stateChangingDelta)} ${tr(
      'tm.trace.stateChanging')}`);
  }
  if (delta.toolsAdded.length) bits.push(`+${delta.toolsAdded.join(', ')}`);
  if (delta.toolsRemoved.length) bits.push(`−${delta.toolsRemoved.join(', ')}`);
  if (delta.elapsedDelta) bits.push(`${signed(delta.elapsedDelta)}s`);
  return Object.freeze({
    delta, bits: Object.freeze(bits), label: bits.join(' · '),
  });
}

Object.assign(orchestrationRegistry as unknown as TraceAttemptsWindow, {
  projectOrchestrationTraceAttempts,
  projectOrchestrationTraceAttemptDelta,
  projectOrchestrationTraceAttemptDeltaPresentation,
});
