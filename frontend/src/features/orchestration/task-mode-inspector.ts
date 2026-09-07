import { orchestrationRegistry } from './registry';
import {
  createOrchestrationScrollState,
  type OrchestrationScrollState,
} from './scroll-state';
import {
  createOrchestrationDisclosureState,
  type OrchestrationDisclosureState,
} from './disclosure-state';
import {
  createTaskModeGateView,
  type TaskModeGateViewOptions,
} from './task-mode-gate-view';
import type { TaskModeGateEvent } from './task-mode-gate-presentation';
import {
  createTaskModeInspectorPresentation,
  type TaskModeInspectorPresentationOptions,
  type TaskModeInspectorState,
} from './task-mode-inspector-presentation';

export interface TaskModeInspectorViewOptions
  extends TaskModeInspectorPresentationOptions {
  document?: Document;
  hostId?: string;
  scrollState?: OrchestrationScrollState;
  disclosureState?: OrchestrationDisclosureState;
  gateView?: ReturnType<typeof createTaskModeGateView>;
  presentation?: ReturnType<typeof createTaskModeInspectorPresentation>;
  bindImageFallbacks?: (root: Element) => unknown;
  requestLimits?: unknown;
  limitPolicy?: unknown;
  onApprove?: (requestId: string, approved: boolean) => unknown;
  onInput?: (requestId: string, input: string) => unknown;
  report?: (context: string, error: unknown) => unknown;
}

type TaskModeInspectorWindow = Window & {
  createTaskModeInspectorView?: typeof createTaskModeInspectorView;
};

const record = (value: unknown): Record<string, unknown> => value
  && typeof value === 'object' && !Array.isArray(value)
  ? value as Record<string, unknown> : {};

export function createTaskModeInspectorView(
  options: TaskModeInspectorViewOptions = {},
) {
  const scrollState = options.scrollState ?? createOrchestrationScrollState();
  const disclosureState = options.disclosureState
    ?? createOrchestrationDisclosureState();
  const gateView = options.gateView ?? createTaskModeGateView(
    options as unknown as TaskModeGateViewOptions);
  const presentation = options.presentation
    ?? createTaskModeInspectorPresentation(options);
  const doc = (): Document => options.document ?? document;
  const refreshRequestLimits = (): boolean => gateView.refreshRequestLimits();
  const render = (stateValue: TaskModeInspectorState = {}): string => {
    const state = stateValue ?? {};
    gateView.releaseBindings();
    const body = doc().getElementById(options.hostId || 'tmInspBody');
    if (!body) return '';
    scrollState.capture(body);
    const gates = record(state.gates) as Record<string, TaskModeGateEvent>;
    const gateProjection = gateView.project(state.runId, gates);
    const projected = presentation.project(state, gateProjection);
    body.innerHTML = projected.html;
    disclosureState.bind(body, projected.disclosureOwner, {
      selector: 'details[data-tm-disclosure-key]',
      attribute: 'data-tm-disclosure-key',
    });
    scrollState.restore(body, projected.scrollOwner);
    options.bindImageFallbacks?.(body);
    gateView.bind(body, state.runId, gates, projected.gateIds);
    return projected.html;
  };
  return {
    render,
    traceDetail: presentation.traceDetail,
    gateCard: gateView.gateCard,
    refreshRequestLimits,
  };
}

(orchestrationRegistry as unknown as TaskModeInspectorWindow).createTaskModeInspectorView =
  createTaskModeInspectorView;
