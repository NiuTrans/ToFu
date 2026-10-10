import { orchestrationRegistry } from './registry';
import { orchestrationEventShouldTimeline } from './event-policy';
import type { ContractSource } from './contracts';
import type {
  OrchestrationEvent,
  OrchestrationEventChange,
} from './events';

export type OrchestrationEventPresentationChannel =
  'graph' | 'trace' | 'gates';

export interface OrchestrationEventPresentation {
  readonly graph: boolean;
  readonly trace: boolean;
  readonly gates: boolean;
  readonly terminal: boolean;
  readonly timeline: boolean;
  readonly inspector: boolean;
  readonly traceNode: string | null;
  readonly channels: readonly OrchestrationEventPresentationChannel[];
}

export interface OrchestrationEventPresentationOptions {
  selectedNode?: unknown;
}

type EventPresentationWindow = Window & {
  projectOrchestrationEventPresentation?:
    typeof projectOrchestrationEventPresentation;
};

export function projectOrchestrationEventPresentation(
  changeValue: OrchestrationEventChange | null | undefined,
  eventValue: OrchestrationEvent | null | undefined,
  eventContract?: ContractSource,
  options: OrchestrationEventPresentationOptions = {},
): OrchestrationEventPresentation {
  const change = changeValue || {} as OrchestrationEventChange;
  const event = eventValue || {};
  const nodeId = event.node_id ? String(event.node_id) : null;
  const selectedNode = options.selectedNode
    ? String(options.selectedNode) : null;
  const graph = Boolean(change.graph);
  const trace = Boolean(change.trace);
  const gates = Boolean(change.gates);
  const terminal = Boolean(change.terminal);
  const traceNode = trace && nodeId ? nodeId : null;
  const channels: OrchestrationEventPresentationChannel[] = [];
  if (graph) channels.push('graph');
  if (trace) channels.push('trace');
  if (gates) channels.push('gates');
  return Object.freeze({
    graph,
    trace,
    gates,
    terminal,
    timeline: orchestrationEventShouldTimeline(eventContract, event.type),
    inspector: Boolean(gates || terminal || traceNode
      && (!selectedNode || selectedNode === traceNode)),
    traceNode,
    channels: Object.freeze(channels),
  });
}

(orchestrationRegistry as unknown as EventPresentationWindow).projectOrchestrationEventPresentation =
  projectOrchestrationEventPresentation;
