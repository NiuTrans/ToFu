import { orchestrationRegistry } from './registry';
import {
  createOrchestrationRequestReader,
  type OrchestrationRequestReader,
} from './request-reader';

type Port = Record<string, unknown>;

export interface TaskModeRunReaderOptions {
  taskClient?: Port | (() => Port | null) | null;
  report?: (context: string, result: unknown) => unknown;
  requests?: OrchestrationRequestReader;
}

export interface TaskModeRunReader {
  accepted(result: unknown, runId: string): Port | null;
  read(runId: string): Promise<Port>;
  report(context: string, result: unknown): unknown;
}

type TaskModeRunReaderWindow = Window & {
  createTaskModeRunReader?: typeof createTaskModeRunReader;
};

/** Read durable details once and certify their backend run identity. */
export function createTaskModeRunReader(
  options: TaskModeRunReaderOptions = {},
): TaskModeRunReader {
  const requests = options.requests ?? createOrchestrationRequestReader({
    client: options.taskClient,
    report: options.report,
  });
  const accepted = (resultValue: unknown, runId: string): Port | null => {
    const result = resultValue && typeof resultValue === 'object'
      ? resultValue as Port : null;
    const run = result?.run && typeof result.run === 'object'
      ? result.run as Port : null;
    return result?.ok === true && result.runId === runId ? run : null;
  };
  const read = (runId: string): Promise<Port> =>
    requests.read('get', [runId]);
  return Object.freeze({ accepted, read, report: requests.report });
}

(orchestrationRegistry as unknown as TaskModeRunReaderWindow).createTaskModeRunReader =
  createTaskModeRunReader;
