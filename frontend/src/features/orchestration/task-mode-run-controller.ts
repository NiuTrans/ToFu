import { orchestrationRegistry } from './registry';
import {
  createOrchestrationRunSession,
  type OrchestrationRunOwner,
  type OrchestrationRunSession,
} from './run-session';
import {
  createTaskModeRunReader,
  type TaskModeRunReader,
} from './task-mode-run-reader';
import {
  createTaskModeRunReplayController,
  type TaskModeRunReplayControllerOptions,
} from './task-mode-run-replay-controller';

type Port = Record<string, unknown>;
export interface TaskModeRunControllerOptions extends Omit<
  TaskModeRunReplayControllerOptions,
  'session' | 'emit' | 'reset' | 'setBusy' | 'acceptedRun' | 'readFinal'
> {
  session?: OrchestrationRunSession;
  reader?: TaskModeRunReader;
  taskClient?: Port | (() => Port | null) | null;
  isTerminal?: (run: unknown) => unknown;
  report?: (context: string, result: unknown) => unknown;
  onTransition?: (transition: Port) => unknown;
}
type TaskModeRunControllerWindow = Window & {
  createTaskModeRunController?: typeof createTaskModeRunController;
};

/** Durable-run identity plus guarded read/replay coordination. */
export function createTaskModeRunController(
  options: TaskModeRunControllerOptions = {},
) {
  const session = options.session ?? createOrchestrationRunSession();
  let busy = false;
  const emit = (type: string, values: Port = {}): unknown =>
    options.onTransition?.({ type, runId: session.id(), ...values });
  const setBusy = (value: unknown): boolean => {
    const next = Boolean(value);
    if (busy === next) return next;
    busy = next;
    emit('busy', { busy: next });
    return next;
  };
  const reader = options.reader ?? createTaskModeRunReader({
    taskClient: options.taskClient,
    report: options.report,
  });
  let replay: ReturnType<typeof createTaskModeRunReplayController>;

  function reset(resetOptions: Port = {}): boolean {
    session.invalidate({ clearId: resetOptions.clearId !== false });
    replay.stop();
    busy = false;
    emit('reset', {
      reason: resetOptions.reason || 'reset',
      targetRunId: resetOptions.runId || null,
    });
    return true;
  }

  async function readFinal(
    runIdValue: unknown,
    ownerValue?: OrchestrationRunOwner | null,
  ): Promise<boolean> {
    const runId = String(runIdValue || '');
    if (!runId || session.id() !== runId) return false;
    const owner = ownerValue ?? session.snapshot();
    const readOwner = session.beginRead(owner);
    if (!readOwner) return false;
    const result = await reader.read(runId);
    if (!session.owns(readOwner, true)) return false;
    reader.report('final read', result);
    const run = reader.accepted(result, runId);
    if (!run) {
      if (result.notFound) reset({ reason: 'missing', runId });
      else emit('connection', {
        state: 'offline', detail: { context: 'final-read', result },
      });
      return false;
    }
    emit('snapshot', { run, source: 'final-read', renderFinal: true });
    return true;
  }

  replay = createTaskModeRunReplayController({
    ...options,
    session,
    emit,
    reset,
    setBusy,
    acceptedRun: (result: unknown, runId: unknown) =>
      reader.accepted(result, String(runId || '')),
    readFinal,
  });

  const open = async (runIdValue: unknown): Promise<boolean> => {
    if (!runIdValue) return false;
    const runId = String(runIdValue);
    const owner = session.begin(runId);
    replay.stop();
    busy = false;
    emit('reset', { reason: 'switch', targetRunId: runId });
    emit('loading', { targetRunId: runId });
    const result = await reader.read(runId);
    if (!session.owns(owner, true)) return false;
    reader.report('get', result);
    const run = reader.accepted(result, runId);
    if (!run) {
      if (result.notFound) {
        reset({ reason: 'missing', runId });
        return false;
      }
      emit('open-rejected', { targetRunId: runId, result });
      return false;
    }
    emit('snapshot', {
      run,
      source: 'open',
      renderList: false,
      renderGraph: true,
    });
    replay.start(owner);
    return true;
  };

  const resync = async (runIdValue: unknown): Promise<boolean> => {
    const runId = String(runIdValue || '');
    if (!runId || session.id() !== runId) return false;
    const readOwner = session.beginRead(session.snapshot());
    if (!readOwner) return false;
    const result = await reader.read(runId);
    if (!session.owns(readOwner, true)) return false;
    reader.report('resync', result);
    const run = reader.accepted(result, runId);
    if (!run) {
      // A transport/5xx outcome is unknown. Only an authoritative 404 may
      // tear down the selected durable projection.
      if (result.notFound) reset({ reason: 'missing', runId });
      return false;
    }
    emit('snapshot', {
      run,
      source: 'resync',
      renderGraph: true,
      renderFinal: options.isTerminal?.(run) ?? false,
    });
    return true;
  };

  return Object.freeze({
    id: () => session.id(),
    snapshot: () => session.snapshot(),
    isBusy: () => busy,
    isPolling: () => session.isPolling(),
    open,
    readFinal,
    reset,
    resync,
  });
}

(orchestrationRegistry as unknown as TaskModeRunControllerWindow).createTaskModeRunController =
  createTaskModeRunController;
