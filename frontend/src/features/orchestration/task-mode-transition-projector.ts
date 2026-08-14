import { orchestrationRegistry } from './registry';
import { reportOrchestrationDiagnostic } from './diagnostic-report';

export interface TaskModeTransition extends Record<string, unknown> {
  type?: unknown;
  state?: unknown;
  page?: Record<string, unknown> | null;
  run?: unknown;
  error?: unknown;
  targetRunId?: unknown;
}

export interface TaskModeTransitionProjectorOptions {
  clear: (transition: TaskModeTransition) => unknown;
  renderTitle: (
    run: unknown,
    emptyKey?: string,
    state?: Record<string, unknown>,
  ) => unknown;
  setTimelineBusy: (busy: unknown) => unknown;
  adoptSnapshot: (run: unknown, projection: Record<string, unknown>) => unknown;
  setTimelineLive: (live: boolean) => unknown;
  replay: (page: unknown) => unknown;
  line: (html: string, className?: string) => unknown;
  report: (context: string, error: unknown) => unknown;
  icon: (name: string) => unknown;
  translate: (key: string) => unknown;
  escape?: (value: unknown) => unknown;
  failureMessage?: (value: unknown, fallback: string) => unknown;
}

type TaskModeTransitionProjectorWindow = Window & {
  createTaskModeTransitionProjector?: typeof createTaskModeTransitionProjector;
};

export function createTaskModeTransitionProjector(
  options: TaskModeTransitionProjectorOptions,
) {
  const escape = (value: unknown): string => String(options.escape
    ? options.escape(value) : value == null ? '' : value);
  const failureMessage = (value: unknown, fallbackKey: string): string => {
    const fallback = String(options.translate(fallbackKey) || fallbackKey);
    return String(options.failureMessage?.(value, fallback) || fallback);
  };
  const project = (transitionValue: TaskModeTransition = {}): unknown => {
    const transition = transitionValue ?? {};
    if (transition.type === 'reset') return options.clear(transition);
    if (transition.type === 'loading') {
      return options.renderTitle(null, 'tm.loading', { busy: true });
    }
    if (transition.type === 'open-rejected') {
      options.renderTitle(null, 'tm.runLoadFailed', {
        retryId: transition.targetRunId,
        message: failureMessage(transition.result, 'tm.runLoadFailed'),
      });
      options.setTimelineBusy(false);
      return false;
    }
    if (transition.type === 'busy') {
      return options.setTimelineBusy(transition.busy);
    }
    if (transition.type === 'snapshot') {
      return options.adoptSnapshot(transition.run, {
        renderList: transition.renderList,
        renderGraph: transition.renderGraph,
        renderFinal: transition.renderFinal,
      });
    }
    if (transition.type === 'replay') {
      const caughtUp = !transition.page || transition.page.caught_up !== false;
      if (!caughtUp) options.setTimelineLive(false);
      options.replay(transition.page);
      if (caughtUp) options.setTimelineLive(true);
      return true;
    }
    if (transition.type === 'connection') {
      const connections: Record<string, [string, string, string]> = {
        reconnecting: ['loop', 'orch.run.reconnecting', 'is-err'],
        recovered: ['check', 'orch.run.reconnected', 'is-done'],
        offline: ['warn', 'orch.run.offline', 'is-err'],
      };
      const connection = connections[String(transition.state || '')];
      if (connection) {
        const message = String(options.translate(connection[1]));
        const detail = transition.state === 'recovered' ? ''
          : failureMessage(transition.detail, connection[1]);
        options.line(`${String(options.icon(connection[0]))} ${message}${
          detail && detail !== message ? ` · ${escape(detail)}` : ''
        }`, connection[2]);
      }
      return Boolean(connection);
    }
    if (transition.type === 'projection-error') {
      reportOrchestrationDiagnostic(
        options.report, 'event projection', transition.error);
      options.line(`${String(options.icon('warn'))} ${String(
        options.translate('tm.runProjectionFailed'))}`, 'is-err');
      return false;
    }
    return false;
  };
  return Object.freeze({ project });
}

(orchestrationRegistry as unknown as TaskModeTransitionProjectorWindow)
  .createTaskModeTransitionProjector = createTaskModeTransitionProjector;
