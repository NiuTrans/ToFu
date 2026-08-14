import { orchestrationRegistry } from './registry';
import { type ContractSource } from './contracts';
import { formatOrchestrationEventLines } from './event-format';
import {
  createOrchestrationEventState,
  reduceOrchestrationEvent,
  resetOrchestrationEventState,
  type OrchestrationEvent,
  type OrchestrationEventChange,
  type OrchestrationEventState,
} from './events';
import {
  projectOrchestrationEventPresentation,
  type OrchestrationEventPresentation,
} from './event-presentation';
import { orchestrationNodeTraceSnapshot } from './trace-state';
import { projectOrchestrationFinalResult } from './outcome-result';

export interface RunEventControllerOptions {
  contract?: (name: string) => ContractSource;
  escape(value: unknown): string;
  translate(key: string, params?: Record<string, unknown>): string;
  icon(name: string): string;
  log(html: string, className?: string): unknown;
  humanGates: {
    render(event: OrchestrationEvent): unknown;
    clear(requestId: unknown): unknown;
  };
  onResetTrace?: () => unknown;
  onStateChange?: (
    state: OrchestrationEventState,
    change: OrchestrationEventChange,
    presentation: OrchestrationEventPresentation,
  ) => unknown;
  onGraphChange?: (
    state: OrchestrationEventState,
    event: OrchestrationEvent,
    change: OrchestrationEventChange,
  ) => unknown;
  onTraceChange?: RunEventControllerOptions['onGraphChange'];
  onGateChange?: RunEventControllerOptions['onGraphChange'];
}

type RunEventControllerWindow = Window & {
  createOrchestrationRunEventController?:
    typeof createOrchestrationRunEventController;
};

/** Event reduction, trace notification, timeline copy and gate projection. */
export function createOrchestrationRunEventController(
  options: RunEventControllerOptions,
) {
  const state = createOrchestrationEventState();
  const contract = (name: string): ContractSource =>
    options.contract ? options.contract(name) : null;
  const eventLines = (event: OrchestrationEvent) =>
    formatOrchestrationEventLines(event, {
      escape: options.escape,
      translate: options.translate,
      icon: options.icon,
      dimClass: 'orch-run-dim',
      eventContract: contract('eventContract'),
      outcomeContract: contract('outcomeContract'),
    });
  const reset = (): void => {
    resetOrchestrationEventState(state);
    options.onResetTrace?.();
  };
  const notify = (
    event: OrchestrationEvent,
    change: OrchestrationEventChange,
    presentation: OrchestrationEventPresentation,
  ): unknown => {
    if (typeof options.onStateChange === 'function') {
      return options.onStateChange(state, change, presentation);
    }
    const callbacks = {
      graph: options.onGraphChange,
      trace: options.onTraceChange,
      gates: options.onGateChange,
    };
    presentation.channels.forEach((channel) => {
      callbacks[channel]?.(state, event, change);
    });
    return undefined;
  };
  const render = (event: OrchestrationEvent): void => {
    const change = reduceOrchestrationEvent(
      state, event, contract('eventContract'), contract('traceContract'));
    const presentation = projectOrchestrationEventPresentation(
      change, event, contract('eventContract'));
    notify(event, change, presentation);
    if (event.type === 'human_request') {
      options.humanGates.render(event);
      return;
    }
    if (event.type === 'human_resolved') {
      options.humanGates.clear(event.request_id);
    }
    if (event.type === 'done') {
      const finalProjection = projectOrchestrationFinalResult(
        event.result, options.translate, contract('outcomeContract'));
      if (finalProjection.finalText) {
        options.log(`<b>${options.escape(options.translate(
          finalProjection.partial
            ? 'orch.run.partialResult' : 'orch.run.result')
          + (finalProjection.finalTruncated
            ? ` ${options.translate('orch.run.truncated')}` : ''))}</b>`,
        finalProjection.partial ? finalProjection.lineClass : '');
        options.log(`<pre class="orch-run-final">${
          options.escape(finalProjection.finalText)}</pre>`);
      }
      return;
    }
    if (!presentation.timeline) return;
    eventLines(event).forEach((row) => {
      options.log(row.html, row.className);
    });
  };
  const traceSnapshotFor = (nodeId: unknown) =>
    orchestrationNodeTraceSnapshot(state, nodeId);

  return {
    state: () => state,
    traceSnapshotFor,
    traceFor: (nodeId: unknown) => traceSnapshotFor(nodeId).current,
    traceHistoryFor: (nodeId: unknown) => traceSnapshotFor(nodeId).history,
    traceCountFor: (nodeId: unknown) => traceSnapshotFor(nodeId).total,
    reset,
    render,
  };
}

(orchestrationRegistry as unknown as RunEventControllerWindow).createOrchestrationRunEventController =
  createOrchestrationRunEventController;
