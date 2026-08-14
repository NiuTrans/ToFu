import { orchestrationRegistry } from './registry';
import { orchestrationScrollScope } from './scroll-state';
import { orchestrationResultError } from './result';
import {
  orchestrationNodeTraceSnapshot,
  type OrchestrationNodeTraceSnapshot,
} from './trace-state';
import {
  projectOrchestrationTraceAttempts,
  projectOrchestrationTraceAttemptDeltaPresentation,
} from './trace-attempts';
import {
  projectOrchestrationTraceActivity,
  projectOrchestrationTraceStatusPresentation,
} from './trace-activity';
import {
  projectOrchestrationTraceSections,
  projectOrchestrationTraceText,
} from './trace-contract';
import type { OrchestrationNode } from './node-summary';

export interface TaskModeInspectorPresentationOptions {
  escape?: (value: unknown) => unknown;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  icon?: (name: string) => unknown;
  traceContract?: unknown;
  nodeLabel?: (node: OrchestrationNode) => unknown;
  nodeIconHtml?: (node: OrchestrationNode) => unknown;
  nodeSubtitle?: (node: OrchestrationNode) => unknown;
}

export interface TaskModeInspectorState extends Record<string, unknown> {
  runId?: unknown;
  definition?: unknown;
  selectedNode?: unknown;
  activeNode?: unknown;
  inspectedNode?: unknown;
  gates?: unknown;
  trace?: Record<string, Record<string, unknown>>;
  traceHistory?: Record<string, Record<string, unknown>[]>;
  traceCount?: Record<string, unknown>;
  nodeTrace?: OrchestrationNodeTraceSnapshot;
  stepEvent?: unknown;
}

type TaskModeInspectorPresentationWindow = Window & {
  createTaskModeInspectorPresentation?:
    typeof createTaskModeInspectorPresentation;
};

const record = (value: unknown): Record<string, unknown> | null => value
  && typeof value === 'object' && !Array.isArray(value)
  ? value as Record<string, unknown> : null;

export function createTaskModeInspectorPresentation(
  options: TaskModeInspectorPresentationOptions = {},
) {
  const escape = (value: unknown): string => String(
    options.escape ? options.escape(value) : value == null ? '' : value);
  const translate = (key: string, params?: Record<string, unknown>): unknown =>
    options.translate ? options.translate(key, params) : key;
  const icon = (name: string): string => String(
    options.icon ? options.icon(name) || '' : '');
  const traceDetail = (
    traceValue: Record<string, unknown> | null | undefined,
    stepEventValue?: unknown,
    attemptKey = 'current',
  ): string => {
    const stepEvent = record(stepEventValue);
    if (!traceValue) {
      return stepEvent?.isolation
        ? `<div class="tm-insp-meta">${escape(
          translate('tm.trace.isolation'))}: ${escape(stepEvent.isolation)}</div>`
        : '';
    }
    const trace = traceValue;
    const statusProjection = projectOrchestrationTraceStatusPresentation(
      trace.status, options.traceContract, translate);
    const statusClass = statusProjection.status === 'error' ? 'tm-trace-err'
      : statusProjection.status === 'done' ? 'tm-trace-ok' : '';
    const bits: unknown[] = [];
    if (trace.emits) bits.push(`${String(translate('tm.trace.emits'))} ${String(
      trace.emits)}`);
    if (trace.isolation) bits.push(trace.isolation);
    if (typeof trace.iteration === 'number' && trace.iteration > 0) {
      bits.push(`${String(translate('tm.trace.iter'))} ${trace.iteration}`);
    }
    const activity = projectOrchestrationTraceActivity(
      trace, options.traceContract);
    if (activity.stateChanging > 0) {
      bits.push(`${activity.stateChanging} ${String(
        translate('tm.trace.stateChanging'))}`);
    }
    let html = `<div class="tm-trace-tags"><span class="tm-trace-status ${
      statusClass}">${escape(statusProjection.label)}</span>${bits.length
      ? `<span class="tm-trace-bits">${escape(bits.join(' · '))}</span>`
      : ''}</div>`;
    const traceError = orchestrationResultError(trace.error, '');
    if (traceError) {
      const projection = projectOrchestrationTraceText(
        trace, 'error', options.traceContract, traceError);
      html += `<div class="tm-trace-lbl">${escape(
        translate('tm.trace.error'))}${projection.truncated
        ? ` <span class="tm-text-trunc">${escape(
          translate('tm.trace.truncated'))}</span>` : ''
        }</div><pre class="tm-trace-pre tm-trace-err">${escape(
        projection.text)}</pre>`;
    }
    const labels: Record<string, string> = {
      brief: 'tm.trace.brief',
      input: 'tm.trace.input',
      output: 'tm.trace.output',
    };
    projectOrchestrationTraceSections(
      trace, Object.keys(labels), options.traceContract,
    ).forEach((section) => {
      const open = section.field === 'output' ? ' open' : '';
      html += `<details class="tm-trace-section" data-tm-trace-field="${escape(
        section.field)}" data-tm-disclosure-key="${escape(
        `${attemptKey}:field:${section.field}`)}"${open}><summary class="tm-trace-lbl">${
        escape(translate(labels[section.field]))}${section.truncated
          ? ` <span class="tm-text-trunc">${escape(
            translate('tm.trace.truncated'))}</span>` : ''
        }</summary><pre class="tm-trace-pre">${escape(
          section.text)}</pre></details>`;
    });
    return html;
  };
  const traceAttemptsDetail = (
    attempts: ReturnType<typeof projectOrchestrationTraceAttempts>,
    stepEvent: unknown,
  ): string => {
    if (!attempts.length) return traceDetail(null, stepEvent);
    if (attempts.length === 1) {
      return traceDetail(attempts[0].trace, stepEvent, attempts[0].key);
    }
    return `<div class="tm-trace-attempts">${attempts.map((attempt, index) => {
      const status = projectOrchestrationTraceStatusPresentation(
        attempt.trace.status, options.traceContract, translate);
      const delta = index > 0
        ? projectOrchestrationTraceAttemptDeltaPresentation(
          attempts[index - 1].trace, attempt.trace,
          options.traceContract, translate,
        ) : null;
      return `<details class="tm-trace-attempt" data-tm-disclosure-key="${escape(
        attempt.key)}"${attempt.current ? ' open' : ''}><summary class="tm-trace-lbl"><span>${
        escape(translate('tm.trace.iter'))} ${attempt.ordinal} / ${attempt.total} · ${
        escape(status.label)}</span>${delta?.label
        ? ` · <span class="tm-trace-bits">${escape(delta.label)}</span>` : ''
        }</summary>${traceDetail(
        attempt.trace, attempt.current ? stepEvent : null, attempt.key,
      )}</details>`;
    }).join('')}</div>`;
  };
  const nodeMarkup = (
    state: TaskModeInspectorState,
    inspectId: unknown,
  ): string => {
    const definition = record(state.definition);
    const nodes = Array.isArray(definition?.nodes)
      ? definition.nodes as OrchestrationNode[] : [];
    const node = nodes.find((candidate) => candidate.id === inspectId);
    if (!node) return '';
    const nodeTrace = state.nodeTrace?.nodeId === String(node.id)
      ? state.nodeTrace : orchestrationNodeTraceSnapshot(state, node.id);
    const trace = nodeTrace.current || undefined;
    const attempts = nodeTrace.attempts;
    const pinned = state.selectedNode === node.id;
    const kindLabel = pinned
      ? trace ? translate('tm.insp.runTrace') : translate('tm.insp.node')
      : translate('tm.insp.activeNode');
    const count = attempts.at(-1)?.total || 0;
    const nodeLabel = options.nodeLabel
      ? options.nodeLabel(node) : node.name || node.id || '';
    const nodeIcon = options.nodeIconHtml ? options.nodeIconHtml(node) : '';
    const subtitle = options.nodeSubtitle ? options.nodeSubtitle(node) : '';
    const typeLabel = node.type === 'role'
      ? node.role || translate('tm.node.agent')
      : node.kind || translate('tm.node.control');
    const metadata: unknown[] = [typeLabel];
    if (subtitle && String(subtitle) !== String(typeLabel)) {
      metadata.push(subtitle);
    }
    const stepEvent = record(state.stepEvent);
    const scopedStepEvent = stepEvent
      && String(stepEvent.node_id || '') === String(node.id || '')
      ? stepEvent : null;
    return `<div class="tm-insp-card"><div class="tm-insp-kind">${escape(
      kindLabel)}${Number(count) > 1
      ? ` <span class="tm-insp-runs">×${String(count)}</span>` : ''
      }</div><div class="tm-insp-node"><span class="tm-insp-ava">${String(
      nodeIcon)}</span>${escape(nodeLabel)}</div><div class="tm-insp-meta">${escape(
      metadata.join(' · '))}</div>${traceAttemptsDetail(
      attempts, scopedStepEvent)}</div>`;
  };
  const project = (
    stateValue: TaskModeInspectorState = {},
    gateProjectionValue: { gateIds?: string[]; html?: unknown } = {},
  ) => {
    const state = stateValue ?? {};
    const gateProjection = gateProjectionValue ?? {};
    const gateIds = Array.isArray(gateProjection.gateIds)
      ? gateProjection.gateIds : [];
    const inspectId = state.inspectedNode
      || state.selectedNode || state.activeNode;
    let html = String(gateProjection.html || '') + nodeMarkup(state, inspectId);
    if (!html) {
      html = `<div class="tm-insp-empty">${icon('eye')}<div>${escape(
        translate('tm.insp.empty'))}<br>${escape(
        translate('tm.insp.emptyHint'))}</div></div>`;
    }
    return Object.freeze({
      gateIds: gateIds.slice(),
      html,
      disclosureOwner: orchestrationScrollScope([
        state.runId || 'none',
        state.selectedNode ? 'selected' : state.activeNode ? 'active'
          : state.inspectedNode ? 'recent' : 'none',
        inspectId || 'none',
      ]),
      scrollOwner: orchestrationScrollScope([
        state.runId || 'none',
        state.selectedNode ? 'selected' : state.activeNode ? 'active'
          : state.inspectedNode ? 'recent' : 'none',
        inspectId || 'none',
        gateIds.join('\u0000'),
      ]),
    });
  };
  return Object.freeze({ project, traceDetail });
}

(orchestrationRegistry as unknown as TaskModeInspectorPresentationWindow)
  .createTaskModeInspectorPresentation = createTaskModeInspectorPresentation;
