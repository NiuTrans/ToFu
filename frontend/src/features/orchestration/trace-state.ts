import { orchestrationRegistry } from './registry';
import { projectOrchestrationEventPreview } from './event-policy';
import {
  applyOrchestrationTraceActivity,
  projectOrchestrationTraceStatus,
} from './trace-activity';
import { orchestrationTraceHistoryLimit } from './trace-contract';
import {
  projectOrchestrationTraceAttempts,
  type ProjectedTraceAttempt,
} from './trace-attempts';
import type { ContractSource } from './contracts';

export type OrchestrationTraceEvent = Record<string, unknown> & {
  type?: unknown;
  node_id?: unknown;
};

export type OrchestrationTrace = Record<string, unknown>;

export interface OrchestrationTraceState {
  trace: Record<string, OrchestrationTrace>;
  traceHistory: Record<string, OrchestrationTrace[]>;
  traceCount: Record<string, number>;
  traceSequence: Record<string, number>;
}

export interface OrchestrationNodeTraceSnapshot {
  readonly nodeId: string;
  readonly current: OrchestrationTrace | null;
  readonly history: readonly OrchestrationTrace[];
  readonly total: number;
  readonly attempts: readonly ProjectedTraceAttempt[];
}

export interface OrchestrationTraceEventEffect {
  handled: boolean;
  trace: boolean;
  graph: boolean;
  nodeId: unknown | null;
  nodeStatus: unknown | null;
  activate: boolean;
  finish: boolean;
  hasError: boolean;
  error: unknown | null;
}

type TraceStateWindow = Window & {
  createOrchestrationTraceState?: typeof createOrchestrationTraceState;
  orchestrationNodeTraceSnapshot?: typeof orchestrationNodeTraceSnapshot;
  recordOrchestrationTraceAttempt?: typeof recordOrchestrationTraceAttempt;
  reduceOrchestrationTraceEvent?: typeof reduceOrchestrationTraceEvent;
};

function traceKey(value: unknown): string {
  return String(value == null ? '' : value);
}

export function createOrchestrationTraceState(): OrchestrationTraceState {
  return {
    trace: {},
    traceHistory: {},
    traceCount: {},
    traceSequence: {},
  };
}

export function orchestrationNodeTraceSnapshot(
  stateValue: unknown,
  nodeId: unknown,
): OrchestrationNodeTraceSnapshot {
  const state = stateValue && typeof stateValue === 'object'
    ? stateValue as Partial<OrchestrationTraceState> : {};
  const key = traceKey(nodeId);
  const trace = state.trace?.[key] || null;
  const storedHistory = state.traceHistory?.[key];
  const history = Array.isArray(storedHistory) ? storedHistory.slice() : [];
  const count = state.traceCount?.[key];
  const total = Number.isSafeInteger(count) && Number(count) >= 0
    ? Number(count) : history.length;
  const attempts = projectOrchestrationTraceAttempts(trace, history, total);
  return Object.freeze({
    nodeId: key,
    current: trace,
    history: Object.freeze(history),
    total: attempts.at(-1)?.total ?? total,
    attempts,
  });
}

export function recordOrchestrationTraceAttempt(
  state: OrchestrationTraceState,
  nodeKey: string,
  trace: OrchestrationTrace,
  traceContract?: ContractSource,
): boolean {
  if (!nodeKey || !trace) return false;
  state.traceSequence ||= {};
  state.traceHistory ||= {};
  state.traceCount ||= {};
  const sequence = Number(trace.seq);
  const hasSequence = Number.isSafeInteger(sequence) && sequence > 0;
  const previousSequence = Number(state.traceSequence[nodeKey]);
  if (hasSequence && Number.isSafeInteger(previousSequence)
    && previousSequence > 0 && sequence <= previousSequence) return false;
  if (hasSequence) state.traceSequence[nodeKey] = sequence;
  const count = (state.traceCount[nodeKey] || 0) + 1;
  state.traceCount[nodeKey] = count;
  const history = state.traceHistory[nodeKey]
    || (state.traceHistory[nodeKey] = []);
  history.push({ ...trace });
  const limit = orchestrationTraceHistoryLimit(traceContract);
  if (history.length > limit) history.splice(0, history.length - limit);
  Object.defineProperty(trace, '__orchestrationTraceAttempt', {
    configurable: true,
    value: count,
  });
  return true;
}

function traceEventEffect(
  event: OrchestrationTraceEvent,
): OrchestrationTraceEventEffect {
  return {
    handled: false,
    trace: false,
    graph: false,
    nodeId: event.node_id || null,
    nodeStatus: null,
    activate: false,
    finish: false,
    hasError: false,
    error: null,
  };
}

export function reduceOrchestrationTraceEvent(
  stateValue: OrchestrationTraceState | null | undefined,
  eventValue: OrchestrationTraceEvent | null | undefined,
  eventContract?: ContractSource,
  traceContract?: ContractSource,
): OrchestrationTraceEventEffect {
  const state = stateValue || createOrchestrationTraceState();
  const event = eventValue || {};
  const effect = traceEventEffect(event);
  const type = typeof event.type === 'string' ? event.type : '';
  const nodeId = event.node_id;
  const nodeKey = traceKey(nodeId);
  let trace: OrchestrationTrace | undefined;

  switch (type) {
    case 'step_start':
      effect.handled = true;
      if (!nodeId) break;
      trace = {
        node_id: nodeId,
        role: event.role,
        name: event.name || event.role,
        status: projectOrchestrationTraceStatus('running', traceContract),
        output: '',
        preview: '',
        emits: event.emits || '',
        isolation: event.isolation || '',
      };
      state.trace[nodeKey] = trace;
      effect.nodeStatus = trace.status;
      effect.activate = true;
      effect.graph = true;
      effect.trace = true;
      break;

    case 'step_delta':
      effect.handled = true;
      trace = nodeId ? state.trace[nodeKey] : undefined;
      if (trace) {
        trace.phase = '';
        trace.phaseDetail = '';
        trace.phaseDetailKey = '';
        trace.phaseDetailArgs = null;
        if (event.kind !== 'thinking') {
          trace.output = String(trace.output || '') + String(event.chunk || '');
        }
        effect.trace = true;
      }
      break;

    case 'step_phase':
      effect.handled = true;
      if (!nodeId) break;
      trace = state.trace[nodeKey] || (state.trace[nodeKey] = {
        node_id: nodeId,
        role: event.role,
        name: event.name || event.role,
        status: projectOrchestrationTraceStatus('running', traceContract),
        output: '',
      });
      trace.phase = event.phase || 'working';
      trace.phaseDetail = event.detail || '';
      trace.phaseDetailKey = event.detailKey || '';
      trace.phaseDetailArgs = event.detailArgs || null;
      effect.trace = true;
      break;

    case 'step_complete':
      effect.handled = true;
      if (!nodeId) break;
      trace = state.trace[nodeKey] || (state.trace[nodeKey] = {
        node_id: nodeId,
        output: '',
      });
      trace.role = event.role || trace.role;
      trace.name = event.name || trace.name || trace.role;
      trace.status = projectOrchestrationTraceStatus(
        event.status || 'completed', traceContract);
      trace.output = event.output != null
        ? event.output : (trace.output || event.preview || '');
      trace.preview = event.preview || projectOrchestrationEventPreview(
        trace.output, eventContract, 'wire');
      applyOrchestrationTraceActivity(trace, event, traceContract);
      if (event.emits) trace.emits = event.emits;
      trace.phase = '';
      trace.phaseDetail = '';
      trace.phaseDetailKey = '';
      trace.phaseDetailArgs = null;
      effect.nodeStatus = trace.status;
      effect.finish = true;
      effect.graph = true;
      effect.trace = true;
      break;

    case 'step_trace':
      effect.handled = true;
      if (!nodeId) break;
      trace = state.trace[nodeKey] || {};
      {
        const candidate = {
          ...trace,
          ...event,
          node_id: nodeId,
          status: projectOrchestrationTraceStatus(
            event.status || trace.status || 'completed', traceContract),
        };
        applyOrchestrationTraceActivity(candidate, event, traceContract);
        if (recordOrchestrationTraceAttempt(
          state, nodeKey, candidate, traceContract,
        )) {
          state.trace[nodeKey] = candidate;
          effect.trace = true;
        }
      }
      break;

    case 'error':
      effect.handled = true;
      effect.hasError = true;
      effect.error = event.error || { detail: 'error' };
      if (!nodeId) break;
      trace = state.trace[nodeKey] || (state.trace[nodeKey] = {
        node_id: nodeId,
        output: '',
      });
      trace.status = projectOrchestrationTraceStatus('error', traceContract);
      trace.error = effect.error;
      trace.phase = '';
      effect.nodeStatus = trace.status;
      effect.finish = true;
      effect.graph = true;
      effect.trace = true;
      break;
  }

  return effect;
}

const bridge = orchestrationRegistry as unknown as TraceStateWindow;
bridge.createOrchestrationTraceState = createOrchestrationTraceState;
bridge.orchestrationNodeTraceSnapshot = orchestrationNodeTraceSnapshot;
bridge.recordOrchestrationTraceAttempt = recordOrchestrationTraceAttempt;
bridge.reduceOrchestrationTraceEvent = reduceOrchestrationTraceEvent;
