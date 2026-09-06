import { orchestrationRegistry } from './registry';
import { record, type ContractSource } from './contracts';
import {
  orchestrationEventPreviewLimit,
  orchestrationEventShouldTimeline,
} from './event-policy';
import { normalizeOrchestrationOutcome } from './outcome-result';
import { orchestrationResultError } from './result';

export interface OrchestrationEventLine {
  html: string;
  className: string;
}

export interface OrchestrationEventFormatOptions {
  escape?: (value: unknown) => unknown;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  icon?: (name: string) => unknown;
  dimClass?: string;
  eventContract?: ContractSource;
  outcomeContract?: ContractSource;
}

type EventFormatWindow = Window & {
  formatOrchestrationEventLines?: typeof formatOrchestrationEventLines;
};

/** Pure event-to-copy projection shared by Studio and Task Mode. */
export function formatOrchestrationEventLines(
  eventValue: unknown,
  options: OrchestrationEventFormatOptions = {},
): OrchestrationEventLine[] {
  const event = record(eventValue) ?? {};
  if (!orchestrationEventShouldTimeline(
    options.eventContract, event.type)) return [];
  const escape = (value: unknown): string => String(options.escape
    ? options.escape(value == null ? '' : value)
    : value == null ? '' : value);
  const translate = (
    key: string,
    params?: Record<string, unknown>,
  ): unknown => options.translate ? options.translate(key, params) : key;
  const text = (key: string, params?: Record<string, unknown>): string =>
    escape(translate(key, params));
  const icon = (name: string): string => String(
    options.icon ? options.icon(name) || '' : '');
  const dim = (value: unknown, limit?: number): string => {
    if (value == null || value === '') return '';
    const className = options.dimClass || 'orch-event-dim';
    return ` <span class="${escape(className)}">${
      escape(String(value).slice(0, limit || 120))}</span>`;
  };
  const line = (html: string, className?: string): OrchestrationEventLine => ({
    html,
    className: className || '',
  });
  const localizedCode = (
    prefix: string,
    value: unknown,
    fallbackKey?: string,
  ): string => {
    const raw = String(value == null || value === '' ? '' : value);
    if (!raw && fallbackKey) return String(translate(fallbackKey));
    const key = prefix + raw;
    const translated = translate(key);
    return String(translated === key ? raw : translated);
  };

  const rows: OrchestrationEventLine[] = [];
  switch (event.type) {
    case 'flow_start':
      rows.push(line(`${icon('flag')} <b>${
        escape(event.name || translate('orch.ev.flowFallback'))}</b> — ${
        text('orch.ev.flowNodes', {
          n: event.nodes == null ? 0 : event.nodes,
        })}`));
      break;
    case 'step_start':
      rows.push(line(`${icon('bot')} <b>${escape(
        event.name || event.role || translate('orch.ev.stepFallback'))
      }</b> ${text('orch.ev.running')}`, 'is-active'));
      break;
    case 'step_complete':
      rows.push(line(`${icon('check')} ${escape(
        event.name || event.role || translate('orch.ev.stepFallback'))}${dim(
        event.preview,
        orchestrationEventPreviewLimit(options.eventContract, 'timeline'),
      )}`));
      break;
    case 'loop_start':
      rows.push(line(`${icon('loop')} ${text('orch.ev.loopStart', {
        max: event.max_iterations == null ? 0 : event.max_iterations,
      })}`));
      break;
    case 'loop_iteration':
      rows.push(line(`${icon('loop')} ${text('orch.ev.loopIteration', {
        i: event.iteration == null ? 0 : event.iteration,
        max: event.max == null ? 0 : event.max,
      })}`));
      break;
    case 'zero_deliverable_guard':
      rows.push(line(`${icon('warn')} ${text('orch.ev.zeroGuard')}`));
      break;
    case 'replan':
      rows.push(line(`${icon('compass')} ${text('orch.ev.replan', {
        n: event.replan == null ? 0 : event.replan,
      })}${dim(event.defect || translate('orch.ev.structuralDefect'), 100)}`));
      break;
    case 'stuck_detected':
      rows.push(line(`${icon('loop')} ${text('orch.ev.stuck')}`));
      break;
    case 'no_progress':
      rows.push(line(`${icon('warn')} ${text('orch.ev.noProgress', {
        n: event.window == null ? 0 : event.window,
      })}`));
      break;
    case 'parallel_start':
      rows.push(line(`${icon('fanout')} ${text('orch.ev.fanout', {
        n: event.branches == null ? 0 : event.branches,
      })}`));
      break;
    case 'branch_pick':
      rows.push(line(`${icon('branch')} ${text('orch.ev.route', {
        name: event.chosen || translate('orch.ev.none'),
      })}`));
      break;
    case 'artifact_declared':
      rows.push(line(`${icon('package')} ${text('orch.ev.deliverable')}<b>${
        escape(event.path || event.name || translate('orch.ev.unnamed'))
      }</b>${dim(event.description, 120)}`));
      break;
    case 'human_notify':
      rows.push(line(`${icon('person')} <b>${escape(
        event.name || translate('orch.gate.who'))}</b>${dim(event.prompt, 200)}`));
      break;
    case 'human_request':
      rows.push(line(`${icon('person')} <b>${escape(
        event.name || translate('orch.gate.who'))}</b> ${
        text('orch.ev.gateAwaiting')}${dim(
        event.prompt || translate(event.mode === 'approve'
          ? 'orch.gate.approvePrompt' : 'orch.gate.inputPrompt'), 160,
      )}`, 'is-gate'));
      break;
    case 'human_resolved':
      rows.push(line(`${icon('person')} ${event.mode === 'approve'
        ? event.approved
          ? `${icon('check')} ${text('orch.ev.gateApproved')}`
          : `${icon('reject')} ${text('orch.ev.gateRejected')}`
        : `${icon('check')} ${text('orch.ev.gateAnswered')}`}`));
      break;
    case 'flow_complete': { // eslint-disable-line no-case-declarations
      const hasOutcome = Boolean(event.outcome)
        && typeof event.outcome === 'object'
        || Boolean(event.outcome_category);
      const outcome = hasOutcome
        ? normalizeOrchestrationOutcome(event, options.outcomeContract) : null;
      const outcomeClass = outcome?.category === 'success'
        ? 'is-done' : outcome?.category === 'incomplete'
          ? 'is-warn' : 'is-err';
      const outcomeLabel = outcome
        ? localizedCode(
          'orch.ev.outcome.', outcome.category, 'orch.ev.status.unknown')
        : localizedCode(
          'orch.ev.status.', event.status, 'orch.ev.status.unknown');
      rows.push(line(`${icon('flag')} <b>${escape(outcomeLabel)}</b> — ${
        text('orch.ev.completeSummary', {
          agents: event.agents_run == null ? 0 : event.agents_run,
          seconds: event.elapsed == null ? 0 : event.elapsed,
        })}`, outcome ? outcomeClass
        : event.ok !== false && event.status === 'completed'
          ? 'is-done' : 'is-err'));
      if (event.ok === false && event.stop_reason) {
        rows.push(line(`${icon('warn')} ${escape(localizedCode(
          'orch.ev.stopReason.', event.stop_reason))}`,
        outcome?.category === 'incomplete' ? 'is-warn' : 'is-err'));
      }
      break;
    }
    case 'error': {
      let errorValue = event.error;
      const error = record(errorValue);
      if (error?.detail != null && error.detail !== '') {
        errorValue = error.detail;
      }
      if (errorValue == null || errorValue === '') {
        errorValue = event.detail || event.content;
      }
      rows.push(line(`${icon('warn')} ${escape(orchestrationResultError(
        errorValue, translate('orch.ev.errorFallback'), { maxMessages: 1 },
      ))}`, 'is-err'));
      break;
    }
    case 'step_phase':
    case 'step_delta':
    case 'step_trace':
    case 'done':
      break;
    default:
      if (event.type) {
        rows.push(line(`${icon('warn')} ${text('orch.ev.unknown', {
          type: event.type,
        })}`, 'is-err'));
      }
  }
  return rows;
}

(orchestrationRegistry as unknown as EventFormatWindow).formatOrchestrationEventLines =
  formatOrchestrationEventLines;
